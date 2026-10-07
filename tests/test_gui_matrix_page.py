"""Markup tests for the operator-facing 'groups matrix' UI in the dashboard.

These are PURE string assertions on render_page(): the page is one inlined
HTML/JS string and the backend endpoint (/api/group-matrix) is owned by
another batch, so nothing here talks to it. What is pinned:

* a per-listing 'groups matrix' button exists and is wired to openMatrix();
* the page POSTs the operator's verdict back to /api/group-matrix;
* the modal's CSS classes ship with the document;
* the group names (user-controlled Facebook data) are NEVER inserted with
  innerHTML and the page never folds names itself (the server's own folded
  `key` values are what travel back);
* the 5s state poll loop still exists (this feature must not break refresh).
"""
from __future__ import annotations

from poster.gui.page import render_page


def test_page_has_groups_matrix_button():
    html = render_page()
    assert "groups matrix" in html
    # wired to the opener
    assert "function openMatrix(" in html
    assert "=> openMatrix(" in html
    # the distinct-modal button class ships with the page
    assert "matrix-btn" in html


def test_page_posts_to_the_group_matrix_endpoint():
    html = render_page()
    assert '"/api/group-matrix"' in html


def test_page_ships_the_matrix_modal_css():
    html = render_page()
    for selector in (".modal {", ".modalbox {", ".modalhead {", ".modalbody {",
                     ".matrixrow {", ".modalfoot {", ".matrix-btn {"):
        assert selector in html, selector


def test_matrix_modal_has_close_and_mass_toggle_controls():
    html = render_page()
    assert "function closeMatrix(" in html
    # openMatrix builds the toggle-all affordances and the live counter
    start = html.index("function openMatrix(")
    nxt = html.find("\nfunction ", start + 1)
    body = html[start:nxt if nxt != -1 else len(html)]
    assert '"all"' in body or "all" in body
    assert "group(s) ON" in body          # footer counter
    assert "Refresh groups" in body       # the not-fetched-yet note
    # close paths: the close button, the backdrop, and Escape
    assert "Escape" in body
    assert "ev.target === root" in body or "target === root" in body


def test_matrix_modal_never_uses_innerhtml():
    html = render_page()
    # the whole page must stay innerHTML-free (verified before this test was
    # written — the current file already satisfies it)
    assert ".innerHTML" not in html
    # ...and specifically the modal body builder must create every label node
    start = html.index("function openMatrix(")
    nxt = html.find("\nfunction ", start + 1)
    body = html[start:nxt if nxt != -1 else len(html)]
    assert "innerHTML" not in body
    assert "textContent" in body or "text(" in body


def test_page_does_not_fold_names_client_side():
    """The server sends its own folded keys; the page must not lowercase /
    casefold group names itself."""
    html = render_page()
    start = html.index("function openMatrix(")
    nxt = html.find("\nfunction ", start + 1)
    body = html[start:nxt if nxt != -1 else len(html)]
    assert "toLowerCase" not in body
    assert "casefold" not in body


def test_page_still_polls_state_every_five_seconds():
    html = render_page()
    assert "setInterval(poll, 5000)" in html
    assert "renderState" in html
    # lastState is kept so the modal can read the freshest snapshot on open
    assert "lastState = st;" in html
