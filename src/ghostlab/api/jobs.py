"""The smallest background execution for a synchronous controller. Not a scheduler.

``CampaignController.start`` and ``resume`` run until they pause or finish, so each
campaign that is moving gets one daemon thread for that long. There is no queue,
ordering or retry here: order and budgets stay with M05's graph, and a campaign has at
most one thread. That holds only within one process, so ``RootLock`` keeps a storage root
to one server.

A server stopping is not an owner cancelling. ``Job.cancel`` writes a terminal
``cancelled_by_request``, so shutdown never sets it: ``JobRegistry.stopping`` ends a worker
at its next node instead, with nothing written, and the campaign reads ``running`` and
``stalled`` until its owner resumes it.
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


class ServerStopping(BaseException):
    """Ends a worker between nodes. Not an ``Exception``, so no node can handle it."""


@dataclass
class Job:
    """One thread's lifetime for one campaign, and how its first moments are reported."""

    cancel: threading.Event = field(default_factory=threading.Event)
    settled: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    entered: bool = False  # the controller's own gates passed and the graph began
    failure: BaseException | None = None
    followup: Work | None = None


class JobRegistry:
    def __init__(self, *, limit: int, on_failure: Callable[[str, BaseException, bool], None]):
        self._limit, self._on_failure = limit, on_failure
        self.stopping = threading.Event()
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

        A cancel doesn't count against the limit: it frees capacity, and refusing it
        would leave a campaign unstoppable while others run.
        """
        with self._lock:
            if self._closing:
                raise ApiError(503, "internal", "Server is shutting down", retryable=True)
            current = self._jobs.get(key)
            if current is not None:
                if follow:
                    current.followup = work
                return current, False
            if len(self._jobs) >= self._limit and not cancel:
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
            except ServerStopping:
                pass  # nothing failed, and the checkpoint already says where this stopped
            except BaseException as error:  # a thread has nobody above it to raise to
                job.failure = error
                if job.entered:  # earlier failures are reported to the waiting route
                    logger.error("Campaign worker stopped", exc_info=error)
                    self._on_failure(key, error, job.cancel.is_set())
            finally:
                job.settled.set()
            with self._lock:
                work, job.followup = job.followup, None
                if work is None or self.stopping.is_set():
                    self._jobs.pop(key, None)
                    self._threads.pop(key, None)
                    job.finished.set()
                    return

    def wait_for_first_step(self, job: Job) -> None:
        """Return once the controller has accepted the work; re-raise if it refused."""
        if not job.settled.wait(HANDSHAKE_S):
            raise ApiError(500, "internal", "Campaign did not start in time", retryable=True)
        if job.failure is not None and not job.entered:
            raise job.failure

    def shutdown(self) -> None:
        """Let workers reach a pause or finish; then stop the rest at their next node.

        Never through ``Job.cancel``: nobody asked for these campaigns to be cancelled.
        """
        with self._lock:
            self._closing = True
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(SHUTDOWN_GRACE_S)
        self.stopping.set()
        for thread in threads:
            thread.join(SHUTDOWN_GRACE_S)
