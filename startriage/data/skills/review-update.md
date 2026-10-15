---
name: review-update
description: Decide whether new activity on a bug the team already handled needs action.
tools: [lp_get_bug, pkg_versions, pkg_changelog]
submit: submit_update_review
---
# Phase: review an update

The team already handled this bug (see `precheck.reasons`). Something happened
since. Decide whether someone needs to act.

## Input

The precheck: status, assignees, tags, the series table, the events in the
triage window, the team's last comment, and the new comments. Read the full bug
with `lp_get_bug` only if the new comments are not enough.

## Workflow

1. Read the new comments and events against the team's last comment. To check
   a claim that a fix was released, search the series' changelog
   (`pkg_changelog`, `pattern` the bug number).
2. Decide the action:
   - `none`: nothing new for a human. "Me too" without details, bot noise
     (janitor, apport retracing), subscriptions, tag shuffles.
   - `comment`: a reply would move the bug forward, e.g. the reporter asked a
     question, or an Incomplete bug is about to expire and still lacks the asked
     information.
   - `change`: the state is now wrong, e.g. the reporter answered an Incomplete
     bug (back to Confirmed/Triaged), an SRU verification is done
     (`verification-done-<series>`), or a fix was released.
   - `escalate`: the new information changes the story (a different symptom, a
     new reproducer, another series affected); the bug must be triaged again.
3. Give a one or two sentence `reason`.

## Don't

- Don't redo the triage here; escalate instead.
- Don't comment just to acknowledge.

## Examples

- Incomplete for a month; the reporter now attached the requested logs →
  `change`, status Confirmed, reason "the logs asked for in comment #3 are attached".
- Triaged; a new user writes "same here on noble" → `none`.
- Triaged as a config problem; a new comment shows the crash with the default
  configuration → `escalate`.

## Result

Call `submit_update_review` with `action`, `reason`, and `comment` / `status` /
`tags` when the action needs them.
