# deadline-watch: a Hermes skill

**What is due soon, and is the work for it safe in git?** `deadline-watch` reads a deadlines file you keep (a Markdown table, a small YAML list, or an `.ics` calendar), picks out what is due in the next days, and for each deadline that names a git repository checks - **read-only** - for uncommitted files, commits that were never pushed, a missing upstream, and how old the last commit is.

```
deadline-watch Mon Nov 16 09:00 PST: window 7 days, urgent within 3 days.
OVERDUE Overdue by 1 day: Lab 4 (late) (Sat Nov 14). Repo ~/demo/lab4 was not found.
URGENT Due in 2 days: Assignment 3 (Wed Nov 18). Repo ~/a3: 3 uncommitted files, 1 commit not pushed, last commit under an hour ago. -> commit and push before the deadline.
Due in 3 days: Reading response 5 (Thu Nov 19). No repo.
Due in 4 days: Project proposal (Fri Nov 20). Repo ~/demo/proposal: clean and pushed, last commit 1d ago.
(1 later deadline not shown.)
```

This is a real tool, not a prompt: a Python script (standard library only, Python 3.9+) does the date maths and the git checks, and `SKILL.md` tells a Hermes agent when to run it and how to report the result. It also runs on its own, from the shell or from cron.

## Install it in Hermes (from this repository as a skill "tap")

A tap is a GitHub repository that holds `skills/<name>/SKILL.md`. These are the commands of Hermes Agent v0.21.5 (verified against the installed version's `hermes skills --help`):

```bash
hermes skills tap add OWNER/hermes-skill-deadline-watch                       # add this repo as a skill source (path defaults to skills/)
hermes skills install OWNER/hermes-skill-deadline-watch/skills/deadline-watch  # fetch, security-scan, install (add --yes to skip the prompt)
hermes skills list                                                              # deadline-watch: source github, trust community, enabled
```

(`hermes skills search deadline-watch` also finds it once the tap is added.) The skill lands in `$HERMES_HOME/skills/deadline-watch/` and Hermes records what it installed in `$HERMES_HOME/skills/.hub/lock.json`. Hermes' own scanner gives it the verdict **safe** (one informational note: it runs `git` through `subprocess`), so `--force` is not needed. Update later with `hermes skills update`; remove with `hermes skills uninstall deadline-watch`.

A proof of an install on a fresh Hermes home, with the pinned commit, is in [`docs/install_transcript.md`](docs/install_transcript.md); `scripts/install_proof.sh OWNER` regenerates it.

## Use it

Ask the agent: *"What's due this week? My deadlines are in ~/deadlines.md."* or run the script yourself:

```bash
python3 skills/deadline-watch/scripts/deadline_watch.py ~/deadlines.md                       # text summary
python3 skills/deadline-watch/scripts/deadline_watch.py ~/deadlines.md --repo ~/a3 --json    # machine-readable
```

| Option | Meaning |
|---|---|
| `DEADLINES_FILE` | `.md` table, `.yaml`/`.yml` list or `.ics` (sniffed if the extension says nothing) |
| `--repo PATH` | a repo to check (repeatable). A deadline's `repo:` may be a path or the folder name of a `--repo` |
| `--root DIR` | refuse to check repos outside this folder |
| `--days 7` / `--urgent-days 3` / `--past-days 7` | window, urgent threshold, how far back an overdue deadline is still shown |
| `--tz America/Los_Angeles` | zone for times written without one (default: this machine's zone) |
| `--now ISO` | pretend it is this moment (tests, demos) |
| `--json` | one JSON document, schema in [`references/output.schema.json`](skills/deadline-watch/references/output.schema.json) |

Exit status: `0` it ran (even if work is due), `2` the deadlines file or an option is unusable (the message names the file and line; with `--json` the error is `{"schema": ..., "error": ...}`).

### The deadlines file

Markdown (any column order; names `title|name|deadline|task`, `due|date|when`, `repo|path|repository`):

```markdown
| Title        | Due              | Repo   |
|--------------|------------------|--------|
| Assignment 3 | 2026-11-18 23:59 | ~/a3   |
| Essay        | 2026-11-19       |        |
```

YAML (a small subset: a list of mappings, block or `{flow}` style, `#` comments):

```yaml
deadlines:
  - title: Assignment 3
    due: 2026-11-18 23:59
    repo: ~/a3
```

`.ics`: each `VEVENT` (`DTSTART`) or `VTODO` (`DUE`) is a deadline, `SUMMARY` its title; the repo is an `X-DEADLINE-REPO:` property or a `repo: <path>` line in `DESCRIPTION`. Recurring events are not expanded.

Dates: `2026-11-18` (= the END of that day), `2026-11-18 17:00`, `2026-11-18T17:00:00-08:00`, `...Z`. A time without a zone is read in `--tz`. A bad line is an error with its line number, never a silently skipped deadline. Synthetic examples are in [`skills/deadline-watch/examples/`](skills/deadline-watch/examples/).

### JSON for cron

```json
{"schema": "deadline-watch/1", "items": [{"title": "Assignment 3", "status": "urgent", "days_left": 2, "actions": ["commit", "push"], "needs_action": true,
  "git": {"state": "ok", "ahead": 1, "behind": 0, "uncommitted": {"total": 3, "staged": 0, "modified": 0, "untracked": 3}}, "summary": "..."}]}
```

`status` is `urgent` (inside `--urgent-days`), `soon` (inside `--days`) or `overdue`; `actions` are `commit`, `push`, `set_upstream`, `pull`, `first_commit`, `checkout_branch`. A Hermes cron job can run the script with `--json` and speak only when some item has `needs_action`.

## Security notes

* **Read-only git.** Every git call goes through one function that allows only `rev-parse`, `status`, `rev-list` and `log` (a test asserts nothing else can run), with `--no-optional-locks` (so `git status` does not even refresh the index) and `core.fsmonitor=false` (a repository's own config cannot make `git status` run a command; a test plants such a hook and checks it never runs). It never commits, adds, fetches, pulls, pushes, checks out or edits.
* **No network.** No sockets, no HTTP, no `git fetch`. "Not pushed" and "behind" are measured against the remote-tracking branch as of your last fetch; run `git fetch` yourself if that matters.
* **No secrets.** It reads no environment secrets and git runs with a minimal environment (`PATH`, `HOME`, locale). It prints no tokens. Paths are shown with your home directory as `~`.
* **Paths are only what you pass.** The only files it opens are the deadlines file and the repositories named in it or with `--repo`; `--root DIR` confines them further. Reads are bounded (1 MB file, 500 deadlines, 15 s per git call, 5000 status lines).
* **Text in a deadlines file is data.** The skill file tells the agent never to treat it as instructions.
* Hermes scans every installed skill; this one scans as **safe** (the only note is the `subprocess` use that runs git).

## Develop

```bash
python3 -m pytest          # 90+ offline tests: window maths and time zones, three file formats, malformed files, git states in temp repos, read-only guarantees, JSON schema
```

Tests need only `pytest` and `git`; a few extra checks run Hermes' own scanner when Hermes is installed and skip otherwise. Licence: MIT, see [`LICENSE`](LICENSE).
