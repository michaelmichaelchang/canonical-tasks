"""ct: the command-line tool for Canonical Tasks.

    ct new        --title "..." --area work --outcome "desired outcome" [--state active] [--due D]
    ct done       <task-id> --outcome "..." (--attest "my words" | --evidence REF=ASSERTION ...)
    ct wait       <task-id> --outcome "..." --review-after YYYY-MM-DD
    ct schedule   <task-id> --outcome "..." --review-after YYYY-MM-DD
    ct park       <task-id> --outcome "..." [--review-after YYYY-MM-DD]
    ct dismiss    <task-id> --outcome "..."
    ct reactivate <task-id> --outcome "..."
    ct reschedule <task-id> --due YYYY-MM-DD|null --outcome "..."
    ct note       <task-id> --outcome "..."      # progress, no state change
    ct link       <task-id> --source-key KIND:ID --outcome "why"   # attach a source, no state change
    ct validate                                  # check every task file
    ct render                                    # write .ct/state.json and .ct/queue.md (always writes)
    ct new-id                                    # print a fresh task ID

Changes to tasks are dry runs unless you pass --write. render always writes its
generated files, which are disposable views. Changes don't re-render them, so
run render after writing if something reads .ct/state.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import uuid
from pathlib import Path

from . import actions
from .ledger import LedgerError, load_ledger, new_task_id, parse_timestamp, render_ledger

VERBS = sorted(actions.VERB_TO_DISPOSITION)


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ct", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=VERBS + ["new", "link", "validate", "render", "new-id"])
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--root", type=Path, default=Path("."), help="folder that holds tasks/ (default: .)")
    parser.add_argument("--title")
    parser.add_argument("--area", help="short lowercase slug, like work or home")
    parser.add_argument("--state", default="active", help="new: active|waiting|scheduled|parked")
    parser.add_argument("--why", help="new: why this task exists now")
    parser.add_argument("--source-key", action="append", help="new/link: kind:id of a source thread; repeatable")
    parser.add_argument("--outcome", help="what happened, or for new, the desired outcome")
    parser.add_argument("--attest", help="your own words; makes a close owner-attested")
    parser.add_argument("--evidence", action="append", help="REF=ASSERTION or REF@REVISION=ASSERTION; repeatable; makes a close verified")
    parser.add_argument("--review-after", help="YYYY-MM-DD; when a waiting or scheduled task comes back")
    parser.add_argument("--due", help="YYYY-MM-DD, or null with reschedule")
    parser.add_argument("--reason", help="a short controlled reason, for your own later review")
    parser.add_argument("--session", help="session id; one change per task per session (default: unique per run)")
    parser.add_argument("--now", help="override the timestamp (ISO-8601 with offset)")
    parser.add_argument("--write", action="store_true", help="apply a task change instead of printing a dry run")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    now = args.now or _now()
    session = args.session or f"cli-{uuid.uuid4().hex[:12]}"
    try:
        if args.now:
            parse_timestamp(args.now, "--now", Path("<argument>"))
        if args.command == "new-id":
            print(new_task_id())
            return 0
        if args.command == "validate":
            tasks = load_ledger(root / actions.TASKS_DIR, root)
            print(json.dumps({"valid": True, "tasks": len(tasks)}, indent=2))
            return 0
        if args.command == "render":
            result = render_ledger(root / actions.TASKS_DIR, root / ".ct", root, now)
            print(json.dumps(result, indent=2))
            return 0
        if not args.outcome:
            parser.error(f"{args.command} requires --outcome")
        if args.command == "new":
            result = actions.create_task(
                root, title=args.title, area=args.area, state=args.state, outcome=args.outcome,
                now=now, session_id=session, write=args.write, due=args.due,
                review_after=args.review_after, source_keys=args.source_key, why_now=args.why,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if not args.task_id:
            parser.error(f"{args.command} needs a task id")
        if args.command == "link":
            result = actions.link_sources(root, args.task_id, source_keys=args.source_key,
                                          outcome=args.outcome, now=now, session_id=session, write=args.write)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command in ("wait", "schedule") and not args.review_after:
            parser.error(f"{args.command} requires --review-after")
        if args.command == "reschedule" and args.due is None:
            parser.error("reschedule requires --due YYYY-MM-DD or --due null")
        claim = actions.claim_for_verb(
            root, args.command, args.task_id, outcome=args.outcome, now=now, attest=args.attest,
            evidence=args.evidence, review_after=args.review_after, due=args.due, reason=args.reason,
        )
        result = actions.run_claims(root, [claim], session_id=session, now=now, write=args.write)
        record = result["records"][0]
        summary = f"{record['task_id']}: {record['from']} → {record['proposed_to']} ({record['verification']})"
        if record.get("due_change"):
            summary += f" · due {record['due_change']['from'] or '—'} → {record['due_change']['to'] or '—'}"
        if record.get("verification_pending"):
            summary += " · completion held for evidence"
        if not args.write:
            summary += " · dry run, nothing written"
        result["summary"] = summary
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except actions.PartialWrite as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 4
    except (OSError, ValueError, LedgerError, actions.Refusal) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
