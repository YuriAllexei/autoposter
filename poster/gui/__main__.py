"""Entry point: `python -m poster.gui [--port 8765] [--open]`.

Mirrors `poster.main`'s conventions: argparse, config loaded from .env via
`load_config()`, UTC-timestamped logging. Two deliberate differences:

* the port defaults to 8765 and the host is NOT configurable — see
  `poster.gui.server.create_server` for why loopback-only is not a knob.
* opening the page in a browser is opt-in (`--open`), because a dashboard
  started over SSH/CI must not try to launch anything.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from ..config import load_config
from .runner import RingBuffer, RunManager
from .server import create_server
from .state import GuiPaths, identity_view

DEFAULT_PORT = 8765


def _on_wsl() -> bool:
    """True inside WSL (the only host shape this project runs on where the
    user's real browser is on the Windows side)."""
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def open_in_browser(url: str) -> str:
    """Open the dashboard URL in the user's browser; returns the channel
    used (for the log line). webbrowser.open inside WSL hits xdg-open,
    which knows no browser there (user's is Windows-side) — so on WSL the
    first attempt is `cmd.exe /c start`."""
    if _on_wsl():
        for exe in ("cmd.exe", "/mnt/c/Windows/System32/cmd.exe"):
            try:
                subprocess.run([exe, "/c", "start", "", url],
                               check=False, timeout=10, capture_output=True)
                return f"windows ({exe})"
            except (OSError, subprocess.SubprocessError):
                continue
    try:
        if webbrowser.open(url):
            return "webbrowser"
    except webbrowser.Error:
        pass
    return "none — open the URL by hand"

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="poster.gui",
        description="Local control dashboard for the autoposter pipelines "
                    "(reads on-disk artifacts, spawns the CLI for runs)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"loopback port to bind (default {DEFAULT_PORT}; "
                         "0 picks an ephemeral port)")
    ap.add_argument("--open", dest="open_browser", action="store_true",
                    help="open the dashboard in the default browser once it is up")
    ap.add_argument("--root", default=None,
                    help="override the capture root (default: the parent of "
                         "AP_SCREENSHOT_DIR, i.e. .local-capture)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    paths = (GuiPaths.from_root(args.root) if args.root
             else GuiPaths.from_config(cfg))
    buffer = RingBuffer()
    manager = RunManager(cfg.screenshot_dir.parent, buffer)
    httpd = create_server(paths, manager, identity_view(cfg), port=args.port)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    stamp = datetime.now(UTC).strftime("%H:%M:%S")
    print(f"[{stamp}] autoposter gui listening on {url}")
    print(f"[{stamp}] running as {cfg.identity_label!r} · "
          f"AP_DRY_RUN={'true' if cfg.dry_run else 'false'} · "
          f"capture root {paths.capture_root}")
    print(f"[{stamp}] available modes: "
          + ", ".join(f"{m}={ok}" for m, ok in manager.available().items()))
    print(f"[{stamp}] Ctrl-C to stop (runs already started keep their own "
          "process group; use the Kill button to stop one)")
    if args.open_browser:
        print(f"[{stamp}] --open: browser via {open_in_browser(url)}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[{datetime.now(UTC).strftime('%H:%M:%S')}] shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
