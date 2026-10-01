"""Changing tasks: claims, the append-only log, and the note writer.

Every change to a task is a *claim*: a task ID, a requested disposition, a
declared outcome, and how that outcome is known. A claim is validated against
the task's current state and the transition table, recorded in an append-only
JSONL log, and only then applied to the task file as one new event row.

The core safety rule: a claim of ``done`` closes a task only with structured
evidence (``verified``) or the owner's own words (``owner-attested``). A
``done`` without either is held: the task stays where it was and the log
records the completion as pending.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .ledger import (
    EVENT_ROW_HEADER,
    TASK_ID_RE,
    TERMINAL,
    LedgerError,
    atomic_write,
    load_ledger,
    new_task_id,
    parse_task,
    transition_allowed,
    validate_task,
    AREA_RE,
)


SCHEMA_VERSION = 1
TASKS_DIR = "tasks"
LOG_RELATIVE = Path(TASKS_DIR) / "_log.jsonl"
DISPOSITIONS = {"done", "active", "waiting", "scheduled", "parked", "dismissed"}
# owner-attested: the owner stated the outcome in their own words and no
# checkable artifact will exist. It closes a task, but the record and the event
# row carry the words verbatim, so an attested close can never be read as a
# verified one. Model inference is never owner-attested.
VERIFICATIONS = {"verified", "pending", "not-applicable", "owner-attested"}
CREATE_STATES = {"active", "waiting", "scheduled", "parked"}
VERB_TO_DISPOSITION = {
    "done": "done", "wait": "waiting", "park": "parked", "schedule": "scheduled",
    "dismiss": "dismissed", "reactivate": "active", "reschedule": "active", "note": "active",
}


class Refusal(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_id(session_id: str, task_id: str) -> str:
    """One record per task per session, so a retried session is a no-op."""
    digest = hashlib.sha256(f"{session_id}\0{task_id}".encode()).hexdigest()[:16]
    return f"rec-{digest}"


def resolve_tasks(root: Path) -> dict[str, dict[str, Any]]:
    tasks_dir = root / TASKS_DIR
    if not tasks_dir.exists():
        return {}
    try:
        tasks = load_ledger(tasks_dir, root)
    except LedgerError as error:
        raise Refusal(f"ledger invalid; refusing to change anything: {error}") from error
    return {
        task.fields["id"]: {
            "path": task.path.relative_to(root).as_posix(),
            "title": task.fields["title"],
            "status": task.fields["status"],
            "observed_revision": task.content_sha256,
            "due": task.fields.get("due"),
            "review_after": task.fields.get("review_after"),
        }
        for task in tasks
    }


def validate_evidence(evidence: Any, *, index: int) -> list[dict[str, str]]:
    if evidence is None:
        return []
    if not isinstance(evidence, list):
        raise Refusal(f"claim {index}: evidence must be a list")
    normalized: list[dict[str, str]] = []
    for evidence_index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise Refusal(f"claim {index}: evidence {evidence_index} must be an object")
        required = ("source_ref", "observed_revision", "assertion")
        missing = [key for key in required if not str(item.get(key, "")).strip()]
        if missing:
            raise Refusal(f"claim {index}: evidence {evidence_index} missing {', '.join(missing)}")
        normalized.append({key: str(item[key]).strip() for key in required})
    return normalized


def build_records(
    *, claims: Any, session_id: str, root: Path, generated_at: str
) -> list[dict[str, Any]]:
    """Validate claims and turn them into log records. Writes nothing."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise Refusal("session_id is required")
    if not isinstance(claims, list) or not claims:
        raise Refusal("claims must be a non-empty list")
    tasks = resolve_tasks(root)
    seen: set[str] = set()
    records: list[dict[str, Any]] = []

    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise Refusal(f"claim {index} must be an object")
        task_id = claim.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise Refusal(f"claim {index}: task_id must be an exact task ID")
        if task_id in seen:
            raise Refusal(f"claim {index}: duplicate claim for {task_id}")
        seen.add(task_id)
        task = tasks.get(task_id)
        if task is None:
            raise Refusal(f"claim {index}: unknown task id {task_id}")

        requested = claim.get("disposition")
        if requested not in DISPOSITIONS:
            raise Refusal(f"claim {index}: disposition must be one of {sorted(DISPOSITIONS)}")
        declared_outcome = str(claim.get("declared_outcome", "")).strip()
        if not declared_outcome:
            raise Refusal(f"claim {index}: declared_outcome is required")
        verification = claim.get("verification", "pending")
        if verification not in VERIFICATIONS:
            raise Refusal(f"claim {index}: verification must be one of {sorted(VERIFICATIONS)}")
        evidence = validate_evidence(claim.get("evidence"), index=index)
        attestation = claim.get("attestation")
        if verification == "owner-attested":
            if not isinstance(attestation, str) or not attestation.strip():
                raise Refusal(f"claim {index}: owner-attested requires the owner's words, verbatim")
        elif attestation is not None:
            raise Refusal(f"claim {index}: attestation is only valid with owner-attested")
        if verification == "verified" and not evidence:
            raise Refusal(f"claim {index}: verified requires at least one structured evidence item")

        current = task["status"]
        if current in TERMINAL:
            raise Refusal(f"claim {index}: {task_id} is already closed ({current})")
        # `due` is the obligation date. Only an explicit claim moves it.
        due = claim.get("due", "__unset__")
        due_change = None
        if due != "__unset__":
            if due is not None:
                try:
                    dt.date.fromisoformat(str(due))
                except ValueError as error:
                    raise Refusal(f"claim {index}: due must be YYYY-MM-DD or null") from error
            if due != task.get("due"):
                due_change = {"from": task.get("due"), "to": due}
        review_after = claim.get("review_after")
        if requested == "scheduled" and not review_after:
            raise Refusal(f"claim {index}: review_after is required for {requested}")
        if review_after is not None:
            try:
                dt.date.fromisoformat(str(review_after))
            except ValueError as error:
                raise Refusal(f"claim {index}: review_after must be YYYY-MM-DD or null") from error

        # A declared completion without evidence or attestation stays open.
        # Inference can propose a closure; it cannot manufacture one.
        proposed = requested
        verification_pending = False
        if requested == "done" and verification not in {"verified", "owner-attested"}:
            proposed = current
            verification = "pending"
            verification_pending = True
            # A held completion leaves the task where it was, visibility gate included.
            if current in {"waiting", "scheduled", "parked"} and review_after is None:
                review_after = task.get("review_after")

        if proposed != current and not transition_allowed(current, proposed):
            raise Refusal(f"claim {index}: illegal transition {current} -> {proposed}")

        records.append({
            "schema_version": SCHEMA_VERSION,
            "kind": "task-reconciliation",
            "record_id": record_id(session_id, task_id),
            "session_id": session_id,
            "generated_at": generated_at,
            "task_id": task_id,
            "task_title": task["title"],
            "task_ref": {"path": task["path"], "observed_revision": task["observed_revision"]},
            "from": current,
            "requested_disposition": requested,
            "proposed_to": proposed,
            "declared_outcome": declared_outcome,
            "evidence": evidence,
            "verification": verification,
            "attestation": attestation,
            "verification_pending": verification_pending,
            "review_after": review_after,
            "due_change": due_change,
            "decision_reason": claim.get("decision_reason"),
        })
    return records


def append_log(log_path: Path, records: list[dict[str, Any]]) -> dict[str, int]:
    """Append records once. A retry is a no-op; a conflicting retry refuses."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, Any]] = {}
    if log_path.exists():
        for line_number, line in enumerate(log_path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise Refusal(f"log is corrupt at line {line_number}: {error.msg}") from error
            if record.get("record_id"):
                existing[record["record_id"]] = record

    new_records: list[dict[str, Any]] = []
    for record in records:
        old = existing.get(record["record_id"])
        if old is None:
            new_records.append(record)
        # generated_at is append metadata, not claim meaning: a retry of the
        # same session's claim at a later time is still a no-op.
        elif canonical_json({k: v for k, v in old.items() if k != "generated_at"}) != canonical_json(
            {k: v for k, v in record.items() if k != "generated_at"}
        ):
            raise Refusal(
                f"record {record['record_id']} already exists with different content; "
                "use a new session id rather than overwriting history"
            )

    if new_records:
        with log_path.open("a") as handle:
            for record in new_records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return {"appended": len(new_records), "idempotent": len(records) - len(new_records)}


def _set_frontmatter(text: str, key: str, value: str) -> str:
    head, sep, rest = text.partition("\n---\n")
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    if not pattern.search(head):
        raise Refusal(f"task file lacks frontmatter key {key}")
    return pattern.sub(f"{key}: {value}", head, count=1) + sep + rest


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def apply_records(root: Path, records: list[dict[str, Any]]) -> list[str]:
    """Apply each record to its task file as one appended event row.

    Refuses if the file changed since the claim was resolved. Re-validates
    after writing and restores the original bytes if the result is invalid.
    """
    written: list[str] = []
    for record in records:
        note_path = root / record["task_ref"]["path"]
        original = note_path.read_bytes()
        task = parse_task(note_path)
        text = original.decode("utf-8")
        if record["record_id"] in text:
            continue  # already applied; a retry after a partial run is a no-op
        if task.content_sha256 != record["task_ref"]["observed_revision"]:
            raise Refusal(f"{note_path.name} changed since the claim was resolved")
        detail = _cell(record["declared_outcome"])
        if record["verification_pending"]:
            detail = f"completion held for evidence: {detail}"
        elif record.get("attestation"):
            detail = f"{detail} (owner-attested: \"{_cell(record['attestation'])}\")"
        due_change = record.get("due_change")
        if record["proposed_to"] == "parked" and task.fields.get("due") is not None and not due_change:
            due_change = {"from": task.fields.get("due"), "to": None}  # parking clears the due date
        event = "reconciled"
        if due_change and record["proposed_to"] == record["from"] and not record["verification_pending"]:
            event = "rescheduled"
        if due_change:
            detail = f"due {due_change['from'] or '—'} → {due_change['to'] or '—'} · {detail}"
        row = (
            f"| {record['generated_at']} | {event} | {record['from']} | {record['proposed_to']} | "
            f"{detail} (record `{record['record_id']}`, verification {record['verification']}) |"
        )
        if EVENT_ROW_HEADER not in text:
            raise Refusal(f"{note_path.name} has no event log table")
        text = text.rstrip("\n") + "\n" + row + "\n"
        text = _set_frontmatter(text, "status", record["proposed_to"])
        text = _set_frontmatter(text, "updated_at", record["generated_at"])
        review_after = record.get("review_after")
        text = _set_frontmatter(text, "review_after", review_after if review_after else "null")
        if due_change:
            text = _set_frontmatter(text, "due", due_change["to"] if due_change["to"] else "null")
        note_path.write_bytes(text.encode("utf-8"))
        try:
            errors = validate_task(parse_task(note_path), root)
        except LedgerError as error:
            errors = [str(error)]
        if errors:
            note_path.write_bytes(original)
            raise Refusal(f"{note_path.name} failed validation after write: {errors}")
        written.append(note_path.relative_to(root).as_posix())
    return written


def claim_for_verb(
    root: Path, verb: str, task_id: str, *, outcome: str, now: str,
    attest: str | None = None, evidence: list[str] | None = None,
    review_after: str | None = None, due: str | None = None, reason: str | None = None,
) -> dict[str, Any]:
    """Spell one verb as one claim. Adds no new way to change a task."""
    if verb not in VERB_TO_DISPOSITION:
        raise Refusal(f"unknown verb {verb!r}")
    disposition = VERB_TO_DISPOSITION[verb]
    if verb in ("note", "reschedule"):
        # Progress or a date move keeps the current state; never a reactivation in disguise.
        task = resolve_tasks(root).get(task_id)
        if task is None:
            raise Refusal(f"{task_id}: unknown task id")
        disposition = task["status"]
        if disposition in ("parked", "scheduled", "waiting") and not review_after:
            review_after = task.get("review_after")
    items = []
    for item in evidence or []:
        ref, _, assertion = item.partition("=")
        if not ref or not assertion:
            raise Refusal(f"evidence must be REF=ASSERTION, got {item!r}")
        items.append({"source_ref": ref, "observed_revision": now, "assertion": assertion})
    if attest:
        verification = "owner-attested"
    elif items:
        verification = "verified"
    else:
        verification = "not-applicable" if disposition != "done" else "pending"
    claim: dict[str, Any] = {
        "task_id": task_id,
        "disposition": disposition,
        "declared_outcome": outcome,
        "verification": verification,
        "evidence": items,
        "review_after": review_after,
        "decision_reason": reason,
    }
    if attest:
        claim["attestation"] = attest
    if verb == "reschedule":
        claim["due"] = None if due in (None, "null") else due
    elif due:
        claim["due"] = due
    return claim


def run_claims(root: Path, claims: list[dict[str, Any]], *, session_id: str, now: str, write: bool) -> dict[str, Any]:
    records = build_records(claims=claims, session_id=session_id, root=root, generated_at=now)
    if not write:
        return {"mode": "dry-run", "records": records}
    counts = append_log(root / LOG_RELATIVE, records)
    written = apply_records(root, records)
    return {"mode": "write", **counts, "written": written, "records": records}


def create_task(
    root: Path, *, title: str, area: str, state: str, outcome: str, now: str, session_id: str,
    write: bool, due: str | None = None, review_after: str | None = None,
    source_keys: list[str] | None = None, why_now: str | None = None,
) -> dict[str, Any]:
    """Create a task directly (``origin: manual``). Dry-run unless ``write``."""
    title = (title or "").strip()
    if not title:
        raise Refusal("title is required")
    if not isinstance(area, str) or not AREA_RE.fullmatch(area):
        raise Refusal("area must be a short lowercase slug, like work or home")
    if state not in CREATE_STATES:
        raise Refusal(f"state must be one of {sorted(CREATE_STATES)}")
    if state == "scheduled" and not review_after:
        raise Refusal("a scheduled task needs review_after")
    for value in (due, review_after):
        if value:
            dt.date.fromisoformat(value)
    if not (outcome or "").strip():
        raise Refusal("outcome (the desired outcome) is required")
    task_id = new_task_id()
    origin = {"type": "manual", "created_at": now, "session_id": session_id}
    refs: list[dict[str, Any]] = [{"kind": "owner-request", "id": session_id}]
    # Source threads the task is about, as `kind:id` keys, so a plan reading
    # those sources can treat them as handled once the task closes.
    for key in source_keys or []:
        kind, _, ident = key.partition(":")
        if not kind or not ident:
            raise Refusal(f"source key must look like kind:id, got {key!r}")
        refs.append({"kind": kind, "id": ident, "source_key": key})
    check = {"kind": "owner-declared", "expected_outcome": outcome.strip()}
    compact = {"separators": (",", ":"), "ensure_ascii": False}
    note = "\n".join([
        "---", "type: task", f"id: {task_id}", f"title: {json.dumps(title, ensure_ascii=False)}",
        f"status: {state}", f"created_at: {now}", f"updated_at: {now}",
        f"due: {due or 'null'}", f"review_after: {review_after or 'null'}", f"area: {area}",
        f"source_refs: {json.dumps(refs, **compact)}",
        f"origin: {json.dumps(origin, **compact)}",
        f"completion_check: {json.dumps(check, **compact)}",
        "---", "", "## Why now", "",
        (why_now or "Created directly by the owner.").strip(), "",
        "## Desired outcome", "", outcome.strip(), "",
        "## Event log", "", EVENT_ROW_HEADER, "|---|---|---|---|---|",
        f"| {now} | created | — | {state} | Created directly (session `{session_id}`) |", "",
    ])
    tasks_dir = root / TASKS_DIR
    path = tasks_dir / f"{task_id}.md"
    if write:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(path, note)
        try:
            load_ledger(tasks_dir, root)
        except LedgerError:
            path.unlink(missing_ok=True)
            raise
    return {"mode": "write" if write else "dry-run", "task_id": task_id,
            "path": f"{TASKS_DIR}/{task_id}.md", "title": title, "area": area, "status": state,
            "due": due, "review_after": review_after}
