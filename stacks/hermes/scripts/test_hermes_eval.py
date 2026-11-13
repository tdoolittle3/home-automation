"""Tests for hermes_eval.py's pure logic: stream parsing, tool unwrapping,
time/day matching, grading and verdicts. No Hermes, MCP or HA needed."""

import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermes_eval as he  # noqa: E402

STREAM = [
    '{"type": "system", "subtype": "init"}',
    '{"type": "tool_use", "name": "tool_search", "input": {"queries": ["camera"]}}',
    '{"type": "tool_use", "name": "tool_call", "input": {"calls": [{"name": "mcp__vision__tags", "arguments": {}}, {"name": "mcp__vision__recent_events"}]}}',
    '{"type": "tool_use", "name": "ha_get_state", "input": {}}',
    '{"type": "text", "text": "partial"}',
    'not json at all',
    '{"type": "result", "session_id": "local-abc", "exit_code": 0, "text": "Yes, at 9:54 AM on Wednesday."}',
]


def test_parse_stream_unwraps_meta_tools_and_takes_result_text():
    out = he.parse_stream(STREAM)
    assert out["tools"] == ["mcp__vision__tags", "mcp__vision__recent_events", "ha_get_state"]
    assert out["answer"] == "Yes, at 9:54 AM on Wednesday."
    assert out["session_id"] == "local-abc"


def test_parse_stream_falls_back_to_text_events():
    out = he.parse_stream(['{"type":"text","text":"Hel"}', '{"type":"text","text":"lo"}'])
    assert out["answer"] == "Hello" and out["tools"] == []


def test_answer_minutes_handles_12h_and_24h():
    assert 9 * 60 + 54 in he.answer_minutes("at 9:54 AM")
    assert 21 * 60 + 5 in he.answer_minutes("at 9:05 pm")
    assert he.answer_minutes("at 12:30 a.m.") == [30]
    bare = he.answer_minutes("around 7:21")          # no am/pm: both readings
    assert 7 * 60 + 21 in bare and 19 * 60 + 21 in bare
    assert he.answer_minutes("ratio 99:99") == []


def test_check_time_tolerance():
    assert he.check_time("It came at 9:54 AM.", ["09:53"])[0]
    assert not he.check_time("It came at 10:10 AM.", ["09:53"])[0]
    assert not he.check_time("It came this morning.", ["09:53"])[0]
    assert he.check_time("anything", [])[0]


def test_check_day_accepts_weekday_date_or_relative_word():
    today = datetime.date(2026, 11, 13)               # a Friday
    assert he.check_day("on Wednesday at 9:54", ["Wed Nov 11 09:54"], today)[0]
    assert he.check_day("on November 11th", ["Wed Nov 11 09:54"], today)[0]
    assert he.check_day("yesterday at 9:54", ["Thu Nov 12 09:54"], today)[0]
    assert not he.check_day("on Tuesday", ["Wed Nov 11 09:54"], today)[0]


def test_tools_check():
    assert he.tools_check(["mcp__vision__tags"], ["mcp__vision__"], ["ha_"])[0]
    ok, note = he.tools_check(["mcp__vision__tags", "ha_get_state"], ["mcp__vision__"], ["ha_"])
    assert not ok and "ha_get_state" in note
    assert not he.tools_check([], ["ha_"])[0]


def test_judge_parses_json_and_survives_garbage():
    ok, why = he.judge("q", "a", {"x": 1}, "r",
                       call=lambda p: 'Sure: {"pass": true, "reason": "matches"}')
    assert ok and why == "matches"
    assert he.judge("q", "a", {}, "r", call=lambda p: "PASS!!")[0] is False


def test_grade_combines_rule_and_judge_checks():
    q = {"prompt": "Did it come?", "checks": {"tools_any": ["mcp__vision__"], "tools_none": ["ha_"],
                                              "time": True, "judge": "say yes"}}
    run = {"answer": "Yes — 9:54 AM.", "tools": ["mcp__vision__recent_events"]}
    truth = {"facts": "yes 09:53", "times": ["09:53"], "answer": "yes"}
    seen = {}

    def fake(prompt):
        seen["p"] = prompt
        return '{"pass": true, "reason": "ok"}'

    checks = he.grade(q, run, truth, datetime.date(2026, 11, 13), judge_call=fake)
    assert [c[0] for c in checks] == ["tools", "time", "judge"]
    assert he.verdict(checks)
    assert "Ground-truth yes/no: yes" in seen["p"]


def test_grade_skips_time_when_truth_is_no_and_fails_empty_answers():
    q = {"prompt": "?", "checks": {"time": True}}
    assert he.grade(q, {"answer": "No.", "tools": []}, {"answer": "no", "times": []},
                    datetime.date.today()) == []
    bad = he.grade(q, {"answer": "", "tools": [], "error": "timeout 420s"}, {}, datetime.date.today())
    assert not he.verdict(bad) and bad[0][2] == "timeout 420s"


def test_truth_word_rejects_contradiction():
    q = {"prompt": "?", "checks": {"truth_word": True}}
    good = he.grade(q, {"answer": "The garage door is closed.", "tools": []}, {"word": "closed"},
                    datetime.date.today())
    bad = he.grade(q, {"answer": "It is open, not closed.", "tools": []}, {"word": "closed"},
                   datetime.date.today())
    assert he.verdict(good) and not he.verdict(bad)


def test_verdict_ignores_unavailable_judge_but_needs_a_real_check():
    assert he.verdict([("tools", True, "", "rule"), ("judge", None, "down", "judge")])
    assert not he.verdict([("judge", None, "down", "judge")])


def test_summary_lines_lists_failures_and_delta():
    run = {"date": "2026-11-13", "passed": 7, "total": 9, "results": [
        {"id": "a", "pass": True, "checks": []},
        {"id": "b", "pass": False, "checks": [("tools", True, "", "rule"),
                                              ("time", False, "want ~09:53", "rule")]}]}
    lines = he.summary_lines(run, {"date": "2026-11-12", "passed": 5})
    assert lines[0] == "Hermes eval 2026-11-13: 7/9 passed (+2 vs 11-12)"
    assert lines[1].startswith("✗ b — time: want ~09:53")
    assert lines[-1].endswith("/2026-11-13.html")


def test_question_file_is_valid():
    with open(he.QUESTIONS, encoding="utf-8") as f:
        qs = json.load(f)["questions"]
    ids = [q["id"] for q in qs]
    assert len(ids) == len(set(ids))
    known = {"tools_any", "tools_none", "time", "day", "regex_all", "regex_none",
             "truth_word", "truth_any", "no_stale", "judge"}
    fns = {"none", "fixed", "tag_latest", "tag_in_window", "timeline", "ha_state",
           "adsb_recent"}
    for q in qs:
        assert q["prompt"] and q.get("why")
        assert set(q.get("checks", {})) <= known, q["id"]
        assert q.get("truth", {}).get("fn", "none") in fns, q["id"]


def test_adsb_recent_reads_window_across_midnight(tmp_path):
    tz = he.TZ

    def line(flight, seen):
        return json.dumps({"hex": "abc", "flight": flight, "last_seen": seen,
                           "min_dist_nm": 3.0, "max_alt": 9000}) + "\n"
    (tmp_path / "2026-11-12.jsonl").write_text(
        line("OLD1", "2026-11-12T02:30:00-07:00") + line("LATE1", "2026-11-12T23:40:00-07:00"))
    (tmp_path / "2026-11-13.jsonl").write_text(
        line("NEW1  ", "2026-11-13T00:10:00-07:00") + "garbage\n")
    now = datetime.datetime(2026, 11, 13, 0, 20, tzinfo=tz)
    t = he.adsb_recent(60, now=now, adsb_dir=str(tmp_path))
    assert t["tokens"] == ["NEW1", "LATE1"] and t["answer"] == "yes"
    assert he.adsb_recent(5, now=now, adsb_dir=str(tmp_path))["answer"] == "no"


def test_truth_any_requires_a_real_callsign():
    q = {"prompt": "?", "checks": {"truth_any": True}}
    truth = {"tokens": ["SKW4643", "UAL12"]}
    ok = he.grade(q, {"answer": "SKW4643 passed at 19:15.", "tools": []}, truth, datetime.date.today())
    bad = he.grade(q, {"answer": "SKW5551 overnight.", "tools": []}, truth, datetime.date.today())
    assert he.verdict(ok) and not he.verdict(bad)


def test_call_transcript_pairs_results_and_flags_blocked():
    events = [json.loads(l) for l in [
        '{"type": "tool_use", "name": "tool_search", "tool_call_id": "s", "input": {}}',
        '{"type": "tool_use", "name": "terminal", "tool_call_id": "a", "input": {"command": "python3 - <<EOF"}}',
        '{"type": "tool_result", "tool_call_id": "a", "output": "{\\"status\\": \\"blocked\\"}"}',
        '{"type": "tool_use", "name": "terminal", "tool_call_id": "b", "input": {"command": "ls"}}',
        '{"type": "tool_result", "tool_call_id": "b", "output": "x.jsonl"}']]
    calls = he.call_transcript(events)
    assert [c["blocked"] for c in calls] == [True, False]
    assert calls[1]["output"] == "x.jsonl" and "ls" in calls[1]["input"]


def test_adsb_truth_lists_every_aircraft(tmp_path):
    now = datetime.datetime(2026, 11, 13, 0, 30, tzinfo=he.TZ)
    rows = "".join(json.dumps({"hex": f"h{i}", "flight": f"F{i}",
                               "last_seen": f"2026-11-13T00:{i % 30:02d}:00-07:00"}) + "\n"
                   for i in range(45))
    (tmp_path / "2026-11-13.jsonl").write_text(rows)
    t = he.adsb_recent(60, now=now, adsb_dir=str(tmp_path))
    assert t["facts"]["count"] == 45 and len(t["facts"]["aircraft"]) == 45


def test_no_stale_flags_callsigns_from_outside_the_window():
    q = {"prompt": "?", "checks": {"no_stale": True}}
    truth = {"tokens": ["UAL504", "SKW4666"], "all_tokens": ["UAL504", "SKW4666", "SKW5551"]}
    today = datetime.date.today()
    good = he.grade(q, {"answer": "UAL504 and SKW4666 passed.", "tools": []}, truth, today)
    bad = he.grade(q, {"answer": "UAL504, and earlier SKW5551.", "tools": []}, truth, today)
    assert he.verdict(good)
    assert not he.verdict(bad) and "SKW5551" in bad[0][2]
