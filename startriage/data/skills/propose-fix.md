---
name: propose-fix
description: Propose a fix for the bug and check that it applies to Ubuntu.
tools: [patch_applies, fetch_patch, source_file, verify_url]
required: [patch_applies]
submit: submit_fix
max_calls: 12
---
# Phase: propose a fix

The cause of the bug is known: a fix was found (upstream, another
distribution), an update was confirmed to have caused it, or the source
analysis found the faulty code. Propose the fix an Ubuntu developer should
apply, and check that it applies to the package.

## Input

`package`, the bug facts, `check_refs` (the series the host will check the fix
against), and what is known: the fix candidates and findings that claim a fix,
the regression (with its hunk), and the source analysis.

## Workflow

1. **An existing fix comes first.** For each candidate (best first): read it
   with `fetch_patch`, decide whether it fixes this bug, and check it with
   `patch_applies` (`url`, `ref`: the `check_refs`). A merged upstream fix
   that applies (clean, offset or fuzz) is the best answer.
2. **A regression:** the fix is to revert or correct the hunk the regression
   check found, as a diff against the package (usually a
   `debian/patches/` change).
3. **No existing fix:** write a minimal unified diff against the package's
   source with its patches applied, from the source analysis. Paths as in the
   package (`a/src/file.c`, `b/src/file.c`), with three lines of context read
   with `source_file` from the patched tree (`ref: applied/ubuntu/devel` or
   `applied/ubuntu/<series>-devel`). Check it with `patch_applies` (`patch`),
   and fix it until it applies.
4. **If nothing convincing,** `kind: none`; say why in `summary`.

## Don't

- Don't propose a fix you have not checked with `patch_applies`. The host
  checks a diff again on submit and rejects one that applies nowhere.
- Don't page through whole files: `source_file` with a `pattern` gives the
  lines you need. You have about 12 tool calls.
- Don't propose a fix that does not address the root cause, however clean it
  applies.
- Don't cite a URL you have not verified with a tool.

## Result

Call `submit_fix` with `proposed_fix` (`kind: reference` with the fix's URL,
`kind: diff` with the diff, or `kind: none`), `unmerged` (true when the fix
is not merged upstream yet), a `summary` of what you checked and how it
applies, the verified `references`, and `upstream_suggestion` (e.g. "send the
diff upstream", "review MR !45") when relevant.
