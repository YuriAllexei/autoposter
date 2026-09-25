"""Process control for dashboard actions: one run at a time, streamed output.

WHY subprocess instead of importing the pipelines: `poster.main` and
`poster.crosspost` drive ONE persistent Firefox profile. Importing their code
into the server process would let two browser sessions race for that profile
(and would drag playwright's event loop into an otherwise stdlib-only
dashboard). Spawning the documented CLI keeps the GUI a thin controller and
keeps the "one browser owner" invariant enforceable.

Concurrency is a single global non-blocking lock: a browser run takes minutes,
so a second request must get an immediate 409 rather than queue behind it.
The child is started in its OWN process group/session so `/api/kill` can
terminate the whole tree (playwright's driver + browsers) with one SIGTERM
instead of leaving orphans behind.

Live authorisation is deliberately NOT a CLI flag: `poster.main --live`
refuses unless .env already says AP_DRY_RUN=false, and that file edit stays
the single place a human can authorise publishing. The dashboard therefore
omits `--dry-run` for a live run and never synthesises `--live`.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The gate phrase for live runs: matched EXACTLY (after strip) so muscle-memory
# typos or a stray "y" cannot fire a real publish.
CONFIRM_PHRASE = "PUBLICAR"

# mode -> (module, dry-run flag). `groups-refresh` is the read-only joins
# fetch; it has no live variant and takes the same global lock because it
# opens the same browser profile. `listings-refresh` only becomes available
# once `poster.listings` grows a `__main__` guard (it has none today) — see
# `has_cli_main`.
MODE_SPECS: dict[str, dict[str, Any]] = {
    "groups": {"module": "poster.main", "dry_flag": "--dry-run",
               "live_allowed": True, "label": "Entire Inventory Distribution run"},
    "crosspost": {"module": "poster.crosspost", "dry_flag": "--dry-run",
                  "live_allowed": True, "label": "Initial Post Sharing run"},
    "share": {"module": "poster.share", "dry_flag": "--dry-run",
              "live_allowed": False, "label": "Individual Listing Sequential Group Posting run"},
    "groups-refresh": {"module": "poster.main", "dry_flag": "--list-groups",
                       "live_allowed": False, "label": "joined-groups refresh"},
    "listings-refresh": {"module": "poster.listings", "dry_flag": "",
                         "live_allowed": False, "needs_cli": True,
                         "label": "marketplace listings refresh"},
}


class RunBusy(RuntimeError):
    """Another run already owns the browser/profile — caller answers 409."""


class RunUnavailable(RuntimeError):
    """The pipeline's CLI does not exist on this checkout (e.g. an
    un-landed poster.crosspost) — the UI must say so, not crash."""


class RunRejected(RuntimeError):
    """Request was malformed (unknown mode, live on a read-only mode)."""


def module_available(dotted: str) -> bool:
    """True when the module can be spawned as `python -m <dotted>`.

    `find_spec` can raise (not just return None) when a PARENT package is
    missing, and `finder` is None in embedded interpreters — both mean
    "cannot run it", so they are folded into False here.
    """
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def has_cli_main(dotted: str) -> bool:
    """True when `python -m <dotted>` would actually DO something.

    `find_spec` alone only proves the module imports; the repo's CLI modules
    are runnable because they end in an `if __name__ == "__main__"` guard
    (poster.main, poster.notify, poster.listings). A module
    without the guard would spawn as a silent no-op — this check lets the
    dashboard answer "no CLI on this checkout" instead of pretending.
    """
    try:
        spec = importlib.util.find_spec(dotted)
    except (ImportError, ValueError, AttributeError):
        return False
    origin = getattr(spec, "origin", None) if spec else None
    if not origin:
        return False
    try:
        source = Path(origin).read_text(encoding="utf-8")
    except OSError:
        return False
    return "__name__" in source and "__main__" in source


def build_command(mode: str, live: bool, python: str | None = None) -> list[str]:
    """The exact CLI invocation for a mode. Pure: trivially unit-testable."""
    if mode not in MODE_SPECS:
        raise RunRejected(f"unknown mode {mode!r}")
    spec = MODE_SPECS[mode]
    if live and not spec["live_allowed"]:
        raise RunRejected(f"mode {mode!r} is read-only and cannot run live")
    exe = python or sys.executable
    if mode == "groups-refresh":
        return [exe, "-m", spec["module"], spec["dry_flag"]]
    if mode == "listings-refresh":
        # no flag: a bare `-m poster.listings` is the least-assumption call
        # for a pipeline whose whole job is fetch -> snapshot.
        return [exe, "-m", spec["module"]]
    cmd = [exe, "-m", spec["module"]]
    if not live:
        cmd.append(spec["dry_flag"])
    return cmd


def live_confirmation_error(payload: dict[str, Any]) -> str | None:
    """None when the request may publish; otherwise the reason to refuse it.

    Dry runs need no confirmation. A live run must carry the double-typing
    gate: the browser sends `{"confirm": "PUBLICAR"}`, which it only produces
    after a human typed the phrase into the prompt field.
    """
    if not payload.get("live"):
        return None
    phrase = str(payload.get("confirm") or "").strip()
    if phrase != CONFIRM_PHRASE:
        return (f"live runs require confirm={CONFIRM_PHRASE!r} "
                "(type the phrase to double-confirm)")
    return None


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


class RingBuffer:
    """Bounded in-memory log tail with STABLE line indices.

    The browser polls `/api/log?since=N` and must never miss or duplicate a
    line: indices are monotonically increasing counters, and when the buffer
    evicts old lines it reports the new `first` index so a slow client can
    tell it fell behind instead of silently skipping output.
    """

    def __init__(self, capacity: int = 4000) -> None:
        self.capacity = capacity
        self._lines: deque[str] = deque(maxlen=capacity)
        self._appended = 0
        self._lock = threading.Lock()

    def append(self, line: str) -> int:
        """Add one line; returns the index the NEXT line will get."""
        with self._lock:
            self._lines.append(line.rstrip("\n"))
            self._appended += 1
            return self._appended

    def since(self, index: int) -> dict[str, Any]:
        """Lines with index >= `index`, plus the cursor for the next poll.

        `dropped` is True when the requested index was already evicted — the
        client shows a gap marker rather than pretending it saw everything.
        """
        with self._lock:
            first = self._appended - len(self._lines)
            dropped = index < first
            start = max(index, first)
            offset = start - first
            return {"next": self._appended, "first": first, "dropped": dropped,
                    "lines": list(self._lines)[offset:]}

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


class RunManager:
    """Owns the single child process + the global run lock."""

    def __init__(self, root: Path, buffer: RingBuffer, *, python: str | None = None,
                 probe: Callable[[str], bool] = module_available,
                 cli_probe: Callable[[str], bool] = has_cli_main,
                 popen: Callable[..., Any] = subprocess.Popen,
                 project_dir: Path | None = None) -> None:
        self.root = Path(root)
        #: child CLIs run here (NOT the capture root): `-m poster.<x>` only
        #: resolves when cwd is the project root (the repo is not pip-
        #: installed; python adds '' / cwd to sys.path for -m). Default:
        #: poster/gui/runner.py -> parents[2] == repo root.
        self.project_dir = Path(project_dir) if project_dir else Path(__file__).resolve().parents[2]
        self.buffer = buffer
        self.python = python or sys.executable
        self._probe = probe
        self._cli_probe = cli_probe
        self._popen = popen
        self._lock = threading.Lock()
        self._proc: Any | None = None
        self._state: dict[str, Any] = {
            "running": False, "mode": None, "live": False, "pid": None,
            "started": None, "rc": None, "command": [],
        }
        self._pump_thread: threading.Thread | None = None

    # -- introspection ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        proc = self._proc
        state = dict(self._state)
        if proc is not None and state["running"]:
            state["rc"] = proc.poll()
        return state

    def available(self) -> dict[str, bool]:
        """Which modes this checkout can actually run (crosspost and the
        listings CLI may not exist yet)."""
        return {mode: self._mode_available(mode) for mode in MODE_SPECS}

    def _mode_available(self, mode: str) -> bool:
        module = MODE_SPECS[mode]["module"]
        if MODE_SPECS[mode].get("needs_cli"):
            return self._cli_probe(module)
        return self._probe(module)

    def _require_available(self, mode: str) -> None:
        module = MODE_SPECS[mode]["module"]
        if self._mode_available(mode):
            return
        if MODE_SPECS[mode].get("needs_cli"):
            raise RunUnavailable(
                f"{module} has no CLI entry point on this checkout "
                f"({MODE_SPECS[mode]['label']} unavailable)")
        raise RunUnavailable(
            f"{module} is not present on this checkout "
            f"({MODE_SPECS[mode]['label']} unavailable)")

    # -- control ----------------------------------------------------------
    def start(self, mode: str, live: bool = False) -> dict[str, Any]:
        """Launch one run. Raises RunBusy / RunRejected / RunUnavailable."""
        if mode not in MODE_SPECS:
            raise RunRejected(f"unknown mode {mode!r}")
        spec = MODE_SPECS[mode]
        if live and not spec["live_allowed"]:
            raise RunRejected(f"mode {mode!r} is read-only")
        # Non-blocking: a busy browser gets an immediate 409, never a queue.
        if not self._lock.acquire(blocking=False):
            raise RunBusy("a run is already in progress")
        try:
            self._require_available(mode)
            cmd = build_command(mode, live, self.python)
            self.buffer.append(f"[gui] {utc_stamp()} starting "
                               f"{spec['label']} ({'LIVE' if live else 'dry'})")
            self.buffer.append("[gui] $ " + " ".join(cmd))
            proc = self._popen(
                cmd, cwd=str(self.project_dir), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                bufsize=1, start_new_session=True,
            )
        except BaseException:
            self._lock.release()
            raise
        self._proc = proc
        self._state = {"running": True, "mode": mode, "live": live,
                       "pid": getattr(proc, "pid", None),
                       "started": datetime.now(UTC).isoformat(timespec="seconds"),
                       "rc": None, "command": cmd}
        self._pump_thread = threading.Thread(
            target=self._pump, args=(proc, mode, live), daemon=True,
            name=f"gui-run-{mode}")
        self._pump_thread.start()
        return dict(self._state)

    def kill(self) -> bool:
        """SIGTERM the child's whole process group. False when idle/dead.

        The child is its own session leader (`start_new_session=True`), so its
        pgid equals its pid; killing the group reaps playwright's driver and
        browser children instead of orphaning them.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        pid = getattr(proc, "pid", None)
        try:
            if pid:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            # already gone, or the group was reaped by the OS: fall back to
            # signalling the direct child and report what actually happened
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001 - kill is best-effort by design
                return False
        self.buffer.append(f"[gui] {utc_stamp()} kill requested (pid {pid})")
        return True

    def shutdown(self, *, timeout: float = 8.0) -> bool:
        """Kill the WHOLE dashboard footprint: SIGTERM the running child's
        process group (playwright driver + browser included), wait, escalate
        to SIGKILL if it ignores the polite one. Returns True when a run was
        actually in flight. The Ctrl-C path in __main__ calls this — closing
        the terminal must never orphan a posting browser."""
        killed = self.kill()
        if not killed:
            return False
        if not self.wait_idle(timeout):
            proc = self._proc
            pid = getattr(proc, "pid", None) if proc else None
            try:
                if pid:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                    self.buffer.append(f"[gui] {utc_stamp()} SIGKILL group "
                                       f"{pid} (child ignored SIGTERM)")
            except OSError:
                pass
            self.wait_idle(2.0)
        return True

    def wait_idle(self, timeout: float | None = 5.0) -> bool:
        """Block until the current run's reader thread exits (True) or the
        timeout lapses (False). Used by /api/kill and by tests."""
        thread = self._pump_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # -- internals --------------------------------------------------------
    def _pump(self, proc: Any, mode: str, live: bool) -> None:
        """Stream the child's merged stdout/stderr into the ring buffer, then
        release the lock. Runs on a daemon thread so a client disconnect or
        server shutdown can never wedge the last run's lock."""
        rc: int | None = None
        try:
            stream = getattr(proc, "stdout", None)
            if stream is not None:
                for line in _iter_lines(stream):
                    self.buffer.append(line)
            rc = proc.wait()
        except Exception as e:  # noqa: BLE001 - a broken pipe must not hang the lock
            self.buffer.append(f"[gui] reader error: {type(e).__name__}: {e}")
        finally:
            self._state["running"] = False
            self._state["rc"] = rc
            self.buffer.append(
                f"[gui] {utc_stamp()} finished ({mode}"
                f"{' live' if live else ' dry'}) rc={rc}")
            self._lock.release()


def _iter_lines(stream: Iterable[str]):
    """Line iterator tolerant of a stream that closes mid-read."""
    try:
        yield from stream
    except ValueError:  # stream closed under us
        return
