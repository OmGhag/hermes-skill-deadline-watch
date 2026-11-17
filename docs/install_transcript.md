# deadline-watch: install proof on a fresh Hermes

* Date (UTC): 2026-11-17T05:50Z
* Hermes: Hermes Agent v0.21.5 (2026.9.24)
* Tap repository: https://github.com/OmGhag/hermes-skill-deadline-watch
* **Pinned commit (GitHub HEAD = local HEAD): `3962981ba738ae048278f67201ff5abff69d77c1`** (root tree `168e2ee5ceec6e62b0e88371afb86fbf465e6f1e`)
* HERMES_HOME: a new empty temp directory (shown below as `<fresh HERMES_HOME>`); the model call used provider `openai`, model `gpt-5.4-mini`, key from the environment (not recorded)

## 1. Add the tap

```
$ hermes skills tap add OmGhag/hermes-skill-deadline-watch
Added tap: OmGhag/hermes-skill-deadline-watch

```

## 2. Install the skill from the tap (Hermes scans it first; community trust, no --force needed)

```
$ hermes skills install OmGhag/hermes-skill-deadline-watch/skills/deadline-watch --yes

Fetching: OmGhag/hermes-skill-deadline-watch/skills/deadline-watch
Quarantined to .hub/quarantine/deadline-watch
Running security scan...
Scan: deadline-watch (skills-sh/OmGhag/hermes-skill-deadline-watch/skills/deadline-watch/community)  Verdict: SAFE
  MEDIUM   execution      scripts/deadline_watch.py:439  "return subprocess.run(command, capture_output=True, text=Tru"

Decision: ALLOWED — Allowed (community source, safe verdict)
Scan provenance: fresh; scanner skills-guard-v6; hash sha256:b460f796b7c5c6a1c0dc21db0c767ca3f0129f387935f2ec762624e71af99030
Source: https://github.com/OmGhag/hermes-skill-deadline-watch/tree/3962981ba738ae048278f67201ff5abff69d77c1/skills/deadline-watch; scanned 2026-11-17T05:50:50.800393+00:00; rules:
python_subprocess
╭─────────────────────────────────────────────────────────────────────────────── Upstream Metadata ───────────────────────────────────────────────────────────────────────────────╮
│ Repo: https://github.com/OmGhag/hermes-skill-deadline-watch                                                                                                                     │
│ Detail Page: https://skills.sh/OmGhag/hermes-skill-deadline-watch/skills/deadline-watch                                                                                         │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Installed: deadline-watch
Files: SKILL.md, examples/deadlines.example.ics, examples/deadlines.example.md, examples/deadlines.example.yaml, references/output.schema.json, scripts/deadline_watch.py

```

## 3. The skill is installed and enabled

```
$ hermes skills list
                       Installed Skills                        
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┓
┃ Name           ┃ Category ┃ Source    ┃ Trust     ┃ Status  ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━┩
│ deadline-watch │          │ skills.sh │ community │ enabled │
└────────────────┴──────────┴───────────┴───────────┴─────────┘
1 hub-installed, 0 builtin, 0 local — 1 enabled, 0 disabled

```

## 4. Install record: which revision was installed

```
$ python3 - <fresh HERMES_HOME> 168e2ee5ceec6e62b0e88371afb86fbf465e6f1e 3962981ba738ae048278f67201ff5abff69d77c1
identifier      : skills-sh/OmGhag/hermes-skill-deadline-watch/skills/deadline-watch
trust / verdict : community / safe
install path    : deadline-watch
source revision : 3962981ba738ae048278f67201ff5abff69d77c1 (what Hermes recorded when it fetched the default branch)
pinned commit   : 3962981ba738ae048278f67201ff5abff69d77c1 (git rev-parse HEAD)
root tree       : 168e2ee5ceec6e62b0e88371afb86fbf465e6f1e (git rev-parse HEAD^{tree})
MATCH (source revision = the pinned COMMIT sha)
```

## 5. Ground truth: the installed script run directly on the demo files

```
$ python3 <fresh HERMES_HOME>/skills/deadline-watch/scripts/deadline_watch.py ~/deadline-watch-demo/deadlines.md --repo ~/deadline-watch-demo/a3 --now 2026-11-16T09:00:00-08:00 --tz America/Los_Angeles
deadline-watch Mon Nov 16 09:00 PST: window 7 days, urgent within 3 days.
OVERDUE Overdue by 1 day: Lab 4 (late) (Sat Nov 14). Repo ~/demo/lab4 was not found.
URGENT Due in 2 days: Assignment 3 (Wed Nov 18). Repo ~/deadline-watch-demo/a3: 3 uncommitted files, 1 commit not pushed, last commit under an hour ago. -> commit and push before the deadline.
Due in 3 days: Reading response 5 (Thu Nov 19). No repo.
Due in 4 days: Project proposal (Fri Nov 20). Repo demo-proposal was not found.
(1 later deadline not shown.)
```

## 6. One chat request, answered with the installed skill

```
$ timeout 300 hermes chat -Q -t terminal,skills --provider openai -m gpt-5.4-mini -q Use the deadline-watch skill. My deadlines file is ~/deadline-watch-demo/deadlines.md and the repository is ~/deadline-watch-demo/a3. Pretend the date is 2026-11-16T09:00:00-08:00 and the time zone is America/Los_Angeles (use the script's --now and --tz options). Run the skill and tell me what is urgent and what I must do about the repository.
  ⚠ tirith security scanner enabled but not available — command scanning will use pattern matching only

session_id: 20261116_215053_460c11
URGENT:
Assignment 3 is due in 2 days, and ~/deadline-watch-demo/a3 has 3 uncommitted files and 1 commit not pushed. You must commit and push before Wed Nov 18.

OVERDUE:
Lab 4 (late) was due Sat Nov 14; its repo path ~/demo/lab4 was not found.

Also due soon:
Reading response 5 due Thu Nov 19, no repo.
Project proposal due Fri Nov 20; repo demo-proposal was not found.
```

