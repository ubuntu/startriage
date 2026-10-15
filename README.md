# startriage

Unified triage tool for Ubuntu

- Launchpad
- GitHub
- Discourse
- Proposed migration

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

# Generate an interactive HTML report
# that allows filling a discourse post markdown template
# (remember /tmp/ is private when running as snap)
startriage triage --html ~/triage.html

# Generate a markdown report as template for discourse alongside
startriage triage --markdown ~/triage-summary.md

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
# Generate an interactive web page as for a housekeeping meeting
startriage todo --html ~/todo.html

# Subscription backlog (ubuntu-server subscribed bugs)
startriage todo --subscribed
```

## Triage Options

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
| `--html PATH` | Write a single-page html report whose rows can be checked off |

Run `startriage triage --help` for the full option reference, including the bug flags legend.

## AI Triage (experimental)

Inspect one or more Launchpad bugs, optionally running an AI agent over them. By
default `analyze` prints the bug metadata (status, tags, affected targets,
description, comments) so you can eyeball it — no provider or credentials
required. Add `--ai` to run the agent, which produces a suggested status, tags,
analysis, and (where applicable) a proposed fix. The agent never edits bugs and
never applies patches — it only prints its analysis.

`--ai` needs a provider, a model and credentials, see
[Configuring the AI backend](#configuring-the-ai-backend).

The agent has no shell and none of the model's built-in tools. It can only call
the tools startriage provides, all read-only except `fetch_source`, which runs
`pull-lp-source` (off by default):

| Tool | Default | What it does |
| --- | --- | --- |
| `lp_get_bug` | on | Read a public Launchpad bug (metadata, description, comments) |
| `lp_get_attachment` | on | Read or grep a text attachment of a public bug |
| `lp_search_bugs` | on | Search public bugs of a source package (duplicates, related bugs) |
| `pkg_versions` | on | Versions of a source package per pocket in each supported series |
| `pkg_changelog` | on | Read or grep the `debian/changelog` of an Ubuntu or Debian package version |
| `source_file` | on | Read one file of a package's source from git-ubuntu (no download) |
| `source_list` | on | List a directory of a package's source in git-ubuntu |
| `source_symbols` | on | Find where a function or other symbol is defined (Debian's code search index) |
| `source_diff` | on | Diff two versions of a source package (git-ubuntu) |
| `upstream_info` | on | Find a package's upstream homepage, repository and bug tracker |
| `patch_applies` | on | Check whether a patch applies to a package, per series (like `patch --dry-run`) |
| `debian_bugs` | on | List the Debian bugs of a source package, filtered by subject words |
| `debian_bug` | on | Read a Debian bug (status and messages) |
| `github_search` | on | Search a GitHub repository's issues, pull requests or commits |
| `gitlab_search` | on | Search a GitLab project's issues or merge requests (salsa, freedesktop, ...) |
| `bugzilla_search` | on | Search a Bugzilla (Red Hat/Fedora, Gentoo, Samba, Mozilla, kernel) |
| `fetch_patch` | on | Read the patch behind a commit, pull/merge request or patch URL |
| `verify_url` | on | Check that a URL exists before citing it |
| `fetch_url` | off | Read a public web page (upstream trackers, mailing lists) |
| `fetch_source` | off | Download and unpack a source package with `pull-lp-source` (runs a program) |
| `grep_source` | off | Search the sources fetched by `fetch_source` (only with `fetch_source`) |
| `read_source_file` | off | Read a file of the sources fetched by `fetch_source` (only with `fetch_source`) |

A phase whose tools are all disabled is skipped (e.g. disabling the three
search tools skips the upstream and other-distro searches). `github_search`
uses your GitHub token (`GH_TOKEN`/`GITHUB_TOKEN`, or `general.github_token`)
when there is one, for a higher rate limit.

The first `--ai` run on a terminal asks which tools to enable and saves the
answer under `[ai.tools]` in your config. Change it later with
`startriage config ai-tools` (interactive), `--list`, or
`--enable fetch_url --disable verify_url`. Tools only reach public addresses and
never write anywhere, except `fetch_source`, which unpacks into a scratch
directory under `~/.cache/startriage/ai` that is removed at the end of the run
(keep it with `--ai-keep-workdir`).

```bash
# Show metadata for one or more specific bugs (URL, NNNNNN, or #NNNNNN)
startriage analyze 2101234 '#2105678'

# Run the AI agent over those bugs
startriage analyze --ai 2101234 '#2105678'

# Run the normal daily triage, then AI-triage every bug found
startriage triage --ai
```

The AI output is printed after the normal triage results. With `--markdown FILE`
the AI section is folded into that same report file (behind a clear "review
critically" notice) so you get a single cohesive document.

Triage runs in phases, each a short session with its own instructions (the
skills in [startriage/data/skills](startriage/data/skills)). startriage first
routes every bug without any session:

- **private** bugs are never sent to the model;
- bugs where, in the triage window, only the team acted and nobody commented are
  skipped;
- bugs the team already handled (triaged, assigned, commented on before) only get
  their new activity reviewed;
- other bugs are validated (bug, feature request, support request, process
  ticket, ...). Support requests, incomplete or invalid reports stop there with a
  suggested comment; real bugs go on to a Launchpad search for existing reports,
  searches upstream and in other distributions, an assessment of what is already
  known or fixed, a regression check against recent uploads, an analysis of the
  source if no fix is known, a proposed fix, and a final report.

startriage checks what the model submits: cited URLs must resolve, code
locations must exist, and a proposed patch is applied (dry run) to the affected
series and the development release; the report lists how it applied. The
`bitesize` tag is only accepted for a fix that applies without fuzz everywhere.

The report shows what each phase did and the tokens it used. Set
`suggest_improvements = true` under `[ai]` to also ask the model what to improve
in the skills; its feedback is listed in the report and appended to
`~/.local/state/startriage/skill-feedback.jsonl`.

### Configuring the AI backend

There is no default provider or model, so `--ai` fails until you choose both:

| `ai.provider` | Backend | Credential |
| --- | --- | --- |
| `copilot` | GitHub Copilot, via the bundled Copilot CLI | GitHub token with Copilot enabled |
| `openrouter` | Any OpenAI-compatible endpoint (BYOK), defaulting to OpenRouter | OpenRouter API key |

`ai.model` is passed to the provider as is: look up the id in
[Copilot's supported models](https://docs.github.com/en/copilot/reference/ai-models/supported-models)
or on [openrouter.ai/models](https://openrouter.ai/models).

Credentials are written to the 0600 config file and never echoed:

```bash
# GitHub Copilot (needs a Copilot-enabled account)
startriage config set --ai-provider copilot \
    --ai-model <copilot-model-id> \
    --ai-github-token github_pat_...

# Or bring your own key via an OpenAI-compatible provider (e.g. OpenRouter)
startriage config set --ai-provider openrouter \
    --ai-model <openrouter-model-id> \
    --ai-openrouter-key sk-or-...
```

The Copilot token may also come from `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` /
`GITHUB_TOKEN`, and the OpenRouter key from `OPENROUTER_API_KEY`; the config file
wins over the environment.

The snap bundles the Copilot runtime and `ubuntu-dev-tools`, so source analysis
works inside strict confinement; from a git checkout install the extra with
`uv sync --extra ai`.

## Configuration

adjust [the defaults](startriage/data/defaults.toml) with your user configuration file:

- for the snap: `~/snap/startriage/current/.config/startriage.toml`
- otherwise: `~/.config/startriage.toml`


```toml
[general]
triage_updates = "theirs"   # theirs | ours | all
default_team = "ubuntu-server"
savebugs_dir = "~/your-path-to-persisting-bug-progress"

[team.ubuntu-server]
lp_team = "ubuntu-server"
lp_todo_tag = "server-todo"
lp_ignore_packages = ["linux", "linux-meta"]
discourse_categories = ["project/server"]
discourse_triage_categories = ["project/server/server-triage"]  # suppress triage-post main entries; show replies only
github_repos = ["canonical/ubuntu-server-documentation"]
# members of this GitHub team count as the team: other people's comments are "external updates"
github_team = "canonical/ubuntu-server-distro"
proposed_migration_teams = ["ubuntu-server"]

# other team definitions
```

Persist common settings without editing the file by hand:

```bash
startriage config show
startriage config set --default-team ubuntu-server
```

Authorize access once:

```bash
startriage login launchpad
startriage login github
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

## Library Usage

Fetch structured results without rendering, e.g. for a web backend:

```python
from startriage.config import load_config
from startriage.source import FetchMode
from startriage.triage import build_filter, fetch

config = load_config(None)  # or construct a StarTriageConfig directly
opts = build_filter(config, interval="yesterday", sources="launchpad,github")
results = await fetch(config, opts, FetchMode.triage)  # or FetchMode.todo

for source, result in results.items():
    data = await result.to_dict()  # JSON-ready with json_default; {"error": ...} if the fetch failed
```

The objects in `results` (e.g. `LaunchpadTriage.tasks`) are available as well.
`startriage triage --format json` prints the same `to_dict()` data.

Nothing prompts: without stored credentials (`startriage login launchpad`, path in `general.lp_credentials_file`), the Launchpad result carries a `LaunchpadAuthError`.
