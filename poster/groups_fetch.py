"""Dynamic discovery of the groups the ACTIVE identity has joined.

Ground truth: recording 20260922T214219Z_groups_fetcher (Carmazon Alex
profile). The "Tus grupos" tab at
https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added
loads its list from ONE authenticated GraphQL query:

    POST https://www.facebook.com/api/graphql/
    doc_id = 24648931168042404
    fb_api_req_friendly_name = GroupsCometJoinsRootQuery
    variables = {"ordering":["viewer_added"],"scale":1}   (+ "after": cursor)

    response: data.viewer.all_joined_groups.tab_groups_list
                .edges[].node { id, name, url, viewer_join_state,
                                viewer_last_visited_time, ... }
                .page_info { end_cursor, has_next_page }   (20 per page)

Implementation: we run fetch() INSIDE the logged-in page so cookies and
FB's own anti-CSRF tokens (DTSGInitialData/LSD globals; jazoest is the
classic "2"+sum(charCodes of dtsg)) come from the page itself — nothing
secret is read, copied or logged. The cursor loop mirrors what the
infinite-scroll on /groups/joins/ does.

NOTE: joined-groups belong to the ACTING identity (profile vs page lists
differ), so this must run AFTER ensure_active_profile with av of that same
identity — one truth, one av.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict

from playwright.async_api import Page

DOC_ID_JOINS = "24648931168042404"
FRIENDLY_NAME = "GroupsCometJoinsRootQuery"
# 'load more' doc the joins-tab itself uses (sniffed live 2026-09-22)
PAG_DOC_ID = "9974006939348139"
PAG_FRIENDLY = "GroupsCometAllJoinedGroupsSectionPaginationQuery"
GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
MAX_PAGES = 25  # 25*20 = 500 groups ceiling; guard vs cursor loops


class JoinedGroup(TypedDict):
    id: str
    name: str
    url: str
    last_visited: int | None  # informational only (saved snapshot)


class GroupsFetchError(RuntimeError):
    pass


# Runs in the page context. Returns {groups:[...], pages} or {error:...}.
#
# Pagination [sniffed live 2026-09-22, scroll on /groups/joins/]: the ROOT
# query ignores any `after` we invent (returns page 1 forever, has_next=true
# — measured). The UI's "load more" is a SECOND doc:
#   GroupsCometAllJoinedGroupsSectionPaginationQuery
#   variables = {"count":20,"cursor":<end_cursor>,"ordering":[...],"scale":1}
# so: page 1 = root doc, later pages = pagination doc with cursor.
#
# anti-CSRF tokens: FB Comet exposes them via require('DTSGInitialData'/
# 'LSD').token; older/edge pages inline them in the boot-load JSON
# (["DTSGInitialData",[],{"token":"…"}]) with no window global (confirmed
# that way in recording 20260922T214219Z). Try require -> global -> DOM
# boot-load regex, in that order. Nothing is logged from these values.
TOKEN_PROBE_JS = r"""
() => {
  const grab = (mod, re) => {
    try { const m = window.require && window.require(mod);
          if (m && m.token) return m.token; } catch (e) {}
    try { const g = window[mod]; if (g && g.token) return g.token; } catch (e) {}
    const mm = (document.documentElement.innerHTML || '').match(re);
    return mm ? mm[1] : null;
  };
  return {
    dtsg: grab('DTSGInitialData',
      /"DTSGInitialData",\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"/),
    lsd: grab('LSD',
      /"LSD",\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"/),
  };
}
"""

FETCH_JOINS_JS = r"""
async ({ av, docId, friendly, pagDocId, pagFriendly, maxPages, tokens }) => {
  const { dtsg, lsd } = tokens;
  let jazo = 0; for (const ch of dtsg) jazo += ch.charCodeAt(0);
  jazo = '2' + jazo;
  let after = null, pages = 0;
  const seen = new Set();
  const groups = [];
  const pick = (j) => {
    const v = j && j.data && (j.data.viewer || j.data.node);
    const l = v && v.all_joined_groups && v.all_joined_groups.tab_groups_list;
    if (l) return l;
    // pagination doc may root the list elsewhere — hunt for the shape
    const s = JSON.stringify(j);
    if (s.indexOf('tab_groups_list') < 0) {
      return { __shape: s.slice(0, 260) };
    }
    let found = null;
    (function walk(o) {
      if (found || !o || typeof o !== 'object') return;
      if (o.tab_groups_list && o.tab_groups_list.edges) { found = o.tab_groups_list; return; }
      for (const k of Object.keys(o)) walk(o[k]);
    })(j.data);
    return found || { __shape: s.slice(0, 260) };
  };
  while (pages < maxPages) {
    pages++;
    const isPage1 = pages === 1;
    const variables = isPage1
      ? { ordering: ['viewer_added'], scale: 1 }
      : { count: 20, cursor: after, ordering: ['viewer_added'], scale: 1 };
    const form = new URLSearchParams({
      av, __a: '1', fb_dtsg: dtsg, jazoest: jazo, lsd,
      fb_api_caller_class: 'RelayModern',
      fb_api_req_friendly_name: isPage1 ? friendly : pagFriendly,
      server_timestamps: 'true',
      doc_id: isPage1 ? docId : pagDocId,
      variables: JSON.stringify(variables),
    });
    const resp = await fetch('https://www.facebook.com/api/graphql/', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: form.toString(),
    });
    let text = await resp.text();
    if (text.startsWith(")]}'\n")) text = text.slice(4);
    else if (text.startsWith(")]}'")) text = text.slice(3);
    let json;
    try { json = JSON.parse(text); }
    catch (e) {
      return { error: 'unparsable response (HTTP ' + resp.status + '): '
                    + text.slice(0, 140) };
    }
    const list = pick(json);
    if (!list || list.__shape) {
      return { error: 'unexpected payload shape (page ' + pages + '): '
                    + (list && list.__shape ? list.__shape
                       : JSON.stringify(json).slice(0, 260)) };
    }
    let added = 0;
    for (const e of (list.edges || [])) {
      const n = e.node || {};
      if (n.id && !seen.has(String(n.id))) {
        seen.add(String(n.id));
        groups.push({ id: String(n.id), name: n.name || '',
                      url: n.url || ('https://www.facebook.com/groups/'
                                     + n.id + '/'),
                      last_visited: n.viewer_last_visited_time ?? null });
        added++;
      }
    }
    const pi = list.page_info || {};
    // Stop on the REAL signal: a page that adds nothing means the cursor
    // isn't advancing. Never spin to maxPages.
    if (!added || !pi.has_next_page || !pi.end_cursor
        || pi.end_cursor === after) break;
    after = pi.end_cursor;
  }
  return { groups, pages };
}
"""


async def _wait_tokens(page: Page, timeout_ms: int = 20000) -> dict:
    """Poll TOKEN_PROBE_JS until both anti-CSRF tokens resolve (boot-load
    pages take a moment to render inline). Returns {dtsg, lsd}."""
    import time
    deadline = time.time() + timeout_ms / 1000
    last = {"dtsg": None, "lsd": None}
    while time.time() < deadline:
        try:
            last = await page.evaluate(TOKEN_PROBE_JS) or last
        except Exception:  # noqa: BLE001, S110 - evaluate can throw mid-
            pass  # navigation; retry until deadline, then GroupsFetchError
        if last.get("dtsg"):
            return last
        await page.wait_for_timeout(700)
    return last


async def fetch_joined_groups(page: Page, *, av: str,
                              log: Callable[[str], None] | None = None,
                              max_pages: int = MAX_PAGES) -> list[JoinedGroup]:
    """All groups the identity `av` has joined (follows the joins-tab
    pagination). Raises GroupsFetchError on any auth/shape problem —
    callers must treat it as FATAL (run aborts, exit 4 + Discord);
    a failed fetch must never be mistaken for 'the account joined 0'."""
    if not av:
        raise GroupsFetchError("fetch needs the acting identity's av id")

    def _log(m: str) -> None:
        if log:
            log(m)

    # Straight to the joins tab — it renders the boot-load tokens and is
    # what the recording used (an extra home-page load bought nothing).
    if "/groups/joins" not in (page.url or ""):
        await page.goto("https://www.facebook.com/groups/joins/"
                        "?nav_source=tab&ordering=viewer_added",
                        wait_until="domcontentloaded", timeout=60000)
    tokens = await _wait_tokens(page)
    if not tokens.get("dtsg"):
        raise GroupsFetchError(
            f"DTSG token never appeared at {page.url!r} — "
            "not logged in, or FB changed its boot globals")
    res = await page.evaluate(FETCH_JOINS_JS, {
        "av": av, "docId": DOC_ID_JOINS, "friendly": FRIENDLY_NAME,
        "pagDocId": PAG_DOC_ID, "pagFriendly": PAG_FRIENDLY,
        "maxPages": max_pages, "tokens": {"dtsg": tokens["dtsg"],
                                          "lsd": tokens.get("lsd") or ""}})
    if not isinstance(res, dict) or "error" in res:
        raise GroupsFetchError(f"joins fetch failed: {(res or {}).get('error')}")
    groups: list[JoinedGroup] = res["groups"]
    _log(f"joins: {len(groups)} group(s) over {res['pages']} page(s) "
         f"for av={av}")
    return groups
