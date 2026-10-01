"""Task file format, validation, identity, and the generated index.

A task is one Markdown file, ``tasks/tsk-<ULID>.md``, with strict top-level
frontmatter. Container values are inline JSON, which is also valid YAML, so
parsing stays deterministic without a YAML dependency. The task files are the
authority; everything this module renders from them is a disposable view.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
TERMINAL = {"done", "dismissed"}
NONTERMINAL = {"proposed", "active", "waiting", "scheduled", "parked"}
STATUSES = NONTERMINAL | TERMINAL
ACTION_KINDS = {
    "reply", "follow_up", "prepare", "decide", "review", "schedule",
    "physical", "research", "notify", "unclassified",
}
TRANSITIONS = {
    "proposed": {"active", "scheduled", "parked", "dismissed"},
    "active": {"done", "waiting", "scheduled", "parked", "dismissed"},
    # Gated states may close directly: "waiting on X, X happened, done" is the
    # common path, and closing is still guarded by the evidence rule.
    "waiting": {"active", "done", "dismissed"},
    "scheduled": {"active", "done", "dismissed"},
    "parked": {"active", "done", "dismissed"},
    "done": set(),
    "dismissed": set(),
}
AREA_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
TASK_ID_RE = re.compile(r"^tsk-[0-9A-HJKMNP-TV-Z]{26}$")
TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$"
)
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REQUIRED_HEADINGS = ("## Why now", "## Desired outcome", "## Event log")
EVENT_ROW_HEADER = "| At | Event | From | To | Detail |"


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedTask:
    path: Path
    fields: dict[str, Any]
    body: str
    content_sha256: str


def encode_crockford(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(CROCKFORD[value & 31])
        value >>= 5
    if value:
        raise ValueError("value does not fit requested Crockford length")
    return "".join(reversed(chars))


def new_task_id() -> str:
    """A ULID: 48 bits of millisecond time, then 80 random bits, Crockford base32."""
    timestamp_ms = int(time.time() * 1000)
    entropy = int.from_bytes(os.urandom(10), "big")
    return "tsk-" + encode_crockford(timestamp_ms, 10) + encode_crockford(entropy, 16)


def parse_scalar(raw: str, *, path: Path, line_number: int) -> Any:
    value = raw.strip()
    if value == "":
        return ""
    if value in {"null", "true", "false"} or value[0] in '[{"' or re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise LedgerError(f"{path}:{line_number}: invalid inline JSON: {error.msg}") from error
    return value


def parse_task(path: Path) -> ParsedTask:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise LedgerError(f"{path}: missing opening frontmatter delimiter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise LedgerError(f"{path}: missing closing frontmatter delimiter") from error

    fields: dict[str, Any] = {}
    for offset, line in enumerate(lines[1:end], start=2):
        if not line.strip():
            continue
        if line[:1].isspace():
            raise LedgerError(
                f"{path}:{offset}: nested YAML is not supported; use inline JSON for lists/objects"
            )
        key, separator, value = line.partition(":")
        if not separator or not key.strip():
            raise LedgerError(f"{path}:{offset}: expected key: value")
        key = key.strip()
        if key in fields:
            raise LedgerError(f"{path}:{offset}: duplicate frontmatter key {key!r}")
        fields[key] = parse_scalar(value, path=path, line_number=offset)

    body = "\n".join(lines[end + 1 :]).strip() + "\n"
    return ParsedTask(path, fields, body, hashlib.sha256(raw).hexdigest())


def parse_timestamp(value: Any, field: str, path: Path) -> dt.datetime:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise LedgerError(
            f"{path}: {field} must be ISO-8601 with Z or an explicit ±HH:MM offset"
        )
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LedgerError(f"{path}: {field} is not a valid ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise LedgerError(f"{path}: {field} must include a timezone offset")
    return parsed


def parse_date(value: Any, field: str, path: Path) -> dt.date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LedgerError(f"{path}: {field} must be YYYY-MM-DD or null")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise LedgerError(f"{path}: {field} must be YYYY-MM-DD or null") from error


def action_kind_from_signal_key(signal_key: str) -> str:
    action = signal_key.rsplit(":", 1)[-1]
    if action not in ACTION_KINDS:
        raise LedgerError(f"signal_key action kind {action!r} is not allowlisted")
    return action


def resolve_root_path(root: Path, relative: str, task_path: Path) -> Path:
    """Resolve a source path inside ``root`` without following it outside."""
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise LedgerError(f"{task_path}: source path escapes the root: {relative}")
    # Compare logical paths, not resolved ones, so a deliberate symlink inside
    # the root stays valid provenance.
    candidate = (root / relative_path).absolute()
    try:
        candidate.relative_to(root.absolute())
    except ValueError as error:
        raise LedgerError(f"{task_path}: source path escapes the root: {relative}") from error
    return candidate


def validate_task(task: ParsedTask, root: Path) -> list[str]:
    errors: list[str] = []
    fields = task.fields
    required = {
        "type", "id", "title", "status", "created_at", "updated_at",
        "due", "review_after", "source_refs", "origin", "completion_check",
    }
    missing = sorted(required - fields.keys())
    if missing:
        errors.append(f"missing required fields: {', '.join(missing)}")
        return errors

    task_id = fields["id"]
    if fields["type"] != "task":
        errors.append("type must be task")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        errors.append("id must be tsk- followed by a 26-character Crockford ULID")
    elif task.path.name != f"{task_id}.md":
        errors.append(f"filename must be {task_id}.md")
    if not isinstance(fields["title"], str) or not fields["title"].strip():
        errors.append("title must be a non-empty string")
    status = fields["status"]
    if status not in STATUSES:
        errors.append(f"status must be one of {sorted(STATUSES)}")

    try:
        created = parse_timestamp(fields["created_at"], "created_at", task.path)
        updated = parse_timestamp(fields["updated_at"], "updated_at", task.path)
        if updated < created:
            errors.append("updated_at cannot precede created_at")
    except LedgerError as error:
        errors.append(str(error))
    for field in ("due", "review_after"):
        try:
            parse_date(fields[field], field, task.path)
        except LedgerError as error:
            errors.append(str(error))
    # `parked` may be undated: it then sits in the full inventory and never
    # nags. `scheduled` still needs the date it comes back on.
    if status == "scheduled" and not fields["review_after"]:
        errors.append(f"review_after is required when status is {status}")

    refs = fields["source_refs"]
    if not isinstance(refs, list) or not refs:
        errors.append("source_refs must be a non-empty inline JSON list")
    else:
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict) or not ref.get("kind") or not ref.get("id"):
                errors.append(f"source_refs[{index}] must contain kind and id")
                continue
            if ref.get("path"):
                try:
                    target = resolve_root_path(root, ref["path"], task.path)
                    if not target.exists():
                        errors.append(f"source_refs[{index}] path does not resolve: {ref['path']}")
                except LedgerError as error:
                    errors.append(str(error))

    for field in ("origin", "completion_check"):
        if not isinstance(fields[field], dict):
            errors.append(f"{field} must be an inline JSON object")
    signal_key = fields.get("signal_key")
    occurrence_key = fields.get("occurrence_key")
    if signal_key is not None:
        if not isinstance(signal_key, str) or not signal_key:
            errors.append("signal_key must be a non-empty string or omitted")
        else:
            try:
                action_kind_from_signal_key(signal_key)
            except LedgerError as error:
                errors.append(str(error))
    if occurrence_key is not None and (not isinstance(occurrence_key, str) or not occurrence_key):
        errors.append("occurrence_key must be a non-empty string or omitted")
    area = fields.get("area")
    if area is not None and (not isinstance(area, str) or not AREA_RE.fullmatch(area)):
        errors.append("area must be a short lowercase slug, like work or home")
    origin = fields.get("origin")
    if isinstance(origin, dict) and origin.get("type") == "manual" and not area:
        errors.append("manual tasks require area")
    if isinstance(origin, dict) and origin.get("type") == "signal":
        if not signal_key or not occurrence_key:
            errors.append("signal tasks require signal_key and occurrence_key")

    for heading in REQUIRED_HEADINGS:
        if heading not in task.body.splitlines():
            errors.append(f"body is missing {heading}")
    return errors


def load_ledger(tasks_dir: Path, root: Path) -> list[ParsedTask]:
    """Parse and validate every task. One bad file fails the whole ledger closed."""
    if not tasks_dir.is_dir():
        raise LedgerError(f"tasks directory does not exist: {tasks_dir}")
    tasks: list[ParsedTask] = []
    errors: list[str] = []
    for path in sorted(tasks_dir.glob("tsk-*.md")):
        try:
            task = parse_task(path)
            task_errors = validate_task(task, root)
            if task_errors:
                errors.extend(f"{path}: {error}" for error in task_errors)
            tasks.append(task)
        except (LedgerError, UnicodeDecodeError) as error:
            errors.append(str(error))

    ids: dict[str, Path] = {}
    occurrences: dict[str, Path] = {}
    for task in tasks:
        task_id = task.fields.get("id")
        if isinstance(task_id, str):
            if task_id in ids:
                errors.append(f"duplicate task id {task_id}: {ids[task_id]} and {task.path}")
            ids[task_id] = task.path
        occurrence = task.fields.get("occurrence_key")
        if occurrence and task.fields.get("status") in NONTERMINAL:
            if occurrence in occurrences:
                errors.append(
                    f"duplicate nonterminal occurrence {occurrence}: "
                    f"{occurrences[occurrence]} and {task.path}"
                )
            occurrences[occurrence] = task.path
    if errors:
        raise LedgerError("ledger validation failed:\n- " + "\n- ".join(errors))
    return tasks


def normalize_signal(source: str, observation: dict[str, Any], action_kind: str) -> dict[str, Any]:
    """Turn a source observation into deterministic identity keys.

    ``signal_key`` names the obligation (this thread, this kind of action);
    ``occurrence_key`` adds the source state that created this particular turn
    of it, so a new reply is distinguishable from one already handled.
    """
    if action_kind not in ACTION_KINDS:
        raise LedgerError(f"action kind {action_kind!r} is not allowlisted")
    entity_key: str | None
    revision: str | None
    if source == "mail":
        entity_key = f"mail:{observation['thread_key']}" if observation.get("thread_key") else None
        revision = observation.get("last_ts")
    elif source == "messages":
        entity_key = (
            f"messages:{observation['channel']}:{observation['thread_key']}"
            if observation.get("channel") and observation.get("thread_key") else None
        )
        revision = observation.get("last_ts")
    elif source == "calendar":
        entity_key = f"calendar:{observation['id']}" if observation.get("id") else None
        revision = observation.get("start")
    else:
        entity_key = observation.get("entity_key")
        revision = observation.get("observed_revision")

    if not entity_key:
        return {"keyed": False, "lane": "unkeyed", "auto_merge": False}
    signal_key = f"{entity_key}:{action_kind}"
    occurrence_parts = [signal_key, revision or "unknown"]
    if source in {"mail", "messages"}:
        occurrence_parts.append(observation.get("bucket") or "unknown")
    occurrence_key = ":".join(occurrence_parts)
    bucket = observation.get("bucket")
    blocked = bool(observation.get("blocked"))
    suggested_state = "waiting" if blocked or bucket == "owed_by_them" else "active"
    eligible = not (action_kind == "reply" and bucket == "owed_by_them")
    return {
        "keyed": True,
        "entity_key": entity_key,
        "signal_key": signal_key,
        "occurrence_key": occurrence_key,
        "suggested_state": suggested_state,
        "eligible": eligible,
    }


def decide_signal(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> str:
    """What to do with an incoming signal, given the task it matches (if any)."""
    if not incoming.get("keyed", True):
        return "unkeyed-proposal"
    if existing is None:
        return "create"
    same_signal = existing.get("signal_key") == incoming.get("signal_key")
    same_occurrence = existing.get("occurrence_key") == incoming.get("occurrence_key")
    terminal = existing.get("status") in TERMINAL
    if same_occurrence:
        return "suppress" if terminal else "update-evidence"
    if same_signal:
        return "create-successor" if terminal else "update-open-task"
    return "create"


def visible_today(task: dict[str, Any], as_of: dt.date) -> bool:
    status = task.get("status")
    if status in TERMINAL:
        return False
    review_after = task.get("review_after")
    if status in {"waiting", "scheduled", "parked"}:
        return bool(review_after and dt.date.fromisoformat(review_after) <= as_of)
    return status in {"active", "proposed"}


def transition_allowed(old: str, new: str) -> bool:
    return old in TRANSITIONS and new in TRANSITIONS[old]


def collision_warning(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Same title, different source: warn a person. Never merge on title alone."""
    left_title = " ".join(str(left.get("title", "")).lower().split())
    right_title = " ".join(str(right.get("title", "")).lower().split())
    return bool(left_title and left_title == right_title and left.get("entity_key") != right.get("entity_key"))


def task_record(task: ParsedTask, root: Path) -> dict[str, Any]:
    record = dict(task.fields)
    record["path"] = task.path.resolve().relative_to(root.resolve()).as_posix()
    record["content_sha256"] = task.content_sha256
    return record


def render_outputs(tasks: list[ParsedTask], root: Path, generated_at: str) -> tuple[str, str]:
    records = [task_record(task, root) for task in tasks]
    revision_input = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    revision = hashlib.sha256(revision_input).hexdigest()[:16]
    state = {
        "schema_version": SCHEMA_VERSION,
        "kind": "generated-task-state",
        "do_not_edit": True,
        "generated_at": generated_at,
        "index_revision": revision,
        "task_count": len(records),
        "tasks": records,
    }
    state_text = json.dumps(state, indent=2, sort_keys=True) + "\n"

    as_of = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00")).date()
    visible = [record for record in records if visible_today(record, as_of)]
    retained = [record for record in records if record not in visible and record["status"] not in TERMINAL]
    terminal = [record for record in records if record["status"] in TERMINAL]
    lines = [
        "---",
        "kind: generated-task-queue",
        "do_not_edit: true",
        f"generated_at: {generated_at}",
        f"index_revision: {revision}",
        "---",
        "",
        "# Task Queue",
        "",
        "> Generated from the task files; edit the task, never this file.",
    ]
    for heading, rows in (("Today", visible), ("Retained", retained), ("Closed", terminal)):
        lines.extend(["", f"## {heading}", ""])
        if not rows:
            lines.append("_None._")
            continue
        for record in rows:
            lines.append(f"- [{record['status']}] [{record['title']}]({record['path']}) `{record['id']}`")
    return state_text, "\n".join(lines) + "\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def render_ledger(tasks_dir: Path, out_dir: Path, root: Path, generated_at: str) -> dict[str, Any]:
    """Validate, then write the index. An invalid ledger writes nothing."""
    tasks = load_ledger(tasks_dir, root)
    state_text, queue_text = render_outputs(tasks, root, generated_at)
    atomic_write(out_dir / "state.json", state_text)
    atomic_write(out_dir / "queue.md", queue_text)
    return {"tasks": len(tasks), "index_revision": json.loads(state_text)["index_revision"]}
