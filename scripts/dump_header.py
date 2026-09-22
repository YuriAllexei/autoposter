"""Dump the real Facebook header buttons so we can write a correct switcher selector."""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from poster.fb import cookie_map

PROFILE = str(Path(__file__).resolve().parent.parent / ".local-capture/profiles/facebook")

DUMP_JS = r"""
(() => {
  const vis = el => { const r=el.getBoundingClientRect(); return r.width>0 && r.height>0; };
  const out = { url: location.href, header: [] };
  // anything in the top 90px that looks interactive
  for (const el of document.querySelectorAll('[role="button"], a, [aria-label]')) {
    const r = el.getBoundingClientRect();
    if (r.top > 95 || r.bottom < 0 || !vis(el)) continue;
    out.header.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role')||'',
      aria: el.getAttribute('aria-label')||'',
      text: (el.innerText||'').replace(/\s+/g,' ').trim().slice(0,60),
      hasImg: !!el.querySelector('img'),
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width),
    });
  }
  out.header.sort((a,b)=>b.x-a.x);
  return JSON.stringify(out);
})()
"""

async def main():
    async with async_playwright() as p:
        ctx = await p.firefox.launch_persistent_context(PROFILE, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            cm = await cookie_map(ctx)
            print("c_user=", cm.get("c_user"), "i_user=", cm.get("i_user"), flush=True)
        except Exception as e:  # noqa: BLE001 - probe must survive any CDP hiccup
            print("cookie err", e, flush=True)
        try:
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:  # noqa: BLE001 - FB stalls domcontentloaded on long-poll
            print("goto note:", type(e).__name__, flush=True)
        await page.wait_for_timeout(15000)
        d = json.loads(await page.evaluate(DUMP_JS))
        print("URL:", d["url"], flush=True)
        print(f"{len(d['header'])} header elements:", flush=True)
        for h in d["header"][:25]:
            print(f"  x={h['x']:4} y={h['y']:3} w={h['w']:3} img={h['hasImg']} role={h['role']:8} aria={h['aria']!r} text={h['text']!r}", flush=True)
        await ctx.close()

asyncio.run(main())
