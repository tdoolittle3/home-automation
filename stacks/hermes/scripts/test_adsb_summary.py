"""Tests for the radio-logs skill's adsb_summary.py (lives in the skill so the
agent's sandbox can run it; tested here so CI covers it)."""

import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "homelab", "radio-logs", "scripts"))
import adsb_summary as a  # noqa: E402

MST = datetime.timezone(datetime.timedelta(hours=-7))


def _write(tmp_path):
    def rec(flight, last, alt=9000, dist=3.0):
        return json.dumps({"hex": "h" + flight, "flight": flight, "first_seen": last,
                           "last_seen": last, "min_alt": alt, "min_dist_nm": dist,
                           "max_gs": 400.0}) + "\n"
    (tmp_path / "2026-11-12.jsonl").write_text(
        rec("MORNING", "2026-11-12T09:00:00-07:00", alt=4000)
        + rec("LATE", "2026-11-12T23:50:00-07:00"))
    (tmp_path / "2026-11-13.jsonl").write_text(
        rec("NEW", "2026-11-13T00:15:00-07:00", dist=None) + "garbage\n")


def test_last_n_minutes_crosses_midnight_and_excludes_earlier(tmp_path):
    _write(tmp_path)
    now = datetime.datetime(2026, 11, 13, 0, 30, tzinfo=MST)
    recs, cutoff, _ = a.load("60", now=now, adsb_dir=str(tmp_path))
    assert [r["flight"] for r in recs] == ["NEW", "LATE"]   # newest first, no MORNING
    out = a.report(recs, cutoff, [])
    assert "ALL IN WINDOW" in out and "MORNING" not in out
    assert "dist=?" in out                                   # null distance shown as ?, not None


def test_whole_day_ranks_lowest(tmp_path):
    _write(tmp_path)
    recs, cutoff, files = a.load("2026-11-12", adsb_dir=str(tmp_path))
    assert cutoff is None and len(recs) == 2
    out = a.report(recs, cutoff, files)
    assert out.splitlines()[2].strip().startswith("MORNING")  # LOWEST: first entry


def test_missing_day_is_empty_not_an_error(tmp_path):
    recs, _, _ = a.load("2026-01-01", adsb_dir=str(tmp_path))
    assert recs == []
