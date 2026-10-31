"""The smallest background execution for a synchronous controller. Not a scheduler.

``CampaignController.start`` and ``resume`` run until they pause or finish, so each
campaign that is moving gets one daemon thread for that long. There is no queue,
ordering or retry here: order and budgets stay with M05's graph, and a campaign has at
most one thread. A registry in one process is why this API supports a single server
process, as ``AGENTS.md`` expects of the local SQLite MVP.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .errors import ApiError

logger = logging.getLogger("ghostlab.api")

HANDSHAKE_S = 30.0
SHUTDOWN_GRACE_S = 5.0

Work = Callable[["Job"], object]


@dataclass
class Job:
    """One thread's lifetime for one campaign, and how its first moments are reported."""

    cancel: threading.Event = field(default_factory=threading.Event)
    settled: threading.Event = field(default_factory=threading.Event)
    entered: bool = False  # the controller's own gates passed and the graph began
    failure: BaseException | None = None
    followup: Work | None = None


class JobRegistry:
    def __init__(self, *, limit: int, on_failure: Callable[[str, BaseException], None]):
        self._limit, self._on_failure = limit, on_failure
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._closing = False

    def get(self, key: str) -> Job | None:
        with self._lock:
            return self._jobs.get(key)

    def submit(self, key: str, work: Work, *, cancel: bool = False,
               follow: bool = False) -> tuple[Job, bool]:
        """Start ``work``, or join the job already running: returns (job, started).

        ``follow`` queues ``work`` to run when that job ends. A decision needs this:
        a client can approve in the moment between the campaign reporting
        ``awaiting_approval`` and its worker reaching the pause, and the resume must
        not be lost. Several follow-ups collapse into one, since resuming is idempotent.
        """
        with self._lock:
            if self._closing:
                raise ApiError(503, "internal", "Server is shutting down", retryable=True)
            current = self._jobs.get(key)
            if current is not None:
                if follow:
                    current.followup = work
                return current, False
            if len(self._jobs) >= self._limit:
                raise ApiError(429, "rate_limited", "Too many campaigns are running",
                               reason="too_many_campaigns", retryable=True,
                               headers={"Retry-After": "5"})
            job = Job()
            if cancel:
                job.cancel.set()
            thread = threading.Thread(target=self._run, args=(key, job, work), daemon=True,
                                      name=f"campaign-{len(self._jobs)}")
            self._jobs[key], self._threads[key] = job, thread
            thread.start()
            return job, True

    def _run(self, key: str, job: Job, work: Work):
        while True:
            try:
                work(job)
            except BaseException as error:  # a thread has nobody above it to raise to
                job.failure = error
                if job.entered:  # earlier failures are reported to the waiting route
                    logger.error("Campaign worker stopped", exc_info=error)
                    self._on_failure(key, error)
            finally:
                job.settled.set()
            with self._lock:
                work, job.followup = job.followup, None
                if work is None:
                    self._jobs.pop(key, None)
                    self._threads.pop(key, None)
                    return

    def wait_for_first_step(self, job: Job) -> None:
        """Return once the controller has accepted the work; re-raise if it refused."""
        if not job.settled.wait(HANDSHAKE_S):
            raise ApiError(500, "internal", "Campaign did not start in time", retryable=True)
        if job.failure is not None and not job.entered:
            raise job.failure

    def shutdown(self) -> None:
        """Let workers reach a pause or finish; then ask the rest to stop."""
        with self._lock:
            self._closing = True
            threads, jobs = list(self._threads.values()), list(self._jobs.values())
        for thread in threads:
            thread.join(SHUTDOWN_GRACE_S)
        for job in jobs:
            job.cancel.set()
        for thread in threads:
            thread.join(SHUTDOWN_GRACE_S)
