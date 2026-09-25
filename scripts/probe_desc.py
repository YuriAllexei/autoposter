"""One-off: print the EXACT fetched description (repr) for a listing.

Read-only: opens the item page, runs the real fetch_listing_description.
Run in container:  python scripts/probe_desc.py TAHOE
"""
import asyncio
import sys

from playwright.async_api import async_playwright

from poster import listings
from poster.config import load_config
from poster.fb import adopt_identity, launch
from poster.flows import fetch_listing_description


async def main() -> int:
    cfg = load_config()
    want = (sys.argv[1] if len(sys.argv) > 1 else "TAHOE").upper()
    async with async_playwright() as p:
        ctx, page = await launch(cfg, p)
        state = await adopt_identity(ctx, page, cfg, print)
        if state not in ("ok", "switched"):
            print("identity not adopted:", state)
            return 2
        items = await listings.fetch_active_listings(page, cfg, print)
        hit = next((l for l in items if want in str(l.get("title", "")).upper()
                    or want.upper() in str(l.get("name", "")).upper()), None)
        if not hit:
            print("no listing matches", want, "— have:",
                  [l.get("title") or l.get("name") for l in items])
            return 1
        desc = await fetch_listing_description(page, cfg, print, listing=hit)
        print("== len", len(desc))
        print(repr(desc))
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
