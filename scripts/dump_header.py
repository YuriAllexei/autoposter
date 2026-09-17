"""Dump the real Facebook header buttons so we can write a correct switcher selector."""
import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

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
            cookies = await ctx.cookies("https://www.facebook.com")
            cu = next((c.get("value") for c in cookies if c.get("name")=="c_user"), None)
            iu = next((c.get("value") for c in cookies if c.get("name")=="i_user"), None)
            print("c_user=", cu, "i_user=", iu, flush=True)
        except Exception as e:
            print("cookie err", e, flush=True)
        try:
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("goto note:", type(e).__name__, flush=True)
        await page.wait_for_timeout(15000)
        d = json.loads(await page.evaluate(DUMP_JS))
        print("URL:", d["url"], flush=True)
        print(f"{len(d['header'])} header elements:", flush=True)
        for h in d["header"][:25]:
            print(f"  x={h['x']:4} y={h['y']:3} w={h['w']:3} img={h['hasImg']} role={h['role']:8} aria={h['aria']!r} text={h['text']!r}", flush=True)
        await ctx.close()

asyncio.run(main())
