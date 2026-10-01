# Canonical Tasks

A reference implementation of tasks that people and agents can both rely on: one durable record of a commitment, with a permanent ID, a clear state, pointers to where it came from, and a rule for how it closes.

The write-up is at [michaeldchang.com/projects/canonical-tasks](https://michaeldchang.com/projects/canonical-tasks/). This repo is the core of the system described there, extracted from the version I run every day.

**This is a reference, not a product.** It's small, has no dependencies beyond the Python standard library, and is meant to be read and borrowed from. I'm not maintaining it as a package.

## The idea

AI tools are good at reading your email, messages, and calendar and suggesting what to do. They're bad at remembering what you decided. A suggestion gets rewritten every morning, the same work shows up from three places, and an agent's "done" closes nothing anyone can check.

Canonical Tasks keeps three rules:

1. **AI suggests, a person commits.** A suggestion becomes a task only when someone creates it. Nothing in this repo turns a model's output into a task on its own.
2. **One record, many surfaces.** Every task has a permanent ID and keys back to its sources, so an email, a plan item, and an agent session about the same work point to the same task.
3. **Done needs proof.** A task closes only with structured evidence (`verified`) or the owner's own words, kept verbatim (`owner-attested`). A claimed completion without either is held, and the task stays open.

## Quick start

Requires Python 3.9+. No install needed:

```sh
python3 -m canonical_tasks new --title "Reply to Alex" --area work \
    --outcome "Alex has the revised contract." --source-key mail:18f2c7a1 --write

python3 -m canonical_tasks wait  tsk-... --outcome "Alex is checking with legal." --review-after 2026-10-02 --write
python3 -m canonical_tasks done  tsk-... --outcome "Sent." --evidence "mail:18f2c7a1=reply sent in thread" --write
python3 -m canonical_tasks done  tsk-... --outcome "Not going." --attest "not going. close it" --write

python3 -m canonical_tasks validate   # check every task file; one bad file fails the whole ledger
python3 -m canonical_tasks render     # write .ct/state.json and .ct/queue.md
```

Changes to tasks are dry runs unless you pass `--write`. `render` always writes, since its output is a disposable view. Or `pip install -e .` for a `ct` command.

[`examples/`](examples/) has three tasks built with these commands. Run `python3 examples/build_examples.py` to rebuild them.

## The task file

One Markdown file per task, at `tasks/tsk-<ULID>.md`. Frontmatter is strict top-level `key: value`, with lists and objects as inline JSON (also valid YAML), so it parses without a YAML library and reads fine in any editor.

```markdown
---
type: task
id: tsk-01M3WQBP0HJGQVF9YZKXNKDPTJ
title: "Decide on the job fair"
status: done
created_at: 2026-09-28T22:23:48-07:00
updated_at: 2026-09-28T22:24:06-07:00
due: null
review_after: null
area: career
source_refs: [{"kind":"owner-request","id":"demo-2"},{"kind":"mail","id":"2b9e04d7","source_key":"mail:2b9e04d7"}]
origin: {"type":"manual","created_at":"2026-09-28T22:23:48-07:00","session_id":"demo-2"}
completion_check: {"kind":"owner-declared","expected_outcome":"A decision: go or don't."}
---

## Why now

An invitation to a job fair on Wednesday.

## Desired outcome

A decision: go or don't.

## Event log

| At | Event | From | To | Detail |
|---|---|---|---|---|
| 2026-09-28T22:23:48-07:00 | created | — | active | Created directly (session `demo-2`) |
| 2026-09-28T22:24:06-07:00 | reconciled | active | done | Not attending. (owner-attested: "not going. close it") ... |
```

| Field | Meaning |
|---|---|
| `id` | `tsk-` plus a ULID. Permanent; the filename must match. |
| `status` | `proposed`, `active`, `waiting`, `scheduled`, `parked`, `done`, `dismissed` |
| `due` | The date the work is owed. Only an explicit change moves it. Parking always clears it, and refuses a new one. |
| `review_after` | When a `waiting` or `scheduled` task comes back into view. |
| `source_refs` | Where the task came from. At least one; any `path` must resolve inside the root, symlinks included. |
| `origin` | How the task was created (`manual` needs an `area`; `signal` needs both keys below). |
| `signal_key` | Optional. The obligation: this thread, this kind of action, e.g. `mail:18f2c7a1:reply`. |
| `occurrence_key` | Optional. This particular turn of it, so a new reply is distinct from one already handled. |
| `completion_check` | What "done" means for this task. |

The event log only grows. Every change appends one row; nothing is rewritten.

## States

| From | Allowed to |
|---|---|
| `proposed` | `active`, `scheduled`, `parked`, `dismissed` |
| `active` | `done`, `waiting`, `scheduled`, `parked`, `dismissed` |
| `waiting`, `scheduled`, `parked` | `active`, `done`, `dismissed` |
| `done`, `dismissed` | nothing; closed tasks never reopen |

A new turn of a closed obligation gets a new task (a successor), never a reopened one.

## How a change is applied

Every verb (`done`, `wait`, `park`, `schedule`, `dismiss`, `reactivate`, `reschedule`, `note`) is spelled as one **claim**: task ID, requested disposition, declared outcome, and how the outcome is known. [`actions.py`](canonical_tasks/actions.py) then:

1. Validates the whole ledger first. One corrupt file blocks every change.
2. Checks the transition table and the closing rule. `done` without evidence or attestation is held. Evidence fields must be non-empty text; nothing is coerced.
3. Builds every changed task file in memory and validates the result.
4. Only then writes: first one record per task to `tasks/_log.jsonl`, then one event row to each task file, replaced atomically and only if it's unchanged since it was read.

If any step refuses, nothing is written. If the disk fails after writing has started, the command says so (`ERROR`, not `REFUSED`) and names the session; re-running with that `--session` applies whatever the log holds that a task file doesn't. The log keeps one record per task per session: re-running the same claim is a no-op, and a claim that differs in any way (outcome, evidence, dates, reason) under the same session ID is refused rather than overwriting history.

The code checks that a close *carries* evidence. It can't check that the evidence says what an agent claims. That part is on the person, and it's quick because evidence is a pointer.

Validation checks each file's shape and the rules across files (unique IDs, one open task per occurrence). It doesn't replay the event log to prove the current status was reached legally. The files are meant to be editable by hand, and a hand edit is trusted the way a commit is. The rules above bind the tools, not the owner.

## Identity and duplicates

[`ledger.py`](canonical_tasks/ledger.py) turns a source observation into keys (`normalize_signal`) and decides what an incoming signal means for an existing task (`decide_signal`):

| Incoming signal vs. existing task | Result |
|---|---|
| Same occurrence, task still open | `update-evidence` |
| Same occurrence, task closed | `suppress`: already handled, don't resurface it |
| Same obligation, new occurrence, task closed | `create-successor` |
| No stable source ID | `unkeyed-proposal`: never merged automatically |

Two open tasks for the same occurrence are refused. For two items with the same title from different sources, `collision_warning` gives a planner something to show a person; nothing here merges on a title.

## What's not here

The parts of my system that depend on my own setup: the scripts that turn my calendar, email, and texts into state files; the morning plan that reads them; the nightly integrity check; and the tooling I used to migrate older checklists into tasks. The page above describes how they fit together.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

Includes [`tests/test_adversarial.py`](tests/test_adversarial.py): the same input twice, renames that must keep identity, two sources with one title, completions without proof, illegal and reopening transitions, and eight kinds of corrupt task file. Each refusal is checked to leave the files on disk unchanged. [`tests/test_review_fixes.py`](tests/test_review_fixes.py) covers the cases a pre-release review found: null evidence, refusals after preparation, retries, malformed fields, and symlinks.

## License

MIT. See [LICENSE](LICENSE).
