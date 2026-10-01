# Agent Notes for startriage

## Project Overview
`startriage` is a unified triage and planning tool for Ubuntu bugs, GitHub pull requests/issues, Discourse forum posts, and proposed migration tracking.
It is written in Python 3.12+ and uses asyncio for concurrent fetching.
It is designed to be used by all Ubuntu development teams, adjustable to their needs by customization.

## Development Environment
- **Python**: >=3.12
- **Package Manager**: `uv`
- **Dependencies**: managed in `pyproject.toml`

### Running the Tool
For development, run through `uv run`:

```bash
uv run startriage ...
```

### Running Tests
- Libraries: `pytest`, `pytest-cov`, `pytest-asyncio`

```bash
uv run pytest
```

### Releasing
- Push a `vX.Y.Z` tag: `.github/workflows/release.yaml` runs CI, builds and publishes to PyPI via trusted publishing (GitHub environment `pypi`)
- The version comes from the tag (setuptools-scm), never edit it by hand

## Code Style
- Lint with `ty`, `ruff`, `ruff format` (line-length 110, target Python 3.12)
- Import sorting enabled (`I001`)

## Libraries and Design
- use asyncio, aiohttp, ...
- keep it simple
- design for future extensibility and adjustment
- no overengineering or weird object orientation

## Key Architecture
- `startriage/config.py` — Pydantic-based TOML config loading
- `startriage/cli.py` — argparse CLI with subcommands (`triage`, `todo`, `config`)
- `startriage/triage.py` — library API: `build_filter()`, `fetch()`; no rendering
- `startriage/report.py` — renders `triage`/`todo` reports (terminal, markdown, json) on top of `fetch()`
- `startriage/sources/` — per-source packages: `github/`, `launchpad/`, `discourse/`, `proposed/`
- Each source has a `finder.py` (fetch data) and `triage.py` (render output)
- The common data structure for triage results is `startriage/output.py/TriageResult`
- Library entry: `triage.build_filter()` + `triage.fetch()` return `{source: TriageResult}` without rendering; `await result.to_dict()` gives structured data (also `--format json`)
- Only `startriage login {launchpad,github}` prompts; sources never do and fail without credentials

## Design
- `triage` mode
  - the idea is to find work and asses it to move it into `todo` mode
  - usually run every day to see changes on the previous day(s)
  - filters tasks by watched projects and recent activity
  - does not consider "todo" tags
- `todo` mode
  - the idea is to keep track of actionable tasks found in `triage` mode - and to assign/work on them
  - usually run once a week for housekeeping
  - filters tasks tags found during triage by the team's "todo" tags that were set if actionable
  - supports saving and comparing against previous runs to spot new and closed tasks
