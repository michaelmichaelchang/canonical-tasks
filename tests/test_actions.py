from __future__ import annotations

import contextlib
import io
import json
import unittest

from canonical_tasks import actions
from canonical_tasks.cli import main as ct
from canonical_tasks.ledger import load_ledger, parse_task
from tests.helpers import RootCase

LATER = "2026-09-28T22:24:06-07:00"


class TaskActionTests(RootCase):
    def run_ct(self, *argv: str) -> tuple[int, dict | str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ct(["--root", str(self.root), "--now", LATER, *argv])
        return code, (json.loads(out.getvalue()) if code == 0 else err.getvalue())

    def test_dry_run_writes_nothing(self):
        task_id = self.add_task()
        before = (self.tasks / f"{task_id}.md").read_bytes()
        code, result = self.run_ct("done", task_id, "--outcome", "Sent it", "--attest", "I sent it")
        self.assertEqual(code, 0)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual((self.tasks / f"{task_id}.md").read_bytes(), before)
        self.assertFalse((self.root / actions.LOG_RELATIVE).exists())

    def test_done_needs_attest_or_evidence(self):
        task_id = self.add_task()
        code, result = self.run_ct("done", task_id, "--outcome", "I think it's done", "--write")
        self.assertEqual(code, 0)
        self.assertTrue(result["records"][0]["verification_pending"])
        self.assertEqual(self.status_of(task_id), "active")
        self.assertIn("completion held for evidence", (self.tasks / f"{task_id}.md").read_text())

    def test_attested_done_closes_and_keeps_the_words(self):
        task_id = self.add_task()
        code, _ = self.run_ct("done", task_id, "--outcome", "Not going", "--attest", "not going, close it", "--write")
        self.assertEqual(code, 0)
        self.assertEqual(self.status_of(task_id), "done")
        self.assertIn('owner-attested: "not going, close it"', (self.tasks / f"{task_id}.md").read_text())

    def test_done_with_evidence_is_verified(self):
        task_id = self.add_task()
        code, result = self.run_ct("done", task_id, "--outcome", "Replied",
                                   "--evidence", "mail:18f2c7a1=reply sent in thread", "--write")
        self.assertEqual(code, 0)
        self.assertEqual(result["records"][0]["verification"], "verified")
        self.assertEqual(self.status_of(task_id), "done")

    def test_wait_then_note_keeps_state(self):
        task_id = self.add_task()
        self.run_ct("wait", task_id, "--outcome", "Asked Alex", "--review-after", "2026-10-05", "--write", "--session", "a")
        code, _ = self.run_ct("note", task_id, "--outcome", "Alex said Friday", "--write", "--session", "b")
        self.assertEqual(code, 0)
        task = parse_task(self.tasks / f"{task_id}.md")
        self.assertEqual(task.fields["status"], "waiting")
        self.assertEqual(task.fields["review_after"], "2026-10-05")

    def test_park_clears_due(self):
        task_id = self.add_task(due="2026-10-03")
        code, _ = self.run_ct("park", task_id, "--outcome", "Not this month", "--write")
        self.assertEqual(code, 0)
        task = parse_task(self.tasks / f"{task_id}.md")
        self.assertEqual(task.fields["status"], "parked")
        self.assertIsNone(task.fields["due"])

    def test_reschedule_writes_rescheduled_event_and_moves_due(self):
        task_id = self.add_task(due="2026-10-03")
        code, _ = self.run_ct("reschedule", task_id, "--due", "2026-10-10", "--outcome", "Pushed a week", "--write")
        self.assertEqual(code, 0)
        text = (self.tasks / f"{task_id}.md").read_text()
        self.assertIn("| rescheduled | active | active |", text)
        self.assertEqual(parse_task(self.tasks / f"{task_id}.md").fields["due"], "2026-10-10")

    def test_attested_done_directly_from_each_gated_state(self):
        for state, extra in (("waiting", {"review_after": "2026-10-05"}), ("scheduled", {"review_after": "2026-10-05"}), ("parked", {})):
            task_id = self.add_task(status=state, **extra)
            code, _ = self.run_ct("done", task_id, "--outcome", "Happened", "--attest", "it happened", "--write")
            self.assertEqual(code, 0, state)
            self.assertEqual(self.status_of(task_id), "done", state)

    def test_unverified_done_on_gated_task_holds_state_and_review_after(self):
        task_id = self.add_task(status="waiting", review_after="2026-10-05")
        self.run_ct("done", task_id, "--outcome", "Probably done", "--write")
        task = parse_task(self.tasks / f"{task_id}.md")
        self.assertEqual(task.fields["status"], "waiting")
        self.assertEqual(task.fields["review_after"], "2026-10-05")

    def test_done_retry_after_close_refuses_without_double_event(self):
        task_id = self.add_task()
        self.run_ct("done", task_id, "--outcome", "Sent", "--attest", "sent", "--write", "--session", "s")
        rows_before = (self.tasks / f"{task_id}.md").read_text().count("| reconciled |")
        code, err = self.run_ct("done", task_id, "--outcome", "Sent", "--attest", "sent", "--write", "--session", "s2")
        self.assertEqual(code, 3)
        self.assertIn("already closed", err)
        self.assertEqual((self.tasks / f"{task_id}.md").read_text().count("| reconciled |"), rows_before)

    def test_new_creates_a_valid_task(self):
        code, result = self.run_ct("new", "--title", "Book the car service", "--area", "home",
                                   "--outcome", "Service booked", "--source-key", "mail:18f2c7a1", "--write")
        self.assertEqual(code, 0)
        tasks = load_ledger(self.tasks, self.root)
        self.assertEqual([t.fields["id"] for t in tasks], [result["task_id"]])
        self.assertEqual(tasks[0].fields["source_refs"][1]["source_key"], "mail:18f2c7a1")

    def test_file_edited_after_claim_resolved_refuses(self):
        task_id = self.add_task()
        claim = actions.claim_for_verb(self.root, "dismiss", task_id, outcome="Not needed", now=LATER)
        records = actions.build_records(claims=[claim], session_id="s", root=self.root, generated_at=LATER)
        path = self.tasks / f"{task_id}.md"
        path.write_text(path.read_text().replace("Reply sent.", "Reply sent, and cc Sam."))
        with self.assertRaisesRegex(actions.Refusal, "changed since the claim"):
            actions.apply_records(self.root, records)


if __name__ == "__main__":
    unittest.main()
