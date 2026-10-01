from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from canonical_tasks.ledger import EVENT_ROW_HEADER, new_task_id

NOW = "2026-09-28T22:23:48-07:00"


def task_text(task_id: str, **overrides) -> str:
    fields = {
        "type": "task",
        "id": task_id,
        "title": json.dumps("Reply to Alex about the contract"),
        "status": "active",
        "created_at": NOW,
        "updated_at": NOW,
        "due": "null",
        "review_after": "null",
        "area": "work",
        "source_refs": json.dumps([{"kind": "mail-thread", "id": "18f2c7a1", "source_key": "mail:18f2c7a1"}]),
        "origin": json.dumps({"type": "manual", "created_at": NOW, "session_id": "s-1"}),
        "completion_check": json.dumps({"kind": "owner-declared", "expected_outcome": "Reply sent."}),
    }
    fields.update({k: v for k, v in overrides.items() if v is not None})
    for key, value in overrides.items():
        if value is None:
            fields.pop(key, None)
    head = "\n".join(f"{key}: {value}" for key, value in fields.items())
    body = "\n".join([
        "## Why now", "", "Alex asked for the revised contract.", "",
        "## Desired outcome", "", "Reply sent.", "",
        "## Event log", "", EVENT_ROW_HEADER, "|---|---|---|---|---|",
        f"| {NOW} | created | — | active | test fixture |", "",
    ])
    return f"---\n{head}\n---\n\n{body}"


class RootCase(unittest.TestCase):
    """A throwaway root with an empty tasks/ folder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add_task(self, **overrides) -> str:
        task_id = overrides.pop("id", None) or new_task_id()
        (self.tasks / f"{task_id}.md").write_text(task_text(task_id, **overrides))
        return task_id

    def status_of(self, task_id: str) -> str:
        for line in (self.tasks / f"{task_id}.md").read_text().splitlines():
            if line.startswith("status: "):
                return line.split(": ", 1)[1]
        raise AssertionError("no status line")
