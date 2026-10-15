---
name: search-upstream
description: Search the upstream project's tracker and history for this bug and its fix.
tools: [upstream_info, github_search, gitlab_search, bugzilla_search, fetch_patch, verify_url, fetch_url]
required: ["github_search|gitlab_search|bugzilla_search"]
submit: submit_upstream_search
max_calls: 15
---
# Phase: search upstream

The bug is valid. Find out whether the upstream project knows it, and whether
it has a fix. Only search; another phase judges the results.

## Input

The bug facts, the package's upstream links as its packaging gives them
(`upstream`: homepage, repository, bug database, and `forges`, the trackers the
search tools can query), and the current version in each Ubuntu series.

## Workflow

1. **Pick the tracker.** Use `upstream.forges` first (`github owner/repo` →
   `github_search`, `gitlab host project` → `gitlab_search`). A Bugzilla bug
   database (Samba, Mozilla, kernel) → `bugzilla_search`. If the packaging gives
   none but you know for sure where upstream lives (e.g. nginx is `nginx/nginx`
   on GitHub), use that. If there is no searchable tracker, submit at once with
   no findings and say so.
2. **Search** with the verbatim error message first, then 2-4 distinctive
   words of the symptom. On GitHub, also search merged pull requests
   (`is:pr is:merged ...`) and commits (`kind: commits`). Three to six searches
   are usually enough.
3. **Read the fix.** For a promising fix, read it with `fetch_patch` and check
   it touches the code in question. Note the upstream release that has it when
   the tracker says so.
4. Keep findings that are the same problem, or a fix for it. Drop the rest.

## Don't

- Don't report a pull request as merged unless the tool says so.
- Don't cite an issue or commit a tool did not return.
- Don't keep searching once a merged fix is found.

## Result

Call `submit_upstream_search` with `searched` (the queries you ran),
`findings` (`source: upstream`; `claims_fix` if it fixes this bug, `merged` if
that fix is merged or released, `fixed_version`, `has_reproducer`,
`activity`) and a one-sentence `summary`.
