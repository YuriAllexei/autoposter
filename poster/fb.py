"""Facebook browser primitives — selectors & flows proven by recordings/dry-runs.

Selector status legend (repo rule: never act on an unproven selector):
  [proven]  verified against recording 20260917T063422Z or scripts/dryrun_post.py
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
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Page

NON_APP_URLS = re.compile(
    r"/(login|checkpoint|two_step_verification|recover|security)/|recaptcha|/tr/"
)

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
  const els = Array.from(
    document.querySelectorAll('[role="menuitem"], [role="button"], span, div')
  ).filter(el => {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const r = el.getBoundingClientRect();
    return r.width > 0 && t && t.length < 200 && t.includes(want);
  });
  if (!els.length) return 'no-profile-item';
  // smallest containing clickable row = the LAST match (most specific/innermost is too
  // small); FB wants the row-level node with a React handler:
  const inner = els[els.length - 1];
  const row = inner.closest('[role="menuitem"],[role="button"],[role="listitem"]') || inner;
  row.setAttribute('data-ap-switch', '1');
  const r = row.getBoundingClientRect();
  return 'stamped:' + row.tagName.toLowerCase() + ':' + row.getAttribute('role')
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
    ctx: BrowserContext, page: Page, *, posting_user_id: str, posting_name: str, log
) -> bool:
    """[proven] Switch personal account -> professional posting profile via the
    account menu. JS .click() and raw mouse.click() both fail (untrusted / no
    auto-scroll); only Playwright locator.click() on the stamped row works.
    After 6 automated attempts it stops clicking and waits for a manual switch.
    """
    if not posting_user_id:
        log(f"AP_FB_POSTING_USER not set — skipping auto-switch; ensure the active "
            f"profile is {posting_name!r} manually before going live")
    if (await cookie_map(ctx)).get("i_user") == posting_user_id:
        log(f"active profile already correct (i_user={posting_user_id})")
        return True
    log(f"switching active profile to {posting_name!r} ...")
    try:
        if "facebook.com" not in page.url:
            await page.goto("https://www.facebook.com/", timeout=60000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        log(f"pre-switch nav failed: {type(e).__name__}")

    async def try_switch() -> str:
        r1 = await page.evaluate(OPEN_ACCOUNT_MENU_JS)
        await page.wait_for_timeout(2000)
        r2 = await page.evaluate(click_profile_item_js(posting_name))
        if r2.startswith("stamped:"):
            try:
                await page.locator('[data-ap-switch="1"]').first.click(timeout=5000)
                return f"{r1} -> trusted-click ({r2})"
            except Exception as e:
                return f"{r1} -> click-fail {type(e).__name__} ({r2})"
        return f"{r1} -> {r2}"

    async def try_row_only() -> str:
        r2 = await page.evaluate(click_profile_item_js(posting_name))
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
        if (await cookie_map(ctx)).get("i_user") == posting_user_id:
            switched = True
            break
        if attempts < 6:
            attempts += 1
            try:
                if not await page.evaluate(menu_open_js(posting_name)):
                    log(f"switch try {attempts}: {await try_switch()}")
                else:
                    log(f"switch row (menu open): {await try_row_only()}")
            except Exception as e:
                log(f"switch err: {type(e).__name__}")
        await page.wait_for_timeout(5000)
        if (await cookie_map(ctx)).get("i_user") == posting_user_id:
            switched = True
            break
    if not switched:
        log(f"STILL not {posting_name!r} after automated attempts — waiting up to "
            "10 min for a MANUAL switch in the open window ...")
        end = time.time() + 600
        while time.time() < end and not switched:
            await asyncio.sleep(3)
            switched = (await cookie_map(ctx)).get("i_user") == posting_user_id
    if switched:
        log(f"active profile = {posting_name} (i_user={posting_user_id})")
    return switched


# ---- evidence ----------------------------------------------------------------

def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


async def dump_evidence(
    page: Page, dir_: Path, tag: str, html: str = "", extra: dict | None = None
) -> Path:
    """Selector-miss / dry-run evidence: screenshot + optional html + meta json."""
    dir_.mkdir(parents=True, exist_ok=True)
    base = dir_ / f"{tag}_{_stamp()}"
    try:
        await page.screenshot(path=str(base) + ".png")
    except Exception:
        pass
    if html:
        (base.with_suffix(".html")).write_text(html, encoding="utf-8")
    (base.with_suffix(".json")).write_text(
        json.dumps({"url": page.url, **(extra or {})}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return base.with_suffix(".png")
