# startriage

Unified triage tool for Ubuntu bugs (Launchpad), documentation (GitHub), and forum posts (Discourse).

## Installation

```bash
snap install startriage
# or clone this git repo, then run with:
uv run startriage
```

## Quick Start

```bash
# Daily triage (yesterday's activity, or Friday–Sunday if today is Monday)
startriage triage
# Generate a markdown report as template for discourse alongside
startriage triage --markdown /tmp/summary.md

# Specify a team explicitly
startriage -t ubuntu-server triage

# Use a subset of sources
startriage -s discourse,github triage

# Changes on a specific day or range
startriage triage -i monday
startriage triage -i yesterday:  # changes since yesterday
startriage triage -i 2026-04-14
startriage triage -i 2026-04-14:2026-04-18  # range is inclusive

# Housekeeping: server-todo tagged bugs with assignees
startriage todo

# Subscription backlog (ubuntu-server subscribed bugs)
startriage todo --subscribed
```

## Common Options

| Option | Description |
|--------|-------------|
| `-t --team TEAM` | Select a configured team |
| `-i --interval DATE[:DATE]` | Include changes from this day or range (YYYY-MM-DD, day name, or `yesterday`) |
| `-s --source SOURCE` | Restrict to one source: `launchpad`, `discourse`, `github` |
| `--update {theirs,ours,all}` | Filter bugs by who last updated them |
| `--flag-recent DAYS` | Mark bugs updated within N days with `U` flag |
| `--flag-old DAYS` | Mark bugs inactive for more than N days with `O` flag |
| `--open -o` | Open results in the web browser |
| `--fullurls` | Print full URLs instead of terminal hyperlinks |
| `--markdown PATH` | Write parallel markdown output (for pasting into Discourse posts) |

Run `startriage triage --help` for the full option reference, including the bug flags legend.

## Configuration

adjust [the defaults](startriage/data/defaults.toml) with your user configuration file:

- for the snap: `~/snap/startriage/current/.config/startriage.toml`
- otherwise: `~/.config/startriage.toml`


```toml
[general]
lp_triage_updates = "theirs"   # theirs | ours | all
default_team = "ubuntu-server"
savebugs_dir = "~/your-path-to-persisting-bug-progress"

[team.ubuntu-server]
lp_team = "ubuntu-server"
lp_todo_tag = "server-todo"
lp_ignore_packages = ["linux", "linux-meta"]
discourse_categories = ["project/server"]
discourse_triage_categories = ["project/server/server-triage"]  # suppress triage-post main entries; show replies only
github_repos = ["canonical/ubuntu-server-documentation"]
proposed_migration_teams = ["ubuntu-server"]

# other team definitions
```

Persist common settings without editing the file by hand:

```bash
startriage config show
startriage config set --default-team ubuntu-server
```

## Save / Compare Bug Lists

```bash
# Auto-save and auto-compare (uses the most recent file in startriage.toml general.savebugs_dir)
startriage todo

# specify directory to save/compare todo-$(date) files with current bugs (override for general.savebugs_dir)
startriage todo --save-bugs-dir ~/savebugs

# Save today's todo list
startriage todo -S ~/savebugs/todo-$(date -I).yaml

# Compare against a previous save to spot new and closed bugs
startriage todo -C ~/savebugs/todo-2026-04-01.yaml
```
