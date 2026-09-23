"""READ-ONLY probe: what does the selling-page listing card expose NOW?

Loads /marketplace/you/selling/ in the shared persistent profile, waits for
hydration, then dumps every button/role=button's aria-label + text so we can
see how the CURRENT layout names the '...' menu — the selector that failed at
20260923T220825Z with 0 matches. Clicks nothing; posts nothing. Run:

    docker compose run --rm autoposter python scripts/probe_xpost_card.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from poster.config import load_config
from poster.fb import is_authed, launch

DUMP_JS = """
(() => {
  const fold = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const out = [];
  for (const el of document.querySelectorAll('[role="button"], button, a')) {
    const al = el.getAttribute("aria-label") || "";
    const t = fold(el.textContent).slice(0, 60);
    if (!al && !t) continue;
    const r = el.getBoundingClientRect();
    out.push({aria: fold(al).slice(0, 90), text: t,
              role: el.getAttribute("role") || el.tagName.toLowerCase(),
              top: Math.round(r.top), left: Math.round(r.left)});
  }
  const title = "2019 Chevrolet Tahoe LT";
  const norm = title.toLowerCase();
  const titleNodes = [...document.querySelectorAll("span,div,h1,h2,h3")]
    .filter((n) => fold(n.textContent).toLowerCase() === norm).length;
  const moreSel = document.querySelectorAll(
    '[role="button"][aria-label^="Más opciones para "]').length;
  return {buttons_total: out.length, titleNodes, more_selector_count: moreSel,
          interesting: out.filter((b) => /opciones|more|actions|opcions/i
            .test(b.aria) || b.text === "..." ||
            b.aria.toLowerCase().includes("tahoe"))};
})()
"""


async def main() -> int:
    cfg = load_config()
    async with async_playwright() as pw:
        ctx, page = await launch(cfg, pw)
        if not await is_authed(ctx):
            print("NOT LOGGED IN — probe aborted (nothing typed, no login here)")
            await ctx.close()
            return 2
        await page.goto("https://www.facebook.com/marketplace/you/selling/",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(12000)  # FB hydrates async; we only watch
        dump = await page.evaluate(DUMP_JS)
        print(json.dumps(dump, ensure_ascii=False, indent=2)[:5000])
        await ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
