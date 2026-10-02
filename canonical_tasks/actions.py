"""Changing tasks: claims, the append-only log, and the note writer.

Every change to a task is a *claim*: a task ID, a requested disposition, a
declared outcome, and how that outcome is known. A claim is validated against
the task's current state and the transition table, every resulting file is
built and validated in memory, and only then is anything written: the task
file gets one new event row, and the append-only JSONL log gets one record.
If anything is refused, nothing is written.

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
    parse_task_text,
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


def claim_fingerprint(claim: dict[str, Any]) -> str:
    """The meaning of a claim, for telling a retry from a different claim.

    Every evidence field counts, including ``observed_revision``.
    """
    evidence = claim.get("evidence") or []
    meaning = {
        "disposition": claim.get("disposition"),
        "declared_outcome": str(claim.get("declared_outcome", "")).strip(),
        "verification": claim.get("verification", "pending"),
        "attestation": claim.get("attestation"),
        "evidence": [[e.get("source_ref"), e.get("observed_revision"), e.get("assertion")]
                     if isinstance(e, dict) else e for e in evidence]
        if isinstance(evidence, list) else evidence,
        "review_after": claim.get("review_after"),
        "due": claim.get("due", "__unset__"),
        "decision_reason": claim.get("decision_reason"),
    }
    return hashlib.sha256(canonical_json(meaning).encode()).hexdigest()[:16]


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
        # No coercion: a null or a number is not evidence, even as text.
        missing = [key for key in required if not isinstance(item.get(key), str) or not item[key].strip()]
        if missing:
            raise Refusal(f"claim {index}: evidence {evidence_index} needs non-empty text for {', '.join(missing)}")
        normalized.append({key: item[key].strip() for key in required})
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
        if requested == "parked":
            if due not in ("__unset__", None):
                raise Refusal(f"claim {index}: parking clears the due date; don't pass one")
            due_change = {"from": task.get("due"), "to": None} if task.get("due") is not None else None
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
            "claim_fingerprint": claim_fingerprint(claim),
        })
    return records


def torn_tail_length(log_path: Path) -> int:
    """Bytes after the last newline: an append the disk never finished."""
    if not log_path.exists():
        return 0
    data = log_path.read_bytes()
    return 0 if not data or data.endswith(b"\n") else len(data) - (data.rfind(b"\n") + 1)


def load_log(log_path: Path) -> dict[str, dict[str, Any]]:
    """Every record in the log by record_id. A corrupt complete line refuses.

    An unfinished last line (no trailing newline) is a torn append, never a
    committed record, so it's ignored here and trimmed before the next append.
    """
    existing: dict[str, dict[str, Any]] = {}
    if log_path.exists():
        text = log_path.read_bytes()
        torn = torn_tail_length(log_path)
        complete = text[: len(text) - torn].decode("utf-8")
        for line_number, line in enumerate(complete.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise Refusal(f"log is corrupt at line {line_number}: {error.msg}") from error
            if record.get("record_id"):
                existing[record["record_id"]] = record
    return existing


def new_log_records(existing: dict[str, dict[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records not yet in the log. A same-id record with different meaning refuses."""
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
    return new_records


def append_log(log_path: Path, records: list[dict[str, Any]]) -> dict[str, int]:
    """Append records once. A retry is a no-op; a conflicting retry refuses."""
    new_records = new_log_records(load_log(log_path), records)
    log_path.parent.mkdir(parents=True, exist_ok=True)
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


def prepare_writes(root: Path, records: list[dict[str, Any]]) -> list[tuple[Path, str, bytes]]:
    """Build every task file change in memory and validate it. Writes nothing.

    Returns (path, sha256 of the bytes it was built from, new bytes) for each
    record not already applied. Refuses if a file changed since the claim was
    resolved, or if the result wouldn't validate.
    """
    prepared: list[tuple[Path, str, bytes]] = []
    tasks_dir = (root / TASKS_DIR).resolve()
    for record in records:
        note_path = root / record["task_ref"]["path"]
        if note_path.is_symlink() or note_path.resolve().parent != tasks_dir:
            raise Refusal(f"{note_path} is not a regular file in {TASKS_DIR}/")
        original = note_path.read_bytes()
        task = parse_task_text(original, note_path)
        text = original.decode("utf-8")
        if record["record_id"] in text:
            continue  # already applied; a retry after a partial run is a no-op
        if task.content_sha256 != record["task_ref"]["observed_revision"]:
            raise Refusal(f"{note_path.name} changed since the claim was resolved")
        if EVENT_ROW_HEADER not in text:
            raise Refusal(f"{note_path.name} has no event log table")
        detail = _cell(record["declared_outcome"])
        if record["verification_pending"]:
            detail = f"completion held for evidence: {detail}"
        elif record.get("attestation"):
            detail = f"{detail} (owner-attested: \"{_cell(record['attestation'])}\")"
        due_change = record.get("due_change")
        event = "reconciled"
        if due_change and record["proposed_to"] == record["from"] and not record["verification_pending"]:
            event = "rescheduled"
        if due_change:
            detail = f"due {due_change['from'] or '—'} → {due_change['to'] or '—'} · {detail}"
        row = (
            f"| {record['generated_at']} | {event} | {record['from']} | {record['proposed_to']} | "
            f"{detail} (record `{record['record_id']}`, verification {record['verification']}) |"
        )
        text = text.rstrip("\n") + "\n" + row + "\n"
        text = _set_frontmatter(text, "status", record["proposed_to"])
        text = _set_frontmatter(text, "updated_at", record["generated_at"])
        review_after = record.get("review_after")
        text = _set_frontmatter(text, "review_after", review_after if review_after else "null")
        if due_change:
            text = _set_frontmatter(text, "due", due_change["to"] if due_change["to"] else "null")
        new_bytes = text.encode("utf-8")
        try:
            errors = validate_task(parse_task_text(new_bytes, note_path), root)
        except LedgerError as error:
            errors = [str(error)]
        if errors:
            raise Refusal(f"{note_path.name} would fail validation after this change: {errors}")
        prepared.append((note_path, task.content_sha256, new_bytes))
    return prepared


def verify_unchanged(prepared: list[tuple[Path, str, bytes]]) -> None:
    for path, sha, _ in prepared:
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise Refusal(f"{path.name} changed while this operation was running; nothing written")


def commit_writes(prepared: list[tuple[Path, str, bytes]]) -> list[Path]:
    """Re-check every file is unchanged, then replace each one atomically."""
    verify_unchanged(prepared)
    for path, _, new_bytes in prepared:
        atomic_write(path, new_bytes.decode("utf-8"))
    return [path for path, _, _ in prepared]


def apply_records(root: Path, records: list[dict[str, Any]]) -> list[str]:
    """Apply each record to its task file as one appended event row."""
    written = commit_writes(prepare_writes(root, records))
    return [path.relative_to(root).as_posix() for path in written]


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
        pointer, _, assertion = item.partition("=")
        ref, _, revision = pointer.partition("@")
        if not ref or not assertion:
            raise Refusal(f"evidence must be REF=ASSERTION or REF@REVISION=ASSERTION, got {item!r}")
        # A fixed default keeps a retried command identical to the original.
        items.append({"source_ref": ref, "observed_revision": revision or "unversioned", "assertion": assertion})
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


class PartialWrite(RuntimeError):
    """The disk failed after writing began. Re-run the same session to finish."""


def run_claims(root: Path, claims: list[dict[str, Any]], *, session_id: str, now: str, write: bool) -> dict[str, Any]:
    """Validate and prepare everything, then write the log, then the task files.

    A refusal writes nothing. If the disk fails partway, the log already holds
    the record, and re-running the same session applies whatever is missing.
    """
    log_path = root / LOG_RELATIVE
    resolve_tasks(root)  # the whole ledger must be valid, even for a recovery retry
    existing = load_log(log_path)
    pending: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []
    for claim in claims if isinstance(claims, list) else []:
        task_id = claim.get("task_id") if isinstance(claim, dict) else None
        logged = existing.get(record_id(session_id, task_id)) if isinstance(task_id, str) else None
        if logged is None:
            pending.append(claim)
            continue
        # This session already recorded a claim for this task. Same meaning:
        # a retry. Different meaning: refuse.
        if logged.get("claim_fingerprint") != claim_fingerprint(claim):
            raise Refusal(f"session {session_id} already recorded a different claim for {task_id}")
        already.append(logged)
    records = build_records(claims=pending, session_id=session_id, root=root, generated_at=now) if pending else []
    if not write:
        return {"mode": "dry-run", "records": already + records}
    # Logged but not yet applied (an earlier run failed mid-write): finish it.
    prepared = prepare_writes(root, already + records)
    to_log = new_log_records(existing, records)
    verify_unchanged(prepared)  # last check before anything is written; refusal here writes nothing
    try:
        if to_log:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            torn = torn_tail_length(log_path)
            if torn:
                with log_path.open("r+b") as handle:
                    handle.truncate(log_path.stat().st_size - torn)
            with log_path.open("a") as handle:
                for record in to_log:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        written = commit_writes(prepared)
    except OSError as error:
        raise PartialWrite(
            f"disk error after writing began ({error}); re-run with --session {session_id} to finish"
        ) from error
    except Refusal as error:
        # A task file changed between the last check and the write. The log
        # already holds the record, so this is not a clean refusal.
        raise PartialWrite(
            f"{error}; the log recorded this claim but the task file was not updated. "
            "Review the file, then make a new claim with a new session"
        ) from error
    return {"mode": "write", "appended": len(to_log), "idempotent": len(already),
            "written": [path.relative_to(root).as_posix() for path in written], "records": already + records}


def source_refs_for(source_keys: list[str] | None) -> list[dict[str, Any]]:
    """Source threads a task is about, as `kind:id` keys, so a plan reading those
    sources can treat them as handled while the task covers them."""
    refs = []
    for key in source_keys or []:
        kind, _, ident = key.partition(":")
        if not kind or not ident:
            raise Refusal(f"source key must look like kind:id, got {key!r}")
        refs.append({"kind": kind, "id": ident, "source_key": key})
    return refs


def link_sources(
    root: Path, task_id: str, *, source_keys: list[str] | None, outcome: str, now: str,
    session_id: str, write: bool,
) -> dict[str, Any]:
    """Attach source keys to an existing task, open or closed. Dry-run unless ``write``.

    For a source that turns out to belong to a task created without it: a reply
    that arrives on a thread you're already waiting on, or a notification email
    about a conversation you already decided. Without the key, a plan reading
    that source has nothing tying it to the task and judges it as new work.
    The status doesn't change; the task file gets one ``linked`` event row.
    Linking a key the task already has is a no-op.
    """
    if not source_keys:
        raise Refusal("link needs at least one source key")
    if not (outcome or "").strip():
        raise Refusal("outcome (why these sources belong to the task) is required")
    task = resolve_tasks(root).get(task_id)
    if task is None:
        raise Refusal(f"{task_id}: unknown task id")
    path = root / task["path"]
    if path.is_symlink() or path.resolve().parent != (root / TASKS_DIR).resolve():
        raise Refusal(f"{path} is not a regular file in {TASKS_DIR}/")
    original = path.read_bytes()
    parsed = parse_task_text(original, path)
    refs = list(parsed.fields["source_refs"])
    have = {ref.get("source_key") for ref in refs}
    added = [ref for ref in source_refs_for(source_keys) if ref["source_key"] not in have]
    result: dict[str, Any] = {"mode": "write" if write else "dry-run", "task_id": task_id,
                              "status": task["status"], "added": [r["source_key"] for r in added],
                              "already_linked": [k for k in source_keys if k in have]}
    if not added:
        return result
    text = original.decode("utf-8")
    if EVENT_ROW_HEADER not in text:
        raise Refusal(f"{path.name} has no event log table")
    keys = ", ".join(f"`{r['source_key']}`" for r in added)
    status = task["status"]
    text = text.rstrip("\n") + f"\n| {now} | linked | {status} | {status} | {keys} — {_cell(outcome)} (session `{session_id}`) |\n"
    text = _set_frontmatter(text, "source_refs", json.dumps(refs + added, separators=(",", ":"), ensure_ascii=False))
    text = _set_frontmatter(text, "updated_at", now)
    new_bytes = text.encode("utf-8")
    try:
        errors = validate_task(parse_task_text(new_bytes, path), root)
    except LedgerError as error:
        errors = [str(error)]
    if errors:
        raise Refusal(f"{path.name} would fail validation after this change: {errors}")
    if write:
        commit_writes([(path, parsed.content_sha256, new_bytes)])
    return result


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
    refs: list[dict[str, Any]] = [{"kind": "owner-request", "id": session_id}] + source_refs_for(source_keys)
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
    if tasks_dir.exists():
        resolve_tasks(root)  # an invalid ledger refuses before anything is written
    errors = validate_task(parse_task_text(note.encode("utf-8"), path), root)
    if errors:
        raise Refusal(f"new task would not validate: {errors}")
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
