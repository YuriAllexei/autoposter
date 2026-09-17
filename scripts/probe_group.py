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
NON_APP_URLS = re.compile(
    r"/(login|checkpoint|two_step_verification|recover|security)/|recaptcha|/tr/"
)

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
  const COMPOSER_RE = /escribe algo|write something|qu[eé] estás|create (a )?post|publicaci[oó]n|what's on your mind/i;
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
        print(f"[probe] opening {args.group_url}", flush=True)
        try:
            await page.goto(args.group_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:  # FB often stalls domcontentloaded on long-poll
            print(f"[probe] goto note: {e}", flush=True)

        deadline = time.time() + args.login_timeout
        authed = False
        while time.time() < deadline and not authed:
            try:
                url = page.url
            except Exception:
                url = ""
            if url.startswith("https://www.facebook.com") and not NON_APP_URLS.search(url):
                authed = True
                break
            print("[probe] not authenticated yet — log in inside the Firefox window "
                  "(email, password, 2FA). Waiting...", flush=True)
            await page.wait_for_timeout(4000)
        if not authed:
            print("[probe] TIMEOUT: no authenticated session found. Nothing detected.", flush=True)
            await ctx.close()
            return 2

        # make sure we are on the group page regardless of where login left us
        if "/groups/" not in page.url:
            try:
                await page.goto(args.group_url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
        print("[probe] session OK — letting the group feed hydrate (12 s)...", flush=True)
        await page.wait_for_timeout(12000)

        raw = await page.evaluate(DETECT_JS)
        report = json.loads(raw)
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
