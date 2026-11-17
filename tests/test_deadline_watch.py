"""deadline-watch: offline tests (no network, no Hermes). Git states use throwaway repositories under pytest's tmp_path."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import SCHEMA, SCRIPT, SKILL, commit, dw, git, make_pushed_repo, make_repo, validate

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=LA)          # Monday morning


def window(**kw) -> "dw.Window":
    return dw.Window(now=kw.pop("now", NOW), zone=kw.pop("zone", LA), **kw)


# ================================================================================ due-window maths
@pytest.mark.parametrize("delta, status", [
    (timedelta(seconds=-1), "overdue"), (timedelta(0), "urgent"), (timedelta(days=3), "urgent"), (timedelta(days=3, seconds=1), "soon"),
    (timedelta(days=7), "soon"), (timedelta(days=7, seconds=1), "later"), (timedelta(days=-7), "overdue"), (timedelta(days=-7, seconds=-1), "old"),
])
def test_the_window_edges(delta, status) -> None:
    assert dw.classify(NOW + delta, window())[0] == status


def test_the_window_is_configurable() -> None:
    wide = window(days=30, urgent_days=10, past_days=1)
    assert dw.classify(NOW + timedelta(days=9), wide)[0] == "urgent" and dw.classify(NOW + timedelta(days=29), wide)[0] == "soon"
    assert dw.classify(NOW - timedelta(days=2), wide)[0] == "old"


def test_a_date_without_a_time_means_the_end_of_that_day_in_the_users_zone() -> None:
    due = dw.parse_due("2026-11-18", LA, "t")
    assert (due.hour, due.minute, due.second) == (23, 59, 59) and due.utcoffset() == timedelta(hours=-8)


@pytest.mark.parametrize("text, expected_utc", [
    ("2026-11-18 17:00", "2026-11-19T01:00:00"),               # naive: read in the given zone (LA, PST = UTC-8)
    ("2026-11-18T17:00:00Z", "2026-11-18T17:00:00"),
    ("2026-11-18T17:00:00-05:00", "2026-11-18T22:00:00"),
    ("2026-11-18T17:00:00+0530", "2026-11-18T11:30:00"),
    ("2026-11-18  17:00:30", "2026-11-19T01:00:30"),
])
def test_due_times_in_every_supported_spelling(text, expected_utc) -> None:
    assert dw.parse_due(text, LA, "t").astimezone(timezone.utc).replace(tzinfo=None).isoformat() == expected_utc


def test_the_same_clock_time_means_a_different_moment_in_a_different_zone() -> None:
    assert dw.parse_due("2026-11-18 09:00", ZoneInfo("Asia/Kolkata"), "t") < dw.parse_due("2026-11-18 09:00", LA, "t")


def test_daylight_saving_is_honoured_for_named_zones() -> None:
    before = datetime(2026, 10, 31, 12, 0, tzinfo=LA)                 # PDT (UTC-7); clocks go back on Nov 1
    due = dw.parse_due("2026-11-01 12:00", LA, "t")                   # PST (UTC-8)
    assert (due.astimezone(timezone.utc) - before.astimezone(timezone.utc)).total_seconds() == 25 * 3600
    # ... and the program's own arithmetic agrees (it must not subtract wall-clock times of one shared ZoneInfo)
    assert dw.classify(due, dw.Window(now=before, zone=LA))[1] == 25 * 3600


def test_a_utc_deadline_is_counted_in_the_users_calendar_days() -> None:
    due = dw.parse_due("2026-11-21T01:00:00Z", LA, "t")               # Fri Nov 20, 17:00 in LA
    assert dw.calendar_days(due, window()) == 4 and "Nov 20" in dw.build_report([dw.Deadline("x", due, None, "t")], [], window(), None, LA)["items"][0]["summary"]


@pytest.mark.parametrize("bad", ["", "tomorrow", "2026-13-01", "2026-11-31", "2026-11-18 25:00", "18/11/2026", "2026-11-18 17"])
def test_unreadable_dates_are_errors_not_guesses(bad) -> None:
    with pytest.raises(dw.DeadlineError):
        dw.parse_due(bad, LA, "file:3")


@pytest.mark.parametrize("seconds, text", [(-30 * 3600, "overdue by 1 day"), (-3 * 3600, "overdue by 3 hours"), (-60, "overdue by under an hour"), (1800, "due in under an hour"),
                                           (5 * 3600, "due in 5 hours"), (2 * 86400 + 3600, "due in 2 days")])
def test_the_wording(seconds, text) -> None:
    assert dw.when_text(NOW + timedelta(seconds=seconds), window(), seconds) == text


def test_one_day_and_one_hour_are_singular() -> None:
    assert dw.when_text(NOW + timedelta(hours=30), window(), 30 * 3600) == "due in 1 day" and dw.when_text(NOW + timedelta(hours=1), window(), 3600) == "due in 1 hour"


# ================================================================================ the three file formats
MD = """# Deadlines
Some prose that is not a table.

| Title | Due | Repo |
|---|---|---|
| Assignment 3 | 2026-11-18 23:59 | ~/a3 |
| **Essay** | 2026-11-19 |  |
| Pipe \\| in title | 2026-11-20 10:00 | `~/p` |
"""


def test_markdown_table(tmp_path) -> None:
    path = tmp_path / "d.md"
    path.write_text(MD)
    items = dw.load_deadlines(path, LA)
    assert [i.title for i in items] == ["Assignment 3", "Essay", "Pipe | in title"] and [i.repo for i in items] == ["~/a3", None, "~/p"]
    assert items[0].due == datetime(2026, 11, 18, 23, 59, tzinfo=LA) and items[1].due.hour == 23


def test_markdown_table_with_other_column_names_and_order(tmp_path) -> None:
    path = tmp_path / "d.md"
    path.write_text("| Repository | Date | Name |\n|:--|--:|---|\n| ~/x | 2026-11-18 | Thing |\n")
    assert [(i.title, i.repo) for i in dw.load_deadlines(path, LA)] == [("Thing", "~/x")]


YAML = """# comment
deadlines:
  - title: Assignment 3        # trailing comment
    due: 2026-11-18 23:59
    repo: ~/a3
  - title: "Quoted: title # not a comment"
    due: '2026-11-19'
  - {title: Flow one, due: "2026-11-20T17:00:00-08:00", repo: demo}
"""


def test_yaml_subset(tmp_path) -> None:
    path = tmp_path / "d.yaml"
    path.write_text(YAML)
    items = dw.load_deadlines(path, LA)
    assert [i.title for i in items] == ["Assignment 3", "Quoted: title # not a comment", "Flow one"]
    assert [i.repo for i in items] == ["~/a3", None, "demo"] and items[2].due.utcoffset() == timedelta(hours=-8)


def test_yaml_list_without_the_wrapper_key(tmp_path) -> None:
    path = tmp_path / "d.yml"
    path.write_text("- title: A\n  due: 2026-11-18\n")
    assert dw.load_deadlines(path, LA)[0].title == "A"


ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTODO
SUMMARY:Assignment 3\\, part 2
DUE;TZID=America/Los_Angeles:20261118T235900
DESCRIPTION:notes\\nrepo: ~/a3
END:VTODO
BEGIN:VEVENT
SUMMARY:Proposal with a long title that is folded
  across two lines
DTSTART:20261121T010000Z
X-DEADLINE-REPO:~/p
END:VEVENT
BEGIN:VEVENT
SUMMARY:All day
DTSTART;VALUE=DATE:20261119
END:VEVENT
BEGIN:VEVENT
SUMMARY:Floating
DTSTART:20261120T170000
END:VEVENT
END:VCALENDAR
"""


def test_ics(tmp_path) -> None:
    path = tmp_path / "d.ics"
    path.write_text(ICS)
    items = {i.title: i for i in dw.load_deadlines(path, LA)}
    assert set(items) == {"Assignment 3, part 2", "Proposal with a long title that is folded across two lines", "All day", "Floating"}
    assert items["Assignment 3, part 2"].repo == "~/a3" and items["Assignment 3, part 2"].due == datetime(2026, 11, 18, 23, 59, tzinfo=LA)
    assert items["Proposal with a long title that is folded across two lines"].repo == "~/p"
    assert items["All day"].due.hour == 23 and items["Floating"].due == datetime(2026, 11, 20, 17, 0, tzinfo=LA)


def test_the_format_is_sniffed_when_the_extension_says_nothing(tmp_path) -> None:
    for name, text in (("a", MD), ("b", YAML), ("c", ICS)):
        (tmp_path / name).write_text(text)
        assert dw.load_deadlines(tmp_path / name, LA)


# ================================================================================ malformed files
@pytest.mark.parametrize("name, text, needle", [
    ("e.md", "", "no Markdown table"),
    ("e.md", "just prose\n", "no Markdown table"),
    ("e.md", "| Foo | Bar |\n|---|---|\n| a | b |\n", "no Markdown table"),
    ("e.md", "| Title | Due |\n|---|---|\n| A | not-a-date |\n", "e.md:3"),
    ("e.md", "| Title | Due |\n|---|---|\n|  | 2026-11-18 |\n", "no title"),
    ("e.yaml", "", "no deadlines"),
    ("e.yaml", "- title: A\n", "no due date"),
    ("e.yaml", "- due: 2026-11-18\n", "no title"),
    ("e.yaml", "- title: A\n  due: 2026-02-30\n", "not a real date"),
    ("e.yaml", "title: A\n", "before the first"),
    ("e.yaml", "- title A\n", "expected 'key: value'"),
    ("e.yaml", "- {title: A, due: 2026-11-18\n", "unclosed"),
    ("e.ics", "BEGIN:VCALENDAR\nEND:VCALENDAR\n", "no VEVENT or VTODO"),
    ("e.ics", "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20261118\nEND:VEVENT\nEND:VCALENDAR\n", "no SUMMARY"),
    ("e.ics", "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\nEND:VEVENT\nEND:VCALENDAR\n", "no DTSTART"),
    ("e.ics", "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\nDTSTART:soon\nEND:VEVENT\nEND:VCALENDAR\n", "cannot read the calendar date"),
])
def test_malformed_files_are_errors_that_name_the_problem(tmp_path, name, text, needle) -> None:
    path = tmp_path / name
    path.write_text(text)
    with pytest.raises(dw.DeadlineError) as caught:
        dw.load_deadlines(path, LA)
    assert needle in str(caught.value)


def test_missing_binary_and_oversized_files(tmp_path) -> None:
    with pytest.raises(dw.DeadlineError, match="cannot read"):
        dw.load_deadlines(tmp_path / "nope.md", LA)
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(dw.DeadlineError, match="UTF-8"):
        dw.load_deadlines(tmp_path / "bin.md", LA)
    (tmp_path / "big.md").write_text("x" * (dw.MAX_FILE_BYTES + 1))
    with pytest.raises(dw.DeadlineError, match="refusing"):
        dw.load_deadlines(tmp_path / "big.md", LA)
    with pytest.raises(dw.DeadlineError, match="not a file"):
        dw.load_deadlines(tmp_path, LA)


def test_the_cli_exits_2_with_the_message_on_stderr_and_a_json_error_with_json(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- title: A\n")
    assert dw.main([str(bad)]) == 2
    assert "bad.yaml:1" in capsys.readouterr().err
    assert dw.main([str(bad), "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "deadline-watch/1" and "no due date" in out["error"]
    assert dw.main([str(bad), "--days", "0"]) == 2
    assert dw.main([str(bad), "--tz", "Not/AZone"]) == 2


# ================================================================================ git states (temp repositories)
def state(path, root=None):
    return dw.check_repo(str(path), root, NOW)


def test_clean_and_pushed(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    got = state(clone)
    assert got["state"] == "ok" and got["uncommitted"]["total"] == 0 and (got["ahead"], got["behind"]) == (0, 0)
    assert got["upstream"] == "origin/main" and got["branch"] == "main" and dw.repo_actions(got) == []
    assert "clean and pushed" in dw.repo_sentence(got)


def test_dirty_counts_staged_modified_and_untracked_separately(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    (clone / "a.txt").write_text("changed\n")                                   # modified
    (clone / "new.txt").write_text("n\n")
    git(clone, "add", "new.txt")                                                # staged
    (clone / "untracked.txt").write_text("u\n")                                 # untracked
    got = state(clone)["uncommitted"]
    assert (got["total"], got["staged"], got["modified"], got["untracked"]) == (3, 1, 1, 1)
    assert dw.repo_actions(state(clone)) == ["commit"]


def test_a_file_both_staged_and_modified_counts_once_in_total(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    (clone / "a.txt").write_text("one\n")
    git(clone, "add", "a.txt")
    (clone / "a.txt").write_text("two\n")
    got = state(clone)["uncommitted"]
    assert (got["total"], got["staged"], got["modified"]) == (1, 1, 1)


def test_untracked_files_inside_a_new_directory_are_counted_one_by_one(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    (clone / "d").mkdir()
    for number in range(3):
        (clone / "d" / f"f{number}.txt").write_text("x")
    assert state(clone)["uncommitted"]["untracked"] == 3


def test_ahead_commits_are_not_pushed(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    commit(clone, "b.txt")
    commit(clone, "c.txt")
    got = state(clone)
    assert (got["ahead"], got["behind"]) == (2, 0) and dw.repo_actions(got) == ["push"] and "2 commits not pushed" in dw.repo_sentence(got)


def test_behind_is_measured_against_the_last_fetch(tmp_path) -> None:
    clone, remote = make_pushed_repo(tmp_path)
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    commit(other, "elsewhere.txt")
    git(other, "push", "-q")
    assert state(clone)["behind"] == 0                                           # no fetch yet: the program does not contact the remote
    git(clone, "fetch", "-q")
    got = state(clone)
    assert (got["ahead"], got["behind"]) == (0, 1) and dw.repo_actions(got) == ["pull"]


def test_no_upstream(tmp_path) -> None:
    repo = make_repo(tmp_path / "solo")
    commit(repo)
    got = state(repo)
    assert got["upstream"] is None and got["ahead"] is None and dw.repo_actions(got) == ["set_upstream"] and "no upstream branch" in dw.repo_sentence(got)


def test_dirty_and_ahead_gives_commit_and_push(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    commit(clone, "b.txt")
    (clone / "x").write_text("1")
    assert dw.action_phrase(dw.repo_actions(state(clone))) == "commit and push"


def test_a_repo_without_any_commit(tmp_path) -> None:
    repo = make_repo(tmp_path / "empty")
    got = state(repo)
    assert got["unborn"] is True and got["state"] == "ok" and dw.repo_actions(got) == ["first_commit"]
    (repo / "f").write_text("x")
    assert dw.repo_actions(state(repo)) == ["commit"]


def test_detached_head(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    git(clone, "checkout", "-q", "--detach")
    got = state(clone)
    assert got["detached"] is True and got["branch"] is None and "checkout_branch" in dw.repo_actions(got)


def test_not_a_repo_missing_path_and_outside_root(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert state(plain)["state"] == "not_a_repo"
    assert state(tmp_path / "ghost")["state"] == "missing_path"
    clone, _ = make_pushed_repo(tmp_path)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    assert state(clone, root=allowed.resolve())["state"] == "outside_root"
    assert state(clone, root=tmp_path.resolve())["state"] == "ok"
    for kind in ("not_a_repo", "missing_path", "outside_root"):
        assert dw.repo_actions({"state": kind}) == []


def test_last_commit_age(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    later = datetime.now(timezone.utc) + timedelta(days=2, hours=1)
    assert 2 * 86400 <= dw.check_repo(str(clone), None, later)["last_commit_age_seconds"] < 2 * 86400 + 7200


def test_a_nested_folder_of_a_repo_is_still_that_repo(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    (clone / "sub").mkdir()
    assert state(clone / "sub")["state"] == "ok"


# ================================================================================ read-only guarantees
def test_only_read_only_git_subcommands_can_run(tmp_path) -> None:
    for verb in ("commit", "push", "add", "checkout", "reset", "fetch", "pull", "config", "clean", "gc", "stash"):
        with pytest.raises(ValueError):
            dw.run_git(tmp_path, [verb])
    with pytest.raises(ValueError):
        dw.run_git(tmp_path, [])


def test_every_git_call_the_program_makes_is_on_the_allow_list_and_carries_the_safety_flags(tmp_path, monkeypatch) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    (clone / "x").write_text("1")
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(command, *a, **kw):
        seen.append(list(command))
        return real(command, *a, **kw)

    monkeypatch.setattr(dw.subprocess, "run", spy)
    dw.check_repo(str(clone), None, NOW)
    assert seen and {c[c.index("-C") + 2] for c in seen} <= dw.READ_ONLY_GIT
    for command in seen:
        assert command[0] == "git" and "--no-optional-locks" in command and "core.fsmonitor=false" in command


def test_checking_a_repo_does_not_change_it(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    (clone / "x").write_text("1")
    (clone / "a.txt").write_text("changed\n")

    def files():
        return [(str(p.relative_to(clone)), p.stat().st_mtime_ns, p.stat().st_size) for p in sorted((clone).rglob("*")) if p.is_file() and ".git/objects" not in str(p) or p.name == "index"]

    status_before = git(clone, "status", "--porcelain")          # (this fixture-side call may refresh the index: do it BEFORE the snapshot)
    before = files()
    state(clone)
    assert files() == before and git(clone, "status", "--porcelain") == status_before


def test_a_repositorys_fsmonitor_setting_cannot_run_a_command(tmp_path) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    marker = tmp_path / "ran.txt"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    git(clone, "config", "core.fsmonitor", str(hook))
    (clone / "x").write_text("1")
    assert state(clone)["state"] == "ok"
    assert not marker.exists()


def test_the_script_has_no_network_or_write_apis() -> None:
    text = SCRIPT.read_text()
    for forbidden in ("import socket", "urllib", "requests", "http.client", "os.remove", "shutil", "write_text(", "open(", "os.system", "shell=True", "os.environ["):
        assert forbidden not in text, forbidden


# ================================================================================ the report and the CLI
def run_cli(capsys, *argv):
    code = dw.main([str(a) for a in argv])
    return code, capsys.readouterr().out


def test_the_example_sentence_end_to_end(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    clone, _ = make_pushed_repo(tmp_path, "a3")
    commit(clone, "ahead.txt")
    for number in range(3):
        (clone / f"u{number}.txt").write_text("x")
    deadlines = tmp_path / "d.md"
    deadlines.write_text("| Title | Due | Repo |\n|--|--|--|\n| Assignment 3 | 2026-11-18 23:59 | ~/a3 |\n")
    code, out = run_cli(capsys, deadlines, "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles")
    assert code == 0
    assert ("URGENT Due in 2 days: Assignment 3 (Wed Nov 18). Repo ~/a3: 3 uncommitted files, 1 commit not pushed, last commit under an hour ago."
            " -> commit and push before the deadline.") in out


def test_nothing_due(tmp_path, capsys) -> None:
    deadlines = tmp_path / "d.yaml"
    deadlines.write_text("- title: Far away\n  due: 2027-06-01\n")
    code, out = run_cli(capsys, deadlines, "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles")
    assert code == 0 and "Nothing due in the next 7 days" in out and "1 later deadline not shown" in out


def test_overdue_with_unpushed_work_says_now(tmp_path, capsys) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    commit(clone, "late.txt")
    deadlines = tmp_path / "d.yaml"
    deadlines.write_text(f"- title: Late lab\n  due: 2026-11-15 12:00\n  repo: {clone}\n")
    _, out = run_cli(capsys, deadlines, "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles")
    assert "OVERDUE Overdue by 21 hours: Late lab" in out and "-> push now: the deadline has passed." in out


def test_old_deadlines_are_counted_not_listed(tmp_path, capsys) -> None:
    deadlines = tmp_path / "d.yaml"
    deadlines.write_text("- title: Ancient\n  due: 2026-01-01\n- title: Soon\n  due: 2026-11-17\n")
    _, out = run_cli(capsys, deadlines, "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles", "--json")
    report = json.loads(out)
    assert [i["title"] for i in report["items"]] == ["Soon"] and report["old_count"] == 1


def test_a_deadline_can_name_a_repo_by_its_folder_and_extra_repos_are_listed(tmp_path, capsys) -> None:
    clone, _ = make_pushed_repo(tmp_path, "demo-proposal")
    commit(clone, "ahead.txt")
    other, _ = make_pushed_repo(tmp_path, "unrelated")
    (other / "dirty.txt").write_text("x")
    deadlines = tmp_path / "d.yaml"
    deadlines.write_text("- title: Proposal\n  due: 2026-11-17\n  repo: demo-proposal\n")
    _, out = run_cli(capsys, deadlines, "--repo", clone, "--repo", other, "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles")
    assert "Repo " + str(clone) + ": 1 commit not pushed" in out and "Other repo: Repo " + str(other) + ": 1 uncommitted file" in out


def test_a_clean_unrelated_repo_is_not_mentioned_in_the_text(tmp_path, capsys) -> None:
    clean, _ = make_pushed_repo(tmp_path, "tidy")
    deadlines = tmp_path / "d.yaml"
    deadlines.write_text("- title: Proposal\n  due: 2026-11-17\n")
    _, out = run_cli(capsys, deadlines, "--repo", clean, "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles")
    assert "Other repo" not in out


def test_home_is_shown_as_a_tilde(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dw.display_path(tmp_path / "a3") == "~/a3" and dw.display_path(tmp_path) == "~" and dw.display_path("/elsewhere/x") == "/elsewhere/x"
    assert dw.check_repo("~/ghost", None, NOW)["path"] == "~/ghost"


def test_root_confines_the_repositories_a_deadlines_file_can_point_at(tmp_path, capsys) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    deadlines = tmp_path / "d.yaml"
    deadlines.write_text(f"- title: T\n  due: 2026-11-17\n  repo: {clone}\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _, out = run_cli(capsys, deadlines, "--root", elsewhere, "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles")
    assert "is outside --root" in out


# ================================================================================ --json
def test_json_output_matches_the_published_schema(tmp_path, capsys) -> None:
    clone, _ = make_pushed_repo(tmp_path)
    commit(clone, "b.txt")
    (clone / "u").write_text("x")
    plain = tmp_path / "plain"
    plain.mkdir()
    deadlines = tmp_path / "d.md"
    deadlines.write_text("| Title | Due | Repo |\n|--|--|--|\n" + f"| A | 2026-11-18 | {clone} |\n| B | 2026-11-19 |  |\n| C | 2026-11-20 | {plain} |\n"
                         f"| D | 2026-11-14 | {tmp_path / 'ghost'} |\n| E | 2027-01-01 |  |\n")
    code, out = run_cli(capsys, deadlines, "--json", "--repo", clone, "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles")
    report = json.loads(out)
    assert code == 0
    validate(report, SCHEMA)
    assert [i["title"] for i in report["items"]] == ["D", "A", "B", "C"] and [i["status"] for i in report["items"]] == ["overdue", "urgent", "soon", "soon"]
    a = report["items"][1]
    assert a["actions"] == ["commit", "push"] and a["needs_action"] is True and a["days_left"] == 2 and a["git"]["ahead"] == 1 and a["git"]["uncommitted"]["untracked"] == 1
    assert report["items"][2]["git"] is None and report["items"][2]["repo"] is None and report["later_count"] == 1
    assert report["items"][3]["git"]["state"] == "not_a_repo" and report["items"][0]["git"]["state"] == "missing_path"
    assert report["schema"] == "deadline-watch/1" and report["window_days"] == 7 and report["urgent_days"] == 3


def test_the_schema_checker_itself_rejects_wrong_output() -> None:
    with pytest.raises(AssertionError):
        validate({"schema": "other"}, SCHEMA)
    with pytest.raises(AssertionError):
        validate({"schema": "deadline-watch/1", "generated_at": "x", "timezone": "x", "window_days": 7, "urgent_days": 3, "past_days": 7, "later_count": 0,
                  "old_count": 0, "items": [{"title": "t"}], "repos": []}, SCHEMA)


def test_json_has_no_home_directory_path(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    clone, _ = make_pushed_repo(tmp_path, "a3")
    deadlines = tmp_path / "d.yaml"
    deadlines.write_text("- title: T\n  due: 2026-11-17\n  repo: ~/a3\n")
    _, out = run_cli(capsys, deadlines, "--json", "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles")
    assert str(tmp_path) not in out and '"~/a3"' in out


# ================================================================================ the shipped examples and the skill file
@pytest.mark.parametrize("name", ["deadlines.example.md", "deadlines.example.yaml", "deadlines.example.ics"])
def test_the_examples_parse_and_give_the_documented_result(name, capsys) -> None:
    code, out = run_cli(capsys, SKILL / "examples" / name, "--now", "2026-11-16T09:00:00-08:00", "--tz", "America/Los_Angeles")
    assert code == 0 and "URGENT Due in 2 days: Assignment 3 (Wed Nov 18)" in out
    if name.endswith(".md"):
        assert "OVERDUE Overdue by 1 day: Lab 4 (late)" in out


def test_the_script_runs_as_a_program_with_a_clean_interpreter(tmp_path) -> None:
    done = subprocess.run([sys.executable, "-I", str(SCRIPT), str(SKILL / "examples" / "deadlines.example.yaml"), "--json", "--now", "2026-11-16T09:00:00-08:00",
                           "--tz", "America/Los_Angeles"], capture_output=True, text=True, timeout=60, cwd=tmp_path, check=False)
    assert done.returncode == 0, done.stderr
    validate(json.loads(done.stdout), SCHEMA)


def test_skill_md_has_the_frontmatter_hermes_expects() -> None:
    text = (SKILL / "SKILL.md").read_text()
    assert text.startswith("---\n")
    front = text.split("---\n", 2)[1]
    fields = dict(line.split(":", 1) for line in front.splitlines() if line and not line.startswith(" ") and ":" in line)
    assert fields["name"].strip() == "deadline-watch" and fields["description"].strip() and fields["version"].strip() == "1.0.0" and fields["license"].strip() == "MIT"
    assert len(fields["description"].strip()) <= 1024 and (SKILL.name == fields["name"].strip())
    for section in ("## When to Use", "## Procedure", "## Pitfalls", "## Verification"):
        assert section in text
    assert "${HERMES_SKILL_DIR}/scripts/deadline_watch.py" in text
    for line in text.splitlines():
        if "${HERMES_SKILL_DIR}/" in line:
            for token in line.split():
                if token.startswith("${HERMES_SKILL_DIR}/"):
                    assert (SKILL / token.replace("${HERMES_SKILL_DIR}/", "").rstrip("`.,")).exists(), token


def test_the_repository_contains_only_synthetic_data_and_no_secrets() -> None:
    import re
    root = SKILL.parent.parent
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts and path.suffix in (".md", ".yaml", ".ics", ".py", ".json", ".toml", ""):
            text = path.read_text(errors="ignore")
            assert not re.search(r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|BEGIN (RSA|EC|OPENSSH) PRIVATE KEY|/home/[a-z]+/)", text), path


# ================================================================================ Hermes' own install-time scan (skipped where Hermes is not installed)
HERMES = __import__("pathlib").Path("~/opt/hermes-agent").expanduser()


@pytest.mark.skipif(not (HERMES / ".venv" / "bin" / "python").exists(), reason="Hermes is not installed here")
def test_hermes_skill_scanner_lets_a_community_install_through_without_force() -> None:
    """`hermes skills install` blocks a community skill whose scan verdict is caution/dangerous (INSTALL_POLICY). Ours must be 'safe' so no --force is needed."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from tools.skills_guard import scan_skill, should_allow_install; "
            "r = scan_skill(Path(sys.argv[2]), source='someone/hermes-skill-deadline-watch/skills/deadline-watch'); "
            "print(r.verdict, should_allow_install(r)[0], [f.pattern_id + ':' + f.severity for f in r.findings])")
    home = __import__("tempfile").mkdtemp()
    done = subprocess.run([str(HERMES / ".venv" / "bin" / "python"), "-c", code, str(HERMES), str(SKILL)], capture_output=True, text=True, timeout=120, check=False,
                          env={"HOME": home, "HERMES_HOME": home, "PATH": "/usr/bin:/bin"})
    assert done.returncode == 0, done.stderr[-500:]
    verdict, allowed, findings = done.stdout.strip().split(" ", 2)
    assert verdict == "safe" and allowed == "True", f"{verdict} {allowed} {findings}"
