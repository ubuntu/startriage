---
name: search-other-distros
description: Look for this bug's fix in Fedora, Arch and Gentoo.
tools: [bugzilla_search, gitlab_search, fetch_patch, verify_url]
required: ["bugzilla_search|gitlab_search"]
submit: submit_distro_search
---
# Phase: search other distributions

The bug is valid, and the upstream search found no fix. Other distributions may
have hit it and carry a patch. Only search; another phase judges the results.

## Input

The bug facts and the upstream search summary.

## Workflow

Search in this order, and stop at the first fix you find:

1. **Fedora:** `bugzilla_search` on `redhat`, e.g. `product:Fedora PACKAGE <words>`.
2. **Arch:** `gitlab_search` on `gitlab.archlinux.org`, project
   `archlinux/packaging/packages/PACKAGE`.
3. **Gentoo:** `bugzilla_search` on `gentoo`, e.g. `PACKAGE <words>`.

Use the Arch or Gentoo package name, which can differ from Ubuntu's. Use the
verbatim error message or 2-4 distinctive words. Keep only reports that point
to a fix (a patch, a commit, an upstream release): open reports without a fix
add nothing to the Launchpad bug. Read a patch with `fetch_patch` before
calling it a fix.

## Don't

- Don't cite a bug or patch a tool did not return.
- Don't keep searching once a fix is found.

## Result

Call `submit_distro_search` with `searched` (the queries you ran), `findings`
(`source`: fedora, arch or gentoo; `claims_fix`, `merged`, `fixed_version`) and
a one-sentence `summary`.
