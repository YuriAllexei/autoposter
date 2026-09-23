"""Unit + smoke tests for the localhost dashboard.

Pure-logic coverage (ledger tolerance, confirm gate, ring buffer, rotation and
state assembly over tmp fixtures) runs without a browser, without playwright
and without network. One live smoke test spins the real HTTP server on an
ephemeral port and talks to it over urllib.

Fixtures are SYNTHETIC (tmp_path trees mirroring the on-disk shapes) so the
tests never read, mutate or depend on the real .local-capture artifacts.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from poster.gui import runner as runner_mod
from poster.gui import state as state_mod
from poster.gui.page import render_page
from poster.gui.runner import (
    CONFIRM_PHRASE,
    MODE_SPECS,
    RingBuffer,
    RunBusy,
    RunManager,
    RunUnavailable,
    build_command,
    has_cli_main,
    live_confirmation_error,
)
from poster.gui.server import create_server
from poster.gui.state import (
    GuiPaths,
    build_state,
    parse_ledger_line,
    read_groups,
    read_ledger,
)

G1, G2, G3 = "111111111111111", "222222222222222", "333333333333333"
L1 = "1923574311937261"

IDENTITY = {"label": "Carmazon", "id": "61592323007979", "post_as": "page",
            "env_dry_run": True, "max_posts_per_run": 2}


# --------------------------------------------------------------------------
# fixtures on disk
# --------------------------------------------------------------------------

def write_groups_snapshot(root: Path, stamp: str = "20260922T235134Z") -> Path:
    groups = [
        {"id": G1, "name": "VENTAS DE CARROS 💥 CHIHUAHUA",
         "url": f"https://www.facebook.com/groups/{G1}/", "last_visited": 1790112313},
        {"id": G2, "name": 'Autos "baratos" (Chihuahua)',
         "url": f"https://www.facebook.com/groups/{G2}/", "last_visited": 1790117960},
        {"id": G3, "name": "Compra venta de autos Chihuahua",
         "url": f"https://www.facebook.com/groups/{G3}/", "last_visited": 1790117997},
    ]
    out = root / "groups" / f"joined_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(groups, ensure_ascii=False), encoding="utf-8")
    return out


def write_listings(root: Path) -> Path:
    data = [{"id": L1, "title": "2019 Chevrolet Tahoe LT", "price": "$340.000",
             "approved": True, "rejected": False}]
    out = root / "cache" / "listings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


def write_ledger(root: Path, lines: list[dict | str]) -> Path:
    out = root / "results" / "ledger.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        (line if isinstance(line, str) else json.dumps(line)) + "\n"
        for line in lines)
    out.write_text(body, encoding="utf-8")
    return out


def group_line(gid: str, name: str, status: str, ts: str, run_id: str = "R1",
               dry: bool = True, error: str | None = None) -> dict:
    return {"run_id": run_id, "ts": ts, "group_id": gid, "name": name,
            "status": status, "error": error, "dry_run": dry}


def crosspost_line(ts: str, groups: list[str], run_id: str = "R2",
                   listing: str = L1, count: int | None = None,
                   status: str = "published", dry: bool = False) -> dict:
    return {"run_id": run_id, "ts": ts, "listing_id": listing,
            "listing_title": "2019 Chevrolet Tahoe LT", "batch": 1,
            "group_ids": groups, "group_names": ["a", "b"],
            "count": len(groups) if count is None else count,
            "status": status, "error": None, "dry_run": dry}


def populated_root(tmp_path: Path) -> Path:
    write_groups_snapshot(tmp_path)
    write_listings(tmp_path)
    write_ledger(tmp_path, [
        group_line(G1, "VENTAS", "staged", "2026-09-19T00:00:51+00:00"),
        group_line(G2, "Autos baratos", "published", "2026-09-19T00:36:49+00:00",
                   dry=False),
        "corrupt-not-json",
        group_line(G2, "Autos baratos", "skipped", "2026-09-22T23:51:59+00:00",
                   error="no composer within 5s (marketplace tab 'Vender algo')"),
        crosspost_line("2026-09-23T02:00:00+00:00", [G1, G2]),
    ])
    return tmp_path


# --------------------------------------------------------------------------
# ledger tolerance
# --------------------------------------------------------------------------

def test_parse_group_line():
    line = parse_ledger_line(json.dumps(
        group_line(G1, "VENTAS 💥", "staged", "2026-09-19T00:00:51+00:00")))
    assert line is not None
    assert line.kind == state_mod.KIND_GROUP
    assert line.group_id == G1 and line.status == "staged" and line.dry_run is True


def test_parse_crosspost_line():
    line = parse_ledger_line(json.dumps(crosspost_line(
        "2026-09-23T02:00:00+00:00", [G1, G2], count=2)))
    assert line is not None
    assert line.kind == state_mod.KIND_CROSSPOST
    assert line.listing_id == L1 and line.batch == 1 and line.count == 2
    assert line.group_ids == [G1, G2] and line.listing_title.startswith("2019")


def test_group_id_wins_when_both_present():
    """The contract orders group_id first — a line carrying both is a group
    attempt, never double-counted as a crosspost."""
    line = parse_ledger_line(json.dumps(
        {"group_id": G1, "listing_id": L1, "status": "published"}))
    assert line is not None and line.kind == state_mod.KIND_GROUP


def test_parse_tolerates_junk():
    assert parse_ledger_line("") is None
    assert parse_ledger_line("   \n") is None
    assert parse_ledger_line("not json at all") is None
    assert parse_ledger_line("[1, 2, 3]") is None        # valid JSON, not an object
    assert parse_ledger_line('{"ts": "x"}') is not None  # parses, kind=unknown


def test_unknown_kind_is_kept_not_guessed():
    line = parse_ledger_line(json.dumps({"run_id": "R", "status": "weird"}))
    assert line is not None and line.kind == state_mod.KIND_UNKNOWN


def test_crosspost_field_coercions():
    """A future/looser writer may send group_ids as a bare string and count
    as a numeric string; neither may crash the view."""
    line = parse_ledger_line(json.dumps(
        {"listing_id": L1, "group_ids": G1, "group_names": None, "count": "3",
         "batch": "2"}))
    assert line is not None
    assert line.group_ids == [G1] and line.group_names == []
    assert line.count == 3 and line.batch == 2


def test_read_ledger_skips_corrupt_and_blank(tmp_path):
    path = write_ledger(tmp_path, [
        group_line(G1, "a", "staged", "2026-09-19T00:00:51+00:00"), "",
        "}{", crosspost_line("2026-09-23T02:00:00+00:00", [G1]),
    ])
    lines = read_ledger(path)
    assert [x.kind for x in lines] == [state_mod.KIND_GROUP,
                                       state_mod.KIND_CROSSPOST]


def test_read_ledger_missing_file_is_empty(tmp_path):
    assert read_ledger(tmp_path / "nope.jsonl") == []


# --------------------------------------------------------------------------
# confirm gate
# --------------------------------------------------------------------------

def test_dry_run_needs_no_confirmation():
    assert live_confirmation_error({"mode": "groups", "live": False}) is None
    assert live_confirmation_error({}) is None


def test_live_run_requires_the_exact_phrase():
    assert live_confirmation_error({"live": True}) is not None
    assert live_confirmation_error({"live": True, "confirm": ""}) is not None
    assert live_confirmation_error({"live": True, "confirm": "publicar"}) is not None
    assert live_confirmation_error({"live": True, "confirm": "PUBLICA"}) is not None
    assert live_confirmation_error({"live": True, "confirm": "PUBLICARX"}) is not None


def test_live_run_accepts_the_phrase():
    assert live_confirmation_error({"live": True,
                                    "confirm": CONFIRM_PHRASE}) is None
    assert live_confirmation_error({"live": True, "confirm": " PUBLICAR "}) is None
    assert CONFIRM_PHRASE in (live_confirmation_error({"live": True}) or "")


# --------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------

def test_dry_commands_carry_the_dry_flag():
    assert build_command("groups", False, "PY") == ["PY", "-m", "poster.main", "--dry-run"]
    assert build_command("crosspost", False, "PY") == [
        "PY", "-m", "poster.crosspost", "--dry-run"]


def test_live_commands_omit_the_flag():
    """Live must NEVER synthesise --live: poster.main only goes live when .env
    already says AP_DRY_RUN=false, so .env stays the single authorisation."""
    assert build_command("groups", True, "PY") == ["PY", "-m", "poster.main"]
    assert build_command("crosspost", True, "PY") == ["PY", "-m", "poster.crosspost"]
    assert "--live" not in " ".join(build_command("groups", True, "PY"))


def test_refresh_groups_uses_list_groups():
    assert build_command("groups-refresh", False, "PY") == [
        "PY", "-m", "poster.main", "--list-groups"]


def test_read_only_modes_refuse_live():
    with pytest.raises(runner_mod.RunRejected):
        build_command("groups-refresh", True, "PY")
    with pytest.raises(runner_mod.RunRejected):
        build_command("nonsense", False, "PY")


def test_module_available_is_false_for_missing_modules():
    assert runner_mod.module_available("poster.gui") is True
    assert runner_mod.module_available("definitely_not_a_module_xyz") is False


def test_has_cli_main_requires_the_main_guard(tmp_path, monkeypatch):
    """`python -m x` only does something when x has an `if __name__` guard;
    the probe is what stops the dashboard offering an inert refresh button."""
    without = tmp_path / "probe_mod_without_guard.py"
    without.write_text("def fetch():\n    return 1\n", encoding="utf-8")
    with_guard = tmp_path / "probe_mod_with_guard.py"
    with_guard.write_text(
        "def main():\n    return 0\n\n\nif __name__ == '__main__':\n    main()\n",
        encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert has_cli_main("probe_mod_without_guard") is False
    assert has_cli_main("probe_mod_with_guard") is True
    assert has_cli_main("definitely_not_a_module_xyz") is False
    # the real repo: poster.main is runnable, and that is the mode we spawn
    assert has_cli_main("poster.main") is True


# --------------------------------------------------------------------------
# ring buffer
# --------------------------------------------------------------------------

def test_ring_buffer_since_returns_new_lines_once():
    buf = RingBuffer(capacity=10)
    buf.append("one")
    buf.append("two")
    first = buf.since(0)
    assert first["lines"] == ["one", "two"] and first["next"] == 2
    assert first["dropped"] is False
    buf.append("three")
    second = buf.since(first["next"])
    assert second["lines"] == ["three"] and second["next"] == 3
    assert buf.since(3)["lines"] == []


def test_ring_buffer_evicts_and_reports_the_gap():
    buf = RingBuffer(capacity=3)
    for i in range(5):
        buf.append(f"line{i}")
    assert len(buf) == 3
    tail = buf.since(0)
    assert tail["lines"] == ["line2", "line3", "line4"]
    assert tail["dropped"] is True and tail["first"] == 2
    assert buf.since(2)["dropped"] is False


def test_ring_buffer_strips_newlines_and_clear_resets_view():
    buf = RingBuffer()
    buf.append("hello\n")
    assert buf.since(0)["lines"] == ["hello"]
    buf.clear()
    assert len(buf) == 0


# --------------------------------------------------------------------------
# RunManager (no real CLI spawned)
# --------------------------------------------------------------------------

class FakeProc:
    """Stands in for subprocess.Popen: lines then optional blocking gate.

    `exiting=False` models a child that is still alive (so `kill()` has
    something to signal), whatever the pump thread thinks.
    """

    def __init__(self, lines, pid: int = 4321, gate: threading.Event | None = None,
                 exiting: bool = True):
        self.pid = pid
        self.returncode: int | None = None
        self.stdout: object = _gated(lines, gate)
        self.terminated = False
        self.exiting = exiting

    def poll(self) -> int | None:
        return self.returncode if self.exiting else None

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


def _gated(lines, gate):
    yield from lines
    if gate is not None:
        gate.wait(5)


def test_manager_streams_child_output_into_the_buffer(tmp_path):
    bufs = RingBuffer()
    proc = FakeProc(["[run] mode: DRY RUN\n", "[group] VENTAS\n"])
    mgr = RunManager(tmp_path, bufs, python="PY", probe=lambda m: True,
                     popen=lambda *a, **k: proc)
    started = mgr.start("groups", live=False)
    assert started["pid"] == 4321 and started["live"] is False
    assert mgr.wait_idle(5) is True
    text = "\n".join(bufs.since(0)["lines"])
    assert "starting group posting run (dry)" in text
    assert "$ PY -m poster.main --dry-run" in text
    assert "[group] VENTAS" in text
    assert "finished (groups dry) rc=0" in text
    assert mgr.status()["running"] is False and mgr.status()["rc"] == 0


def test_manager_shutdown_sigkills_stubborn_child(tmp_path, monkeypatch):
    """A child ignoring SIGTERM must still die — an orphaned headed Firefox
    would hold the one-owner profile lock forever."""
    import subprocess as sp
    import time

    monkeypatch.setitem(runner_mod.MODE_SPECS, "stubborn", {
        "module": "x", "dry_flag": "", "live_allowed": False,
        "label": "stubborn run"})
    proc = sp.Popen(["sh", "-c", "trap '' TERM; sleep 30"],
                    start_new_session=True)
    mgr = RunManager(tmp_path, RingBuffer(), probe=lambda m: True,
                     popen=lambda cmd, **kw: proc)
    mgr.start("stubborn", live=False)
    assert mgr.shutdown(timeout=1.0) is True
    for _ in range(60):
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    assert proc.poll() is not None, "stubborn child survived SIGKILL escalation"


def test_open_in_browser_prefers_windows_cmd_on_wsl(monkeypatch):
    import poster.gui.__main__ as gm
    calls = []
    monkeypatch.setattr(gm, "_on_wsl", lambda: True)
    monkeypatch.setattr(gm.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd))
    assert "windows" in gm.open_in_browser("http://127.0.0.1:8765/")
    assert calls[0][:4] == ["cmd.exe", "/c", "start", ""]
    assert calls[0][4] == "http://127.0.0.1:8765/"


def test_open_in_browser_falls_back_when_cmd_missing(monkeypatch):
    import poster.gui.__main__ as gm
    monkeypatch.setattr(gm, "_on_wsl", lambda: True)

    def boom(cmd, **kw):
        raise OSError("no cmd.exe")

    monkeypatch.setattr(gm.subprocess, "run", boom)
    opened = []
    monkeypatch.setattr(gm.webbrowser, "open", lambda u: opened.append(u) or True)
    assert "webbrowser" in gm.open_in_browser("http://x/")
    assert opened == ["http://x/"]


def test_dashboard_exit_kills_the_inflight_run(monkeypatch):
    """USER REQUIREMENT: Ctrl-C (KeyboardInterrupt from serve_forever) must
    take the dashboard down AND terminate an in-flight bot run."""
    import poster.gui.__main__ as gm
    calls: list = []

    class _Httpd:
        server_address = ("127.0.0.1", 8765)
        def serve_forever(self):
            raise KeyboardInterrupt
        def server_close(self):
            calls.append("close")

    class _Mgr:
        def available(self):
            return {"groups": True}
        def shutdown(self, *, timeout: float = 8.0):
            calls.append("kill")   # kill + wait + SIGKILL now live INSIDE
            return True            # RunManager.shutdown (tested separately)

    monkeypatch.setattr(gm, "RunManager", lambda *a, **k: _Mgr())
    monkeypatch.setattr(gm, "create_server", lambda *a, **k: _Httpd())
    #: main() INTENTIONALLY mutates the process signal handlers (SIGTERM ->
    #: KeyboardInterrupt, teardown -> SIG_IGN). Restore them or EVERY later
    #: Popen child inherits SIG_IGN (dispositions do!) and becomes unkillable
    #: — that poisoned the real-signal kill test for exactly 5 seconds.
    import signal as _sig
    _saved = (_sig.getsignal(_sig.SIGINT), _sig.getsignal(_sig.SIGTERM))
    try:
        assert gm.main(["--port", "0"]) == 0
    finally:
        _sig.signal(_sig.SIGINT, _saved[0])
        _sig.signal(_sig.SIGTERM, _saved[1])
    assert calls == ["kill", "close"]


def test_sigterm_walks_the_keyboardinterrupt_path():
    import signal

    import pytest

    import poster.gui.__main__ as gm
    with pytest.raises(KeyboardInterrupt):
        gm._term_to_int(signal.SIGTERM, None)


def test_child_clis_run_from_the_project_root_not_the_capture_root(tmp_path):
    """2026-09-23 live bug: cwd was the capture root (.local-capture), so a
    spawned `-m poster.x` died with ModuleNotFoundError. The repo is not
    installed into site-packages; python -m only sees it when cwd IS the
    project root."""
    seen: dict = {}

    def spy_popen(cmd, **kw):
        seen.update(kw)
        return FakeProc(["done\n"])

    bufs = RingBuffer()
    mgr = RunManager(tmp_path, bufs, python="PY", probe=lambda m: True,
                     popen=spy_popen)
    mgr.start("listings-refresh", live=False)
    assert mgr.wait_idle(5) is True
    import poster.gui.runner as runner_mod
    repo = str(Path(runner_mod.__file__).resolve().parents[2])
    assert seen["cwd"] == repo
    assert tmp_path not in Path(seen["cwd"]).parents  # capture root is NOT it


def test_manager_is_busy_until_the_run_finishes(tmp_path):
    bufs = RingBuffer()
    gate = threading.Event()
    mgr = RunManager(tmp_path, bufs, python="PY", probe=lambda m: True,
                     popen=lambda *a, **k: FakeProc(["[run] hi\n"], gate=gate))
    mgr.start("groups", live=False)
    with pytest.raises(RunBusy):
        mgr.start("groups", live=True)
    gate.set()
    assert mgr.wait_idle(5) is True
    # lock released: a follow-up run is accepted again
    mgr2 = RunManager(tmp_path, bufs, python="PY", probe=lambda m: True,
                      popen=lambda *a, **k: FakeProc(["done\n"]))
    mgr2._lock = mgr._lock          # same lock object = same global invariant
    assert mgr2.start("groups", live=False)["mode"] == "groups"


def test_manager_refuses_absent_cli(tmp_path):
    mgr = RunManager(tmp_path, RingBuffer(), python="PY", probe=lambda m: False,
                     cli_probe=lambda m: False)
    with pytest.raises(RunUnavailable):
        mgr.start("crosspost", live=False)
    with pytest.raises(RunUnavailable):
        mgr.start("listings-refresh", live=False)
    assert mgr.available() == {mode: False for mode in MODE_SPECS}


def test_manager_rejects_unknown_and_readonly_modes(tmp_path):
    mgr = RunManager(tmp_path, RingBuffer(), python="PY", probe=lambda m: True)
    with pytest.raises(runner_mod.RunRejected):
        mgr.start("nope", live=False)
    with pytest.raises(runner_mod.RunRejected):
        mgr.start("groups-refresh", live=True)


def test_manager_spawns_a_real_child_and_streams_its_output(tmp_path, monkeypatch):
    """The genuine Popen path: a real interpreter, real pipes, real exit code.

    A throwaway module stands in for poster.main so the test never opens a
    browser (the shared Firefox profile allows exactly one owner, and a test
    must never be that owner). `project_dir` (NOT the capture root) is the
    child's cwd — that separation is the 2026-09-23 bug fix.
    """
    (tmp_path / "gui_probe_cli.py").write_text(
        '"""throwaway CLI for the dashboard runner test"""\n'
        "import sys\n"
        "if __name__ == '__main__':\n"
        "    print('probe: argv=' + ' '.join(sys.argv[1:]))\n"
        "    print('probe: done')\n",
        encoding="utf-8")
    monkeypatch.setitem(runner_mod.MODE_SPECS, "probe", {
        "module": "gui_probe_cli", "dry_flag": "--dry-run",
        "live_allowed": True, "label": "probe run"})

    buffer = RingBuffer()
    mgr = RunManager(tmp_path, buffer, python=sys.executable,
                     probe=lambda m: True, project_dir=tmp_path)
    started = mgr.start("probe", live=False)
    assert started["command"][-1] == "--dry-run"
    assert mgr.wait_idle(20) is True
    text = "\n".join(buffer.since(0)["lines"])
    assert "probe: argv=--dry-run" in text   # dry flag really reached the child
    assert "probe: done" in text
    assert "rc=0" in text
    assert mgr.status()["rc"] == 0 and mgr.status()["running"] is False


def test_cli_parser_defaults_to_the_documented_port():
    from poster.gui.__main__ import DEFAULT_PORT, build_parser
    args = build_parser().parse_args([])
    assert args.port == DEFAULT_PORT == 8765
    assert args.open_browser is False and args.root is None
    assert build_parser().parse_args(["--port", "0", "--open"]).port == 0


def test_kill_terminates_the_child_process_group(tmp_path):
    """Real signal, real process: the child is spawned in its own session so
    one killpg reaps playwright's whole tree instead of orphaning browsers."""
    sleeper = subprocess.Popen([sys.executable, "-c",
                                "import time; time.sleep(30)"],
                               start_new_session=True)
    gate = threading.Event()
    try:
        bufs = RingBuffer()
        proc = FakeProc([], pid=sleeper.pid, gate=gate, exiting=False)
        mgr = RunManager(tmp_path, bufs, python="PY", probe=lambda m: True,
                         popen=lambda *a, **k: proc)
        mgr.start("groups", live=False)
        assert mgr.kill() is True
        sleeper.wait(timeout=5)
        assert sleeper.returncode == -15        # SIGTERM, delivered to the group
        assert "kill requested" in "\n".join(bufs.since(0)["lines"])
    finally:
        gate.set()
        if sleeper.poll() is None:
            sleeper.kill()


def test_kill_when_idle_is_a_noop(tmp_path):
    mgr = RunManager(tmp_path, RingBuffer(), python="PY", probe=lambda m: True)
    assert mgr.kill() is False


def test_status_reports_a_dead_child_as_not_running(tmp_path):
    bufs = RingBuffer()
    mgr = RunManager(tmp_path, bufs, python="PY", probe=lambda m: True,
                     popen=lambda *a, **k: FakeProc(["x\n"]))
    mgr.start("groups", live=False)
    mgr.wait_idle(5)
    status = mgr.status()
    assert status["running"] is False and status["mode"] == "groups"


# --------------------------------------------------------------------------
# state assembly
# --------------------------------------------------------------------------

def test_missing_artifacts_read_as_not_fetched(tmp_path):
    paths = GuiPaths.from_root(tmp_path)
    assert read_groups(paths)["available"] is False
    st = build_state(paths, IDENTITY)
    assert st["groups"]["available"] is False and st["groups"]["rows"] == []
    assert st["listings"]["available"] is False
    assert st["ledger"]["available"] is False
    assert st["runs"]["recent"] == [] and st["runs"]["logs"] == []
    assert st["rotation"]["total"] == 0 and st["rotation"]["planned"] == []
    assert st["identity"]["label"] == "Carmazon"


def test_groups_snapshot_is_read_with_unicode_names(tmp_path):
    populated_root(tmp_path)
    snap = read_groups(GuiPaths.from_root(tmp_path))
    assert snap["available"] is True and snap["count"] == 3
    assert snap["fetched_at"] == "2026-09-22T23:51:34+00:00"
    assert snap["rows"][0]["name"] == "VENTAS DE CARROS 💥 CHIHUAHUA"
    assert snap["rows"][0]["id"] == G1


def test_newest_snapshot_wins(tmp_path):
    populated_root(tmp_path)
    write_groups_snapshot(tmp_path, "20260923T010000Z")
    snap = read_groups(GuiPaths.from_root(tmp_path))
    assert snap["fetched_at"] == "2026-09-23T01:00:00+00:00"


def test_cache_groups_json_is_honoured_when_newer(tmp_path):
    """The task's `cache/groups.json` path is tolerated alongside
    `groups/joined_*.json`; the freshest of the two wins."""
    populated_root(tmp_path)
    fixed = tmp_path / "cache" / "groups.json"
    fixed.write_text(json.dumps([{"id": G3, "name": "only", "url": "u"}]),
                     encoding="utf-8")
    import os
    os.utime(fixed, (time.time() + 60, time.time() + 60))
    snap = read_groups(GuiPaths.from_root(tmp_path))
    assert snap["count"] == 1 and snap["rows"][0]["id"] == G3


def test_state_assembles_group_rollup_and_rotation(tmp_path):
    populated_root(tmp_path)
    st = build_state(GuiPaths.from_root(tmp_path), IDENTITY)
    rows = {r["id"]: r for r in st["groups"]["rows"]}

    # last-run per group (the most recent attempt, whatever its status)
    assert rows[G1]["last_status"] == "staged" and rows[G1]["published"] == 0
    assert rows[G2]["last_status"] == "skipped" and rows[G2]["published"] == 1
    assert rows[G2]["attempts"] == 2
    assert rows[G3]["last_status"] == "never"
    # a group whose last attempt found no composer is flagged not postable
    assert rows[G2]["postable"] is False and rows[G1]["postable"] is True
    assert "marketplace tab" in (rows[G2]["last_error"] or "")

    # rotation: never-attempted first, then oldest attempt — exactly what
    # poster.results.select_targets would pick for the next run (cap = 2)
    assert st["rotation"]["never_attempted"] == [G3]
    assert st["rotation"]["planned"] == [G3, G1]
    assert rows[G3]["next"] is True and rows[G1]["next"] is True
    assert rows[G2]["next"] is False
    assert st["rotation"]["planned_names"] == ["Compra venta de autos Chihuahua",
                                               "VENTAS DE CARROS 💥 CHIHUAHUA"]


def test_state_counts_ledger_and_crosspost_coverage(tmp_path):
    populated_root(tmp_path)
    st = build_state(GuiPaths.from_root(tmp_path), IDENTITY)
    assert st["ledger"]["available"] is True
    assert st["ledger"]["lines"] == 4          # corrupt line dropped
    assert st["ledger"]["group_lines"] == 3
    assert st["ledger"]["crosspost_lines"] == 1
    assert st["ledger"]["published_total"] == 1

    listing = st["listings"]["rows"][0]
    assert listing["id"] == L1 and listing["title"] == "2019 Chevrolet Tahoe LT"
    assert listing["approved"] is True and listing["rejected"] is False
    assert listing["crossposts"] == 1 and listing["crosspost_groups"] == 2
    assert listing["crosspost_coverage"] == 67      # 2 of 3 joined groups
    assert listing["last_status"] == "published"


def test_state_recent_runs_are_newest_first(tmp_path):
    populated_root(tmp_path)
    st = build_state(GuiPaths.from_root(tmp_path), IDENTITY)
    runs = st["runs"]["recent"]
    assert [r["run_id"] for r in runs] == ["R2", "R1"]
    assert runs[1]["dry_run"] is True and runs[1]["staged"] == 1
    assert runs[1]["published"] == 1 and runs[1]["skipped"] == 1
    assert runs[0]["crossposts"] == 1 and runs[0]["dry_run"] is False
    assert st["ledger"]["recent"][0]["kind"] == state_mod.KIND_CROSSPOST


def test_state_lists_run_logs_newest_first(tmp_path):
    populated_root(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "run_20260922T235421Z.log").write_text("[run] old\n", encoding="utf-8")
    newer = logs / "run_20260923T010000Z.log"
    newer.write_text("[run] new\n", encoding="utf-8")
    st = build_state(GuiPaths.from_root(tmp_path), IDENTITY)
    names = [entry["name"] for entry in st["runs"]["logs"]]
    assert names[0] == newer.name
    assert st["runs"]["logs"][0]["size"] > 0


def test_state_includes_manager_status_and_cap(tmp_path):
    populated_root(tmp_path)
    st = build_state(GuiPaths.from_root(tmp_path), IDENTITY,
                     {"running": True, "mode": "groups", "live": True, "pid": 9})
    assert st["manager"]["pid"] == 9 and st["manager"]["live"] is True
    assert st["rotation"]["cap"] == 2
    assert st["identity"]["max_posts_per_run"] == 2


def test_identity_view_has_no_secrets():
    from poster.config import Config
    view = state_mod.identity_view(Config(post_as="page", fb_posting_user="61592323007979",
                                          fb_posting_profile_name="Carmazon"))
    assert view == {"label": "Carmazon", "id": "61592323007979", "post_as": "page",
                    "env_dry_run": True, "max_posts_per_run": 1}
    assert not any("token" in k or "password" in k for k in view)


# --------------------------------------------------------------------------
# page + live server smoke test
# --------------------------------------------------------------------------

def test_page_is_self_contained():
    html = render_page()
    assert html.startswith("<!DOCTYPE html>") and html.rstrip().endswith("</html>")
    # no external resources: an offline dashboard must never wait on a CDN
    assert "http://" not in html and "https://" not in html
    assert "//cdn" not in html and "src=" not in html
    assert "PUBLICAR" in html and 'id="confirm"' in html
    assert "/api/log?since=" in html.replace(" ", "")


def test_server_refuses_to_bind_beyond_loopback(tmp_path):
    with pytest.raises(ValueError):
        create_server(GuiPaths.from_root(tmp_path),
                      RunManager(tmp_path, RingBuffer()), host="0.0.0.0", port=0)


@pytest.fixture
def live_server(tmp_path):
    """Real HTTP server on an ephemeral port, backed by tmp fixtures."""
    populated_root(tmp_path)
    buffer = RingBuffer()
    manager = RunManager(tmp_path, buffer, python=sys.executable,
                         probe=lambda m: True,
                         cli_probe=lambda m: False)
    httpd = create_server(GuiPaths.from_root(tmp_path), manager, IDENTITY, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", manager, buffer
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def _post(url: str, payload: dict | None, raw: str | None = None):
    body = raw.encode("utf-8") if raw is not None else (
        json.dumps(payload).encode("utf-8") if payload is not None else b"")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def test_live_server_serves_page_and_state(live_server):
    base, _manager, _buffer = live_server
    status, ctype, body = _get(base + "/")
    assert status == 200 and "text/html" in ctype
    assert b"autoposter" in body and b"PUBLICAR" in body

    status, ctype, body = _get(base + "/api/state")
    assert status == 200 and "application/json" in ctype
    state = json.loads(body)
    assert state["groups"]["count"] == 3
    assert state["identity"]["label"] == "Carmazon"
    assert state["ledger"]["lines"] == 4
    assert state["manager"]["running"] is False
    assert state["modes"]["groups"] is True          # injected probe
    assert state["modes"]["listings-refresh"] is False


def test_live_server_log_endpoint_advances_cursor(live_server):
    base, _manager, buffer = live_server
    buffer.append("[gui] synthetic line")
    status, _ctype, body = _get(base + "/api/log?since=0")
    assert status == 200
    data = json.loads(body)
    assert data["lines"] == ["[gui] synthetic line"] and data["next"] == 1
    status, _ctype, body = _get(base + "/api/log?since=1")
    assert json.loads(body)["lines"] == []


def test_live_server_gates_live_runs_without_confirm(live_server):
    base, manager, _buffer = live_server
    status, data = _post(base + "/api/run", {"mode": "groups", "live": True})
    assert status == 403 and "PUBLICAR" in data["error"]
    assert manager.status()["running"] is False      # nothing was spawned

    status, data = _post(base + "/api/run", {"mode": "groups", "live": True,
                                             "confirm": "nope"})
    assert status == 403 and manager.status()["running"] is False


def test_live_server_rejects_unknown_mode(live_server):
    base, manager, _buffer = live_server
    status, data = _post(base + "/api/run", {"mode": "teleport", "live": False})
    assert status == 400 and "unknown mode" in data["error"]
    assert manager.status()["running"] is False


def test_live_server_surfaces_a_missing_cli_as_501(live_server):
    """cli_probe is stubbed False in this fixture, mirroring the current repo:
    poster.listings has no `__main__` guard, so there is no listings CLI to
    reuse and the endpoint answers 501 with the reason instead of pretending."""
    base, manager, _buffer = live_server
    status, data = _post(base + "/api/refresh-listings", None)
    assert status == 501 and "listings" in data["error"]
    assert manager.status()["running"] is False


def test_live_server_kill_and_404s(live_server):
    base, _manager, _buffer = live_server
    status, data = _post(base + "/api/kill", None)
    assert status == 200 and data["killed"] is False
    status, _ctype, body = _get(base + "/api/nope")
    assert status == 404 and "no such endpoint" in json.loads(body)["error"]


def test_live_server_tolerates_a_malformed_body(live_server):
    base, _manager, _buffer = live_server
    status, data = _post(base + "/api/run", None, raw="{not json")
    assert status == 400 and "unknown mode" in data["error"]


# ---- delivery verdict plumbing ("did it go live?") ----

def test_ledger_line_carries_delivered_and_rollup_surfaces_it(tmp_path):
    from poster.gui import state as st
    lines = [
        ('{"run_id":"r1","ts":"2026-09-23T01:00:00+00:00","group_id":"g1",'
         '"name":"G1","status":"published","error":null,"dry_run":false}'),
        ('{"run_id":"r2","ts":"2026-09-23T03:00:00+00:00","group_id":"g1",'
         '"name":"G1","status":"published","error":null,"dry_run":false,'
         '"delivered":"pending"}'),
        ('{"run_id":"r3","ts":"2026-09-23T04:00:00+00:00","group_id":"g1",'
         '"name":"G1","status":"failed","error":"boom","dry_run":false}'),
    ]
    led = tmp_path / "ledger.jsonl"
    led.write_text("\n".join(lines) + "\n", encoding="utf-8")
    parsed = st.read_ledger(led)
    assert parsed[0].delivered is None and parsed[1].delivered == "pending"
    roll = st.group_rollup(parsed)
    # the verdict travels with the newest PUBLISH — a later failure must
    # not erase that the post itself is awaiting review
    assert roll["g1"]["delivered"] == "pending"
    assert roll["g1"]["last_status"] == "failed"


def test_page_shows_live_column_and_legend():
    from poster.gui.page import render_page
    html = render_page()
    assert "<th>live?</th>" in html
    assert "test run" in html and "exit code" in html
    assert "head-flex" in html                  # hint lives in the header row

def test_container_bind_is_the_only_way_off_loopback(tmp_path):
    import pytest as _pt

    from poster.gui.server import create_server
    mgr = RunManager(Path("."), RingBuffer(), python="PY",
                     probe=lambda m: True)
    paths = GuiPaths.from_root(tmp_path)
    # default: loopback only — a bare host can never accidentally LAN-bind
    with _pt.raises(ValueError):
        create_server(paths, mgr, host="0.0.0.0")
    # explicit container mode: allowed (compose maps the HOST side to
    # 127.0.0.1, so this still never meets a network)
    httpd = create_server(paths, mgr, host="0.0.0.0",
                          container_bind=True, port=0)
    try:
        assert httpd.server_address[0] == "0.0.0.0"
    finally:
        httpd.server_close()



def test_bind_container_flag_reaches_the_real_bind(tmp_path, monkeypatch):
    """The container bug this pins: --bind-container used to flip a validator
    ALLOWANCE only; create_server still got host=127.0.0.1, so the published
    port found nobody home inside the container."""
    import poster.gui.__main__ as gm
    seen: dict = {}

    class _Httpd:
        server_address = ("0.0.0.0", 8765)
        def serve_forever(self):
            raise KeyboardInterrupt
        def server_close(self):
            pass

    def fake_create_server(paths, manager, identity, host="127.0.0.1",
                           port=8765, container_bind=False):
        seen["host"] = host
        seen["container_bind"] = container_bind
        return _Httpd()

    monkeypatch.setattr(gm, "create_server", fake_create_server)
    monkeypatch.setattr(gm, "RunManager",
                        lambda *a, **k: __import__("types").SimpleNamespace(
                            available=dict, shutdown=lambda **kw: False))
    import signal as _sig
    held = [_sig.getsignal(_sig.SIGINT), _sig.getsignal(_sig.SIGTERM)]
    try:
        gm.main(["--port", "0", "--bind-container"])
        assert seen["host"] == "0.0.0.0" and seen["container_bind"] is True
        gm.main(["--port", "0"])
        assert seen["host"] == "127.0.0.1" and seen["container_bind"] is False
    finally:
        _sig.signal(_sig.SIGINT, held[0])
        _sig.signal(_sig.SIGTERM, held[1])
