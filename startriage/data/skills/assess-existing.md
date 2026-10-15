---
name: assess-existing
description: Judge what existing reports and fixes say about this bug.
tools: [lp_get_bug, lp_search_bugs, debian_bug, debian_bugs, pkg_versions, pkg_changelog, source_diff, fetch_patch, verify_url]
submit: submit_assessment
---
# Phase: assess existing reports and fixes

The host and earlier phases searched for this bug. Judge what they found: is it
already reported, known, or fixed?

## Input

- `facts`: the bug facts.
- `candidates`: Launchpad (`source: launchpad`) and Debian (`source: debian`)
  bugs whose titles matched the search terms, most hits first. A Debian
  candidate has `fixed_version` when Debian fixed it; `in_series` lists the
  Ubuntu series whose current version is at least that (or, for a Launchpad
  candidate, whose changelog closes it).
- `findings`: upstream and other-distribution reports and fixes.
- `changelog_hits`: uploads whose changelog closes this bug or a candidate
  (`LP: #N`), per series: the first version in that series with the fix.
- `series`: the series table.

## Workflow

1. Read the promising candidates (`lp_get_bug`, `debian_bug`). Titles alone are
   not enough.
2. **Duplicate?** Same symptom, same cause and same package, on Launchpad. Prefer
   the older or better-reported bug. Set `duplicate_of` to its number.
3. **Known issue?** Related but not the same, or the same but unfixed (a Debian
   or upstream report included): add it to `known_issues`, noting whether it has
   a reproducer and whether someone works on it.
4. **Fixed in which series?** For each affected series, say if it is fixed and
   the evidence. Good evidence: a changelog hit for this bug, a Debian fix whose
   `in_series` includes the series, a Fix Released task for that series. Check
   with `pkg_changelog` (`pattern`: the bug number, CVE or function) when in
   doubt. A related fix is not proof.
5. **SRU candidates:** a stable series that is affected while a newer series is
   fixed. Give the series and the reason only; the triager decides feasibility.
6. **Fix candidates:** patches, commits or merge requests from the candidates
   and findings. Say whether each is merged or only proposed. Read a patch with
   `fetch_patch` before relying on it.
7. You may search again (`lp_search_bugs` with `include_closed`, `debian_bugs`
   with other words) if the candidates miss an obvious angle.

## Don't

- Don't call a bug a duplicate because the titles look alike.
- Don't mark a series fixed without evidence.

## Result

Call `submit_assessment` with `summary` and whatever applies of `duplicate_of`,
`known_issues`, `series`, `sru_candidates` and `fix_candidates`.
