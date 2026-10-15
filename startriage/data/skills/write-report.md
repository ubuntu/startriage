---
name: write-report
description: Turn the phase results into the final recommendation for the triager.
tools: []
submit: submit_report
---
# Phase: write the report

Earlier phases validated the bug, searched for existing reports and fixes
(Launchpad, Debian, upstream, other distributions), and may have checked for a
regression in a recent update, analysed the source and proposed a fix. Turn
their results into one recommendation for the person running the triage. You
have no tools: use only the input.

## Workflow

1. **Status:**
   - Duplicate, with `duplicate_of`, if the assessment found one.
   - Fix Released if fixed in every affected series.
   - Triaged if the cause or a fix is known; Confirmed if real but not analysed.
   - Wishlist importance and Triaged/Confirmed for a sensible feature request.
   - no-change when the results disagree or are too weak.
2. **Per-series status** in `series`, only for series in the series table, e.g.
   `{"stonking": "Fix Released", "noble": "Triaged"}`.
3. **Importance** (see the core rules) and **tags** (`server-todo` for a known
   fix or high priority, `server-triage-discuss` when in doubt). Add
   `regression-update` only if `regression.confirmed` is true and the suspect
   update was in -updates or -security, and `bitesize` only if
   `bitesize_verified` is true; the host rejects them otherwise.
4. **analysis:** markdown for the triager: the bug in two or three sentences,
   then what was found (related Launchpad, Debian and upstream bugs, fixes, the
   regression and its hunk, the root cause and its code locations) with links
   from the input. Mark unmerged fixes as such, and say how the proposed fix
   applies (`fix_applies`: per series, as the host checked it).
5. **suggested_comment:** required for Incomplete and Invalid; otherwise only if
   a comment would help the reporter (e.g. a workaround).
6. Copy `references` and `sru_candidates` from the input; do not add new ones.
   The host adds the proposed fix itself. Set `upstream_suggestion` when
   upstream should hear about it.

## Result

Call `submit_report`.
