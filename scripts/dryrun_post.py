"""DRY-RUN post test v2: navigate as Carmazon, open composer, TYPE text — stop.

NEVER clicks Publicar. v1 lesson: raw mouse.click(x, y) does NOT scroll and
y=877 was below the fold -> nothing happened. v2 uses Playwright locators
(auto-scroll, trusted clicks) and scopes EVERYTHING to div[role="dialog"] —
the composer dialog — so we can never type into a comment box by accident.
If a stale draft is in the dialog, it is cleared before typing (logged).
Window stays open after typing for human inspection; killing the process just
discards the draft — nothing can post itself.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import async_playwright

GROUP_URL = "https://www.facebook.com/groups/249803862915566"
CARMAZON_ID = "61592323007979"
CARMAZON_NAME = "Carmazon"
POST_TEXT = "Hola grupo, ando buscando una Tacoma 2021 para arriba :)"
_REPO = Path(__file__).resolve().parent.parent
PROFILE = str(_REPO / ".local-capture/profiles/facebook")
OUTDIR = _REPO / ".local-capture/dryrun"

from probe_group import CLICK_PROFILE_ITEM_JS, OPEN_ACCOUNT_MENU_JS  # noqa: E402

STAMP_TRIGGER_JS = r"""
(() => {
  document.querySelectorAll('[data-ap-target]').forEach(e => e.removeAttribute('data-ap-target'));
  const els = Array.from(document.querySelectorAll('div,span')).filter(el => {
    const t = (el.innerText || '').trim();
    const r = el.getBoundingClientRect();
    return r.width > 0 && (t === 'Escribe algo...' || t === 'Escribe algo…'
      || t === 'Write something...' || /^Escribe algo\b/.test(t) && t.length < 40);
  });
  if (!els.length) return 'no-trigger';
  els.sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
  const el = els[0];                       // TOPMOST match = the real composer
  el.setAttribute('data-ap-target', '1');
  const r = el.getBoundingClientRect();
  return 'topmost:' + el.tagName.toLowerCase() + ':viewport_y=' + Math.round(r.y)
    + ':' + els.length + ' matches';
})()
"""


async def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    async with async_playwright() as p:
        ctx = await p.firefox.launch_persistent_context(
            PROFILE, headless=False, viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def cookies() -> dict:
            return {c.get("name"): c.get("value") for c in await ctx.cookies("https://www.facebook.com")}

        ck = await cookies()
        if not ck.get("c_user"):
            print("[dryrun] NOT LOGGED IN — run probe_group.py first.", flush=True)
            await ctx.close()
            return 2
        print(f"[dryrun] logged in (c_user={ck.get('c_user')})", flush=True)

        if ck.get("i_user") != CARMAZON_ID:
            print("[dryrun] switching to Carmazon...", flush=True)
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            await page.wait_for_timeout(12000)
            await page.evaluate(OPEN_ACCOUNT_MENU_JS)
            await page.wait_for_timeout(2000)
            r = await page.evaluate(CLICK_PROFILE_ITEM_JS.replace("%NAME%", CARMAZON_NAME))
            if r.startswith("stamped:"):
                await page.locator('[data-ap-switch="1"]').first.click(timeout=5000)
            for _ in range(15):
                await asyncio.sleep(2)
                if (await cookies()).get("i_user") == CARMAZON_ID:
                    break
        if (await cookies()).get("i_user") != CARMAZON_ID:
            print("[dryrun] could not switch — ABORT (nothing posted).", flush=True)
            await ctx.close()
            return 3
        print(f"[dryrun] active profile: {CARMAZON_NAME}", flush=True)

        # ---- group page ----
        try:
            await page.goto(GROUP_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        try:
            await page.wait_for_selector("text=Escribe algo", timeout=25000)
        except Exception:
            print("[dryrun] composer never appeared — ABORT.", flush=True)
            await page.screenshot(path=str(OUTDIR / f"fail_{stamp}.png"))
            await ctx.close()
            return 4
        await page.wait_for_timeout(4000)

        async def dialog_textbox():
            return page.locator('div[role="dialog"] [contenteditable="true"]').first

        async def dismiss_overlays():
            """Press Escape twice to close non-composer popups (notif prompts etc.)."""
            for _ in range(2):
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(1200)

        # Is the composer dialog ALREADY open (draft restore etc.)?
        box = await dialog_textbox()
        if await box.count() and await box.is_visible():
            print("[dryrun] composer dialog already open — skipping trigger click", flush=True)
        else:
            await dismiss_overlays()
            box = await dialog_textbox()
            if await box.count() and await box.is_visible():
                print("[dryrun] overlay dismissed; composer dialog was open", flush=True)
            else:
                # ---- open the dialog: stamp TOPMOST trigger, locator auto-scrolls ----
                info = await page.evaluate(STAMP_TRIGGER_JS)
                print(f"[dryrun] trigger: {info}", flush=True)
                if not str(info).startswith("topmost:"):
                    await ctx.close()
                    return 5
                trig = page.locator('[data-ap-target="1"]').first
                await trig.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                try:
                    await trig.click(timeout=10000)
                except Exception as e:
                    # Evidence, not guesses: who is covering the trigger?
                    cover = await page.evaluate(
                        "(() => { const el = document.querySelector('[data-ap-target=\"1\"]');"
                        " if (!el) return 'stamp-gone';"
                        " const r = el.getBoundingClientRect();"
                        " const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);"
                        " return top ? top.tagName + '.' + String(top.className).slice(0,60)"
                        "   + ' text=' + (top.innerText||'').slice(0,80) : 'none'; })()")
                    print(f"[dryrun] trigger click intercepted. Cover: {cover}", flush=True)
                    await page.screenshot(path=str(OUTDIR / f"fail_cover_{stamp}.png"))
                    await ctx.close()
                    return 7
                print("[dryrun] clicked trigger — waiting for dialog textbox...", flush=True)
                box = await dialog_textbox()
                try:
                    await box.wait_for(state="visible", timeout=15000)
                except Exception:
                    print("[dryrun] dialog textbox NOT found — ABORT.", flush=True)
                    await page.screenshot(path=str(OUTDIR / f"fail_dialog_{stamp}.png"))
                    await ctx.close()
                    return 6
        await page.wait_for_timeout(2000)

        existing = (await box.inner_text()).strip()
        if existing:
            print(f"[dryrun] NOTE: dialog had stale draft {existing!r} — clearing it", flush=True)
            await box.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(500)

        await box.click()
        await page.keyboard.type(POST_TEXT, delay=45)
        await page.wait_for_timeout(2500)

        typed_now = (await box.inner_text()).strip()
        pub = page.locator('div[role="dialog"] [role="button"]').filter(has_text="Publicar")
        pub_n = await pub.count()
        pub_state = "none"
        if pub_n:
            disabled = await pub.first.get_attribute("aria-disabled")
            pub_state = "ENABLED — but NOT clicked" if disabled != "true" else "still-disabled"
        ok_shot = OUTDIR / f"typed_{stamp}.png"
        await page.screenshot(path=str(ok_shot))
        print("[dryrun] ===== RESULT =====", flush=True)
        print(f"    dialog contains : {typed_now!r}", flush=True)
        print(f"    matches target  : {typed_now == POST_TEXT}", flush=True)
        print(f"    Publicar button : count={pub_n} state={pub_state}", flush=True)
        print(f"    screenshot      : {ok_shot}", flush=True)
        print("[dryrun] STOPPED BEFORE PUBLISH — window left open for you.", flush=True)
        end = time.time() + 900
        while time.time() < end:
            await asyncio.sleep(5)
            try:
                if page.is_closed():
                    break
            except Exception:
                break
        try:
            await ctx.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
