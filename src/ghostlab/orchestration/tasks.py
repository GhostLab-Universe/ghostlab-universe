"""Task DAG with M00's legal transitions, over plain-JSON TaskSpec records.

Graph state keeps tasks as ``TaskSpec`` dumps (plain JSON) so checkpoints never
carry library types; every change re-validates through the contract.
"""

from ghostlab.contracts import TASK_TRANSITIONS, TaskSpec


class TaskTransitionError(Exception):
    pass


def new_task(campaign_id: str, task_id: str, kind: str, role: str, *, depends_on=(),
             max_attempts: int = 1, exit_condition: str) -> dict:
    return TaskSpec.model_validate({
        "schema_version": 1, "task_id": task_id, "campaign_id": campaign_id, "kind": kind,
        "role": role, "depends_on": list(depends_on), "status": "pending", "attempt": 0,
        "max_attempts": max_attempts, "exit_condition": exit_condition,
        "output_ref": None}).model_dump(mode="json")


def _index(tasks: list[dict], task_id: str) -> int:
    for index, task in enumerate(tasks):
        if task["task_id"] == task_id:
            return index
    raise TaskTransitionError(f"unknown task {task_id}")


def _move(task: dict, status: str, **changes) -> dict:
    if status not in TASK_TRANSITIONS[task["status"]]:
        raise TaskTransitionError(f"{task['task_id']}: {task['status']} -> {status} is illegal")
    return TaskSpec.model_validate({**task, **changes, "status": status}).model_dump(mode="json")


def start(tasks: list[dict], task_id: str) -> list[dict]:
    """pending -> ready -> running, only once every dependency has succeeded."""
    index = _index(tasks, task_id)
    task = tasks[index]
    by_id = {t["task_id"]: t for t in tasks}
    waiting = [d for d in task["depends_on"] if by_id.get(d, {}).get("status") != "succeeded"]
    if waiting:
        raise TaskTransitionError(f"{task_id} waits on {waiting}")
    if task["status"] == "pending":
        task = _move(task, "ready")
    if task["attempt"] >= task["max_attempts"]:
        raise TaskTransitionError(f"{task_id} has no attempts left")
    task = _move(task, "running", attempt=task["attempt"] + 1)
    return [*tasks[:index], task, *tasks[index + 1:]]


def finish(tasks: list[dict], task_id: str, status: str, *, output_ref: dict | None = None):
    index = _index(tasks, task_id)
    changes = {"output_ref": output_ref} if output_ref is not None else {}
    task = _move(tasks[index], status, **changes)
    return [*tasks[:index], task, *tasks[index + 1:]]


def retry(tasks: list[dict], task_id: str) -> list[dict]:
    """running -> ready for another bounded attempt."""
    index = _index(tasks, task_id)
    task = tasks[index]
    if task["attempt"] >= task["max_attempts"]:
        raise TaskTransitionError(f"{task_id} has no attempts left")
    return [*tasks[:index], _move(task, "ready"), *tasks[index + 1:]]


def fail_dependents(tasks: list[dict]) -> list[dict]:
    """Skip pending/ready tasks whose dependencies can no longer succeed."""
    changed = True
    while changed:
        changed = False
        by_id = {t["task_id"]: t for t in tasks}
        for index, task in enumerate(tasks):
            dead = any(by_id.get(d, {}).get("status") in {"failed", "cancelled", "skipped"}
                       for d in task["depends_on"])
            if dead and task["status"] in {"pending", "ready"}:
                tasks = [*tasks[:index], _move(task, "skipped"), *tasks[index + 1:]]
                changed = True
                break
    return tasks


def cancel_open(tasks: list[dict]) -> list[dict]:
    open_states = {"pending", "ready", "running", "awaiting_approval"}
    return [_move(t, "cancelled") if t["status"] in open_states else t for t in tasks]
