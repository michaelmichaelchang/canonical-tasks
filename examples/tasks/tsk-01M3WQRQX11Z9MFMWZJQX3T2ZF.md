---
type: task
id: tsk-01M3WQRQX11Z9MFMWZJQX3T2ZF
title: "Book the car service"
status: parked
created_at: 2026-09-29T08:00:00-07:00
updated_at: 2026-09-30T08:00:00-07:00
due: null
review_after: null
area: home
source_refs: [{"kind":"owner-request","id":"demo-3"}]
origin: {"type":"manual","created_at":"2026-09-29T08:00:00-07:00","session_id":"demo-3"}
completion_check: {"kind":"owner-declared","expected_outcome":"A service appointment on the calendar."}
---

## Why now

Created directly by the owner.

## Desired outcome

A service appointment on the calendar.

## Event log

| At | Event | From | To | Detail |
|---|---|---|---|---|
| 2026-09-29T08:00:00-07:00 | created | — | active | Created directly (session `demo-3`) |
| 2026-09-30T08:00:00-07:00 | reconciled | active | parked | due 2026-10-03 → — · Not this month. (record `rec-a740a5ff29abc39e`, verification not-applicable) |
