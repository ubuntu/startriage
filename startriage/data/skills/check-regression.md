---
name: check-regression
description: Decide whether a recent Ubuntu update caused the bug.
tools: [source_diff, pkg_changelog, source_file, pkg_versions]
required: [source_diff]
submit: submit_regression
max_calls: 10
---
# Phase: check for a regression

The bug may have been caused by a recent update of the package. The host lists
the suspects: versions published in the affected series shortly before the bug
was filed, each with the version before it. Decide whether one of them broke
what the report describes.

## Input

The bug facts and `suspects` (`series`, `version`, `pockets`, `published`,
`previous`).

## Workflow

1. Read what the update meant to change: `pkg_changelog` with
   `since: <previous>` and `ref: <version>`.
2. Read the diff: `source_diff` from `previous` to `version`. Narrow it with
   `path_glob` (e.g. `debian/patches/*`) when it is large.
3. Look for a change that explains the symptom: the code path in the error, a
   changed default, a removed file, a dependency change. Read the surrounding
   code with `source_file` (ref `applied/<version>`) if needed.
4. **Confirm only with evidence:** a hunk of that diff that plausibly causes
   the symptom. Timing alone ("it started after the update") is not proof.

## Don't

- Don't confirm a regression because the update touched the same package or
  file in general.
- Don't paraphrase the hunk: copy its lines verbatim, with their `+`/`-`
  prefixes. The host checks them against the diff.

## Result

Call `submit_regression` with `confirmed`, a `summary` of what you compared,
and, when confirmed, `suspect_version`, `previous_version` and the `hunk`.
