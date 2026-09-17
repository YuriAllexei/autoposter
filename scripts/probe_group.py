"""Dry-run composer probe for autoposer. NEVER posts, NEVER clicks publish.

Opens a headed Firefox with a PERSISTENT profile at the target Facebook group.
If not logged in: you log in by hand (incl. 2FA) in that window; the script
polls until the session is authenticated, then navigates to the group itself.
It then locates the posting UI (composer trigger, contenteditable boxes,
photo buttons, publish button) read-only and dumps a JSON report.
Cookies stay in the profile dir -> future automation reuses the login.

Usage:
  python3 scripts/probe_group.py [--group-url URL] [--profile DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext

DEFAULT_GROUP = "https://www.facebook.com/groups/249803862915566"
# posting must happen as the professional profile, not the main account
CARMAZON_ID = "61592323007979"
CARMAZON_NAME = "Carmazon"
NON_APP_URLS = re.compile(
    r"/(login|checkpoint|two_step_verification|recover|security)/|recaptcha|/tr/"
)

OPEN_ACCOUNT_MENU_JS = r"""
(() => {
  const cands = Array.from(document.querySelectorAll('[role="button"]')).filter(el => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.top > 90 || !el.querySelector('img')) return false;
    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
    const rightish = r.left > innerWidth * 0.4;
    const named = /cuenta|account|perfil|profile|configuraci/.test(aria);
    return rightish && (named || r.top < 70);
  });
  if (!cands.length) return 'no-account-button';
  cands.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
  cands[0].click();
  return 'clicked-account:' + (cands[0].getAttribute('aria-label') || '');
})()
"""

CLICK_PROFILE_ITEM_JS = r"""
((want) => {
  const els = Array.from(
    document.querySelectorAll('[role="menuitem"], [role="button"], span, div')
  ).filter(el => {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const r = el.getBoundingClientRect();
    return r.width > 0 && t && t.length < 200 && t.includes(want);
  });
  if (!els.length) return 'no-profile-item';
  const row = els[0].closest('[role="menuitem"],[role="button"]') || els[0];
  row.click();
  return 'clicked:' + row.tagName.toLowerCase() + ':' + row.getAttribute('role');
})("%NAME%")
"""


MENU_OPEN_JS = r"""
(() => {
  for (const el of document.querySelectorAll('[role="menuitem"],[role="button"],span,div')) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const r = el.getBoundingClientRect();
    if (r.width > 0 && t && t.length < 200 && t.includes('%NAME%')) return true;
  }
  return false;
})()
"""

DETECT_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 10 && r.height > 10 && getComputedStyle(el).visibility !== 'hidden';
  };
  const info = (el, why) => {
    const r = el.getBoundingClientRect();
    return {
      why,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      aria: el.getAttribute('aria-label') || '',
      text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80),
      placeholder: el.getAttribute('aria-placeholder') || el.getAttribute('placeholder') || '',
      contenteditable: el.getAttribute('contenteditable') || '',
      testid: el.getAttribute('data-pbe') ? 'data-pbe' : '',
      selector: (el.id ? '#' + el.id : '') || null,
      box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    };
  };
  const out = { url: location.href, title: document.title, group_name: '', hits: {} };
  const h1 = document.querySelector('h1');
  if (h1) out.group_name = (h1.innerText || '').trim().slice(0, 120);
  const COMPOSER_RE = /escribe algo|write something|qu[eé] estás pensando|what'?s on your mind/i;
  const PHOTO_RE = /foto|photo|imagen|image/i;
  const PUBLISH_RE = /^(publicar|post|publish)$/i;

  const triggers = [];
  for (const el of document.querySelectorAll('[role="button"], [role="textbox"], div, span')) {
    if (!vis(el)) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const aria = (el.getAttribute('aria-label') || '');
    if ((COMPOSER_RE.test(t) && t.length < 90) || COMPOSER_RE.test(aria)) {
      const el2 = el.closest('[role="button"]') || el;
      const s = info(el2, 'composer-trigger');
      if (!triggers.some(x => x.selector === s.selector && x.text === s.text)) triggers.push(s);
    }
    if (triggers.length >= 6) break;
  }
  out.hits.composer_trigger = triggers;

  out.hits.textboxes = Array.from(document.querySelectorAll('[contenteditable="true"], [contenteditable=""], [role="textbox"]'))
    .filter(vis).slice(0, 8).map(el => info(el, 'contenteditable'));

  const photo = [];
  for (const el of document.querySelectorAll('[role="button"], span, div')) {
    if (!vis(el)) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const aria = (el.getAttribute('aria-label') || '');
    if ((PHOTO_RE.test(aria) || PHOTO_RE.test(t)) && t.length < 60 && el.closest('[role="dialog"],[role="main"]')) {
      photo.push(info(el.closest('[role="button"]') || el, 'photo'));
    }
    if (photo.length >= 8) break;
  }
  out.hits.photo_buttons = photo;

  out.hits.file_inputs = Array.from(document.querySelectorAll('input[type="file"]'))
    .map(el => ({ accept: el.getAttribute('accept') || '', multiple: el.multiple, id: el.id || '' }));

  const pubs = [];
  for (const el of document.querySelectorAll('[role="button"]')) {
    if (!vis(el)) continue;
    if (PUBLISH_RE.test((el.innerText || '').replace(/\s+/g, ' ').trim())) pubs.push(info(el, 'publish'));
    if (pubs.length >= 4) break;
  }
  out.hits.publish_buttons = pubs;
  return JSON.stringify(out);
})()
"""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group-url", default=DEFAULT_GROUP)
    ap.add_argument(
        "--profile",
        default=str(Path(__file__).resolve().parent.parent / ".local-capture/profiles/facebook"),
    )
    ap.add_argument("--login-timeout", type=int, default=900, help="seconds to wait for manual login")
    args = ap.parse_args()

    profile = Path(args.profile)
    profile.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        ctx: BrowserContext = await p.firefox.launch_persistent_context(
            str(profile), headless=False
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def is_authed() -> bool:
            cookies = await ctx.cookies("https://www.facebook.com")
            return any(c.get("name") == "c_user" and c.get("value") for c in cookies)

        deadline = time.time() + args.login_timeout
        sent_to_login = False
        while not await is_authed():
            if time.time() > deadline:
                print("[probe] TIMEOUT: still not logged in. Nothing detected.", flush=True)
                await ctx.close()
                return 2
            try:
                url = page.url
                on_fb = url.startswith("https://www.facebook.com") or url.startswith("https://m.facebook.com")
                if not on_fb and not sent_to_login:
                    await page.goto("https://www.facebook.com/login/", timeout=60000)
                    sent_to_login = True
                    print("[probe] opened login page.", flush=True)
                if not sent_to_login or not on_fb:
                    print("[probe] LOGGED OUT — log in inside the Firefox window (email, "
                          "password, 2FA). The page will NOT be touched or refreshed "
                          "while you work; this script only watches cookies.", flush=True)
                    sent_to_login = True
                await page.wait_for_timeout(4000)
            except Exception as e:
                print(f"[probe] waiting for login ({type(e).__name__})", flush=True)
                await asyncio.sleep(4)

        print("[probe] session OK (c_user cookie present)", flush=True)

        async def cookie_map() -> dict:
            return {c.get("name"): c.get("value") for c in await ctx.cookies("https://www.facebook.com")}

        # ensure the ACTIVE profile is Carmazon (i_user cookie), not the main account
        if (await cookie_map()).get("i_user") != CARMAZON_ID:
            print(f"[probe] logged in but NOT as {CARMAZON_NAME} (i_user="
                  f"{(await cookie_map()).get('i_user')}) — auto-switching via the "
                  "profile menu...", flush=True)
            try:
                if "facebook.com" not in page.url:
                    await page.goto("https://www.facebook.com/", timeout=60000)
                await page.wait_for_timeout(3000)
                r1 = await page.evaluate(OPEN_ACCOUNT_MENU_JS)
                await page.wait_for_timeout(2000)
                r2 = await page.evaluate(
                    CLICK_PROFILE_ITEM_JS.replace("%NAME%", CARMAZON_NAME))
                print(f"[probe] switch clicks: {r1} -> {r2}", flush=True)
            except Exception as e:
                print(f"[probe] auto-switch failed: {type(e).__name__}: {e}", flush=True)
            switched = False
            attempts = 0
            deadline_sw = time.time() + 600
            while not switched and time.time() < deadline_sw:
                await page.wait_for_timeout(5000)
                if (await cookie_map()).get("i_user") == CARMAZON_ID:
                    switched = True
                    break
                # bounded auto-switch retries; after that, pure cookie polling
                # so we never fight a manual user interaction with the menu.
                if attempts >= 4:
                    continue
                attempts += 1
                try:
                    if "facebook.com" not in page.url:
                        await page.goto("https://www.facebook.com/", timeout=60000)
                    menu_open = await page.evaluate(
                        MENU_OPEN_JS.replace("%NAME%", CARMAZON_NAME))
                    if not menu_open:
                        await page.evaluate(OPEN_ACCOUNT_MENU_JS)
                        await page.wait_for_timeout(1500)
                    r2 = await page.evaluate(
                        CLICK_PROFILE_ITEM_JS.replace("%NAME%", CARMAZON_NAME))
                    print(f"[probe] switch attempt {attempts}: "
                          f"menu_open={menu_open} -> {r2}", flush=True)
                except Exception:
                    pass
                await page.wait_for_timeout(5000)
                if (await cookie_map()).get("i_user") == CARMAZON_ID:
                    switched = True
                    break
            if not switched:
                print(f"[probe] STILL not {CARMAZON_NAME} — open the top-right "
                      "profile picture in the window and click it manually. Waiting "
                      "up to 10 min for the switch...", flush=True)
                end = time.time() + 600
                while time.time() < end and not switched:
                    await asyncio.sleep(3)
                    switched = (await cookie_map()).get("i_user") == CARMAZON_ID
                if not switched:
                    print("[probe] TIMEOUT: never switched. Aborting without touching "
                          "the group.", flush=True)
                    await ctx.close()
                    return 3
        print(f"[probe] active profile = {CARMAZON_NAME} (i_user={CARMAZON_ID})", flush=True)

        try:
            await page.goto(args.group_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:  # FB often stalls domcontentloaded on long-poll
            print(f"[probe] goto note: {e}", flush=True)
        # wait for the logged-in group feed to hydrate (composer visible)
        for probe_text in ("Escribe algo", "Write something", "¿Qué estás"):
            try:
                await page.wait_for_selector(f"text={probe_text}", timeout=15000)
                break
            except Exception:
                continue
        await page.wait_for_timeout(5000)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(2000)

        raw = await page.evaluate(DETECT_JS)
        report = json.loads(raw)
        cm = await cookie_map()
        report["c_user"] = cm.get("c_user")
        report["i_user"] = cm.get("i_user")
        report["ts"] = datetime.now(UTC).isoformat()
        report["logged_in"] = True

        shots = Path(__file__).resolve().parent.parent / ".local-capture/probe"
        shots.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        png = shots / f"group_{stamp}.png"
        await page.screenshot(path=str(png), full_page=False)
        js_path = shots / f"probe_{stamp}.json"
        js_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"[probe] group: {report.get('group_name') or '?'}")
        print(f"[probe] report: {js_path}")
        print(f"[probe] screenshot: {png}")
        for key, hits in report["hits"].items():
            print(f"[probe] {key}: {len(hits)} hit(s)")
            for h in hits[:4]:
                if isinstance(h, dict):
                    print(f"    - role={h.get('role','')} aria={h.get('aria','')[:40]!r} "
                          f"text={h.get('text','')[:40]!r} ph={h.get('placeholder','')[:30]!r} "
                          f"ce={h.get('contenteditable','')} box={h.get('box')}")
        # leave profile cleanly so cookies flush
        await ctx.close()
    print("[probe] DONE — no clicks, no post. Profile kept at "
          f"{profile} (gitignored).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
