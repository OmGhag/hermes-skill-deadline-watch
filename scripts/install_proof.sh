#!/usr/bin/env bash
# =============================================================================
# install_proof.sh - the install proof for the deadline-watch skill, on a FRESH Hermes home.
#
# Usage:  bash scripts/install_proof.sh OWNER [--dry-run]
#         OWNER = the GitHub account that owns the PUBLIC repo OWNER/hermes-skill-deadline-watch (push it first; this script checks that GitHub has your commit).
# Needs:  hermes on PATH (or HERMES_BIN=/path/to/hermes), git, python3, network access to github.com, and OPENAI_API_KEY in YOUR environment for the one chat request
#         (read from the environment only; it is never printed or written; PROOF_PROVIDER / PROOF_MODEL override the provider and model, default openai / gpt-5.4-mini).
#
# WHAT it does, in a throw-away HERMES_HOME (a new temp directory: your real Hermes home is never touched):
#   1. hermes skills tap add OWNER/hermes-skill-deadline-watch      (a tap = a GitHub repo with skills/<name>/SKILL.md)
#   2. hermes skills install OWNER/hermes-skill-deadline-watch/skills/deadline-watch --yes
#   3. shows the install record (lock file: identifier, trust, scan verdict, source revision) and the commit it must match
#   4. builds a demo git repo under ~/deadline-watch-demo (3 uncommitted files, 1 commit not pushed) and a deadlines file from the shipped example
#   5. ONE chat request: "use the deadline-watch skill on that file and repo", answered by the installed skill
#   6. writes docs/install_transcript.md (home directory shown as ~, ANSI colours removed, refuses to write if a key-like string is in the text)
# It makes no change to anything outside the temp HERMES_HOME, ~/deadline-watch-demo and docs/install_transcript.md. --dry-run builds the demo repo and PRINTS the
# commands without running any hermes command (no network, no key needed).
# =============================================================================
set -euo pipefail
OWNER="${1:-}"; DRY=0
[ "${2:-}" = "--dry-run" ] && DRY=1
[ -n "$OWNER" ] && [ "$OWNER" != "--dry-run" ] || { echo "usage: bash scripts/install_proof.sh OWNER [--dry-run]" >&2; exit 2; }
REPO_NAME="hermes-skill-deadline-watch"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES="${HERMES_BIN:-hermes}"
PROVIDER="${PROOF_PROVIDER:-openai}"; MODEL="${PROOF_MODEL:-gpt-5.4-mini}"
IDENT="$OWNER/$REPO_NAME/skills/deadline-watch"
DEMO="$HOME/deadline-watch-demo"
NOW_ISO="2026-11-16T09:00:00-08:00"
TRANSCRIPT="$HERE/docs/install_transcript.md"
export NO_COLOR=1 TERM=dumb COLUMNS=110 GIT_AUTHOR_NAME=Demo GIT_AUTHOR_EMAIL=demo@example.invalid GIT_COMMITTER_NAME=Demo GIT_COMMITTER_EMAIL=demo@example.invalid

# ---- 4. the demo repo (SYNTHETIC): a clone with 1 commit not pushed and 3 uncommitted files -------------------------------------------------------------------
if [ -e "$DEMO" ] && [ ! -f "$DEMO/.deadline-watch-demo" ]; then echo "ERROR: $DEMO exists and is not ours; move it away" >&2; exit 1; fi
rm -rf "$DEMO"; mkdir -p "$DEMO"; touch "$DEMO/.deadline-watch-demo"
git init -q --bare -b main "$DEMO/remote.git"
git init -q -b main "$DEMO/a3"
git -C "$DEMO/a3" config commit.gpgsign false
echo "work" >"$DEMO/a3/notes.txt"; git -C "$DEMO/a3" add notes.txt; git -C "$DEMO/a3" commit -q -m "first"
git -C "$DEMO/a3" remote add origin "$DEMO/remote.git"; git -C "$DEMO/a3" push -q -u origin main
echo "more" >>"$DEMO/a3/notes.txt"; git -C "$DEMO/a3" commit -q -am "second (not pushed)"
for n in 1 2 3; do echo "draft $n" >"$DEMO/a3/draft$n.txt"; done
sed "s#~/demo/a3#$DEMO/a3#" "$HERE/skills/deadline-watch/examples/deadlines.example.md" >"$DEMO/deadlines.md"

PROMPT="Use the deadline-watch skill. My deadlines file is $DEMO/deadlines.md and the repository is $DEMO/a3. Pretend the date is $NOW_ISO and the time zone is America/Los_Angeles (use the script's --now and --tz options). Run the skill and tell me what is urgent and what I must do about the repository."
CMDS=(
  "$HERMES skills tap add $OWNER/$REPO_NAME"
  "$HERMES skills tap list"
  "$HERMES skills install $IDENT --yes"
  "$HERMES skills list"
  "$HERMES chat -Q -t terminal,skills --provider $PROVIDER -m $MODEL -q \"<prompt below>\""
)
if [ "$DRY" = 1 ]; then
  echo "(dry run) demo repo built at $DEMO; nothing else was run. With a fresh HERMES_HOME=\$(mktemp -d) the proof runs:"
  printf '  %s\n' "${CMDS[@]}"
  echo "prompt: $PROMPT"
  exit 0
fi

# ---- preconditions ----------------------------------------------------------------------------------------------------------------------------------------------
command -v "$HERMES" >/dev/null || { echo "ERROR: hermes not found (set HERMES_BIN)" >&2; exit 1; }
[ -n "${OPENAI_API_KEY:-}" ] || [ "$PROVIDER" != openai ] || { echo "ERROR: OPENAI_API_KEY is not set in this shell (needed for the one chat request)" >&2; exit 1; }
LOCAL_HEAD="$(git -C "$HERE" rev-parse HEAD)"; LOCAL_TREE="$(git -C "$HERE" rev-parse 'HEAD^{tree}')"
REMOTE_HEAD="$(git ls-remote "https://github.com/$OWNER/$REPO_NAME.git" HEAD | cut -f1)"
[ -n "$REMOTE_HEAD" ] || { echo "ERROR: https://github.com/$OWNER/$REPO_NAME is not reachable (is it public, and pushed?)" >&2; exit 1; }
[ "$REMOTE_HEAD" = "$LOCAL_HEAD" ] || { echo "ERROR: GitHub's HEAD ($REMOTE_HEAD) is not your local HEAD ($LOCAL_HEAD): push (or pull) first, so the proof is about the code you committed" >&2; exit 1; }

FRESH="$(mktemp -d)"; export HERMES_HOME="$FRESH"
trap 'rm -rf "$FRESH"' EXIT
OUT="$(mktemp)"; trap 'rm -rf "$FRESH" "$OUT"' EXIT
{
  echo "# deadline-watch: install proof on a fresh Hermes"
  echo
  echo "* Date (UTC): $(date -u +%Y-%m-%dT%H:%MZ)"
  echo "* Hermes: $("$HERMES" --version 2>&1 | head -1)"
  echo "* Tap repository: https://github.com/$OWNER/$REPO_NAME"
  echo "* **Pinned commit (GitHub HEAD = local HEAD): \`$LOCAL_HEAD\`** (root tree \`$LOCAL_TREE\`)"
  echo "* HERMES_HOME: a new empty temp directory (shown below as \`<fresh HERMES_HOME>\`); the model call used provider \`$PROVIDER\`, model \`$MODEL\`, key from the environment (not recorded)"
  echo
} >"$OUT"
step() {   # step "title" command...   -> runs it, appends the command and its output to the transcript; a failing step stops the proof (set -e) after being recorded
  local title="$1"; shift
  { echo "## $title"; echo; echo '```'; echo "\$ ${*//$HOME/\~}"; } >>"$OUT"
  local rc=0; "$@" >>"$OUT" 2>&1 || rc=$?
  { echo '```'; echo; } >>"$OUT"
  [ "$rc" = 0 ] || { echo "ERROR: step '$title' failed (exit $rc); the output so far is in $OUT" >&2; cp "$OUT" "$HERE/docs/install_transcript.FAILED.txt"; exit "$rc"; }
}
step "1. Add the tap" "$HERMES" skills tap add "$OWNER/$REPO_NAME"
step "2. Install the skill from the tap (Hermes scans it first; community trust, no --force needed)" "$HERMES" skills install "$IDENT" --yes
step "3. The skill is installed and enabled" "$HERMES" skills list
step "4. Install record: which revision was installed" python3 - "$FRESH" "$LOCAL_TREE" "$LOCAL_HEAD" <<'PY'
import json, sys
home, tree, commit = sys.argv[1:4]
entry = json.load(open(f"{home}/skills/.hub/lock.json"))["installed"]["deadline-watch"]
meta = entry.get("metadata") or {}
print("identifier      :", entry["identifier"])
print("trust / verdict :", entry["trust_level"], "/", entry["scan_verdict"])
print("install path    :", entry["install_path"])
print("source revision :", meta.get("source_revision"), "(GitHub's root tree of the default branch when it was fetched)")
print("expected tree   :", tree, "(git rev-parse HEAD^{tree} of the commit below)")
print("pinned commit   :", commit)
print("MATCH" if meta.get("source_revision") == tree else "MISMATCH: the default branch moved after the commit above")
PY
step "5. Ground truth: the installed script run directly on the demo files" python3 "$FRESH/skills/deadline-watch/scripts/deadline_watch.py" "$DEMO/deadlines.md" --repo "$DEMO/a3" --now "$NOW_ISO" --tz America/Los_Angeles
step "6. One chat request, answered with the installed skill" timeout 300 "$HERMES" chat -Q -t terminal,skills --provider "$PROVIDER" -m "$MODEL" -q "$PROMPT"
# ---- sanitise and write ---------------------------------------------------------------------------------------------------------------------------------------
sed -e 's/\x1b\[[0-9;?]*[A-Za-z]//g' -e "s#$FRESH#<fresh HERMES_HOME>#g" -e "s#$HOME#~#g" "$OUT" >"$OUT.clean"
if grep -E -q 'sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|github_pat_|AKIA[0-9A-Z]{12,}|BEGIN [A-Z ]*PRIVATE KEY' "$OUT.clean"; then
  echo "ERROR: the transcript contains something that looks like a key; NOT written. Inspect $OUT.clean by hand." >&2; exit 1
fi
mv "$OUT.clean" "$TRANSCRIPT"; rm -rf "$DEMO"
echo "wrote $TRANSCRIPT (pinned commit $LOCAL_HEAD); review it, then commit it and put the hash in the A3 README"
