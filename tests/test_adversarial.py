"""Adversarial cases: the ways a task system quietly goes wrong.

Each case asserts the bad outcome cannot happen, and that refusing changes
nothing on disk.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from canonical_tasks import actions, ledger
from tests.helpers import NOW, RootCase

LATER = "2026-09-29T09:00:00-07:00"


def snapshot(root) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def claim(task_id: str, **extra) -> dict:
    base = {"task_id": task_id, "disposition": "done", "declared_outcome": "done",
            "verification": "owner-attested", "attestation": "done"}
    base.update(extra)
    return base


class SameInputTwice(RootCase):
    def test_render_is_byte_deterministic_and_disposable(self) -> None:
        self.add_task()
        out = self.root / ".ct"
        first = ledger.render_ledger(self.tasks, out, self.root, NOW)
        state = (out / "state.json").read_bytes()
        for path in out.iterdir():
            path.unlink()
        second = ledger.render_ledger(self.tasks, out, self.root, NOW)
        self.assertEqual(first, second)
        self.assertEqual((out / "state.json").read_bytes(), state)

    def test_log_append_is_idempotent_for_a_retried_session(self) -> None:
        task_id = self.add_task()
        records = actions.build_records(claims=[claim(task_id)], session_id="s", root=self.root, generated_at=LATER)
        log = self.root / actions.LOG_RELATIVE
        self.assertEqual(actions.append_log(log, records)["appended"], 1)
        retried = actions.build_records(claims=[claim(task_id)], session_id="s", root=self.root,
                                        generated_at="2026-09-29T10:00:00-07:00")
        self.assertEqual(actions.append_log(log, retried), {"appended": 0, "idempotent": 1})

    def test_same_session_cannot_rewrite_its_history(self) -> None:
        task_id = self.add_task()
        log = self.root / actions.LOG_RELATIVE
        actions.append_log(log, actions.build_records(claims=[claim(task_id)], session_id="s", root=self.root, generated_at=LATER))
        changed = actions.build_records(claims=[claim(task_id, declared_outcome="something else")],
                                        session_id="s", root=self.root, generated_at=LATER)
        with self.assertRaisesRegex(actions.Refusal, "different content"):
            actions.append_log(log, changed)


class RenameKeepsIdentity(RootCase):
    def test_retitled_task_keeps_id_and_path(self) -> None:
        task_id = self.add_task()
        path = self.tasks / f"{task_id}.md"
        path.write_text(path.read_text().replace("Reply to Alex about the contract", "Send Alex the updated agreement"))
        tasks = ledger.load_ledger(self.tasks, self.root)
        self.assertEqual([(t.fields["id"], t.path.name) for t in tasks], [(task_id, f"{task_id}.md")])

    def test_mail_subject_change_on_same_thread_keeps_identity(self) -> None:
        a = ledger.normalize_signal("mail", {"thread_key": "t1", "last_ts": "1", "bucket": "owed_by_me", "subject": "Contract"}, "reply")
        b = ledger.normalize_signal("mail", {"thread_key": "t1", "last_ts": "1", "bucket": "owed_by_me", "subject": "Re: Agreement"}, "reply")
        self.assertEqual(a["occurrence_key"], b["occurrence_key"])


class TwoSourcesOneTitle(RootCase):
    def test_identical_titles_from_different_sources_warn_but_never_merge(self) -> None:
        left = {"title": "Follow up with Alex", "entity_key": "mail:t1"}
        right = {"title": "follow up  with alex", "entity_key": "messages:sms:t9"}
        self.assertTrue(ledger.collision_warning(left, right))
        self.add_task(title='"Follow up with Alex"')
        self.add_task(title='"Follow up with Alex"')
        self.assertEqual(len(ledger.load_ledger(self.tasks, self.root)), 2)

    def test_a_claim_touches_only_the_claimed_task(self) -> None:
        mine, other = self.add_task(), self.add_task()
        other_before = (self.tasks / f"{other}.md").read_bytes()
        actions.run_claims(self.root, [claim(mine)], session_id="s", now=LATER, write=True)
        self.assertEqual((self.tasks / f"{other}.md").read_bytes(), other_before)


class ClaimsWithoutProof(RootCase):
    def test_claimed_done_without_evidence_holds_open(self) -> None:
        task_id = self.add_task()
        result = actions.run_claims(self.root, [claim(task_id, verification="pending", attestation=None)],
                                    session_id="s", now=LATER, write=True)
        self.assertTrue(result["records"][0]["verification_pending"])
        self.assertEqual(self.status_of(task_id), "active")

    def test_verified_needs_structured_evidence(self) -> None:
        task_id = self.add_task()
        before = snapshot(self.root)
        with self.assertRaisesRegex(actions.Refusal, "structured evidence"):
            actions.run_claims(self.root, [claim(task_id, verification="verified", attestation=None)],
                               session_id="s", now=LATER, write=True)
        self.assertEqual(snapshot(self.root), before)

    def test_attestation_cannot_ride_along_on_another_verification(self) -> None:
        task_id = self.add_task()
        with self.assertRaisesRegex(actions.Refusal, "only valid with owner-attested"):
            actions.build_records(claims=[claim(task_id, verification="pending")], session_id="s",
                                  root=self.root, generated_at=LATER)


class LifecycleTransitions(RootCase):
    def test_transition_table_is_exactly_the_spec_table(self) -> None:
        self.assertEqual(ledger.TRANSITIONS, {
            "proposed": {"active", "scheduled", "parked", "dismissed"},
            "active": {"done", "waiting", "scheduled", "parked", "dismissed"},
            "waiting": {"active", "done", "dismissed"},
            "scheduled": {"active", "done", "dismissed"},
            "parked": {"active", "done", "dismissed"},
            "done": set(),
            "dismissed": set(),
        })

    def test_illegal_transition_refuses(self) -> None:
        task_id = self.add_task(status="waiting", review_after='"2026-10-05"')
        with self.assertRaisesRegex(actions.Refusal, "illegal transition waiting -> parked"):
            actions.build_records(claims=[{"task_id": task_id, "disposition": "parked", "declared_outcome": "x",
                                           "verification": "not-applicable"}],
                                  session_id="s", root=self.root, generated_at=LATER)

    def test_closed_task_refuses_every_reopen(self) -> None:
        for terminal in ledger.TERMINAL:
            task_id = self.add_task(status=terminal)
            for disposition in actions.DISPOSITIONS:
                with self.assertRaisesRegex(actions.Refusal, "already closed"):
                    actions.build_records(claims=[{"task_id": task_id, "disposition": disposition,
                                                   "declared_outcome": "reopen", "verification": "not-applicable"}],
                                          session_id="s", root=self.root, generated_at=LATER)


class CorruptTaskRecord(RootCase):
    CORRUPTIONS = {
        "no frontmatter": lambda t: t.replace("---\n", "", 1),
        "nested yaml": lambda t: t.replace("area: work", "area:\n  - work"),
        "bad json": lambda t: t.replace('source_refs: [', 'source_refs: [{', 1),
        "unknown status": lambda t: t.replace("status: active", "status: finished"),
        "missing heading": lambda t: t.replace("## Event log", "## Log"),
        "naive timestamp": lambda t: t.replace("updated_at: 2026-09-28T22:23:48-07:00", "updated_at: 2026-09-28T22:23:48"),
        "time travel": lambda t: t.replace("updated_at: 2026-09-28T22:23:48-07:00", "updated_at: 2020-01-01T00:00:00Z"),
        "empty source refs": lambda t: t.replace(t[t.index("source_refs: "):t.index("\n", t.index("source_refs: "))], "source_refs: []"),
    }

    def test_every_corruption_fails_the_ledger_closed(self) -> None:
        for name, corrupt in self.CORRUPTIONS.items():
            with self.subTest(name):
                for path in self.tasks.glob("*.md"):
                    path.unlink()
                task_id = self.add_task()
                path = self.tasks / f"{task_id}.md"
                path.write_text(corrupt(path.read_text()))
                with self.assertRaises(ledger.LedgerError):
                    ledger.load_ledger(self.tasks, self.root)

    def test_one_bad_file_blocks_every_change(self) -> None:
        good = self.add_task()
        bad = self.add_task()
        (self.tasks / f"{bad}.md").write_text("not a task\n")
        before = snapshot(self.root)
        with self.assertRaisesRegex(actions.Refusal, "ledger invalid"):
            actions.run_claims(self.root, [claim(good)], session_id="s", now=LATER, write=True)
        self.assertEqual(snapshot(self.root), before)

    def test_duplicate_task_id_refuses(self) -> None:
        task_id = self.add_task()
        text = (self.tasks / f"{task_id}.md").read_text()
        (self.tasks / "tsk-copy.md").write_text(text)
        with self.assertRaisesRegex(ledger.LedgerError, "filename must be"):
            ledger.load_ledger(self.tasks, self.root)


if __name__ == "__main__":
    unittest.main()
