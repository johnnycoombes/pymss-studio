from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from worker_infer import separation_logging
from worker_protocol import current_log_task_id, task_logging


class WorkflowLogScope:
    """Bind synchronous logger records to native node execution boundaries."""

    def __init__(self) -> None:
        self.local = threading.local()
        self.logger: Any = None

    def current_node_id(self) -> str | None:
        return getattr(self.local, "node_id", None)

    def track(self, event: dict[str, Any]) -> None:
        node_id = event.get("node_id")
        kind = event.get("event")
        if kind:
            if kind in {"node_started", "node_progress", "node_failed", "node_cancelled"} and node_id is not None:
                self.local.node_id = str(node_id)
            elif kind in {"node_completed", "node_skipped", "execution_completed", "execution_failed", "execution_cancelled"}:
                self.local.node_id = None
            return
        fraction = event.get("node_fraction")
        if node_id is None or not isinstance(fraction, (float, int)):
            return
        if fraction < 1:
            self.local.node_id = str(node_id)
        elif event.get("audio_index") is None and event.get("unit") is None and self.current_node_id() == str(node_id):
            self.local.node_id = None


@contextmanager
def workflow_logging(task_id: str, *, batch: bool = False) -> Iterator[WorkflowLogScope]:
    scope = WorkflowLogScope()
    with task_logging(task_id), separation_logging(
        task_id,
        node_id_provider=scope.current_node_id,
        task_id_provider=lambda: current_log_task_id() or (None if batch else task_id),
    ) as logger:
        scope.logger = logger
        try:
            yield scope
        finally:
            scope.local.node_id = None
