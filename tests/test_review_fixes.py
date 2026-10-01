"""Regression tests for the pre-release review (2026-10-01)."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from canonical_tasks import actions, ledger
from tests.helpers import NOW, RootCase
from tests.test_adversarial import snapshot

LATER = "2026-09-29T09:00:00-07:00"


class ReviewFixes(RootCase):
    def test_null_evidence_is_not_evidence(self) -> None:
        task_id = self.add_task()
        before = snapshot(self.root)
        bad = {"source_ref": None, "observed_revision": None, "assertion": None}
        with self.assertRaisesRegex(actions.Refusal, "non-empty text"):
            actions.run_claims(self.root, [{"task_id": task_id, "disposition": "done", "declared_outcome": "x",
                                            "verification": "verified", "evidence": [bad]}],
                               session_id="s", now=LATER, write=True)
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(self.status_of(task_id), "active")

    def test_refusal_at_apply_time_leaves_the_log_untouched(self) -> None:
        task_id = self.add_task()
        path = self.tasks / f"{task_id}.md"
        path.write_text(path.read_text().replace("| At | Event | From | To | Detail |", "| When | What |"))
        before = snapshot(self.root)
        with self.assertRaises((actions.Refusal, ledger.LedgerError)):
            actions.run_claims(self.root, [{"task_id": task_id, "disposition": "dismissed", "declared_outcome": "x",
                                            "verification": "not-applicable"}],
                               session_id="s", now=LATER, write=True)
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse((self.root / actions.LOG_RELATIVE).exists())

    def test_backdated_change_refuses_without_writing(self) -> None:
        task_id = self.add_task()
        before = snapshot(self.root)
        with self.assertRaisesRegex(actions.Refusal, "would fail validation"):
            actions.run_claims(self.root, [{"task_id": task_id, "disposition": "dismissed", "declared_outcome": "x",
                                            "verification": "not-applicable"}],
                               session_id="s", now="2020-01-01T00:00:00Z", write=True)
        self.assertEqual(snapshot(self.root), before)

    def test_retrying_a_successful_session_is_a_no_op(self) -> None:
        task_id = self.add_task()
        claim = {"task_id": task_id, "disposition": "done", "declared_outcome": "sent",
                 "verification": "owner-attested", "attestation": "sent"}
        actions.run_claims(self.root, [claim], session_id="s", now=LATER, write=True)
        after_first = snapshot(self.root)
        result = actions.run_claims(self.root, [claim], session_id="s", now="2026-09-29T10:00:00-07:00", write=True)
        self.assertEqual((result["appended"], result["idempotent"]), (0, 1))
        self.assertEqual(snapshot(self.root), after_first)
        different = dict(claim, declared_outcome="something else")
        with self.assertRaisesRegex(actions.Refusal, "different claim"):
            actions.run_claims(self.root, [different], session_id="s", now=LATER, write=True)

    def test_malformed_field_types_fail_closed_not_crash(self) -> None:
        task_id = self.add_task()
        path = self.tasks / f"{task_id}.md"
        path.write_text(path.read_text().replace("status: active", "status: []"))
        with self.assertRaises(ledger.LedgerError):
            ledger.load_ledger(self.tasks, self.root)
        before = snapshot(self.root)
        with self.assertRaisesRegex(actions.Refusal, "ledger invalid"):
            actions.create_task(self.root, title="New", area="work", state="active", outcome="x",
                                now=NOW, session_id="s", write=True)
        self.assertEqual(snapshot(self.root), before)

    def test_symlinked_source_cannot_escape_the_root(self) -> None:
        outside = self.root.parent / f"outside-{os.getpid()}.md"
        outside.write_text("secret\n")
        self.addCleanup(outside.unlink)
        (self.root / "link.md").symlink_to(outside)
        self.add_task(source_refs=json.dumps([{"kind": "note", "id": "n", "path": "link.md"}]))
        with self.assertRaisesRegex(ledger.LedgerError, "escapes the root"):
            ledger.load_ledger(self.tasks, self.root)

    def test_symlinked_task_file_is_refused(self) -> None:
        task_id = self.add_task()
        real = self.root / "elsewhere.md"
        (self.tasks / f"{task_id}.md").rename(real)
        (self.tasks / f"{task_id}.md").symlink_to(real)
        with self.assertRaisesRegex(ledger.LedgerError, "not symlinks"):
            ledger.load_ledger(self.tasks, self.root)

    def test_parking_shows_the_cleared_due_date_in_a_dry_run(self) -> None:
        task_id = self.add_task(due="2026-10-03")
        result = actions.run_claims(self.root, [{"task_id": task_id, "disposition": "parked", "declared_outcome": "later",
                                                 "verification": "not-applicable"}],
                                    session_id="s", now=LATER, write=False)
        self.assertEqual(result["records"][0]["due_change"], {"from": "2026-10-03", "to": None})

    def test_queue_links_resolve_from_the_queue_file(self) -> None:
        self.add_task()
        out = self.root / ".ct"
        ledger.render_ledger(self.tasks, out, self.root, NOW)
        links = [line.split("](", 1)[1].split(")", 1)[0] for line in (out / "queue.md").read_text().splitlines() if "](" in line]
        self.assertTrue(links)
        for link in links:
            self.assertTrue((out / link).resolve().exists(), link)

    def test_non_text_source_path_fails_validation_not_crash(self) -> None:
        self.add_task(source_refs=json.dumps([{"kind": "note", "id": "n", "path": 42}]))
        with self.assertRaisesRegex(ledger.LedgerError, "path must be text"):
            ledger.load_ledger(self.tasks, self.root)

    def test_retry_that_changes_meaning_refuses(self) -> None:
        task_id = self.add_task(due="2026-10-03")
        base = {"task_id": task_id, "disposition": "active", "declared_outcome": "pushed",
                "verification": "not-applicable", "due": "2026-10-10"}
        actions.run_claims(self.root, [base], session_id="s", now=LATER, write=True)
        for change in ({"due": "2026-10-20"}, {"review_after": "2026-10-04"}, {"decision_reason": "other"}):
            with self.subTest(change):
                with self.assertRaisesRegex(actions.Refusal, "different claim"):
                    actions.run_claims(self.root, [dict(base, **change)], session_id="s", now=LATER, write=True)

    def test_parking_with_a_due_date_refuses(self) -> None:
        task_id = self.add_task(due="2026-10-03")
        with self.assertRaisesRegex(actions.Refusal, "parking clears the due date"):
            actions.run_claims(self.root, [{"task_id": task_id, "disposition": "parked", "declared_outcome": "later",
                                            "verification": "not-applicable", "due": "2026-10-20"}],
                               session_id="s", now=LATER, write=True)

    def test_disk_failure_mid_write_is_finished_by_a_retry(self) -> None:
        from unittest import mock
        task_id = self.add_task()
        claim = {"task_id": task_id, "disposition": "done", "declared_outcome": "sent",
                 "verification": "owner-attested", "attestation": "sent"}
        with mock.patch.object(actions, "commit_writes", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(actions.PartialWrite, "re-run with --session s"):
                actions.run_claims(self.root, [claim], session_id="s", now=LATER, write=True)
        self.assertEqual(self.status_of(task_id), "active")
        self.assertTrue((self.root / actions.LOG_RELATIVE).exists())
        result = actions.run_claims(self.root, [claim], session_id="s", now=LATER, write=True)
        self.assertEqual(result["appended"], 0)
        self.assertEqual(self.status_of(task_id), "done")
        log_lines = (self.root / actions.LOG_RELATIVE).read_text().splitlines()
        self.assertEqual(len(log_lines), 1)

    def _attested(self, task_id: str) -> dict:
        return {"task_id": task_id, "disposition": "done", "declared_outcome": "sent",
                "verification": "owner-attested", "attestation": "sent"}

    def test_recovery_retry_still_requires_a_valid_ledger(self) -> None:
        from unittest import mock
        task_id = self.add_task()
        with mock.patch.object(actions, "commit_writes", side_effect=OSError("disk full")):
            with self.assertRaises(actions.PartialWrite):
                actions.run_claims(self.root, [self._attested(task_id)], session_id="s", now=LATER, write=True)
        bad = self.add_task()
        (self.tasks / f"{bad}.md").write_text("not a task\n")
        before = snapshot(self.root)
        with self.assertRaisesRegex(actions.Refusal, "ledger invalid"):
            actions.run_claims(self.root, [self._attested(task_id)], session_id="s", now=LATER, write=True)
        self.assertEqual(snapshot(self.root), before)

    def test_torn_log_append_is_trimmed_and_recovery_works(self) -> None:
        first, second = self.add_task(), self.add_task()
        actions.run_claims(self.root, [self._attested(first)], session_id="s1", now=LATER, write=True)
        log = self.root / actions.LOG_RELATIVE
        with log.open("a") as handle:
            handle.write('{"record_id": "rec-torn", "kind": "task-recon')  # the disk filled up here
        actions.run_claims(self.root, [self._attested(second)], session_id="s2", now=LATER, write=True)
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(json.loads(line)["record_id"].startswith("rec-") for line in lines))
        self.assertEqual(self.status_of(second), "done")

    def test_edit_during_write_reports_a_partial_write_not_a_refusal(self) -> None:
        from unittest import mock
        task_id = self.add_task()
        real_verify = actions.verify_unchanged
        calls = {"n": 0}

        def editor_saves_between_checks(prepared):
            calls["n"] += 1
            if calls["n"] == 2:
                raise actions.Refusal("task changed while this operation was running; nothing written")
            return real_verify(prepared)

        with mock.patch.object(actions, "verify_unchanged", side_effect=editor_saves_between_checks):
            with self.assertRaisesRegex(actions.PartialWrite, "log recorded this claim"):
                actions.run_claims(self.root, [self._attested(task_id)], session_id="s", now=LATER, write=True)

    def test_changed_evidence_revision_is_a_different_claim(self) -> None:
        task_id = self.add_task()
        claim = {"task_id": task_id, "disposition": "done", "declared_outcome": "sent", "verification": "verified",
                 "evidence": [{"source_ref": "mail:t1", "observed_revision": "rev-1", "assertion": "reply sent"}]}
        actions.run_claims(self.root, [claim], session_id="s", now=LATER, write=True)
        changed = dict(claim, evidence=[dict(claim["evidence"][0], observed_revision="rev-2")])
        with self.assertRaisesRegex(actions.Refusal, "different claim"):
            actions.run_claims(self.root, [changed], session_id="s", now=LATER, write=False)

    def test_cli_evidence_is_deterministic_so_retries_match(self) -> None:
        task_id = self.add_task()
        first = actions.claim_for_verb(self.root, "done", task_id, outcome="sent", now=LATER,
                                       evidence=["mail:t1=reply sent"])
        again = actions.claim_for_verb(self.root, "done", task_id, outcome="sent", now="2026-09-30T08:00:00-07:00",
                                       evidence=["mail:t1=reply sent"])
        self.assertEqual(actions.claim_fingerprint(first), actions.claim_fingerprint(again))
        versioned = actions.claim_for_verb(self.root, "done", task_id, outcome="sent", now=LATER,
                                           evidence=["mail:t1@rev-9=reply sent"])
        self.assertEqual(versioned["evidence"][0]["observed_revision"], "rev-9")


if __name__ == "__main__":
    unittest.main()
