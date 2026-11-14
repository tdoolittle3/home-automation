"""Pure-logic tests for tv-fleet.py - a fake adb, no TV, no network.

Fixture text is real output captured from TV 1 (Fire OS 7.7.1.7) on
2026-11-13. Run: python -m pytest stacks/net/ -v
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("tv_fleet", Path(__file__).with_name("tv-fleet.py"))
tvf = importlib.util.module_from_spec(_spec)
sys.modules["tv_fleet"] = tvf  # dataclasses resolves annotations via sys.modules
_spec.loader.exec_module(tvf)

FOCUS_PICKER = ("  mCurrentFocus=Window{fc80037 u0 com.amazon.ftv.profilepicker/"
                "com.amazon.ftv.profilepicker.ui.PickerActivity}\n")
FOCUS_HOME = ("  mCurrentFocus=Window{1a2b3c u0 com.amazon.tv.launcher/"
              "com.amazon.tv.launcher.ui.HomeActivity_vNext}\n")
FOCUS_YT = "  mCurrentFocus=Window{9 u0 com.amazon.firetv.youtube/dev.cobalt.app.MainActivity}\n"
MEDIA_STALE_YT = """\
      package=com.amazon.firetv.youtube
      active=false
      state=PlaybackState {state=3, position=-1, buffered position=0, speed=0.0, updated=873916943, actions=4, custom actions=[], active item id=-1, error=null}
      package=com.amazon.alexamediaplayer.runtime.ftv
      active=false
      state=PlaybackState {state=0, position=0, buffered position=0, speed=0.0, updated=0, actions=6, custom actions=[], active item id=-1, error=null}
      package=com.amazon.vizzini
      active=false
      package=com.sling
      active=false
"""
MEDIA_SLING_PLAYING = """\
      package=com.sling
      active=true
      state=PlaybackState {state=3, position=1000, buffered position=0, speed=1.0, updated=1, actions=0, custom actions=[], active item id=-1, error=null}
"""

INVENTORY = {
    "defaults": {
        "apk": None,
        "dashboard_url": "http://192.168.0.13",
        "ntfy_url": "http://192.168.0.13:8090",
        "screensaver_component": "com.thomas.ladybird/.LadybirdDream",
        "screensaver_timeout_min": 10,
        "app_names": {"com.amazon.tv.launcher": "Home", "com.sling": "Sling"},
    },
    "tvs": [{"name": "living", "room": "living", "ip": "192.168.0.18", "topics": ["extra"]}],
}


def tv(**over):
    _, tvs = tvf.parse_inventory(INVENTORY)
    return {**tvs[0], **over}


class FakeTV:
    """Minimal stateful Fire TV behind a fake `adb` binary."""

    def __init__(self, *, reachable=True, authorized=True, installed=True, overlay="ignore",
                 settings=None, focus=FOCUS_HOME, media=MEDIA_STALE_YT, sticky=True):
        self.reachable, self.authorized = reachable, authorized
        self.installed, self.overlay, self.focus, self.media = installed, overlay, focus, media
        self.sticky = sticky
        self.settings = {
            ("secure", "screensaver_components"): "com.amazon.ftv.screensaver/.app.services.ScreensaverService",
            ("secure", "screensaver_enabled"): "1",
            ("system", "screen_off_timeout"): "300000",
            ("secure", "sleep_timeout"): "840000",
        }
        self.settings.update(settings or {})
        self.calls = []

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        out = ""
        args = argv[1:]
        if args[0] == "connect":
            out = f"connected to {args[1]}" if self.reachable else \
                f"failed to connect to '{args[1]}': Connection refused"
        elif args[0] == "disconnect":
            out = "disconnected"
        elif args[2] == "get-state":
            out = ("device" if self.authorized else "unauthorized") if self.reachable else "error: device not found"
        elif args[2] == "install":
            self.installed = True
            out = "Performing Streamed Install\nSuccess\n"
        elif args[2] == "shell":
            out = self.shell(args[3])
        return subprocess.CompletedProcess(argv, 0, out, "")

    def shell(self, cmd):
        if cmd.startswith("pm path"):
            return "package:/data/app/com.thomas.ladybird-1/base.apk\n" if self.installed else ""
        if cmd.startswith("appops get"):
            return f"SYSTEM_ALERT_WINDOW: {self.overlay}; time=+1d\n"
        if cmd.startswith("appops set"):
            if self.sticky:
                self.overlay = "allow"
            return ""
        if cmd.startswith("settings get"):
            _, _, ns, key = cmd.split()
            return self.settings.get((ns, key), "null") + "\n"
        if cmd.startswith("settings put"):
            _, _, ns, key, val = cmd.split(maxsplit=4)
            if self.sticky:
                self.settings[(ns, key)] = val.strip("'")
            return ""
        if cmd.startswith("dumpsys window"):
            return self.focus
        if cmd.startswith("dumpsys media_session"):
            return self.media
        if cmd.startswith("am start"):
            return "Starting: Intent { cmp=com.thomas.ladybird/.MainActivity (has extras) }\n"
        return ""


def run(fake, mode="check", secrets=None, state=None, **tvover):
    adb = tvf.Adb("192.168.0.18:5555", fake)
    st = {} if state is None else state
    return tvf.converge(adb, tv(**tvover), secrets or {}, st, mode=mode), st


# ---------------------------------------------------------------- inventory

def test_inventory_topics_and_serial():
    t = tv()
    assert t["serial"] == "192.168.0.18:5555"
    assert t["topics"] == ["ladybird-house", "ladybird-tv-living", "extra"]


@pytest.mark.parametrize("bad", [
    {"name": "Living Room", "room": "living", "ip": "1.2.3.4"},
    {"name": "x", "room": "living"},
])
def test_inventory_rejects_bad_entries(bad):
    with pytest.raises(ValueError):
        tvf.parse_inventory({"tvs": [bad]})


def test_inventory_rejects_duplicates():
    t = {"name": "a", "room": "a", "ip": "1.2.3.4"}
    with pytest.raises(ValueError):
        tvf.parse_inventory({"tvs": [t, dict(t)]})


def test_secrets_file_and_env_override(tmp_path, monkeypatch):
    f = tmp_path / "tv-fleet.env"
    f.write_text("# c\nNTFY_TV_TOKEN='tk_file'\nNTFY_PHONE_URL=https://ntfy.sh/x\n")
    monkeypatch.setenv("NTFY_TV_TOKEN", "tk_env")
    s = tvf.load_secrets(str(f))
    assert s["NTFY_TV_TOKEN"] == "tk_env"
    assert s["NTFY_PHONE_URL"] == "https://ntfy.sh/x"


# ---------------------------------------------------------------- parsers

def test_parse_focus():
    assert tvf.parse_focus(FOCUS_HOME) == ("com.amazon.tv.launcher", "com.amazon.tv.launcher.ui.HomeActivity_vNext")
    assert tvf.parse_focus("  mCurrentFocus=null\n") == (None, None)
    assert tvf.parse_focus("mCurrentFocus=Window{1 u0 com.x/.Main}")[1] == "com.x.Main"


def test_stale_youtube_state3_is_not_playing():
    # The real TV 1 case: YouTube state=3 but inactive, profile picker on screen.
    sessions = tvf.parse_media_sessions(MEDIA_STALE_YT)
    assert [s["state"] for s in sessions] == [3, 0, None, None]
    assert tvf.playing_session(sessions, "com.amazon.ftv.profilepicker") is None


def test_state3_counts_when_active_or_foreground():
    assert tvf.playing_session(tvf.parse_media_sessions(MEDIA_SLING_PLAYING), "x") == "com.sling"
    assert tvf.playing_session(tvf.parse_media_sessions(MEDIA_STALE_YT),
                               "com.amazon.firetv.youtube") == "com.amazon.firetv.youtube"


def test_paused_is_not_playing():
    paused = MEDIA_SLING_PLAYING.replace("state=3", "state=2")
    assert tvf.playing_session(tvf.parse_media_sessions(paused), "com.sling") is None


# ---------------------------------------------------------------- converge

def test_unreachable_is_skipped_not_failed():
    rep, _ = run(FakeTV(reachable=False))
    assert rep.status == "SKIPPED" and not rep.failures


def test_unauthorized_is_a_clear_failure():
    rep, _ = run(FakeTV(authorized=False))
    assert rep.status == "FAILED"
    assert "Allow USB debugging" in rep.failures[0]


def test_check_repairs_drift_and_is_idempotent():
    fake = FakeTV()
    rep, _ = run(fake)
    assert rep.status == "CHANGED"
    assert fake.overlay == "allow"
    assert fake.settings[("secure", "screensaver_components")] == "com.thomas.ladybird/.LadybirdDream"
    assert fake.settings[("system", "screen_off_timeout")] == "600000"
    rep2, _ = run(fake)
    assert rep2.status == "OK" and not rep2.changed
    assert not any("am start" in " ".join(c) for c in fake.calls)  # check never launches the app


def test_settings_that_wont_stick_fail():
    rep, _ = run(FakeTV(sticky=False))
    assert rep.status == "FAILED"
    assert any("won't stick" in f for f in rep.failures)


def test_sleep_before_screensaver_warns():
    rep, _ = run(FakeTV(settings={("secure", "sleep_timeout"): "300000"}))
    assert any("sleep before the screensaver" in w for w in rep.warnings)


def test_missing_app_without_apk_fails():
    rep, _ = run(FakeTV(installed=False), apk="/nonexistent.apk")
    assert rep.status == "FAILED"


def test_provision_installs_pushes_config_and_records_sha(tmp_path):
    apk = tmp_path / "ladybird.apk"
    apk.write_bytes(b"apk-v1")
    fake = FakeTV(installed=False)
    rep, st = run(fake, mode="provision", secrets={"NTFY_TV_TOKEN": "tk_x"}, apk=str(apk))
    assert rep.status == "CHANGED", rep.render()
    assert st["apk_sha256"] == tvf.sha256(str(apk))
    am = [c for c in fake.calls if c[-1].startswith("am start")]
    assert len(am) == 1
    assert "--es ntfy_topics ladybird-house,ladybird-tv-living,extra" in am[0][-1]
    assert "--es ntfy_token tk_x" in am[0][-1]
    # Re-provision with the same APK: no reinstall.
    fake.calls.clear()
    rep2, _ = run(fake, mode="provision", secrets={"NTFY_TV_TOKEN": "tk_x"}, state=st, apk=str(apk))
    assert not any(c[3] == "install" for c in fake.calls if len(c) > 3)


def test_check_reinstall_defers_config_while_watching(tmp_path, monkeypatch):
    monkeypatch.setattr(tvf.time, "sleep", lambda _s: None)
    apk = tmp_path / "ladybird.apk"
    apk.write_bytes(b"apk-v1")
    fake = FakeTV(installed=False, focus=FOCUS_YT)
    rep, st = run(fake, apk=str(apk))
    assert st.get("pending_config") is True
    assert not any(c[-1].startswith("am start") for c in fake.calls)
    fake.focus = FOCUS_HOME
    fake.calls.clear()
    rep2, st2 = run(fake, state=st, apk=str(apk))
    assert "pending_config" not in st2
    assert any(c[-1].startswith("am start") for c in fake.calls)
    assert any(c[-1] == "input keyevent KEYCODE_HOME" for c in fake.calls)


def test_config_intent_quotes_values():
    cmd, has_token = tvf.config_intent(tv(room="living", dashboard_url="http://x/?a=1&b=2"), {})
    assert "'http://x/?a=1&b=2'" in cmd
    assert not has_token and "ntfy_token" not in cmd


# ---------------------------------------------------------------- watchdog bookkeeping

def test_record_check_dedupes_and_recovers():
    st = {}
    fail = tvf.Report("living", failures=["overlay grant did not stick"])
    assert len(tvf.record_check(fail, st, 1.0)) == 1
    assert tvf.record_check(fail, st, 2.0) == []          # same failure: no repeat
    assert st["consecutive_failures"] == 2
    ok = tvf.Report("living", ok=["x"])
    msgs = tvf.record_check(ok, st, 3.0)
    assert [m[0] for m in msgs] == ["TV living: recovered"]
    assert st["consecutive_failures"] == 0


def test_record_check_unreachable_counts_silently():
    st = {}
    for i in range(3):
        assert tvf.record_check(tvf.Report("living", skipped="refused"), st, float(i)) == []
    assert st["consecutive_unreachable"] == 3


# ---------------------------------------------------------------- status / MQTT

def test_status_round_publishes_state_and_availability():
    sent = []
    tvf.status_round([tv()], sent.extend, runner=FakeTV(media=MEDIA_SLING_PLAYING), quiet=True)
    topics = dict(sent)
    assert topics["ladybird/tv/living/availability"] == "online"
    assert '"app": "Home"' in topics["ladybird/tv/living/state"]
    assert '"playing": true' in topics["ladybird/tv/living/state"]


def test_status_round_offline():
    sent = []
    tvf.status_round([tv()], sent.extend, runner=FakeTV(reachable=False), quiet=True)
    assert sent == [("ladybird/tv/living/availability", "offline")]


def test_discovery_ids():
    msgs = dict(tvf.discovery_messages(tv(name="den-tv")))
    assert "homeassistant/sensor/ladybird_tv_den_tv/app/config" in msgs
    assert "homeassistant/binary_sensor/ladybird_tv_den_tv/playing/config" in msgs
