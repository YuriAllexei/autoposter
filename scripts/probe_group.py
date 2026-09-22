"""Composer probe for ONE group, as the Carmazon page. NEVER clicks, NEVER posts.

Opens headed Firefox on the persistent profile and brings it to the SAME
state the bot uses before posting — manual login watched (poster.fb.
ensure_login) and identity adopted (poster.fb.ensure_active_profile) — then
navigates to the group, scans the posting UI read-only and dumps a JSON
report + screenshot under .local-capture/probe/.

Kept intentionally thin: login/switch discipline lives ONLY in poster.fb
(the production path) — this script just reports what it finds.

Usage:
  poetry run python scripts/probe_group.py [--group-url URL] [--profile DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from poster.fb import cookie_map, ensure_active_profile, ensure_login

DEFAULT_GROUP = "https://www.facebook.com/groups/249803862915566"
# the probe always reports as the professional PAGE (the original 2026-09-17
# protocol); identity truth for page mode = i_user/av CARMAZON_ID
CARMAZON_ID = "61592323007979"
CARMAZON_NAME = "Carmazon"

# DIAGNOSTIC COPY of the poster.flows matchers (composer/photo/publish
# text patterns) — deliberately plain JS, not imported from flows: this
# script REPORTS what it sees and must keep working even if flows churn.
# When flows.py regexes change materially, mirror them here.
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
  // Same matcher families poster/flows.py ships (es + en)
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


def _log(msg: str) -> None:
    print(f"[probe] {msg}", flush=True)


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
        ctx = await p.firefox.launch_persistent_context(
            str(profile), headless=False
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            # production paths, not copies: login watch + page identity
            if not await ensure_login(ctx, page, _log, login_timeout=args.login_timeout):
                _log("TIMEOUT: still not logged in. Nothing detected.")
                return 2
            if not await ensure_active_profile(
                ctx, page, post_as="page", posting_user_id=CARMAZON_ID,
                main_user_id="", posting_name=CARMAZON_NAME, log=_log,
            ):
                _log(f"TIMEOUT: never became {CARMAZON_NAME}. Aborting without "
                     "touching the group.")
                return 3

            try:
                await page.goto(args.group_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:  # noqa: BLE001 - FB often stalls domcontentloaded
                _log(f"goto note: {e}")      # on long-poll; the SPA still renders
            for probe_text in ("Escribe algo", "Write something", "¿Qué estás"):
                try:
                    await page.wait_for_selector(f"text={probe_text}", timeout=15000)
                    break
                except Exception:  # noqa: BLE001, S112 - try next language
                    continue       # variant; absence just means no early break
            await page.wait_for_timeout(5000)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(2000)

            report = json.loads(await page.evaluate(DETECT_JS))
            cm = await cookie_map(ctx)
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
            js_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                               encoding="utf-8")

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
        finally:
            await ctx.close()  # flush profile cookies
    print(f"[probe] DONE — no clicks, no post. Profile kept at {profile} "
          "(gitignored).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
