from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path

from canonical_tasks import ledger as subject
from tests.helpers import NOW, RootCase


class LedgerFoundationTests(RootCase):
    def test_generated_id_matches_schema(self) -> None:
        for _ in range(50):
            self.assertRegex(subject.new_task_id(), subject.TASK_ID_RE)

    def test_timestamp_requires_explicit_portable_offset(self) -> None:
        subject.parse_timestamp("2026-09-28T22:23:48-07:00", "t", Path("x"))
        subject.parse_timestamp("2026-09-28T22:23:48Z", "t", Path("x"))
        with self.assertRaises(subject.LedgerError):
            subject.parse_timestamp("2026-09-28T22:23:48", "t", Path("x"))

    def test_valid_task_and_deterministic_render(self) -> None:
        self.add_task()
        first = subject.render_outputs(subject.load_ledger(self.tasks, self.root), self.root, NOW)
        second = subject.render_outputs(subject.load_ledger(self.tasks, self.root), self.root, NOW)
        self.assertEqual(first, second)
        state = json.loads(first[0])
        self.assertEqual(state["task_count"], 1)
        self.assertIn("## Today", first[1])

    def test_duplicate_nonterminal_occurrence_refuses(self) -> None:
        key = '"mail:18f2c7a1:reply:2026-09-24T19:08:42Z:owed_by_me"'
        self.add_task(occurrence_key=key, signal_key="mail:18f2c7a1:reply")
        self.add_task(occurrence_key=key, signal_key="mail:18f2c7a1:reply")
        with self.assertRaisesRegex(subject.LedgerError, "duplicate nonterminal occurrence"):
            subject.load_ledger(self.tasks, self.root)

    def test_closed_task_may_share_an_occurrence_with_its_successor(self) -> None:
        key = '"mail:18f2c7a1:reply:2026-09-24T19:08:42Z:owed_by_me"'
        self.add_task(occurrence_key=key, signal_key="mail:18f2c7a1:reply", status="done")
        self.add_task(occurrence_key=key, signal_key="mail:18f2c7a1:reply")
        self.assertEqual(len(subject.load_ledger(self.tasks, self.root)), 2)

    def test_scheduled_requires_review_after_but_parked_may_be_undated(self) -> None:
        self.add_task(status="parked")
        subject.load_ledger(self.tasks, self.root)
        self.add_task(status="scheduled")
        with self.assertRaisesRegex(subject.LedgerError, "review_after is required"):
            subject.load_ledger(self.tasks, self.root)

    def test_terminal_task_does_not_reopen(self) -> None:
        for terminal in subject.TERMINAL:
            for target in subject.STATUSES:
                self.assertFalse(subject.transition_allowed(terminal, target))

    def test_invalid_ledger_does_not_write_generated_outputs(self) -> None:
        self.add_task(title='""')
        out = self.root / ".ct"
        with self.assertRaises(subject.LedgerError):
            subject.render_ledger(self.tasks, out, self.root, NOW)
        self.assertFalse(out.exists())

    def test_source_path_cannot_escape_the_root(self) -> None:
        refs = json.dumps([{"kind": "note", "id": "n1", "path": "../outside.md"}])
        self.add_task(source_refs=refs)
        with self.assertRaisesRegex(subject.LedgerError, "escapes the root"):
            subject.load_ledger(self.tasks, self.root)

    def test_frozen_normalizer_cases(self) -> None:
        mail = subject.normalize_signal(
            "mail", {"thread_key": "18f2c7a1", "last_ts": "2026-09-24T19:08:42Z", "bucket": "owed_by_me"}, "reply")
        self.assertEqual(mail["signal_key"], "mail:18f2c7a1:reply")
        self.assertEqual(mail["occurrence_key"], "mail:18f2c7a1:reply:2026-09-24T19:08:42Z:owed_by_me")
        self.assertTrue(mail["eligible"])
        theirs = subject.normalize_signal(
            "mail", {"thread_key": "18f2c7a1", "last_ts": "t", "bucket": "owed_by_them"}, "reply")
        self.assertFalse(theirs["eligible"])
        self.assertEqual(theirs["suggested_state"], "waiting")
        unkeyed = subject.normalize_signal("note", {"text": "call the plumber"}, "follow_up")
        self.assertFalse(unkeyed["keyed"])
        with self.assertRaises(subject.LedgerError):
            subject.normalize_signal("mail", {"thread_key": "x"}, "vibes")

    def test_decide_signal_suppresses_a_closed_occurrence_and_succeeds_a_new_one(self) -> None:
        closed = {"signal_key": "mail:a:reply", "occurrence_key": "mail:a:reply:1", "status": "done"}
        self.assertEqual(subject.decide_signal(closed, {"signal_key": "mail:a:reply", "occurrence_key": "mail:a:reply:1"}), "suppress")
        self.assertEqual(subject.decide_signal(closed, {"signal_key": "mail:a:reply", "occurrence_key": "mail:a:reply:2"}), "create-successor")
        open_task = dict(closed, status="active")
        self.assertEqual(subject.decide_signal(open_task, {"signal_key": "mail:a:reply", "occurrence_key": "mail:a:reply:1"}), "update-evidence")
        self.assertEqual(subject.decide_signal(None, {"keyed": False}), "unkeyed-proposal")

    def test_visibility_follows_review_after(self) -> None:
        today = dt.date(2026, 10, 1)
        self.assertTrue(subject.visible_today({"status": "active"}, today))
        self.assertFalse(subject.visible_today({"status": "waiting", "review_after": "2026-10-02"}, today))
        self.assertTrue(subject.visible_today({"status": "waiting", "review_after": "2026-10-01"}, today))
        self.assertFalse(subject.visible_today({"status": "parked", "review_after": None}, today))
        self.assertFalse(subject.visible_today({"status": "done"}, today))


if __name__ == "__main__":
    unittest.main()
