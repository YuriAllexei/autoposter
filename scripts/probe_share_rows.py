"""One-off (READ-ONLY): is the 'Compartir en un grupo' picker list COMPLETE and
STABLE between opens?

Opens the hub twice for the first active listing and calls
discover_share_groups() both times — that helper scrolls the picker list to the
bottom and returns every row it found (the same source the share PLAN comes
from). It prints both lists (index, name, rank, name_total) and diffs them.

It never clicks a group, never opens a share composer, never posts.

Run (NEVER while another run is in flight — one Firefox profile, one owner):
  ap-gui run python scripts/probe_share_rows.py
"""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from poster.config import load_config
from poster.fb import adopt_identity, launch
from poster.flows import discover_share_groups
from poster.listings import SELLING_URL, fetch_active_listings


async def _open(page, listing, cfg) -> list[dict]:
    await page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(6)
    return await discover_share_groups(page, listing, cfg, print)


async def main() -> int:
    cfg = load_config()
    async with async_playwright() as p:
        ctx, page = await launch(cfg, p)
        state = await adopt_identity(ctx, page, cfg, lambda *a: None)
        print("[probe] identity:", state)
        if state != "ok":
            await ctx.close()
            return 2
        try:
            listings = await fetch_active_listings(page, cfg, lambda *a: None)
        except Exception as e:  # noqa: BLE001 - probe: report, never crash
            print("[probe] listings fetch failed:", type(e).__name__, e)
            await ctx.close()
            return 1
        if not listings:
            print("[probe] no active listings")
            await ctx.close()
            return 1
        listing = {"id": listings[0]["id"], "title": listings[0]["title"]}
        print("[probe] listing:", listing["id"], repr(listing["title"][:50]))

        runs: list[list[str]] = []
        for attempt in (1, 2):
            try:
                groups = await _open(page, listing, cfg)
            except Exception as e:  # noqa: BLE001 - probe: report, never crash
                print(f"[probe] open {attempt} FAILED: {type(e).__name__}: {e}")
                await ctx.close()
                return 1
            runs.append([g["name"] for g in groups])
            print(f"[probe] open {attempt}: {len(groups)} row(s)")
            for i, g in enumerate(groups):
                print(f"[probe]   {i:>3}. rank={g.get('rank')} "
                      f"total={g.get('name_total')} {g['name']}")
            await asyncio.sleep(3)

        a, b = runs
        print("[probe] same length:", len(a) == len(b), f"({len(a)} vs {len(b)})")
        if a == b:
            print("[probe] VERDICT: ORDER IS STABLE across opens")
        else:
            diffs = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if x != y]
            print("[probe] VERDICT: ORDER DIFFERS —", len(diffs), "position(s)")
            for i, x, y in diffs[:10]:
                print(f"[probe]   #{i}: {x!r} -> {y!r}")
        await ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))