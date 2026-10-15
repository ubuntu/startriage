---
name: analyze-source
description: Find the root cause of the bug in the package's source code.
tools: [source_symbols, source_file, source_list, pkg_changelog, source_diff, fetch_source, grep_source, read_source_file, lp_get_bug, lp_get_attachment, debian_bug, fetch_patch]
required: [source_file]
submit: submit_analysis
max_calls: 20
---
# Phase: analyze the source

The bug is valid, and the earlier phases found no fix for it (Launchpad,
Debian, upstream and other distributions were searched). Find the code that
causes it, in the Ubuntu source of the package at fault.

## Input

`package`, the bug facts, the series table, the assessment of existing reports
and the search findings (if any), and the regression check (if it ran and
found nothing).

## Workflow

1. **Locate the code.** Start from what the report gives: an error message, a
   function in a backtrace, a file, an option.
   - A function name: `source_symbols` gives the files defining it (from
     Debian's index, which may only cover old versions: trust the path, not
     the line).
   - A message or option: `source_file` on a likely file with a `pattern`, or
     `source_list` to find the file. If you have `fetch_source`, fetching the
     package once and searching it with `grep_source` is faster than guessing
     files.
2. **Read it at the affected series:** `source_file` with `ref: <series>`
   (the series' newest upload) and a `pattern` or `offset`. Follow the code
   path until you understand why the reported behaviour happens.
3. **When did it change?** `pkg_changelog` with a `pattern` (the file, the
   function) and `source_diff` between two versions tell whether an upload
   introduced or touched it.
4. **Judge your confidence:**
   - `high`: you read the faulty code and it explains every symptom.
   - `medium`: the code explains the symptom, but you could not check a
     detail (a configuration, another package).
   - `low`: a plausible area of the code, nothing more.
   - `none`: you could not find the code involved.

If a step needs a tool you do not have, skip it and say so in `summary`.

## Don't

- Don't search the trackers again; that was done.
- Don't write a fix: the next phase does that, if your confidence allows it.
- Don't report a location you have not read with a tool. The host checks that
  each path exists at its `ref` and that the line is within the file.

## Result

Call `submit_analysis` with `root_cause` (the mechanism, or what is still
unknown), `confidence`, the `locations` you read (`path` in the source
package, `line`, the `ref` you read it at, a `note` on what is wrong there),
a `summary` of what you checked, and `upstream_suggestion` (e.g. "report it
upstream with the reproducer") when upstream should hear about it.
