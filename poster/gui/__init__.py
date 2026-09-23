"""Localhost control dashboard for the autoposter pipelines (stdlib only).

A read-mostly web UI that answers "what does the bot know, what did it do, and
which groups/rounds are next?" without ever opening a browser itself:
it parses the artifacts finished runs left on disk and delegates every action
to the proven CLI as a subprocess.

WHY stdlib only: the repo's single runtime dependency is playwright, and the
dashboard must keep working (and keep its unit tests runnable) even when the
browser stack is unavailable — it is a viewer over files, not an automation
client. See `poster.gui.state` for the read path and `poster.gui.runner` for
the write path.
"""

__all__ = ["page", "runner", "server", "state"]
