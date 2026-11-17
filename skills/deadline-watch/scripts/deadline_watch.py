#!/usr/bin/env python3
"""deadline-watch: what is due soon, and does the related git repo have work that is not committed or not pushed?

WHAT: reads a deadlines file (a Markdown table, a small YAML list, or an .ics calendar), keeps the deadlines that are due within a window (default 7 days) or
      recently overdue, and for every deadline that names a git repository runs READ-ONLY git checks: uncommitted files, commits not pushed (ahead of the
      upstream), commits not pulled (behind), no upstream, and the age of the last commit. It prints a short human summary, or ``--json`` for machines
      (a Hermes cron job, a script).
WHY:  the classic last-night failure is "it works on my laptop": the work is finished but not committed or not pushed when the deadline passes. A calendar
      cannot know that; git can. This joins the two.
HOW:  * Python standard library only (3.9+); the YAML reader is a deliberately small subset (see ``parse_yaml_subset``) so no package has to be installed.
      * Git is called ONLY through ``run_git``, which allows a fixed set of read-only sub-commands (``READ_ONLY_GIT``), turns off the two ways ``git status``
        can run a command or write (``core.fsmonitor``, optional index locks) and never contacts a remote: "behind" and "ahead" are measured against the
        remote-tracking ref as of the LAST fetch (run ``git fetch`` yourself if you want it fresh).
      * No network, no environment secrets: git gets a minimal environment; the only paths touched are the deadlines file and the repositories named in it
        or with ``--repo`` (optionally confined to ``--root``).
      * Time: every deadline is an aware datetime. A date without a time means the END of that day; a time without a zone is read in ``--tz`` (default: the
        machine's zone). ``--now`` fixes the clock for tests and demos.

USAGE:
    deadline_watch.py DEADLINES_FILE [--repo PATH ...] [--root DIR] [--days 7] [--urgent-days 3] [--past-days 7] [--tz Area/City] [--now ISO] [--json]
Exit status: 0 = ran (even when work is due), 2 = the deadlines file or an argument is unusable (the message says which line).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

try:  # zoneinfo is standard since 3.9; a machine without tz data (some Windows setups) can still use offsets and UTC
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment,misc]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

SCHEMA_ID = "deadline-watch/1"
MAX_FILE_BYTES = 1_000_000          # a deadlines file is a few lines; refuse anything absurd (AGENTS-style bound on every read)
MAX_DEADLINES = 500                 # ... and on the number of entries
GIT_TIMEOUT_S = 15                  # every git call is bounded
MAX_STATUS_LINES = 5000             # a repo with 100k untracked files is reported as "5000+", not walked line by line
# The ONLY git sub-commands this program may run. All of them only read. A test asserts nothing else is ever invoked.
READ_ONLY_GIT = frozenset({"rev-parse", "status", "rev-list", "log"})
TITLE_KEYS = ("title", "name", "deadline", "what", "task", "summary")
DUE_KEYS = ("due", "date", "when", "due date", "due_date", "deadline date")
REPO_KEYS = ("repo", "repository", "path", "git")


class DeadlineError(Exception):
    """The deadlines file (or an argument) cannot be used. The message names the file and, where known, the line."""


@dataclass
class Deadline:
    title: str
    due: datetime            # timezone-aware
    repo: str | None         # as written by the user ("~/a3"), None when the deadline has no repo
    source: str              # "<file>:<line>" for messages


@dataclass
class Window:
    now: datetime
    zone: tzinfo = timezone.utc     # the zone calendar days are counted in (the user's, not the one a deadline happens to be written in)
    days: float = 7.0
    urgent_days: float = 3.0
    past_days: float = 7.0


# ======================================================================================== time helpers
def local_zone() -> tzinfo:
    """The machine's time zone as an IANA zone when it can be found (TZ, /etc/timezone, the /etc/localtime link), else the current UTC offset.

    A fixed offset is a fallback only: it is right for today but not across a daylight-saving change. Pass ``--tz`` to be exact.
    """
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        try:
            name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        except OSError:
            name = ""
    if not name:
        try:
            target = os.readlink("/etc/localtime")
            name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
        except OSError:
            name = ""
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def resolve_zone(name: str | None) -> tzinfo:
    if not name:
        return local_zone()
    if name.upper() in ("UTC", "Z"):
        return timezone.utc
    if ZoneInfo is None:
        raise DeadlineError(f"time zone {name!r} needs Python 3.9+ (zoneinfo)")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise DeadlineError(f"unknown time zone {name!r} (use an IANA name such as America/Los_Angeles)") from exc


_DUE_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"(?:[T ]+(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?(?:\.\d+)?)?"
    r"\s*(?P<z>Z|[+-]\d{2}:?\d{2})?$", re.IGNORECASE)


def parse_due(text: str, zone: tzinfo, where: str) -> datetime:
    """ISO-like text -> aware datetime. ``2026-11-18`` = end of that day (23:59:59); ``2026-11-18 23:59`` and ``...T23:59:00-08:00`` also work."""
    match = _DUE_RE.match((text or "").strip())
    if not match:
        raise DeadlineError(f"{where}: cannot read the due date {text!r} (use 2026-11-18, 2026-11-18 17:00 or 2026-11-18T17:00:00-08:00)")
    try:
        day = date(int(match["y"]), int(match["mo"]), int(match["d"]))
        if match["h"] is None:
            clock = time(23, 59, 59)
        else:
            clock = time(int(match["h"]), int(match["mi"]), int(match["s"] or 0))
    except ValueError as exc:
        raise DeadlineError(f"{where}: {text!r} is not a real date or time ({exc})") from exc
    zone_text = match["z"]
    if zone_text:
        if zone_text.upper() == "Z":
            return datetime.combine(day, clock, tzinfo=timezone.utc)
        sign = 1 if zone_text[0] == "+" else -1
        digits = zone_text[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return datetime.combine(day, clock, tzinfo=timezone(sign * offset))
    return datetime.combine(day, clock, tzinfo=zone)


# ======================================================================================== deadlines file readers
def _clean_cell(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^[*_`]+|[*_`]+$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value.strip()


def _pick(row: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        if row.get(key):
            return row[key]
    return ""


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [_clean_cell(cell.replace("\\|", "|")) for cell in re.split(r"(?<!\\)\|", body)]


def parse_markdown_table(text: str, zone: tzinfo, name: str) -> list[Deadline]:
    """The first table whose header has a title-like and a due-like column. Optional repo column. Every data row must be valid (a skipped row would be a missed deadline)."""
    lines = text.splitlines()
    found: list[Deadline] = []
    index = 0
    seen_table = False
    while index < len(lines) - 1:
        header_line, rule_line = lines[index], lines[index + 1]
        if "|" in header_line and re.match(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$", rule_line):
            header = [cell.lower() for cell in _split_row(header_line)]
            title_col = next((i for i, h in enumerate(header) if h in TITLE_KEYS), None)
            due_col = next((i for i, h in enumerate(header) if h in DUE_KEYS), None)
            repo_col = next((i for i, h in enumerate(header) if h in REPO_KEYS), None)
            index += 2
            if title_col is None or due_col is None:
                continue                       # some other table in the same file
            seen_table = True
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                where = f"{name}:{index + 1}"
                cells = _split_row(lines[index])
                title = cells[title_col] if title_col < len(cells) else ""
                due = cells[due_col] if due_col < len(cells) else ""
                repo = cells[repo_col] if repo_col is not None and repo_col < len(cells) else ""
                if not title:
                    raise DeadlineError(f"{where}: the row has no title")
                found.append(Deadline(title, parse_due(due, zone, where), repo or None, where))
                index += 1
            continue
        index += 1
    if not seen_table:
        raise DeadlineError(f"{name}: no Markdown table with a title column and a due column (for example | Title | Due | Repo |)")
    return found


def _strip_comment(line: str) -> str:
    quote = ""
    for position, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (position == 0 or line[position - 1].isspace()):
            return line[:position]
    return line


def _split_flow(body: str) -> list[str]:
    parts, depth, quote, current = [], 0, "", ""
    for char in body:
        if quote:
            current += char
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote, current = char, current + char
        elif char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [p for p in parts if p.strip()]


def _key_value(text: str, where: str) -> tuple[str, str]:
    key, sep, value = text.partition(":")
    if not sep or not key.strip():
        raise DeadlineError(f"{where}: expected 'key: value', got {text.strip()!r}")
    return key.strip().lower(), _clean_cell(value)


def parse_yaml_subset(text: str, zone: tzinfo, name: str) -> list[Deadline]:
    """A small YAML subset: a list of mappings (optionally under a top-level ``deadlines:`` key), block style (``- title: X`` then indented ``due: ...``) or flow style
    (``- {title: X, due: 2026-11-18, repo: ~/a3}``); quoted or plain scalars; ``#`` comments. Anything else is an error that names the line - a deadline must never be
    silently dropped."""
    entries: list[tuple[dict[str, str], str]] = []
    current: dict[str, str] | None = None
    current_where = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        where = f"{name}:{number}"
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.rstrip(":").lower() == "deadlines" and stripped.endswith(":") and not line.startswith((" ", "-")):
            continue                            # the optional wrapper key
        if stripped.startswith("-"):
            if current is not None:
                entries.append((current, current_where))
            body = stripped[1:].strip()
            current, current_where = {}, where
            if not body:
                continue
            if body.startswith("{"):
                if not body.endswith("}"):
                    raise DeadlineError(f"{where}: unclosed '{{' in a flow mapping")
                for piece in _split_flow(body[1:-1]):
                    key, value = _key_value(piece, where)
                    current[key] = value
                continue
            key, value = _key_value(body, where)
            current[key] = value
            continue
        if current is None:
            raise DeadlineError(f"{where}: unexpected text before the first '- ' entry: {stripped!r}")
        key, value = _key_value(stripped, where)
        current[key] = value
    if current is not None:
        entries.append((current, current_where))
    if not entries:
        raise DeadlineError(f"{name}: no deadlines found (expected a list such as '- title: Assignment 3' / '  due: 2026-11-18')")
    found = []
    for row, where in entries:
        title, due = _pick(row, TITLE_KEYS), _pick(row, DUE_KEYS)
        if not title:
            raise DeadlineError(f"{where}: the entry has no title (use title: ...)")
        if not due:
            raise DeadlineError(f"{where}: the entry {title!r} has no due date (use due: 2026-11-18 or 2026-11-18 17:00)")
        found.append(Deadline(title, parse_due(due, zone, where), _pick(row, REPO_KEYS) or None, where))
    return found


def _unfold_ics(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw[:1] in (" ", "\t") and lines:
            first, previous = lines[-1]
            lines[-1] = (first, previous + raw[1:])
        else:
            lines.append((number, raw))
    return lines


def _ics_unescape(value: str) -> str:
    return value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _ics_when(params: dict[str, str], value: str, zone: tzinfo, where: str) -> datetime:
    value = value.strip()
    compact = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$", value)
    if not compact:
        raise DeadlineError(f"{where}: cannot read the calendar date {value!r}")
    year, month, day, hour, minute, second, zulu = compact.groups()
    iso = f"{year}-{month}-{day}" + (f"T{hour}:{minute}:{second or '00'}" if hour else "") + ("Z" if zulu else "")
    use_zone = zone
    if params.get("TZID"):
        use_zone = resolve_zone(params["TZID"].strip('"'))
    return parse_due(iso, use_zone, where)


def parse_ics(text: str, zone: tzinfo, name: str) -> list[Deadline]:
    """VEVENT (DTSTART) and VTODO (DUE, else DTSTART) become deadlines. SUMMARY is the title. The repo comes from an ``X-DEADLINE-REPO`` property or a
    ``repo: <path>`` line in DESCRIPTION. Recurrence rules are NOT expanded (only the first occurrence is read)."""
    found: list[Deadline] = []
    block: dict[str, tuple[dict[str, str], str, int]] | None = None
    kind = ""
    for number, line in _unfold_ics(text):
        upper = line.strip().upper()
        if upper in ("BEGIN:VEVENT", "BEGIN:VTODO"):
            block, kind = {}, upper.split(":")[1]
            continue
        if upper in ("END:VEVENT", "END:VTODO") and block is not None:
            where = f"{name}:{number}"
            title = _ics_unescape(block.get("SUMMARY", ("", "", 0))[1]).strip()
            stamp = block.get("DUE") if kind == "VTODO" and "DUE" in block else block.get("DTSTART")
            if not title:
                raise DeadlineError(f"{where}: a {kind} has no SUMMARY")
            if stamp is None:
                raise DeadlineError(f"{where}: {title!r} has no {'DUE or ' if kind == 'VTODO' else ''}DTSTART")
            repo = _ics_unescape(block.get("X-DEADLINE-REPO", ({}, "", 0))[1]).strip() or None
            if repo is None and "DESCRIPTION" in block:
                found_repo = re.search(r"(?im)^\s*repo:\s*(\S.*)$", block["DESCRIPTION"][1].replace("\\n", "\n").replace("\\N", "\n"))
                repo = _ics_unescape(found_repo.group(1)).strip() if found_repo else None
            found.append(Deadline(title, _ics_when(stamp[0], stamp[1], zone, f"{name}:{stamp[2]}"), repo, f"{name}:{stamp[2]}"))
            block = None
            continue
        if block is None or ":" not in line:
            continue
        head, _, value = line.partition(":")
        pieces = head.split(";")
        prop = pieces[0].strip().upper()
        params = {}
        for piece in pieces[1:]:
            key, _, val = piece.partition("=")
            params[key.strip().upper()] = val
        if prop in ("SUMMARY", "DTSTART", "DUE", "DESCRIPTION", "X-DEADLINE-REPO") and prop not in block:
            block[prop] = (params, value, number)
    if not found:
        raise DeadlineError(f"{name}: no VEVENT or VTODO with a date found in the calendar file")
    return found


def load_deadlines(path: Path, zone: tzinfo) -> list[Deadline]:
    """Read and parse a deadlines file; the format follows the extension (.ics, .yaml/.yml, .md/.markdown/.txt) and is sniffed otherwise."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DeadlineError(f"cannot read {path}: {exc.strerror or exc}") from exc
    if not path.is_file():
        raise DeadlineError(f"{path} is not a file")
    if size > MAX_FILE_BYTES:
        raise DeadlineError(f"{path} is {size} bytes; refusing to read more than {MAX_FILE_BYTES}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise DeadlineError(f"cannot read {path} as UTF-8 text: {exc}") from exc
    suffix = path.suffix.lower()
    if suffix == ".ics" or text.lstrip().upper().startswith("BEGIN:VCALENDAR"):
        items = parse_ics(text, zone, path.name)
    elif suffix in (".yaml", ".yml"):
        items = parse_yaml_subset(text, zone, path.name)
    elif suffix in (".md", ".markdown", ".txt") or re.search(r"^\s*\|.*\|\s*$", text, re.MULTILINE):
        items = parse_markdown_table(text, zone, path.name)
    else:
        items = parse_yaml_subset(text, zone, path.name)
    if len(items) > MAX_DEADLINES:
        raise DeadlineError(f"{path} has {len(items)} deadlines; the limit is {MAX_DEADLINES}")
    return items


# ======================================================================================== the window
def classify(due: datetime, window: Window) -> tuple[str, float]:
    """(status, seconds until due). status: overdue | urgent | soon | later | old. Negative seconds = already past."""
    # Subtract in UTC: two aware datetimes that share ONE ZoneInfo object are subtracted as wall-clock times, which is an hour off across a daylight-saving change.
    seconds = (due.astimezone(timezone.utc) - window.now.astimezone(timezone.utc)).total_seconds()
    if seconds < 0:
        return ("overdue" if -seconds <= window.past_days * 86400 else "old"), seconds
    if seconds <= window.urgent_days * 86400:
        return "urgent", seconds
    if seconds <= window.days * 86400:
        return "soon", seconds
    return "later", seconds


def calendar_days(due: datetime, window: Window) -> int:
    """Whole calendar days from today to the due day, both dates read in the user's zone (an ICS "...Z" time is still the user's evening)."""
    return (due.astimezone(window.zone).date() - window.now.astimezone(window.zone).date()).days


def when_text(due: datetime, window: Window, seconds: float) -> str:
    if seconds < 0:
        ago = -seconds
        if ago < 3600:
            return "overdue by under an hour"
        if ago < 86400:
            return f"overdue by {int(ago // 3600)} hour{'s' if int(ago // 3600) != 1 else ''}"
        days = int(ago // 86400)
        return f"overdue by {days} day{'s' if days != 1 else ''}"
    if seconds < 3600:
        return "due in under an hour"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"due in {hours} hour{'s' if hours != 1 else ''}"
    days = calendar_days(due, window)
    return f"due in {days} day{'s' if days != 1 else ''}"


# ======================================================================================== git (read-only)
def run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run ONE read-only git command in ``repo``. The sub-command must be in READ_ONLY_GIT; anything else is a programming error, not a runtime option.

    ``--no-optional-locks`` stops ``git status`` refreshing (writing) the index; ``core.fsmonitor=false`` stops a repository's config from making ``git status`` run
    a command of its own; ``core.hooksPath`` points at nothing. The environment is minimal: no tokens, no credential prompts, no pager.
    """
    if not args or args[0] not in READ_ONLY_GIT:
        raise ValueError(f"git sub-command {args[:1]} is not on the read-only list")
    env = {key: value for key in ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "USERPROFILE") if (value := os.environ.get(key))}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "0"})
    command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-C", str(repo), *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env, check=False)


def display_path(path: str | Path) -> str:
    """The path with the home directory shown as ``~`` (summaries are pasted into chats and logs)."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if home and (text == home or text.startswith(home + os.sep)) else text


def check_repo(raw_path: str, root: Path | None, now: datetime) -> dict[str, Any]:
    """The git state of one repository as plain data. Never raises: every failure is a ``state`` the summary can print."""
    shown = display_path(Path(raw_path).expanduser()) if raw_path.startswith("~") or os.path.isabs(raw_path) else raw_path
    result: dict[str, Any] = {"path": shown, "state": "ok", "detail": "", "branch": None, "detached": False, "unborn": False, "upstream": None,
                              "ahead": None, "behind": None, "uncommitted": {"total": 0, "staged": 0, "modified": 0, "untracked": 0, "conflicted": 0, "truncated": False},
                              "last_commit_age_seconds": None}
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        result.update(state="missing_path", detail="the path cannot be resolved")
        return result
    if root is not None:
        try:
            resolved.relative_to(root)
        except ValueError:
            result.update(state="outside_root", detail=f"not inside --root {display_path(root)}")
            return result
    if not resolved.is_dir():
        result.update(state="missing_path", detail="no such directory")
        return result
    try:
        inside = run_git(resolved, ["rev-parse", "--is-inside-work-tree"])
    except FileNotFoundError:
        result.update(state="git_missing", detail="git is not installed or not on PATH")
        return result
    except subprocess.TimeoutExpired:
        result.update(state="error", detail="git timed out")
        return result
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        result.update(state="not_a_repo", detail="not a git work tree")
        return result
    try:
        status = run_git(resolved, ["status", "--porcelain=v1", "--untracked-files=all"])
        if status.returncode != 0:
            result.update(state="error", detail=(status.stderr.strip().splitlines() or ["git status failed"])[0][:200])
            return result
        lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
        counts = result["uncommitted"]
        counts["truncated"] = len(lines) > MAX_STATUS_LINES
        for line in lines[:MAX_STATUS_LINES]:
            code = line[:2]
            if code == "??":
                counts["untracked"] += 1
            elif code in ("DD", "AU", "UD", "UA", "DU", "AA", "UU"):
                counts["conflicted"] += 1
            else:
                if code[0] not in (" ", "?"):
                    counts["staged"] += 1
                if code[1] not in (" ", "?"):
                    counts["modified"] += 1
        counts["total"] = min(len(lines), MAX_STATUS_LINES)
        head = run_git(resolved, ["rev-parse", "--verify", "--quiet", "HEAD"])
        if head.returncode != 0:
            result["unborn"] = True                      # a repo with no commit yet
        else:
            branch = run_git(resolved, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
            result["detached"] = branch == "HEAD"
            result["branch"] = None if result["detached"] else branch
            stamp = run_git(resolved, ["log", "-1", "--format=%ct"]).stdout.strip()
            if stamp.isdigit():
                result["last_commit_age_seconds"] = max(0, int(now.timestamp()) - int(stamp))
            upstream = run_git(resolved, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
            if upstream.returncode == 0 and upstream.stdout.strip():
                result["upstream"] = upstream.stdout.strip()
                counts_out = run_git(resolved, ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]).stdout.split()
                if len(counts_out) == 2 and all(c.isdigit() for c in counts_out):
                    result["ahead"], result["behind"] = int(counts_out[0]), int(counts_out[1])
    except subprocess.TimeoutExpired:
        result.update(state="error", detail="git timed out")
    return result


ACTION_TEXT = {"commit": "commit", "push": "push", "set_upstream": "push (the branch has no upstream yet)", "pull": "pull (behind the last fetched upstream)",
               "first_commit": "make a first commit", "checkout_branch": "check out a branch"}


def repo_actions(git: dict[str, Any]) -> list[str]:
    """What to do about a repo, as action codes (see ACTION_TEXT); empty = nothing to do. Order: commit, push, then pull."""
    if git["state"] != "ok":
        return []
    actions: list[str] = []
    if git["uncommitted"]["total"] > 0:
        actions.append("commit")
    if git["unborn"]:
        return actions or ["first_commit"]
    if git["detached"]:
        actions.append("checkout_branch")
    elif git["upstream"] is None:
        actions.append("set_upstream")
    elif git["ahead"]:
        actions.append("push")
    if git["upstream"] is not None and git["behind"]:
        actions.append("pull")
    return actions


def action_phrase(actions: list[str]) -> str:
    """["commit", "push"] -> "commit and push"; three or more -> "a, b and c"."""
    texts = [ACTION_TEXT[a] for a in actions]
    return texts[0] if len(texts) == 1 else ", ".join(texts[:-1]) + " and " + texts[-1]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def repo_sentence(git: dict[str, Any]) -> str:
    shown = git["path"]
    if git["state"] != "ok":
        reasons = {"not_a_repo": "is not a git repository", "missing_path": "was not found", "outside_root": "is outside --root", "git_missing": "cannot be checked: git is not installed",
                   "error": "could not be checked"}
        return f"Repo {shown} {reasons.get(git['state'], 'could not be checked')}" + (f" ({git['detail']})" if git["detail"] and git["state"] in ("error", "outside_root") else "") + "."
    facts = []
    total = git["uncommitted"]["total"]
    if total:
        facts.append(f"{_plural(total, 'uncommitted file')}" + ("+" if git["uncommitted"]["truncated"] else ""))
    if git["unborn"]:
        facts.append("no commit yet")
    elif git["upstream"] is None:
        facts.append("no upstream branch")
    else:
        if git["ahead"]:
            facts.append(f"{_plural(git['ahead'], 'commit')} not pushed")
        if git["behind"]:
            facts.append(f"{git['behind']} behind upstream")
    if not facts:
        facts.append("clean and pushed")
    age = git["last_commit_age_seconds"]
    if age is not None:
        facts.append("last commit " + (f"{int(age // 86400)}d ago" if age >= 86400 else f"{int(age // 3600)}h ago" if age >= 3600 else "under an hour ago"))
    return f"Repo {shown}: " + ", ".join(facts) + "."


# ======================================================================================== the report
def match_repo(name: str, extra: dict[str, str]) -> str:
    """A deadline's ``repo`` value -> the path to check. A bare word that equals the folder name of a ``--repo`` argument means that repository."""
    return extra.get(name, name)


def build_report(items: list[Deadline], repo_args: list[str], window: Window, root: Path | None, zone: tzinfo) -> dict[str, Any]:
    by_name = {Path(p).expanduser().name: p for p in repo_args}
    cache: dict[str, dict[str, Any]] = {}

    def git_for(value: str) -> dict[str, Any]:
        path = match_repo(value, by_name)
        key = str(Path(path).expanduser())
        if key not in cache:
            cache[key] = check_repo(path, root, window.now)
        return cache[key]

    entries, later, old = [], 0, 0
    for deadline in sorted(items, key=lambda d: d.due):
        status, seconds = classify(deadline.due, window)
        if status == "later":
            later += 1
            continue
        if status == "old":
            old += 1
            continue
        git = git_for(deadline.repo) if deadline.repo else None
        actions = repo_actions(git) if git else []
        when = when_text(deadline.due, window, seconds)
        local_due = deadline.due.astimezone(zone)
        sentence = f"{when[0].upper() + when[1:]}: {deadline.title} ({local_due:%a %b} {local_due.day})."
        if git:
            sentence += " " + repo_sentence(git)
            if actions:
                sentence += f" -> {action_phrase(actions)} " + ("before the deadline." if seconds >= 0 else "now: the deadline has passed.")
        else:
            sentence += " No repo."
        entries.append({"title": deadline.title, "due": deadline.due.isoformat(), "due_in_seconds": int(seconds), "days_left": calendar_days(deadline.due, window),
                        "status": status, "repo": git["path"] if git else None, "git": git, "actions": actions, "needs_action": bool(actions), "summary": sentence})
    repos = []
    for path in repo_args:
        key = str(Path(path).expanduser())
        if key not in cache:
            cache[key] = check_repo(path, root, window.now)
    for git in cache.values():
        repos.append(git)
    return {"schema": SCHEMA_ID, "generated_at": window.now.isoformat(), "timezone": str(getattr(zone, "key", None) or zone), "window_days": window.days,
            "urgent_days": window.urgent_days, "past_days": window.past_days, "items": entries, "repos": repos, "later_count": later, "old_count": old}


def render_text(report: dict[str, Any], window: Window, zone: tzinfo) -> str:
    lines = [f"deadline-watch {window.now.astimezone(zone):%a %b %d %H:%M %Z}: window {window.days:g} days, urgent within {window.urgent_days:g} days."]
    if not report["items"]:
        lines.append(f"Nothing due in the next {window.days:g} days and nothing overdue.")
    for entry in report["items"]:
        tag = {"urgent": "URGENT ", "overdue": "OVERDUE "}.get(entry["status"], "")
        lines.append(tag + entry["summary"])
    linked = {entry["repo"] for entry in report["items"] if entry["repo"]}
    for git in report["repos"]:
        if git["path"] not in linked and (repo_actions(git) or git["state"] != "ok"):
            lines.append("Other repo: " + repo_sentence(git) + (f" -> {action_phrase(repo_actions(git))}." if repo_actions(git) else ""))
    if report["later_count"]:
        lines.append(f"({_plural(report['later_count'], 'later deadline')} not shown.)")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deadline_watch.py", description="What is due soon, and is the related git work committed and pushed? (read-only)")
    parser.add_argument("deadlines", help="a Markdown table, a small YAML list, or an .ics file")
    parser.add_argument("--repo", action="append", default=[], metavar="PATH", help="a git repository to check (repeatable); a deadline's repo: value may name its folder")
    parser.add_argument("--root", metavar="DIR", help="only check repositories inside this directory")
    parser.add_argument("--days", type=float, default=7.0, help="window: show deadlines due within this many days (default 7)")
    parser.add_argument("--urgent-days", type=float, default=3.0, help="flag deadlines due within this many days as URGENT (default 3)")
    parser.add_argument("--past-days", type=float, default=7.0, help="also show deadlines that passed up to this many days ago (default 7)")
    parser.add_argument("--tz", metavar="ZONE", help="IANA zone for times without one (default: this machine's zone)")
    parser.add_argument("--now", metavar="ISO", help="pretend it is this moment (tests and demos), e.g. 2026-11-16T09:00:00-08:00")
    parser.add_argument("--json", action="store_true", help="print one JSON document instead of text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.days <= 0 or args.urgent_days < 0 or args.past_days < 0:
            raise DeadlineError("--days must be positive and --urgent-days / --past-days cannot be negative")
        zone = resolve_zone(args.tz)
        now = parse_due(args.now, zone, "--now") if args.now else datetime.now(timezone.utc).astimezone(zone)
        window = Window(now=now, zone=zone, days=args.days, urgent_days=min(args.urgent_days, args.days), past_days=args.past_days)
        root = Path(args.root).expanduser().resolve() if args.root else None
        items = load_deadlines(Path(args.deadlines).expanduser(), zone)
        report = build_report(items, args.repo, window, root, zone)
    except DeadlineError as exc:
        if args.json:
            print(json.dumps({"schema": SCHEMA_ID, "error": str(exc)}))
        else:
            print(f"deadline-watch: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else render_text(report, window, zone))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
