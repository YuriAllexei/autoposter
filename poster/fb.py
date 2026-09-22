"""Facebook browser primitives — selectors & flows proven by recordings/dry-runs.

Selector status legend (repo rule: never act on an unproven selector):
  [proven]  verified against a recording id or a live dry run (see AGENTS.md)
  [robust]  structural (role/type/aria/text matchers), not obfuscated classes

Proven this session (recording 20260917T063422Z, group 249803862915566):
  - composer trigger on group feed: visible text "Escribe algo..."
  - composer modal opens with body hint "Crea una publicación pública..."
  - photo attach: hidden input[type=file] inside the modal dialog; user
    picking a file lands as C:\\fakepath\\... + POST to
    upload.facebook.com/ajax/react_composer/attachments/photo/upload
    => Playwright answers the OS dialog via FileChooser / set_input_files
  - publish button: [role=button] text "Publicar" inside the dialog
  - profile switch to posting identity: trusted Playwright .click() only
    (JS-dispatched .click() ignored by FB React handler)
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page

# ---- proven selector JS (shared with scripts/probe_group.py) ---------------

OPEN_ACCOUNT_MENU_JS = r"""
(() => {
  // FB's avatar button has NO <img> child (CSS background-image) — trust aria-label.
  let btn = document.querySelector('[role="button"][aria-label="Tu perfil"]')
    || document.querySelector('[role="button"][aria-label="Your profile"]');
  if (!btn) {
    const cands = Array.from(document.querySelectorAll('[role="button"]')).filter(el => {
      const r = el.getBoundingClientRect();
      const aria = (el.getAttribute('aria-label') || '').toLowerCase();
      return r.width > 0 && r.top < 90 && r.left > innerWidth * 0.4
        && /perfil|profile|cuenta|account/.test(aria);
    });
    btn = cands[cands.length - 1] || null;
  }
  if (!btn) return 'no-account-button';
  btn.click();
  return 'clicked-account:' + (btn.getAttribute('aria-label') || '');
})()
"""

CLICK_PROFILE_ITEM_JS = r"""
((want) => {
  document.querySelectorAll('[data-ap-switch]').forEach(e => e.removeAttribute('data-ap-switch'));
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const els = Array.from(
    document.querySelectorAll('[role="menuitem"], [role="button"], span, div')
  ).filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && norm(el.innerText) && norm(el.innerText).length < 200
      && norm(el.innerText).includes(want);
  });
  if (!els.length) return 'no-profile-item';
  // "Carmazon" is a substring of "Carmazon Alex" (both rows in the switcher
  // [recording 20260922T210535Z]) — prefer elements whose text is EXACTLY the
  // wanted identity, so we never stamp the wrong row.
  const exact = els.filter(el => norm(el.innerText) === want);
  const pool = exact.length ? exact : els;
  // smallest containing clickable row = the LAST match (most specific/innermost is too
  // small); FB wants the row-level node with a React handler:
  const inner = pool[pool.length - 1];
  const row = inner.closest('[role="menuitem"],[role="button"],[role="listitem"]') || inner;
  row.setAttribute('data-ap-switch', '1');
  const r = row.getBoundingClientRect();
  return 'stamped:' + (exact.length ? 'exact' : 'contains') + ':'
    + row.tagName.toLowerCase() + ':' + row.getAttribute('role')
    + ':' + Math.round(r.x + r.width / 2) + ',' + Math.round(r.y + r.height / 2);
})("%NAME%")
"""

MENU_OPEN_JS_TEMPLATE = r"""
(() => {
  for (const el of document.querySelectorAll('[role="menuitem"],[role="button"],span,div')) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const r = el.getBoundingClientRect();
    if (r.width > 0 && t && t.length < 200 && t.includes('%NAME%')) return true;
  }
  return false;
})()
"""


def menu_open_js(name: str) -> str:
    return MENU_OPEN_JS_TEMPLATE.replace("%NAME%", name)


def click_profile_item_js(name: str) -> str:
    return CLICK_PROFILE_ITEM_JS.replace("%NAME%", name)


# ---- login / identity -------------------------------------------------------

async def cookie_map(ctx: BrowserContext) -> dict[str, str]:
    return {c.get("name"): c.get("value") for c in await ctx.cookies("https://www.facebook.com")}


async def is_authed(ctx: BrowserContext) -> bool:
    return bool((await cookie_map(ctx)).get("c_user"))


async def current_identity(ctx: BrowserContext, page: Page) -> str | None:
    """The acting identity = the `av` (actor viewer) param FB puts on every
    /api/graphql/ request [proven: recording 20260922T210535Z, all 554
    requests carry it; the professional PROFILE switch never sets an i_user
    cookie, so cookies alone cannot tell the identities apart].
    Captured by watching requests; falls back to the page HTML if we have
    not seen a graphql call yet on this page."""
    url = getattr(page, "_ap_av_url", None)
    if not url:
        try:
            html = await page.content()
        except Exception:
            return None
        m = re.search(r'"av":"?(\d{6,})', html)
        return m.group(1) if m else None
    m = re.search(r"[?&]av=(\d{6,})", url)
    return m.group(1) if m else None


async def _track_av(page: Page) -> None:
    """Attach once-per-page listener stashing the latest graphql av=.
    (playwright's sync check in identity code paths is the cookie-less
    truth; this just feeds it)."""
    if getattr(page, "_ap_av_hooked", False):
        return

    def on_request(req) -> None:
        if "/api/graphql" in req.url and "av=" in req.url:
            page._ap_av_url = req.url  # type: ignore[attr-defined]

    page.on("request", on_request)
    page._ap_av_hooked = True  # type: ignore[attr-defined]


async def get_av(ctx: BrowserContext, page: Page) -> str | None:
    await _track_av(page)
    if not getattr(page, "_ap_av_url", None):
        # give in-flight feed requests a moment, then sample
        try:
            await page.wait_for_timeout(2000)
        except Exception:
            return None
    return await current_identity(ctx, page)


def identity_ok(cookies: Mapping[str, str], *, post_as: str,
                posting_user_id: str, main_user_id: str,
                av: str | None = None) -> bool:
    """Pure predicate for 'are we the right identity to post?'.

    av = the acting id from /api/graphql (authoritative, proven recording
    20260922T210535Z: av flips 61592323007979(page) <-> 61592579496197(personal)
    on each switch; the professional profile never sets i_user). When av is
    unknown we fall back to cookies: page mode still works via i_user;
    profile mode is only satisfied by c_user==main AND no i_user.
    Empty ids never pass (fail-safe).
    """
    if av:  # authoritative when seen
        want = main_user_id if post_as == "profile" else posting_user_id
        return bool(want) and av == want
    if post_as == "profile":
        if not main_user_id:
            return False
        iu = cookies.get("i_user")
        return (cookies.get("c_user") == main_user_id
                and (iu is None or iu == "" or iu == main_user_id))
    if not posting_user_id:
        return False
    return cookies.get("i_user") == posting_user_id


async def ensure_login(ctx: BrowserContext, page: Page, log, login_timeout: int = 900) -> bool:
    """Watches for a manual login in the headed window; NEVER types credentials."""
    deadline = time.time() + login_timeout
    sent_to_login = False
    while not await is_authed(ctx):
        if time.time() > deadline:
            log("TIMEOUT: still not logged in after "
                f"{login_timeout}s — aborting (nothing was posted)")
            return False
        try:
            url = page.url
            on_fb = url.startswith("https://www.facebook.com")
            if not on_fb and not sent_to_login:
                await page.goto("https://www.facebook.com/login/", timeout=60000)
                sent_to_login = True
                log("opened login page — log in BY HAND (email, password, 2FA)")
            if not sent_to_login or not on_fb:
                sent_to_login = True
            await page.wait_for_timeout(4000)
        except Exception as e:
            log(f"waiting for login ({type(e).__name__})")
            await asyncio.sleep(4)
    log("session OK (c_user cookie present)")
    return True


async def ensure_active_profile(
    ctx: BrowserContext, page: Page, *, post_as: str, posting_user_id: str,
    main_user_id: str, posting_name: str, main_profile_name: str = "", log=None
) -> bool:
    """[proven] Make the configured posting identity active via the account
    menu, for either mode: 'page' (row = posting_name, e.g. 'Carmazon') or
    'profile' (row = main_profile_name, e.g. 'Carmazon Alex'; recording
    20260922T210535Z). The SWITCHER LISTS BOTH ROWS in either state, and
    'Carmazon' is a substring of 'Carmazon Alex' — CLICK_PROFILE_ITEM_JS
    prefers exact-text rows. Same caveat both directions: JS .click() and
    raw mouse.click() fail (untrusted / no auto-scroll); only Playwright
    locator.click() on the stamped row works. Identity is verified by the
    `av` graphql param (authoritative; the professional profile never sets
    i_user), falling back to cookies before the first request is seen.
    After 6 automated attempts it stops clicking and waits for a manual switch.
    """
    if log is None:
        def log(msg: str, _p=print) -> None:
            _p(msg)
    row_name = main_profile_name if post_as == "profile" else posting_name
    if post_as == "profile" and not row_name:
        log("AP_POST_AS=profile but AP_FB_MAIN_PROFILE_NAME is empty — the "
            "account-menu row for the personal profile is unknown; abort "
            "(fail-safe, nothing posted)")
        return False
    need = main_user_id if post_as == "profile" else posting_user_id
    if not need:
        log(f"AP_POST_AS={post_as!r} but the id it needs "
            f"({'AP_FB_MAIN_USER' if post_as == 'profile' else 'AP_FB_POSTING_USER'})"
            " is empty — cannot verify identity; abort (fail-safe, nothing posted)")
        return False
    await _track_av(page)

    async def check_identity() -> tuple[bool, str | None]:
        """ONE get_av sample per check — it can wait ~2s for the first
        graphql request; calling it twice doubles the dead wait."""
        av = await get_av(ctx, page)
        ok = identity_ok(await cookie_map(ctx), post_as=post_as,
                         posting_user_id=posting_user_id,
                         main_user_id=main_user_id, av=av)
        return ok, av

    ok0, av0 = await check_identity()
    if ok0:
        log(f"active identity already correct: {row_name!r} "
            f"(post_as={post_as}, av={av0 or 'cookie-fallback'})")
        return True
    log(f"switching active identity to {row_name!r} (post_as={post_as}) ...")
    try:
        if "facebook.com" not in page.url:
            await page.goto("https://www.facebook.com/", timeout=60000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        log(f"pre-switch nav failed: {type(e).__name__}")

    async def try_switch() -> str:
        r1 = await page.evaluate(OPEN_ACCOUNT_MENU_JS)
        await page.wait_for_timeout(2000)
        r2 = await page.evaluate(click_profile_item_js(row_name))
        if r2.startswith("stamped:"):
            try:
                await page.locator('[data-ap-switch="1"]').first.click(timeout=5000)
                return f"{r1} -> trusted-click ({r2})"
            except Exception as e:
                return f"{r1} -> click-fail {type(e).__name__} ({r2})"
        return f"{r1} -> {r2}"

    async def try_row_only() -> str:
        r2 = await page.evaluate(click_profile_item_js(row_name))
        if r2.startswith("stamped:"):
            try:
                await page.locator('[data-ap-switch="1"]').first.click(timeout=5000)
                return f"trusted-click ({r2})"
            except Exception as e:
                return f"click-fail {type(e).__name__} ({r2})"
        return r2

    switched = False
    attempts = 0
    deadline = time.time() + 600
    while not switched and time.time() < deadline:
        await page.wait_for_timeout(5000)
        if (await check_identity())[0]:
            switched = True
            break
        if attempts < 6:
            attempts += 1
            try:
                if not await page.evaluate(menu_open_js(row_name)):
                    log(f"switch try {attempts}: {await try_switch()}")
                else:
                    log(f"switch row (menu open): {await try_row_only()}")
            except Exception as e:
                log(f"switch err: {type(e).__name__}")
        await page.wait_for_timeout(5000)
        if (await check_identity())[0]:
            switched = True
            break
    if not switched:
        log(f"STILL not {row_name!r} after automated attempts — waiting up to "
            "10 min for a MANUAL switch in the open window ...")
        end = time.time() + 600
        while time.time() < end and not switched:
            await asyncio.sleep(3)
            switched = (await check_identity())[0]
    if switched:
        log(f"active identity = {row_name} (post_as={post_as}, "
            f"av={await get_av(ctx, page)})")
    return switched


# ---- evidence ----------------------------------------------------------------

def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


async def dump_evidence(
    page: Page, dir_: Path, tag: str, html: str = "",
    extra: dict | None = None,
) -> Path | None:
    """Selector-miss / dry-run evidence: screenshot + optional html + meta
    json. Returns the most useful artifact that ACTUALLY exists (a dead
    browser must never make the log claim a missing screenshot is there)."""
    dir_.mkdir(parents=True, exist_ok=True)
    base = dir_ / f"{tag}_{_stamp()}"
    try:
        await page.screenshot(path=str(base) + ".png")
    except Exception:
        pass
    if html:
        try:
            (base.with_suffix(".html")).write_text(html, encoding="utf-8")
        except Exception:
            pass
    try:
        (base.with_suffix(".json")).write_text(
            json.dumps({"url": page.url, **(extra or {})}, indent=2,
                     ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    for suffix in (".png", ".html", ".json"):
        if base.with_suffix(suffix).exists():
            return base.with_suffix(suffix)
    return None
