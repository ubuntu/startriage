# startriage

Unified triage and housekeeping tool for Ubuntu teams, pulling Launchpad bugs, GitHub issues/PRs, Discourse topics, and proposed-migration excuses into one view (terminal, Markdown, interactive HTML, or JSON), with optional read-only AI bug analysis.

## Install & Login

```bash
snap install startriage
# or from a git checkout (add `--extra ai` for AI triage):
uv sync && uv run startriage
```

Authorize Launchpad and GitHub once:

```bash
startriage login launchpad
startriage login github
```

## Quick Start

```bash
# Daily triage for your default team (yesterday, or Fri–Sun on Monday)
startriage triage

# Triage for a different configured team
startriage -t my-team triage

# Generate an interactive HTML page (with checkboxes + Discourse export) or Markdown
# Note: in the snap, /tmp is private — write under your home directory
startriage triage --html ~/triage.html
startriage triage --markdown ~/triage.md

# Limit to specific sources or a date/range
startriage triage -s launchpad,github -i 2026-04-14:2026-04-18
startriage triage -i yesterday:

# Weekly housekeeping of team todo-tagged bugs (and subscription backlog)
startriage todo --html ~/todo.html
startriage todo --subscribed

# Inspect one or more Launchpad bugs (URL, NNNNNN, or #NNNNNN)
startriage analyze 2101234 '#2105678'

# Run the AI triage agent on a single bug, or across a full daily triage
startriage analyze --ai 2101234
startriage -t my-team analyze --ai 2101234
startriage triage --ai --html ~/triage.html
```

Run `startriage --help` or `startriage <command> --help` (`triage`, `todo`, `analyze`, `config`, `login`) for all flags, interval formats, save/compare options, and the terminal bug-flags legend.

## Configuration

`startriage` ships defaults for `ubuntu-server` in [defaults.toml](startriage/data/defaults.toml).
Override them or add your own team in your user config file:

- Snap: `~/snap/startriage/current/.config/startriage.toml`
- Otherwise: `~/.config/startriage.toml`

```toml
[general]
default_team = "my-team"                 # used when -t/--team is omitted
triage_updates = "theirs"                # theirs | ours | all
savebugs_dir = "~/startriage-history"    # auto-save & diff weekly `todo` runs
proposed_min_age = 4                     # days stuck in proposed before showing up
# lp_expire_level1_days = 60
# lp_expire_level2_days = 180

[team.my-team]
lp_team = "ubuntu-desktop"               # Launchpad team slug
lp_todo_tag = "desktop-todo"             # tag tracked by `todo`
lp_discuss_tag = "desktop-discuss"       # optional: tag for ambiguous bugs
lp_freezer_tag = "desktop-freezer"       # optional: deferred todo bucket
lp_ignore_packages = ["linux", "linux-meta"]
discourse_categories = ["desktop"]
discourse_triage_categories = ["desktop/triage"]  # show replies only
github_repos = [
    "canonical/my-repo",
    {name = "canonical/my-docs", todo_labels = ["todo"]},
]
github_todo_labels = ["desktop-todo"]
github_ignore_labels = ["triage-ignore"]
github_team = "canonical/ubuntu-desktop" # members count as "ours" for --update
proposed_migration_teams = ["ubuntu-desktop"]
```

Inspect the resolved config or persist common settings from the CLI:

```bash
startriage config show
startriage config set --default-team my-team
startriage config set --help
```

## AI Triage (experimental)

Add `--ai` to `analyze` or `triage` to run a phased, read-only triage agent over Launchpad bugs.
The agent never edits bugs or applies patches: it validates reports, searches Launchpad/Debian/upstream/other distros, checks recent uploads for regressions, analyzes source, dry-run checks candidate patches, and outputs suggested status, tags, comments, and structured actions (printed to stdout, embedded into `--html` / `--markdown` / `--format json`, or saved via `--ai-json`).
Private bugs are never sent to the model.

There is no default provider or model; pick one knowingly:

```bash
# GitHub Copilot (or set COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN)
startriage config set --ai-provider copilot --ai-model <model-id> --ai-github-token github_pat_...

# OpenRouter / OpenAI-compatible BYOK (or set OPENROUTER_API_KEY)
startriage config set --ai-provider openrouter --ai-model <model-id> --ai-openrouter-key sk-or-...
```

Model IDs are passed as-is ([Copilot models](https://docs.github.com/copilot/reference/ai-models/supported-models), [OpenRouter models](https://openrouter.ai/models)).
The agent can only use the tools enabled under `[ai.tools]` (read-only by default; `fetch_url` and `fetch_source` are off until enabled).
Inspect or toggle tools with `startriage config ai-tools` (`--list`, `--enable`, `--disable`), and see `startriage analyze --help` for `--ai-show-thinking`, `--ai-json`, and `--ai-keep-workdir`.
Set `suggest_improvements = true` under `[ai]` to also collect skill improvement suggestions in `~/.local/state/startriage/skill-feedback.jsonl`.

## Library Usage

Use `startriage` from Python without rendering (nothing prompts; authorize with `startriage login` first):

```python
from startriage.config import load_config
from startriage.source import FetchMode
from startriage.triage import build_filter, fetch

config = load_config(None)
opts = build_filter(config, team="my-team", interval="yesterday", sources="launchpad,github")
results = await fetch(config, opts, FetchMode.triage)

for source, result in results.items():
    data = await result.to_dict()  # same payload as `--format json`
```
