"""Posting flows — ONE function per group flavor, dispatched by posting_code.

groups.json::posting_code resolves through REGISTRY below. Unknown code =
KeyError (hard error; the dispatcher never guesses a flow).

Repo rules obeyed by every flow:
  - random uniform(AP_DELAY_MIN, AP_DELAY_MAX) sleep before every sensitive
    action (per photo batch, before publish)
  - post text inserted 1:1 with READ-BACK verification (rule 3)
  - photos attached via the composer's hidden input[type=file] (proven in
    recording 20260917T063422Z: `input.x1s85apg` logged `C:\\fakepath\\car.jpg`)
    — Playwright set_input_files answers it in-process; the OS dialog is
    never opened. FileChooser-click is the fallback. One batch per car
    folder, cars in sorted order => attachment order == post.txt car order
  - DRY RUN stops before Publicar and saves evidence instead

Selector provenance: recording 20260917T063422Z (group 249803862915566) +
scripts/dryrun_post.py (topmost-trigger stamp, dialog-scoped actions, stale
draft clear, keyboard.type). Match by text/aria/role, never obfuscated classes.
"""
from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeoutError

from .config import Config
from .fb import dump_evidence
from .photos import CarPhotos

log_fn = Callable[..., None]


@dataclass
class Post:
    """Everything one group post needs, pre-resolved by the runner."""

    text: str                 # post.txt content, verbatim
    cars: list[CarPhotos]     # ordered cars; photos inside each ordered

    @property
    def all_files(self) -> list:
        return [f for car in self.cars for f in car.files]


class FlowError(RuntimeError):
    """A proven selector did not resolve — abort this group, save evidence."""


# ---- text matchers (UI language, not obfuscated classes) --------------------
COMPOSER_TRIGGER_RE = re.compile(
    r"^\s*(?:Escribe algo|Write something|¿Qu[eé] estás pensando|What'?s on your mind)"
    r"[.…]{0,3}\s*$",
    re.IGNORECASE,
)
# [proven: dryrun_post.py STAMP_TRIGGER_JS — exact texts + /^Escribe algo\b/ fallback]
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
COMPOSER_MODAL_RE = re.compile(
    r"Crea una publicaci[oó]n|Create a (?:public )?post", re.IGNORECASE
)
PUBLISH_RE = re.compile(r"^(Publicar|Post|Publish)$", re.IGNORECASE)
PHOTO_LABEL_RE = re.compile(r"Fotograf[ií]a|Foto|Photo|Imagen|Image", re.IGNORECASE)


async def human_sleep(cfg: Config, log: log_fn, why: str = "") -> None:
    """[rule 6] uniform(min,max) seconds before any sensitive action."""
    s = random.uniform(cfg.delay_min, cfg.delay_max)
    log(f"sleep {s:.1f}s before {why or 'action'}")
    await asyncio.sleep(s)


async def _ensure_text_1to1(page: Page, box, text: str, cfg: Config, log: log_fn) -> None:
    """Content fidelity gate (rule 3): keyboard.type at human delay (proven
    dryrun_post.py path) — then READ BACK; mismatch = FlowError, we never
    publish wrong text.
    """
    await box.click()
    await page.keyboard.type(text, delay=random.randint(cfg.type_delay_min_ms,
                                                        cfg.type_delay_max_ms))
    await page.wait_for_timeout(800)
    got = (await box.inner_text()).strip()
    if got != text.strip():
        raise FlowError(
            f"composer text mismatch after typing ({len(got)} chars vs "
            f"{len(text)}) — refusing to continue"
        )
    log(f"text typed 1:1 ({len(text)} chars, read-back verified)")


# ---- the photo attach (rule 9 solution) --------------------------------------

async def attach_car_photos(page: Page, post: Post, cfg: Config, log: log_fn) -> None:
    """Upload photos car-by-car; order = post.txt car order (rules 2+3).

    The OS file dialog is NEVER allowed to open: Playwright feeds the file
    input directly (A) or answers the intercepted chooser (B).
    """
    for car in post.cars:
        if not car.files:
            continue
        paths = [str(f) for f in car.files]
        await human_sleep(cfg, log, f"attaching photos for {car.name}")

        # (A) [proven] hidden image input inside the composer dialog
        inputs = page.locator('div[role="dialog"] input[type="file"]')
        n = await inputs.count()
        for idx in range(n):
            inp = inputs.nth(idx)
            accept = (await inp.get_attribute("accept")) or ""
            if accept and not re.search(r"image", accept, re.IGNORECASE):
                continue
            try:
                await inp.set_input_files(paths, timeout=8000)
                log(f"{car.name}: {len(paths)} photo(s) -> dialog input[type=file]#{idx}")
                await _wait_upload_settled(page, log)
                break
            except PWTimeoutError:
                continue
        else:
            # (B) fallback: click the photo affordance, intercept filechooser
            try:
                async with page.expect_file_chooser(timeout=8000) as fc_info:
                    how = await _click_photo_button(page)
                chooser = await fc_info.value
                await chooser.set_files(paths)
                log(f"{car.name}: {len(paths)} photo(s) via FileChooser (clicked {how})")
                await _wait_upload_settled(page, log)
            except (PWTimeoutError, FlowError) as e:
                shot = await dump_evidence(
                    page, cfg.screenshot_dir, f"nofileinput_{car.name}",
                    html=await page.content())
                raise FlowError(
                    f"could not attach photos for {car.name}: no usable "
                    f"input[type=file] and no interceptable chooser ({e}). "
                    f"Evidence: {shot}"
                ) from e


async def _click_photo_button(page: Page) -> str:
    """[robust] Photo affordance inside the composer modal (aria/text match)."""
    candidates = (
        (page.get_by_role("button", name=PHOTO_LABEL_RE), "role+aria photo label"),
        (page.locator('div[role="dialog"] [role="button"][aria-label*="oto" i], '
                      'div[role="dialog"] [role="button"][aria-label*="hoto" i]'),
         "dialog aria foto/photo"),
    )
    for loc, why in candidates:
        try:
            first = loc.first
            if await first.count() and await first.is_visible():
                await first.click(timeout=5000)
                return why
        except PWTimeoutError:
            continue
    raise PWTimeoutError("photo button not found")


async def _wait_upload_settled(page: Page, log: log_fn) -> None:
    """[proven] after upload the modal renders per-thumbnail 'Editar' tiles
    (recording 06:41:47) — uploads POST to upload.facebook.com/.../photo/upload."""
    try:
        await page.wait_for_selector('div[role="dialog"] >> text=/Editar/i', timeout=25000)
        log("thumbnail tiles visible (upload settled)")
    except PWTimeoutError:
        log("WARN: no Editar tiles within 25s — continuing anyway")


# ---- flow: group_composer_es_v1 ----------------------------------------------

async def group_composer_es_v1(
    page: Page, post: Post, cfg: Config, log: log_fn = print
) -> None:
    """Spanish group composer (group 249803862915566 & same-layout groups):
    feed trigger 'Escribe algo...' -> modal -> text 1:1 -> photos ->
    random delay -> Publicar (only when AP_DRY_RUN=false).

    The runner has ALREADY navigated to the group URL and waited for feed
    hydration; this owns composer-internal steps only. All actions scoped to
    div[role="dialog"] — we can never type into a comment box by accident
    (proven discipline from dryrun_post.py).
    """
    box = page.locator('div[role="dialog"] [contenteditable="true"]').first

    # 0) a stale/already-open dialog? (proven behavior from dryrun_post.py)
    if await box.count() and await box.is_visible():
        log("composer dialog already open — skipping trigger")
    else:
        # dismiss overlay popups (notif prompts) the way the dryrun did
        for _ in range(2):
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1200)
        if not (await box.count() and await box.is_visible()):
            # 1) stamp TOPMOST trigger and trusted-click it  [proven JS]
            info = await page.evaluate(STAMP_TRIGGER_JS)
            log(f"trigger: {info}")
            if not str(info).startswith("topmost:"):
                shot = await dump_evidence(page, cfg.screenshot_dir,
                                           "no_composer_trigger", html=await page.content())
                raise FlowError(f"composer trigger 'Escribe algo...' not found. {shot}")
            trig = page.locator('[data-ap-target="1"]').first
            await trig.scroll_into_view_if_needed()
            try:
                await trig.click(timeout=10000)
            except PWTimeoutError as e:
                cover = await page.evaluate(  # proven "who covers the trigger?" diagnostic
                    "(() => { const el = document.querySelector('[data-ap-target=\"1\"]');"
                    " if (!el) return 'stamp-gone';"
                    " const r = el.getBoundingClientRect();"
                    " const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);"
                    " return top ? top.tagName + '.' + String(top.className).slice(0,60)"
                    "   + ' text=' + (top.innerText||'').slice(0,80) : 'none'; })()")
                shot = await dump_evidence(page, cfg.screenshot_dir,
                                           "trigger_blocked", html=await page.content(),
                                           extra={"cover": cover})
                raise FlowError(f"trigger click intercepted ({cover}). {shot}") from e

    # 2) composer textbox visible inside dialog; also confirm modal hint  [proven]
    try:
        await box.wait_for(state="visible", timeout=15000)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir, "no_composer_textbox",
                                   html=await page.content())
        raise FlowError(f"composer textbox not visible. Evidence: {shot}") from e
    await page.wait_for_timeout(2000)  # let modal finish rendering

    # 3) clear stale draft if the dialog restored one  [proven]
    existing = (await box.inner_text()).strip()
    if existing:
        log(f"NOTE: dialog had stale draft {existing[:60]!r} — clearing")
        await box.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(500)

    # 4) text 1:1 with read-back gate
    await _ensure_text_1to1(page, box, post.text, cfg, log)

    # 5) photos (rule 9: OS dialog answered in-process)
    await attach_car_photos(page, post, cfg, log)

    # 6) publish gate  [rule 4: dry-run stops here]
    await human_sleep(cfg, log, "PUBLISH" if not cfg.dry_run else "stop (DRY RUN)")
    pub = page.locator('div[role="dialog"] [role="button"]').filter(
        has_text=PUBLISH_RE).last
    try:
        await pub.wait_for(state="visible", timeout=10000)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir, "no_publish_button",
                                   html=await page.content())
        raise FlowError(f"'Publicar' button not found in dialog. Evidence: {shot}") from e

    if cfg.dry_run:
        shot = await dump_evidence(
            page, cfg.screenshot_dir, "dryrun_composer_ready",
            html=await page.content(),
            extra={
                "text_preview": post.text[:200],
                "photo_batches": {c.name: [f.name for f in c.files] for c in post.cars},
                "publish_button_text": (await pub.inner_text()).strip(),
            },
        )
        log(f"DRY RUN — composer fully staged (text + {len(post.all_files)} photo(s), "
            f"Publicar visible) — NOT clicked. Evidence: {shot}")
        try:
            await page.keyboard.press("Escape")
            log("closed composer (nothing posted)")
        except Exception:
            pass
        return

    await pub.click(timeout=5000)
    log("CLICKED Publicar")


# ---- registry / dispatcher ----------------------------------------------------

REGISTRY: dict[str, Callable[..., Awaitable[None]]] = {
    "group_composer_es_v1": group_composer_es_v1,
}


def get_flow(code: str):
    try:
        return REGISTRY[code]
    except KeyError:
        raise KeyError(
            f"unknown posting_code {code!r}; implemented flows: {sorted(REGISTRY)} — "
            "record the group, add the function, never guess"
        ) from None
