"""Rebuild the example tasks in this folder from scratch, using only the ct CLI.

    python3 examples/build_examples.py

All names, threads, and dates are made up.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from canonical_tasks.cli import main as ct  # noqa: E402


def run(*argv: str) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = ct(["--root", str(HERE), *argv, "--write"])
    if code != 0:
        raise SystemExit(f"ct {' '.join(argv)} failed")
    result = json.loads(out.getvalue())
    print(result.get("summary") or f"created {result['task_id']}: {result['title']}")
    return result


def main() -> None:
    for generated in ("tasks", ".ct"):
        shutil.rmtree(HERE / generated, ignore_errors=True)

    contract = run("new", "--title", "Reply to Alex about the revised contract", "--area", "work",
                   "--outcome", "Alex has the revised contract, or knows when to expect it.",
                   "--why", "Alex asked for the revised contract on Tuesday.",
                   "--source-key", "mail:18f2c7a1", "--now", "2026-09-28T09:05:00-07:00", "--session", "demo-1")["task_id"]
    fair = run("new", "--title", "Decide on the job fair", "--area", "career",
               "--outcome", "A decision: go or don't.", "--why", "An invitation to a job fair on Wednesday.",
               "--source-key", "mail:2b9e04d7", "--now", "2026-09-28T22:23:48-07:00", "--session", "demo-2")["task_id"]
    car = run("new", "--title", "Book the car service", "--area", "home",
              "--outcome", "A service appointment on the calendar.", "--due", "2026-10-03",
              "--now", "2026-09-29T08:00:00-07:00", "--session", "demo-3")["task_id"]

    # Waiting on someone else, with a date to check back.
    run("wait", contract, "--outcome", "Sent a draft; Alex is checking with legal.",
        "--review-after", "2026-10-02", "--now", "2026-09-28T16:40:00-07:00", "--session", "demo-4")
    # Closed on the owner's word, kept verbatim.
    run("done", fair, "--outcome", "Not attending.", "--attest", "not going. close it",
        "--now", "2026-09-28T22:24:06-07:00", "--session", "demo-5")
    # Parked: off the list without a date, and its due date cleared.
    run("park", car, "--outcome", "Not this month.", "--now", "2026-09-30T08:00:00-07:00", "--session", "demo-6")
    # A claimed completion with no evidence: held, the task stays waiting.
    run("done", contract, "--outcome", "I think Alex has it by now.",
        "--now", "2026-09-30T09:00:00-07:00", "--session", "demo-7")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ct(["--root", str(HERE), "render", "--now", "2026-10-02T06:00:00-07:00"])
    print("rendered .ct/state.json and .ct/queue.md")


if __name__ == "__main__":
    main()
