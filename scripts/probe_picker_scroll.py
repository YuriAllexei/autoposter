"""One-off (READ-ONLY): how deep is the 'Compartir en un grupo' picker?

Opens the hub for one listing, clicks Grupo, then:
  1. counts DOM rows and dialog scroll geometry (which element scrolls?)
  2. scrolls 6 times, logging row count + any graphql that fires
  3. types 'a' in Buscar grupos and logs how many nodes the typeahead
     payload returns (does SEARCH reach groups the list never loads?)
Closes with Escape. Never clicks a group, never posts.

Run:  docker compose run --rm autoposter python scripts/probe_picker_scroll.py
"""
from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

from poster.config import load_config
from poster.fb import adopt_identity, launch
from poster.flows import XPOST_GROUPS_OP, _find_share_button
from poster.listings import fetch_active_listings

GEOM_JS = r"""
() => {
  const ds = [...document.querySelectorAll('[role="dialog"]')]
    .filter((d) => d.getAttribute('aria-modal') === 'true');
  const d = ds[ds.length - 1];
  if (!d) return {dialog: false};
  const cs = [...d.querySelectorAll('*')].filter(
    (e) => e.scrollHeight > e.clientHeight + 24
        && /auto|scroll/.test(getComputedStyle(e).overflowY))
    .map((e) => ({tag: e.tagName, sh: e.scrollHeight, ch: e.clientHeight,
                  rows: e.querySelectorAll('[role="button"],[role="link"],li').length}));
  return {dialog: true, scrollers: cs.slice(0, 6),
          all: [...d.querySelectorAll('*')].length};
}
"""
ROWS_JS = r"""
() => {
  const fold = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const ds = [...document.querySelectorAll('[role="dialog"]')]
    .filter((d) => d.getAttribute('aria-modal') === 'true');
  const d = ds[ds.length - 1];
  if (!d) return -1;
  return [...d.querySelectorAll('[role="button"],[role="link"]')]
    .filter((el) => fold(el.textContent).includes('grupo')
                 || fold(el.textContent).includes('publico'))
    .length;
}
"""


async def main() -> int:
    cfg = load_config()
    hits: list = []
    async with async_playwright() as p:
        ctx, page = await launch(cfg, p)
        state = await adopt_identity(ctx, page, cfg, lambda *a: print("[p]", *a))
        if state != "ok":
            print("identity:", state)
            await ctx.close()
            return 2

        def on_resp(resp):
            try:
                req = resp.request
                if "graphql" in req.url and XPOST_GROUPS_OP in (req.post_data or ""):
                    hits.append(asyncio.ensure_future(resp.text()))
            except Exception:
                pass
        page.on("response", on_resp)

        listings = await fetch_active_listings(page, cfg, lambda *a: None)
        lst = {"id": listings[0]["id"], "title": listings[0]["title"]}
        await page.goto("https://www.facebook.com/marketplace/you/selling/",
                        wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)
        btn = await _find_share_button(page, lst["title"], cfg, lambda *a: None)
        await btn.click()
        await asyncio.sleep(4)
        # Grupo circle: stamp via the same helper the bot uses
        from poster.flows import _find_hub_group_circle
        del hits[:]
        circle = await _find_hub_group_circle(page, cfg, lambda *a: None)
        await circle.click()
        await asyncio.sleep(5)
        print("rows after open:", await page.evaluate(ROWS_JS))
        print("geometry:", json.dumps(await page.evaluate(GEOM_JS), indent=1)[:700])
        for i in range(6):
            grew = await page.evaluate(
                """() => { const ds=[...document.querySelectorAll('[role="dialog"]')]
                      .filter(d=>d.getAttribute('aria-modal')==='true');
                      const d=ds[ds.length-1]; if(!d) return false;
                      const cs=[...d.querySelectorAll('*')].filter(e=>e.scrollHeight>e.clientHeight+24
                        && /auto|scroll/.test(getComputedStyle(e).overflowY));
                      if(!cs.length) return false;
                      const el=cs.sort((a,b)=>b.scrollHeight-a.scrollHeight)[0];
                      const can=el.scrollTop+el.clientHeight<el.scrollHeight-4;
                      el.scrollTop=el.scrollHeight; return can; }""")
            await asyncio.sleep(2)
            texts = await asyncio.gather(*hits, return_exceptions=True)
            n_payload = set()
            for t in texts:
                if isinstance(t, BaseException) or not isinstance(t, str):
                    continue
                import re as _re
                n_payload.update(_re.findall(r'"id":"(\d+)"', t))
            print(f"scroll {i}: can_scroll={grew} rows={await page.evaluate(ROWS_JS)} "
                  f"graphql_hits={len(texts)} distinct_ids_seen={len(n_payload)}")
            del hits[:]
        # search probe: 'a' should match almost everything if typeahead is global
        try:
            box = page.locator(
                'div[role="dialog"] input[placeholder="Buscar grupos"]').first
            await box.click()
            await page.keyboard.type("o", delay=80)
            await asyncio.sleep(4)
            from poster.flows import parse_share_targets
            texts = await asyncio.gather(*hits, return_exceptions=True)
            ids = set()
            for t in texts:
                if isinstance(t, str):
                    ids.update(g["id"] for g in parse_share_targets(t))
            print(f"search 'o': hits={len(texts)} payload_groups={len(ids)} "
                  f"rows_now={await page.evaluate(ROWS_JS)}")
        except Exception as e:
            print("search probe failed:", type(e).__name__, e)
        for _ in range(5):
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        await ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
