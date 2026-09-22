"""READ-ONLY account-menu probe — dumps the real DOM text of the profile
switcher so identity-row selectors can be built from ground truth.

NEVER clicks any identity row, NEVER posts, NEVER changes the active profile.
Opens the persistent Firefox profile, opens the top-right account menu, and
prints: cookie fingerprints, the avatar button's aria-label, every visible
menu/dialog text node candidate, and a screenshot for the record.

Usage: poetry run python scripts/probe_account_menu.py
"""
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE = Path(".local-capture/profiles/facebook")
OUT = Path(".local-capture/shots")

DUMP_JS = r"""
(() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const btn = document.querySelector('[role="button"][aria-label="Tu perfil"]')
    || document.querySelector('[role="button"][aria-label="Your profile"]');
  const out = {
    avatar_aria: btn ? btn.getAttribute('aria-label') : null,
    url: location.href,
    containers: [],
  };
  const conts = Array.from(document.querySelectorAll('[role="menu"], [role="dialog"], [role="listbox"]')).filter(vis);
  for (const c of conts) {
    const rows = Array.from(c.querySelectorAll('[role="menuitem"], [role="button"], [role="listitem"], [role="option"]'))
      .filter(vis)
      .map(el => ({
        role: el.getAttribute('role'),
        text: norm(el.innerText).slice(0, 120),
        aria: norm(el.getAttribute('aria-label')).slice(0, 120),
        y: Math.round(el.getBoundingClientRect().y),
      }))
      .filter(r => r.text || r.aria);
    if (rows.length) out.containers.push({ cls: c.className.slice(0, 40), rows });
  }
  return JSON.stringify(out);
})()
"""

OPEN_MENU_JS = r"""
(() => {
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
  return 'clicked:' + (btn.getAttribute('aria-label') || '?');
})()
"""


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.firefox.launch_persistent_context(
            str(PROFILE), headless=False, viewport={"width": 1280, "height": 900})
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.facebook.com/", timeout=60000)
            await page.wait_for_timeout(5000)
            cookies = {c.get("name"): c.get("value")
                       for c in await ctx.cookies("https://www.facebook.com")}
            print("cookies:", json.dumps(
                {k: cookies.get(k) for k in ("c_user", "i_user", "av")}))
            print("menu:", await page.evaluate(OPEN_MENU_JS))
            await page.wait_for_timeout(2500)
            data = json.loads(await page.evaluate(DUMP_JS))
            print("avatar_aria:", data.get("avatar_aria"))
            print("url:", data.get("url"))
            for i, cont in enumerate(data.get("containers") or []):
                print(f"-- container {i} ({cont['cls']})")
                for r in cont["rows"][:40]:
                    print(f"   y={r['y']:>4} {r['role']:<9} "
                          f"text={r['text']!r} aria={r['aria']!r}")
            if not data.get("containers"):
                print("!! no menu/dialog containers visible after opening")
            shot = OUT / f"account_menu_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.png"
            await page.screenshot(path=str(shot))
            print("screenshot:", shot)
        finally:
            await ctx.close()  # read-only run; do NOT touch identity further


if __name__ == "__main__":
    asyncio.run(main())
