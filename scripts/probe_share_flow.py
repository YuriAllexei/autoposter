"""READ-ONLY probe: pin the SELLING-CARD 'Compartir' + share-hub selectors.

Ground truth: recording 20260925T014900Z_marketplace_individual_listing_individua
(shots 001 card / 002 hub) — the recorder logged VISIBLE TEXT only, so the
accessible NAMES of the card's Compartir button and the hub's four circle
buttons are unproven in the DOM. This script proves them (Slice A0).

What it does, in order (nothing is posted, nothing downstream of Grupo is
ever reached):
  1. load /marketplace/you/selling/ and wait for hydration;
  2. dump EVERY [role=button]/button/a with aria-label + folded visible text,
     plus the listing card's own region (the smallest ancestor of the title
     '2019 Chevrolet Tahoe LT' that also holds a Compartir-ish button), and
     stamp the best Compartir candidate with data-ap-probe;
  3. click ONLY that stamp -> the hub dialog must render; dump every button
     inside [role=dialog] (the four circle buttons: Messenger / WhatsApp /
     Grupo / Copiar enlace) with aria + folded text;
  4. CLOSE the hub via its X (aria folded 'cerrar'/'close') or Escape, verify
     no aria-modal [role=dialog] remains.

NEVER clicked here: the hub 'Grupo' circle, any group row, 'Publicar',
'Compartir ahora'. Run:

    docker compose run --rm autoposter python scripts/probe_share_flow.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from poster.config import load_config
from poster.fb import is_authed, launch

TITLE = "2019 Chevrolet Tahoe LT"

#: dump buttons + locate the card region + stamp the Compartir candidate.
DUMP_JS = r"""
(args) => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim();
  const err = args.title.toLowerCase();
  const btnSel = '[role="button"], button, a';
  const dumpBtn = (el) => {
    const r = el.getBoundingClientRect();
    return {aria: fold(el.getAttribute("aria-label") || "").slice(0, 120),
            text: fold(el.textContent).slice(0, 80),
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute("role") || el.tagName.toLowerCase(),
            top: Math.round(r.top), left: Math.round(r.left),
            w: Math.round(r.width), h: Math.round(r.height)};
  };
  const buttons = [...document.querySelectorAll(btnSel)].map(dumpBtn);

  // ---- the listing card: smallest ancestor of the title node that also
  //      holds a Compartir-ish button --------------------------------
  let cardRect = null, cardEl = null;
  for (const n of document.querySelectorAll("span,div,h1,h2,h3")) {
    if (fold(n.textContent).toLowerCase() !== err) continue;
    let p = n;
    for (let i = 0; i < 8 && p; i++) {
      const has = [...p.querySelectorAll(btnSel)].some(
        (b) => /compartir/i.test(fold(b.getAttribute("aria-label") || "")
                                 + " " + fold(b.textContent)));
      if (has) { cardEl = p; break; }
      p = p.parentElement;
    }
    if (cardEl) break;
  }
  if (cardEl) {
    const r = cardEl.getBoundingClientRect();
    cardRect = {top: Math.round(r.top), left: Math.round(r.left),
                w: Math.round(r.width), h: Math.round(r.height)};
  }

  // ---- candidates + stamp the best one ----------------------------
  const cands = [];
  const all = [...document.querySelectorAll(btnSel)];
  all.forEach((el, idx) => {
    const hay = fold(el.getAttribute("aria-label") || "") + " " + fold(el.textContent);
    if (!/compartir/i.test(hay)) return;
    const r = el.getBoundingClientRect();
    const inCard = cardEl ? cardEl.contains(el) : false;
    const inDialog = !!el.closest('[role="dialog"]');
    cands.push({idx, inCard, inDialog, top: Math.round(r.top),
                left: Math.round(r.left), w: Math.round(r.width),
                h: Math.round(r.height),
                aria: fold(el.getAttribute("aria-label") || "").slice(0, 120),
                text: fold(el.textContent).slice(0, 80)});
  });
  const best = cands.filter((c) => c.inCard)[0]
            || cands.filter((c) => !c.inDialog).sort((a, b) => a.top - b.top)[0]
            || cands[0] || null;
  let stamped = null;
  if (best) {
    document.querySelectorAll("[data-ap-probe]").forEach(
      (e) => e.removeAttribute("data-ap-probe"));
    const el = all[best.idx];
    el.setAttribute("data-ap-probe", "1");
    stamped = {aria: best.aria, text: best.text, inCard: best.inCard,
               children: el.children.length};
  }
  const titleNodes = [...document.querySelectorAll("span,div,h1,h2,h3")]
    .filter((n) => fold(n.textContent).toLowerCase() === err).length;
  return {page_url: location.href, buttons_total: buttons.length,
          title_nodes: titleNodes, card_rect: cardRect,
          compartir_candidates: cands, stamped: stamped,
          buttons_on_card: cardEl
            ? [...cardEl.querySelectorAll(btnSel)].map(dumpBtn)
            : [],
          dialogs_open: document.querySelectorAll('[role="dialog"]').length};
}
"""

#: every button inside the just-opened hub dialog (the four circle buttons).
DIALOG_DUMP_JS = r"""
() => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim();
  const dialogs = [...document.querySelectorAll('[role="dialog"]')]
    .filter((d) => d.getAttribute("aria-modal") === "true"
                || d.getBoundingClientRect().width > 0);
  const last = dialogs[dialogs.length - 1] || document.querySelector('[role="dialog"]');
  if (!last) return {dialog: null};
  const r = last.getBoundingClientRect();
  const btns = [...last.querySelectorAll('[role="button"], button, a')].map((el) => {
    const b = el.getBoundingClientRect();
    return {aria: fold(el.getAttribute("aria-label") || "").slice(0, 120),
            text: fold(el.textContent).slice(0, 80),
            tag: el.tagName.toLowerCase(),
            top: Math.round(b.top), left: Math.round(b.left),
            w: Math.round(b.width), h: Math.round(b.height)};
  });
  const texts = [...last.querySelectorAll("h1,h2,h3,span,div")]
    .map((n) => fold(n.textContent))
    .filter((t) => t && t.length < 60);
  return {dialog_rect: {top: Math.round(r.top), left: Math.round(r.left),
                        w: Math.round(r.width), h: Math.round(r.height)},
          dialog_aria_label: fold(last.getAttribute("aria-label") || ""),
          buttons: btns,
          has_compartir_en: texts.some((t) => /^compartir en$|^compartir en\b/i.test(t)),
          headings: [...new Set(texts)].slice(0, 40)};
}
"""

GONE_JS = ("() => ![...document.querySelectorAll('[role=dialog]')]"
           ".some(d => d.getAttribute('aria-modal') === 'true')")


async def main() -> int:
    cfg = load_config()
    report: dict = {"title": TITLE}
    async with async_playwright() as pw:
        ctx, page = await launch(cfg, pw)
        try:
            if not await is_authed(ctx):
                print("NOT LOGGED IN — probe aborted (nothing clicked)")
                return 2
            await page.goto("https://www.facebook.com/marketplace/you/selling/",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(12000)  # FB hydrates async; we only watch
            report["card"] = await page.evaluate(DUMP_JS, {"title": TITLE})
            print(json.dumps(report, ensure_ascii=False, indent=2)[:12000])

            # ---- click ONLY the stamped Compartir (opens the hub) ----
            stamp = page.locator('[data-ap-probe="1"]')
            if not await stamp.count():
                report["hub"] = {"error": "no Compartir candidate stamped"}
                print("\n=== NO STAMP — hub not opened ===")
                return 0
            await stamp.first.scroll_into_view_if_needed()
            await stamp.first.click(timeout=10000)
            try:
                await page.wait_for_selector(
                    '[role="dialog"][aria-modal="true"], [role="dialog"]',
                    timeout=15000)
            except Exception as e:  # noqa: BLE001 - probe reports, never raises
                report["hub"] = {"error": f"hub dialog never appeared: {e}"}
                print("\n=== HUB NEVER APPEARED ===")
                return 0
            await page.wait_for_timeout(2500)
            report["hub"] = await page.evaluate(DIALOG_DUMP_JS)
            print("\n=== HUB DIALOG DUMP ===")
            print(json.dumps(report["hub"], ensure_ascii=False, indent=2)[:9000])

            # ---- close via X (folded cerrar/close), fallback Escape ----
            closer = page.locator(
                '[role="dialog"] [role="button"][aria-label*="errar" i], '
                '[role="dialog"] [role="button"][aria-label*="lose" i]').first
            closed_how = None
            if await closer.count() and await closer.is_visible():
                await closer.click(timeout=8000)
                closed_how = "X button"
            else:
                await page.keyboard.press("Escape")
                closed_how = "Escape"
            try:
                await page.wait_for_function(GONE_JS, timeout=6000)
                report["closed"] = closed_how
            except Exception:  # noqa: BLE001
                report["closed"] = f"{closed_how} (dialog MAY still be open)"
            print(f"\n=== CLOSED via {report['closed']} — nothing posted ===")
            return 0
        finally:
            await ctx.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
