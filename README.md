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

# Specify a team explicitly
startriage -t ubuntu-server triage

# Triage for a specific day or range
startriage triage -i monday          # last Monday's triage window
startriage triage -i 2026-04-14
startriage triage -i 2026-04-14:2026-04-18

# Housekeeping: server-todo tagged bugs with assignees
startriage todo

# Subscription backlog (ubuntu-server subscribed bugs, oldest/newest 20)
startriage todo --subscribed --limit 20
```

## Common Options

| Option | Description |
|--------|-------------|
| `-t TEAM` | Select a configured team |
| `-i DATE[:DATE]` | Include changes from this day or range (YYYY-MM-DD, day name, or `yesterday`) |
| `--source SOURCE` | Restrict to one source: `launchpad`/`bugs`, `discourse`/`forum`, `github`/`docs` |
| `--update {theirs,ours,all}` | Filter bugs by who last updated them |
| `--flag-recent DAYS` | Mark bugs updated within N days with `U` flag |
| `--flag-old DAYS` | Mark bugs inactive for more than N days with `O` flag |
| `-o` / `--open` | Open results in the web browser |
| `--fullurls` | Print full URLs instead of terminal hyperlinks |
| `--markdown PATH` | Write parallel markdown output (for pasting into Discourse posts) |

Run `startriage triage --help` for the full option reference, including the bug flags legend.

## Configuration

Config file: `~/.config/startriage.toml`

```toml
[general]
lp_triage_updates = "theirs"   # theirs | ours | all
lp_extended = false            # show date/priority/assignee columns by default
savebugs_dir = "~/your-path-to-savebugs"
default_team = "ubuntu-server"

[team.ubuntu-server]
lp_team = "ubuntu-server"
lp_todo_tag = "server-todo"
lp_ignore_packages = ["linux", "linux-meta"]
discourse_categories = ["project/server"]
discourse_triage_categories = ["project/server/server-triage"]  # suppress triage-post main entries; show replies only
github_repos = ["canonical/ubuntu-server-documentation"]
```

Persist common settings without editing the file by hand:

```bash
startriage config show
startriage config set --default-team ubuntu-server
startriage -t ubuntu-server config set --discourse-category Server
```

## Save / Compare Bug Lists

```bash
# Save today's todo list
startriage todo -S ~/savebugs/todo-$(date -I).yaml

# Compare against a previous save to spot new and closed bugs
startriage todo -C ~/savebugs/todo-2026-04-01.yaml

# Auto-save and auto-compare (uses the most recent file in savebugs_dir)
startriage todo
```

## Discourse Backlog

Print a single Discourse post in backlog format:

```bash
startriage forum backlog 12345
```

## Forum-Only Triage

```bash
startriage triage --source forum
```
