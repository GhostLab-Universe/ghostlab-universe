"""LangGraph checkpoint saver factory (requires the ``orchestration`` extra).

Graph checkpoints hold workflow state (hypotheses, budgets, artifact references).
They are kept in their own SQLite file and are never world snapshots: resuming
a graph may re-run model/tool nodes, while Failure Capsule replay re-executes
recorded actions with no inference. Do not use this saver for capsule replay.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def graph_checkpointer(graph_db: Path, *, app_db: Path | None = None) -> Iterator:
    """Yield a ``SqliteSaver`` bound to ``graph_db``; the connection closes on exit."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    if app_db is not None and graph_db.resolve() == app_db.resolve():
        raise ValueError("Graph checkpoints must not share the application database")
    graph_db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(graph_db)) as saver:
        yield saver
