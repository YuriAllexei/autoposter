"""Posting flows — one function per composer LAYOUT, shared by all groups.

Flows are keyed by the composer UI, NOT by group (diff of all recordings on
2026-09-22): every Spanish group we have feeds the identical 'Escribe algo...'
trigger, 'Crea una publicación pública...' modal and an identical
GroupCometComposerToolbar payload (same sprouts + post_button_label
'Publicar') — so one flow serves N groups. Since the dynamic-targets change
(main.py), runs post to LIVE-joined groups with group_composer_es_v1; the
SOLE per-group gate is that the trigger renders within 5s (composer_gate).
A group whose layout ever differs (non-Spanish UI, rules/questions gate)
would need a new layout function + dispatch — record it first, never guess.

Repo rules obeyed by every flow:
  - random uniform(AP_DELAY_MIN, AP_DELAY_MAX) sleep before the sensitive
    publish step. There is NO sleep per photo batch: photos attach as fast as
    possible, and upload settling is gated by the condition-wait
    _wait_upload_settled, not by a timer
  - post text inserted 1:1 with READ-BACK verification (rule 1): pasted at
    paste-speed (document.execCommand('insertText') per line + Enter between
    lines, so newlines survive) instead of char-by-char keyboard.type — then
    read back, with ONE keyboard.type fallback; a second mismatch = FlowError
  - photos attached via the composer's hidden input[type=file] (proven in
    recording 20260917T063422Z: `input.x1s85apg` logged `C:\\fakepath\\car.jpg`)
    — Playwright set_input_files answers it in-process; the OS dialog is
    never opened. FileChooser-click is the fallback. One batch per car
    folder, cars in sorted order => attachment order == post.txt car order
  - DRY RUN stops before Publicar and saves evidence instead

Selector provenance: recording 20260917T063422Z (group 249803862915566) +
the 2026-09-17 recording sessions (topmost-trigger stamp, dialog-scoped actions, stale
draft clear, keyboard.type). Match by text/aria/role, never obfuscated classes.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import unicodedata
from collections.abc import Callable
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
# [proven: recordings 20260917T063422Z + 2026-09-22 dry runs — exact texts +
#  /^Escribe algo\b/ fallback]
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
PUBLISH_RE = re.compile(r"^(Publicar|Post|Publish)$", re.IGNORECASE)
PHOTO_LABEL_RE = re.compile(r"Fotograf[ií]a|Foto|Photo|Imagen|Image", re.IGNORECASE)


async def human_sleep(cfg: Config, log: log_fn, why: str = "",
                      lo: float | None = None, hi: float | None = None
                      ) -> None:
    """[rule 5] uniform(min,max) seconds before any sensitive action.
    lo/hi override the general range (used for the 10-15s group switch)."""
    s = random.uniform(cfg.delay_min if lo is None else lo,
                       cfg.delay_max if hi is None else hi)
    log(f"sleep {s:.1f}s before {why or 'action'}")
    await asyncio.sleep(s)


async def _ensure_text_1to1(page: Page, box, text: str, cfg: Config, log: log_fn) -> None:
    """Content fidelity gate (rule 3), paste-speed path.

    `document.execCommand('insertText', false, line)` is a paste-like insertion
    that fires the input events FB's React composer listens to (same as a real
    Ctrl+V); an Enter keypress per line break keeps newlines 1:1. That is
    orders of magnitude faster than keyboard.type on a long post. The READ-BACK
    comparison below is still the hard gate: whatever path put the text in, if
    inner_text() doesn't match we fall back ONCE to char-by-char typing (whole
    post, human jitter) and re-read; still mismatched = FlowError, we never
    publish text that failed read-back.
    """
    await box.click()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i:
            await page.keyboard.press("Enter")     # composer line break, 1:1
        if line:
            await page.evaluate(
                "(t) => document.execCommand('insertText', false, t)", line)
    await page.wait_for_timeout(800)
    got = (await box.inner_text()).strip()
    if got != text.strip():
        log(f"paste read-back mismatch ({len(got)} chars vs {len(text)}) — "
            f"falling back to keyboard.type once")
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
    log(f"text pasted 1:1 ({len(text)} chars, read-back verified)")


# ---- the photo attach (rule 3 contract) --------------------------------------

async def attach_car_photos(page: Page, post: Post, cfg: Config, log: log_fn) -> None:
    """Upload photos car-by-car; order = post.txt car order (rules 2+3).

    The OS file dialog is NEVER allowed to open: Playwright feeds the file
    input directly (A) or answers the intercepted chooser (B).
    """
    for car in post.cars:
        if not car.files:
            continue
        paths = [str(f) for f in car.files]

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
) -> dict:
    """Spanish group composer (group 249803862915566 & same-layout groups):
    feed trigger 'Escribe algo...' -> modal -> text 1:1 -> photos ->
    random delay -> Publicar (only when AP_DRY_RUN=false).

    Returns {'evidence': <screenshot path>} after a successful DRY run (the
    runner stamps the group index with it) or {'published': True} when live.

    The runner has ALREADY navigated to the group URL and waited for feed
    hydration; this owns composer-internal steps only. All actions scoped to
    div[role="dialog"] — we can never type into a comment box by accident
    (proven discipline from the 2026-09-17/22 sessions).
    """
    box = page.locator('div[role="dialog"] [contenteditable="true"]').first

    # 0) a stale/already-open dialog? (proven behavior, recording 20260917T063422Z)
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

    # 2) composer textbox visible inside dialog  [proven]
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

    # 5) photos (rule 3: OS dialog answered in-process)
    await attach_car_photos(page, post, cfg, log)

    # 6) publish gate  [rule 6 fail-safe: dry-run stops here]
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
        return {"evidence": str(shot)}

    await pub.click(timeout=5000)
    log("CLICKED Publicar")
    return {"published": True}


# ================= CROSSPOST: "Publicar en más lugares" =====================
# Ground truth: recording 20260923T021450Z_marketplace_article_fetching_and_mass_pu
# + LIVE read-only probe 2026-09-23 (.local-capture/crosspost_probe/report.md —
# 5 dialog opens, every close via Cancelar) + first LIVE dry-run 2026-09-23
# (lessons baked in: the listing menu renders ASYNC — wait_for, never count();
# and the graphql fast-feed MISSES 'Requieren atención' listings, so the
# page's CARDS are the ground-truth publish set).
# Layout key: crosspost_dialog_es_v1 (Spanish UI — the dialog's menu/labels
# were never probed in English, so we fail loudly instead of guessing).

CROSSPOST_MENU_ITEM = "Publicar en más lugares"
CROSSPOST_DIALOG_QUERY = "MarketplaceCrossPostDialogQuery"
CROSSPOST_CANCEL = "Cancelar"
CROSSPOST_PUBLISH = "Publicar"
CROSSPOST_MORE_PREFIX = "Más opciones para "

#: the only clickable rows in the dialog (probe 2026-09-23): checkboxes live
#: in 'En tus grupos'; suggested rows are buttons — never targeted.
CROSSPOST_ROW_SEL = '[role="dialog"] [role="checkbox"]'

#: USER SPEC (2026-09-23): consecutive checkbox rows are clicked with a
#: 0.2-1s micro-pause (rapid human ticking, NOT the 1-3s action sleeps).
CROSSPOST_ROW_CLICK_DELAY = (0.2, 1.0)

#: shared close-detector: the modal dialog is gone when no aria-modal
#: [role=dialog] remains (probe 2026-09-23: FB removes the node / its flag)
_DIALOG_GONE_JS = ("() => ![...document.querySelectorAll('[role=dialog]')]"
                   ".some(d => d.getAttribute('aria-modal') === 'true')")


def _norm(s: str) -> str:
    """Text-folding for name/label matching (accents + whitespace + case):
    group titles carry unicode emoji/acents and dialog rows vs graphql 'name'
    must compare equal regardless of NFC/NFD or line breaks."""
    return " ".join(unicodedata.normalize("NFC", str(s)).split()).casefold()


def _tag_ranks(groups: list) -> list:
    """Tag each group with which OCCURRENCE of its folded name it is, in
    payload order (payload order == DOM row order).

    [live lesson 2026-09-23, batch-2 mis-tick] real dialogs contain DUPLICATE
    GROUP NAMES (two joined 'venta de carros chihuahua'!) and rows carry no
    id, so a name->first-row mapping silently ticks the wrong group. Both the
    crosspost dialog and the individual share picker use this one helper.
    """
    ranks: dict[str, int] = {}
    for g in groups:
        k = _norm(g["name"])
        g["rank"] = ranks.get(k, 0)
        ranks[k] = g["rank"] + 1
    return groups


def parse_crosspost_targets(text: str) -> tuple[list, int, str]:
    """Pure/testable: first chunk of MarketplaceCrossPostDialogQuery ->
    ([{'id','name'}...] in dialog order), per-submission cap, and the LISTING
    id the dialog belongs to (cross_post_info.all_listings[0].id — [proven:
    probe §4] the payload carries it, so card enumeration never needs listing
    ids up front). [proven] edges[] order == checkbox DOM order."""
    body = text.lstrip()
    for pref in ("for(;;);", ")]}'"):
        if body.startswith(pref):
            body = body[len(pref):].lstrip()
    data = json.loads(body.split("\n", 1)[0])["data"]["viewer"]
    limit = int(data.get("marketplace_crosspost_limit") or 20)
    edges = ((data.get("marketplace_suggested_crosspost_targets")
              or {}).get("edges") or [])
    out = [{"id": str(e["node"]["id"]), "name": str(e["node"].get("name") or "")}
           for e in edges
           if isinstance(e.get("node"), dict) and e["node"].get("id")]
    cpi = ((data.get("marketplace_listing") or {}).get("cross_post_info") or {})
    first = (cpi.get("all_listings") or [{}])[0]
    return out, limit, str(first.get("id") or "")


async def listing_card_titles(page: Page, settle_ms: int = 1200,
                              max_wait_ms: int = 30000) -> list[str]:
    """Every sellable card on /marketplace/you/selling — ACTIVE plus flagged
    'Requieren atención' ones (2026-09-23 live lesson). [proven] each card
    exposes exactly one [role=button][aria-label^='Más opciones para ']
    (probe §2). Polls until the count is stable so we never enumerate a
    half-loaded feed."""
    sel = f'[role="button"][aria-label^="{CROSSPOST_MORE_PREFIX}"]'
    try:
        await page.locator(sel).first.wait_for(state="attached",
                                               timeout=max_wait_ms)
    except PWTimeoutError:
        return []
    prev, now = -1, await page.locator(sel).count()
    while now != prev and now > 0:
        prev = now
        await page.wait_for_timeout(settle_ms)
        now = await page.locator(sel).count()
    titles = []
    for i in range(now):
        al = await page.locator(sel).nth(i).get_attribute("aria-label") or ""
        titles.append(al.partition(CROSSPOST_MORE_PREFIX)[2].strip())
    return [t for t in titles if t]


def _dialog_response_filter():
    """Fired ONLY by this button, and only one dialog is ever open — the
    friendly name alone identifies the response (listing id is unknown until
    the response itself carries it)."""
    def ok(resp) -> bool:
        try:
            req = resp.request
            return ("graphql" in req.url
                    and CROSSPOST_DIALOG_QUERY in (req.post_data or ""))
        except Exception:  # a detached request is simply not ours
            return False
    return ok


MORE_ATTACH_WAIT_MS = 20000   # condition-wait, like listing_card_titles


async def _find_more_button(page: Page, title: str, cfg: Config,
                            log: log_fn):
    """The '...' locator of one listing card, tolerant of FB's async
    hydration.

    RACE LESSON (live 2026-09-23, run 20260923T220814Z): the old code ran an
    INSTANT count() right after a fresh goto and blinked at a card whose
    aria-label attached ~1s later — the failure screenshot literally showed
    the button present. Condition-wait first (up to MORE_ATTACH_WAIT_MS),
    then keep the proven folded-title rescue scan for drifted labels.
    """
    sel = f'[role="button"][aria-label="{CROSSPOST_MORE_PREFIX}{title}"]'
    btn = page.locator(sel).first
    try:
        await btn.wait_for(state="attached", timeout=MORE_ATTACH_WAIT_MS)
    except PWTimeoutError:
        pass  # exact label never arrived: the rescue scan reports it properly
    if await btn.count():
        return btn
    all_btns = page.locator(
        f'[role="button"][aria-label^="{CROSSPOST_MORE_PREFIX}"]')
    hits = []
    for i in range(await all_btns.count()):
        al = await all_btns.nth(i).get_attribute("aria-label") or ""
        if _norm(al.partition(CROSSPOST_MORE_PREFIX)[2]) == _norm(title):
            hits.append(all_btns.nth(i))
    if len(hits) != 1:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "crosspost_no_button")
        raise FlowError(f"no unique MORE button for {title!r} "
                        f"({len(hits)} folded matches). Evidence: {shot}")
    return hits[0]


async def open_crosspost_dialog(page: Page, title: str, cfg: Config,
                                log: log_fn = print) -> tuple[list, int, str]:
    """Card '...' -> exact 'Publicar en más lugares' -> dialog. Returns
    (candidate groups, cap, listing id from the dialog payload). Raises
    FlowError with evidence otherwise."""
    btn = await _find_more_button(page, title, cfg, log)
    await human_sleep(cfg, log, "before opening the listing '...' menu",
                      cfg.crosspost_action_min, cfg.crosspost_action_max)
    async with page.expect_response(_dialog_response_filter(),
                                    timeout=30000) as rinfo:
        await btn.click()
        item = page.get_by_role("menuitem", name=CROSSPOST_MENU_ITEM, exact=True)
        try:
            await item.first.wait_for(state="visible", timeout=10000)
        except PWTimeoutError:
            shot = await dump_evidence(page, cfg.screenshot_dir,
                                       "crosspost_menu_never_opened")
            raise FlowError(f"listing menu never rendered "
                            f"'{CROSSPOST_MENU_ITEM}'. Evidence: {shot}") from None
        await asyncio.sleep(
            random.uniform(*CROSSPOST_ROW_CLICK_DELAY))
        await item.first.click()
    resp = await rinfo.value
    try:
        groups, limit, listing_id = parse_crosspost_targets(await resp.text())
    except Exception as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "crosspost_dialog_parse")
        raise FlowError(f"dialog payload unreadable ({type(e).__name__}: {e}). "
                        f"Evidence: {shot}") from e
    # [live lesson 2026-09-23, batch-2 mis-tick] the dialog list contains
    # DUPLICATE GROUP NAMES (two joined 'venta de carros chihuahua'!).
    # Payload order == DOM row order (the skeleton wait above already
    # relies on that 1:1), so tag each group with which occurrence of its
    # NAME it is — select clicks the same occurrence among the rows.
    _tag_ranks(groups)
    if not groups:
        shot = await dump_evidence(page, cfg.screenshot_dir, "crosspost_no_targets")
        raise FlowError(f"dialog offered 0 groups for {title!r}. "
                        f"Evidence: {shot}")
    log(f"crosspost dialog [{title[:32]!r}]: {len(groups)} groups, cap {limit}, "
        f"listing id {listing_id or '?'}")
    #: [live 2026-09-23] the dialog hydrates SLOWER than its graphql answer
    #: — the payload is readable while the rows are still skeleton bars
    #: (0 [role=checkbox]). Do not call the dialog open until the DOM holds
    #: what the payload promised; select would otherwise find no rows.
    if groups:
        try:
            await page.wait_for_function(
                "(args) => document.querySelectorAll(args.sel).length"
                " >= args.n",
                arg={"sel": CROSSPOST_ROW_SEL, "n": len(groups)},
                timeout=15000,
            )
        except PWTimeoutError as e:
            shot = await dump_evidence(page, cfg.screenshot_dir,
                                       "crosspost_dialog_skeleton")
            raise FlowError(
                f"dialog never filled its {len(groups)} rows (skeleton "
                f"after 15s). Evidence: {shot}") from e
    return groups, limit, listing_id


async def select_crosspost_groups(page: Page, groups: list,
                                  cfg: Config, log: log_fn = print) -> int:
    """Check exactly these groups in the OPEN dialog. [proven] checkboxes
    exist ONLY in 'En tus grupos' (suggested rows are 'Unirte al grupo'
    buttons — NEVER clickable by design: the selector cannot reach them);
    rows carry no id — match folded first-line text BY OCCURRENCE RANK
    (duplicate names exist in real dialogs!), read back aria-checked after
    every trusted click, and re-read ALL of one last time before the
    dialog may be submitted (1:1 discipline)."""
    rows = page.locator(CROSSPOST_ROW_SEL)
    n = await rows.count()
    # every DOM row index per folded name, in order (duplicates KEPT —
    # open_crosspost_dialog tagged the payload with occurrence ranks)
    by_name: dict[str, list[int]] = {}
    for i in range(n):
        key = _norm((await rows.nth(i).inner_text()).split("\n", 1)[0])
        by_name.setdefault(key, []).append(i)
    picked: list[int] = []
    for g in groups:
        idxs = by_name.get(_norm(g["name"]), [])
        rank = int(g.get("rank") or 0)
        if rank >= len(idxs):
            shot = await dump_evidence(page, cfg.screenshot_dir,
                                       "crosspost_row_missing")
            raise FlowError(f"dialog row for {g['name']!r} occurrence "
                            f"#{rank + 1} missing ({len(idxs)} same-name rows "
                            f"of {n}). Evidence: {shot}")
        picked.append(idxs[rank])
    for pos, (i, g) in enumerate(zip(picked, groups, strict=True), 1):
        if pos > 1:   # first click is immediate; humans pause BETWEEN ticks
            await asyncio.sleep(random.uniform(*CROSSPOST_ROW_CLICK_DELAY))
        await rows.nth(i).click()
        if await rows.nth(i).get_attribute("aria-checked") != "true":
            shot = await dump_evidence(page, cfg.screenshot_dir,
                                       "crosspost_check_fail")
            raise FlowError(f"row {g['name']!r} click left aria-checked false. "
                            f"Evidence: {shot}")
        log(f"crosspost: checked '{str(g['name'])[:44]}' ({pos}/{len(picked)})")
    # FINAL SWEEP (live 2207Z report: user watched checks disappear before
    # submit): a toggle flipping AFTER its own verify must never reach
    # Publicar unnoticed — re-read every row, abort with evidence otherwise.
    for i, g in zip(picked, groups, strict=True):
        if await rows.nth(i).get_attribute("aria-checked") != "true":
            shot = await dump_evidence(page, cfg.screenshot_dir,
                                       "crosspost_check_lost")
            raise FlowError(f"row {g['name']!r} LOST its check before submit. "
                            f"Evidence: {shot}")
    return len(picked)


async def finish_crosspost_dialog(page: Page, publish: bool, cfg: Config,
                                  log: log_fn = print) -> str:
    """Dry-run: screenshot the fully-checked dialog, then Cancelar and verify
    it is gone (NOTHING submitted — the probe closed every dialog this way).
    Live: screenshot first, then Publicar, then the dialog MUST disappear.

    [proven LIVE 2026-09-23, recording 20260923T042928Z]: the click fires
    ONE request — MarketplaceForSaleItemCreateXPostsMutation
    doc_id=9628145373942390, input {item_id, additional_target_ids =
    selected group ids, actor_id, listing_email_id:null, client_mutation_id,
    attribution_id_v2} -> data.for_sale_item_create_xposts.item. Then the
    dialog is simply GONE (no confirmation UI; user clicked the page behind
    it 9 s later). We keep the DOM route: FB validates the selection
    client-side and the attribution token is minted by the app — a direct
    mutation replay would be guesswork, this path is observed truth."""
    staged_shot = await dump_evidence(page, cfg.screenshot_dir, "crosspost_staged")
    if publish:
        btn = page.get_by_role("button", name=CROSSPOST_PUBLISH, exact=True)
        if await btn.count() != 1 or not await btn.first.is_visible():
            raise FlowError(f"'{CROSSPOST_PUBLISH}' button not unique/visible "
                            f"({await btn.count()}) — refusing to guess-click")
        await human_sleep(cfg, log, "all groups checked -> Publicar",
                          cfg.crosspost_action_min, cfg.crosspost_action_max)
        await btn.first.click()
        try:
            await page.wait_for_function(_DIALOG_GONE_JS,
                            timeout=20000)
        except PWTimeoutError as e:
            shot = await dump_evidence(page, cfg.screenshot_dir,
                                       "crosspost_publish_timeout")
            raise FlowError(
                "dialog still open 20s after Publicar — OUTCOME UNKNOWN, "
                f"check manually before retrying. Evidence: {shot}") from e
        after = await dump_evidence(page, cfg.screenshot_dir,
                                    "crosspost_published")
        return f"published (before:{staged_shot} after:{after})"

    btn = page.get_by_role("button", name=CROSSPOST_CANCEL, exact=True)
    if await btn.count() != 1 or not await btn.first.is_visible():
        raise FlowError(f"'{CROSSPOST_CANCEL}' button not unique/visible "
                        f"({await btn.count()})")
    await human_sleep(cfg, log, "staged dialog -> Cancelar",
                      cfg.crosspost_action_min, cfg.crosspost_action_max)
    await btn.first.click()
    #: [live 2026-09-23 race lesson] Cancelar closes the dialog ASYNCHRONOUSLY
    #: (fade/unmount) — an immediate :visible count sees the dying dialog and
    #: falsely reports 'survived'. Wait for the same predicate the publish
    #: path uses, short timeout, then judge.
    try:
        await page.wait_for_function(_DIALOG_GONE_JS, timeout=5000)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "crosspost_cancel_stuck")
        raise FlowError(
            f"dialog survived Cancelar (5s). Evidence: {shot}") from e
    return f"cancelled (dry run, NOTHING published; staged:{staged_shot})"

async def dismiss_crosspost_dialog(page: Page, log: log_fn = print) -> None:
    """Best-effort close after a mid-dialog failure — never let a stuck
    modal poison the next listing (Cancelar first, Escape as fallback;
    NEVER Publicar)."""
    try:
        btn = page.get_by_role("button", name=CROSSPOST_CANCEL, exact=True)
        if await btn.count() and await btn.first.is_visible():
            await btn.first.click()
        elif await page.locator('[role="dialog"]').count():
            await page.keyboard.press("Escape")
        log("crosspost: dialog dismissed after failure")
    except Exception as e:  # dismissal must never mask the original error
        log(f"crosspost: dialog dismissal failed ({type(e).__name__}: {e})")


# ==========================================================================
# INDIVIDUAL SHARE — one marketplace listing into ONE group per submission
# (the LISTING CARD's 'Compartir' -> hub -> 'Grupo' -> picker -> composer).
#
# GROUND TRUTH: recording 20260925T014900Z_marketplace_individual_listing_
# individua (shots 001 card / 002 hub / 003 picker / 004 composer / 005-006
# 'Ver más') + a LIVE read-only probe, scripts/probe_share_flow.py, run
# 2026-09-25 against the real selling page. The probe pinned the accessible
# NAMES the recorder could not (it logs visible text only):
#   card  'Compartir' button   -> [role=button], folded text 'Compartir'
#   hub dialog 'Compartir'     -> has a 'Compartir en' section, close
#                                 [role=button][aria-label='Cerrar'], a
#                                 'Compartir ahora' primary and the four
#                                 circles Messenger / WhatsApp / Grupo /
#                                 Copiar enlace; the GRUPO circle carries
#                                 aria-label 'Compartir en un grupo'
#                                 (exact-fold text 'Grupo' — never confusable
#                                 with 'Compartir ahora'/'Compartir en el
#                                 feed (solo lectura)')
#   picker 'Compartir en un grupo' -> search box placeholder 'Buscar grupos'
# Layout key: share_composer_es_v1. Everything here is text/aria/role only.
# ==========================================================================

SHARE_HUB_TEXT = "Compartir"
SHARE_GROUP_TEXT = "Grupo"
SHARE_PICKER_TITLE = "Compartir en un grupo"
SHARE_SEARCH_PLACEHOLDER = "Buscar grupos"
SHARE_COMPOSER_PLACEHOLDER = "Crea una publicación pública..."
SHARE_COMPOSER_TITLE = "Crear publicación"
SHARE_PUBLISH_TEXT = "Publicar"
SHARE_DESC_HEADING = "Descripción del vendedor"
SHARE_SEE_MORE_TEXT = "Ver más"
SHARE_PREVIEW_PENDING = "Creando vista previa del enlace"
SHARE_ATTACH_LABEL = "Agregar a tu publicación"
SHARE_ITEM_URL = "https://www.facebook.com/marketplace/item/{id}/"
#: both friendly names are fixed by the recording/probe (req bodies)
XPOST_GROUPS_OP = "CometGroupResharesSearchDataSourceQuery"
XPOST_MUTATION_OP = "ComposerStoryCreateMutation"

#: condition-wait ceiling for the share ritual steps (the plan's
#: `cfg.hard_timeout`). Config carries no such knob and Slice A must not add
#: config fields (that is the pipeline slice's scope), so the cap lives in one
#: proven module constant; the settle poll is the link-preview poll interval.
SHARE_STEP_TIMEOUT_MS = 30000
SHARE_SETTLE_POLL_MS = 500

#: stamps the CARD's 'Compartir' button: the selling feed renders one card per
#: listing and each card exposes its own Compartir, so the ONLY safe anchor is
#: the card that CONTAINS the listing title (same discipline as
#: _find_more_button's 'Más opciones para <title>').
_STAMP_SHARE_BTN_JS = r"""
(args) => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  document.querySelectorAll('[data-ap-share]').forEach(
    (e) => e.removeAttribute('data-ap-share'));
  const want = fold(args.title);
  const sel = '[role="button"], button, a, [role="link"]';
  const isShare = (el) => fold(el.textContent) === 'compartir'
                      || fold(el.getAttribute('aria-label') || '') === 'compartir';
  // RACE + DRIFT LESSON (dry ladder 2026-09-25): the card title node often
  // carries a price/status suffix ('TAHOE LT $340.000 en venta'), so exact
  // fold equality missed it; accept STARTS-WITH + short guard instead.
  const titleNodes = [...document.querySelectorAll('span,div,h1,h2,h3')]
    .filter((n) => {
      const t = fold(n.textContent);
      return t === want
          || (t.startsWith(want) && t.length <= want.length + 40);
    });
  if (!titleNodes.length) return 'no-card';
  let card = null;
  for (const n of titleNodes) {
    let q = n;
    for (let i = 0; i < 8 && q; i++) {
      if ([...q.querySelectorAll(sel)].some(isShare)) { card = q; break; }
      q = q.parentElement;
    }
    if (card) break;
  }
  if (!card) return 'none-in-card';
  const hits = [...card.querySelectorAll(sel)].filter(isShare);
  if (hits.length === 0) return 'none-in-card';
  if (hits.length > 1) return 'ambiguous:' + hits.length;
  hits[0].setAttribute('data-ap-share', '1');
  return 'ok';
}
"""

#: the hub is the modal dialog carrying BOTH the 'Compartir' title and the
#: 'Compartir en' section (probe 2026-09-25).
_WAIT_HUB_JS = r"""
() => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  return [...document.querySelectorAll('[role="dialog"]')].some((d) => {
    const t = fold(d.innerText || d.textContent);
    return t.includes('compartir') && t.includes('compartir en');
  });
}
"""

#: stamps the hub's Grupo circle (exact folded text 'Grupo' or the proven
#: aria-label 'Compartir en un grupo').
_STAMP_HUB_GROUP_JS = r"""
(args) => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  document.querySelectorAll('[data-ap-share-group]').forEach(
    (e) => e.removeAttribute('data-ap-share-group'));
  const want = fold(args.text), aria = fold(args.aria);
  const dialogs = [...document.querySelectorAll('[role="dialog"]')];
  const d = dialogs.find((x) => x.getAttribute('aria-modal') === 'true')
            || dialogs[dialogs.length - 1];
  if (!d) return 'no-dialog';
  const sel = '[role="button"], [role="link"], button, a';
  const hits = [...d.querySelectorAll(sel)].filter((el) =>
    fold(el.textContent) === want
    || fold(el.getAttribute('aria-label') || '') === aria);
  const inner = hits.filter((el) => !hits.some((o) => o !== el && el.contains(o)));
  if (inner.length === 0) return 'none';
  if (inner.length > 1) return 'ambiguous:' + inner.length;
  inner[0].setAttribute('data-ap-share-group', '1');
  return 'ok';
}
"""

#: picker hydration gate: the picker dialog must LIST the name we expect (the
#: graphql answer arrives BEFORE the rows do — same race as the crosspost
#: dialog's skeleton rows).
_PICKER_READY_JS = r"""
(args) => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const want = fold(args.name);
  if (!want) return true;
  return [...document.querySelectorAll('[role="dialog"]')].some(
    (d) => fold(d.innerText || d.textContent).includes(want));
}
"""

#: stamps a picker ROW whose folded FIRST LINE is exactly the group name (a
#: row also carries the privacy line '· Grupo público', an inner span does
#: not — that is what separates the row container from the label).
#: TWO innermost rows with the same exact name => 'ambiguous' (the picker is a
#: TYPEAHEAD of the joined groups; duplicate names exist — see _tag_ranks).
_STAMP_SHARE_ROW_JS = r"""
(args) => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim();
  document.querySelectorAll('[data-ap-share-row]').forEach(
    (e) => e.removeAttribute('data-ap-share-row'));
  const want = fold(args.name).toLowerCase();
  const dialogs = [...document.querySelectorAll('[role="dialog"]')];
  const d = dialogs.find((x) => x.getAttribute('aria-modal') === 'true')
            || dialogs[dialogs.length - 1];
  if (!d) return 'no-dialog';
  const sel = 'div,span,a,[role="button"],[role="link"]';
  const hits = [...d.querySelectorAll(sel)].filter((el) => {
    const raw = el.innerText || el.textContent || '';
    const first = fold(raw.split('\n')[0]).toLowerCase();
    return first === want && /p[úu]blico/i.test(fold(raw));
  });
  const inner = hits.filter((el) => !hits.some((o) => o !== el && el.contains(o)));
  if (inner.length === 0) return 'none';
  if (inner.length > 1) return 'ambiguous:' + inner.length;
  inner[0].setAttribute('data-ap-share-row', '1');
  return 'ok';
}
"""

#: read-back after picking the group: the picker has handed over to the SHARE
#: COMPOSER ('Crear publicación' header + the 'Crea una publicación pública...'
#: placeholder, shot 004).
_COMPOSER_OPEN_JS = r"""
(args) => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const ph = fold(args.ph), title = fold(args.title);
  return [...document.querySelectorAll('[role="dialog"]')].some((d) => {
    const t = fold(d.innerText || d.textContent);
    return t.includes(ph) && t.includes(title);
  });
}
"""

#: link-preview settle (recording 01:53:33 rendered the card as
#: '<title>\nCreando vista previa del enlace' and only later as a real card):
#: the pending line must be GONE and the attachment chrome present (footer
#: 'Agregar a tu publicación' or the preview's link back to the item).
_PREVIEW_SETTLED_JS = r"""
(args) => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const dialogs = [...document.querySelectorAll('[role="dialog"]')];
  const d = dialogs.find((x) => x.getAttribute('aria-modal') === 'true')
            || dialogs[dialogs.length - 1];
  if (!d) return false;
  const t = fold(d.innerText || d.textContent);
  if (t.includes(fold(args.pending))) return false;
  return t.includes(fold(args.attach))
      || !!d.querySelector('a[href*="/marketplace/item/"]');
}
"""

#: reads the 'Descripción del vendedor' block on the ITEM page, stamping the
#: 'Ver más' control when the description is collapsed. The description span
#: OWNS the control (recording xpath .../span[1]/div[1]/span[1]), so the first
#: ancestor that adds text beyond the control is the block.
_SHARE_DESC_JS = r"""
(args) => {
  const fold = (s) => (s || "").replace(/\s+/g, " ").trim();
  const want = fold(args.heading).toLowerCase();
  document.querySelectorAll('[data-ap-share-more]').forEach(
    (e) => e.removeAttribute('data-ap-share-more'));
  let head = null;
  for (const n of document.querySelectorAll('h1,h2,h3,h4,h5,span,div,strong,b')) {
    if (fold(n.innerText || n.textContent).toLowerCase() === want) { head = n; break; }
  }
  if (!head) return {found: false};
  const sel = 'div,span,a,[role="button"],button';
  let ctrl = null, scope = head.parentElement;
  for (let i = 0; i < 6 && scope && !ctrl; i++) {
    ctrl = [...scope.querySelectorAll(sel)].find((e) =>
      /^(ver m[aá]s|ver menos)$/i.test(fold(e.innerText || e.textContent)));
    if (!ctrl) scope = scope.parentElement;
  }
  const ctrlText = ctrl ? fold(ctrl.innerText || ctrl.textContent).toLowerCase() : '';
  let block = null;
  if (ctrl) {
    let el = ctrl;
    while (el.parentElement && fold(el.parentElement.innerText
             || el.parentElement.textContent) === fold(el.innerText
             || el.textContent)) el = el.parentElement;
    block = el.parentElement || el;
  }
  if (!block) block = scope || head.parentElement;
  const raw = block ? (block.innerText || block.textContent || '') : '';
  const kept = [];
  for (const ln of raw.split('\n')) {
    const f = fold(ln).toLowerCase();
    if (f === want || /^(ver m[aá]s|ver menos)$/.test(f)) continue;
    kept.push(ln);
  }
  const text = kept.join('\n').replace(/^\s+|\s+$/g, '');
  if (ctrl && ctrlText === 'ver más') ctrl.setAttribute('data-ap-share-more', '1');
  return {found: true, text: text, has_more: ctrlText === 'ver más',
          has_less: ctrlText === 'ver menos'};
}
"""


def _obj_field(obj, key: str, default: str = "") -> str:
    """One field off a Mapping OR an object (listings.ActiveListing is a
    TypedDict, tests hand plain dicts, share.py may hand a small dataclass)."""
    for get in (lambda: obj[key], lambda: getattr(obj, key)):
        try:
            val = get()
        except (TypeError, KeyError, IndexError, AttributeError):
            continue
        if val:
            return str(val)
    return default


def _graphql_filter(op: str):
    """Friendly-name filter shared by both share ops (the req body carries
    `fb_api_req_friendly_name=<op>`)."""
    def ok(resp) -> bool:
        try:
            req = resp.request
            return "graphql" in req.url and op in (req.post_data or "")
        except Exception:
            return False
    return ok


def parse_share_targets(text: str) -> list[dict]:
    """Pure/testable: CometGroupResharesSearchDataSourceQuery (or the
    '...DialogPushPageContentQuery' sibling) -> [{'id','name'}] in payload
    order. [proven: probe §5] both carry `viewer.actor.groups.nodes[]` =
    {id, name, ...}; the push-page variant uses edges[].node. Order == DOM row
    order, so the index here is the only truth about row order."""
    body = str(text or "").lstrip()
    for pref in ("for(;;);", ")]}'", "while(1);"):
        if body.startswith(pref):
            body = body[len(pref):].lstrip()
    data = json.loads(body.split("\n", 1)[0])["data"]
    nodes: list = []

    def walk(node) -> bool:
        if isinstance(node, dict):
            for key in ("nodes", "edges"):
                seq = node.get(key)
                if isinstance(seq, list):
                    for item in seq:
                        if not isinstance(item, dict):
                            continue
                        cand = item.get("node") if key == "edges" else item
                        if isinstance(cand, dict) and cand.get("id"):
                            nodes.append(cand)
                    if nodes:
                        return True
            for val in node.values():
                if walk(val):
                    return True
        elif isinstance(node, list):
            for val in node:
                if walk(val):
                    return True
        return False

    walk(data)
    return [{"id": str(n["id"]), "name": str(n.get("name") or "")} for n in nodes]


async def _read_share_desc(page: Page, cfg: Config) -> dict:
    """Condition-wait (bounded poll, never a fixed sleep) for the item page's
    'Descripción del vendedor' block to exist."""
    attempts = max(1, SHARE_STEP_TIMEOUT_MS // SHARE_SETTLE_POLL_MS)
    info: dict = {"found": False}
    for i in range(attempts):
        info = await page.evaluate(_SHARE_DESC_JS, {"heading": SHARE_DESC_HEADING})
        if isinstance(info, dict) and info.get("found"):
            return info
        if i + 1 < attempts:
            await page.wait_for_timeout(SHARE_SETTLE_POLL_MS)
    return info if isinstance(info, dict) else {"found": False}


async def fetch_listing_description(page: Page, cfg: Config, log: log_fn = print,
                                    listing=None) -> str:
    """A1: the LISTING's own description text, verbatim (1:1), taken from the
    item page's 'Descripción del vendedor' block — the seller's tab. Clicks
    'Ver más' ONCE when the block is collapsed (recording 01:55:27), then
    re-reads; a click that does not grow the text is logged, never fatal."""
    listing_id = _obj_field(listing, "id")
    url = _obj_field(listing, "url")
    if not url:
        if not listing_id:
            raise FlowError("fetch_listing_description: listing has neither "
                            "url nor id")
        url = SHARE_ITEM_URL.format(id=listing_id)
    if url not in (_obj_field(page, "url")):
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    info = await _read_share_desc(page, cfg)
    if not info.get("found"):
        shot = await dump_evidence(page, cfg.screenshot_dir, "share_desc_missing")
        raise FlowError(f"'{SHARE_DESC_HEADING}' section never rendered at "
                        f"{url}. Evidence: {shot}")
    if info.get("has_more"):
        more = page.locator('[data-ap-share-more="1"]').first
        before = str(info.get("text") or "")
        if await more.count() and await more.is_visible():
            await more.click(timeout=8000)
            after = await _read_share_desc(page, cfg)
            grown = str(after.get("text") or "")
            if len(grown) <= len(before):
                log(f"share: '{SHARE_SEE_MORE_TEXT}' click did not grow the "
                    f"description ({len(before)} -> {len(grown)} chars) — "
                    f"using what the page gave us")
            else:
                info = after
        else:
            log("share: 'Ver más' stamped but not clickable — using the "
                "collapsed text")
    text = str(info.get("text") or "").strip()
    log(f"share: description for {listing_id or url} = {len(text)} chars")
    return text


async def _find_share_button(page: Page, title: str, cfg: Config, log: log_fn):
    """Poll the card-stamp JS until the card hydrates (same discipline as
    _find_more_button — the selling feed renders skeletons first; an instant
    single stamp blinked at a skeleton on the 2026-09-25 dry ladder)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SHARE_STEP_TIMEOUT_MS / 1000.0
    info = "no-card"
    while True:
        info = str(await page.evaluate(_STAMP_SHARE_BTN_JS, {"title": title}))
        if info.startswith("ok"):
            return page.locator('[data-ap-share="1"]').first
        if info.startswith("ambiguous") or loop.time() >= deadline:
            break
        await page.wait_for_timeout(SHARE_SETTLE_POLL_MS)
    shot = await dump_evidence(page, cfg.screenshot_dir, "share_no_button")
    raise FlowError(f"no unique card 'Compartir' button for {title!r} "
                    f"({info}, after a {SHARE_STEP_TIMEOUT_MS} ms hydration "
                    f"wait). Evidence: {shot}")


async def _find_hub_group_circle(page: Page, cfg: Config, log: log_fn):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SHARE_STEP_TIMEOUT_MS / 1000.0
    info = "no-dialog"
    while True:
        info = str(await page.evaluate(_STAMP_HUB_GROUP_JS,
                                       {"text": SHARE_GROUP_TEXT,
                                        "aria": SHARE_PICKER_TITLE}))
        if info.startswith("ok"):
            return page.locator('[data-ap-share-group="1"]').first
        if info.startswith("ambiguous") or loop.time() >= deadline:
            break
        await page.wait_for_timeout(SHARE_SETTLE_POLL_MS)
    shot = await dump_evidence(page, cfg.screenshot_dir,
                               "share_no_group_circle")
    raise FlowError(f"share hub has no unique '{SHARE_GROUP_TEXT}' circle "
                    f"({info}, after a {SHARE_STEP_TIMEOUT_MS} ms hydration "
                    f"wait). Evidence: {shot}")


async def open_share_hub(page: Page, listing, cfg: Config,
                         log: log_fn = print) -> list[dict]:
    """A2: card 'Compartir' -> hub -> 'Grupo' -> picker. Returns the picker's
    groups as [{'id','name','rank'}] in payload/row order (rank = occurrence
    of that same folded name). Raises FlowError with evidence otherwise.
    NOTHING downstream of the picker is touched here."""
    title = _obj_field(listing, "title")
    btn = await _find_share_button(page, title, cfg, log)
    await human_sleep(cfg, log, "before opening the listing 'Compartir' hub",
                      cfg.crosspost_action_min, cfg.crosspost_action_max)
    await btn.click()
    try:
        await page.wait_for_function(_WAIT_HUB_JS, timeout=SHARE_STEP_TIMEOUT_MS)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir, "share_hub_missing")
        raise FlowError(f"share hub never rendered for {title!r}. "
                        f"Evidence: {shot}") from e
    log(f"share: hub open for {title[:44]!r}")
    circle = await _find_hub_group_circle(page, cfg, log)
    await human_sleep(cfg, log, "hub -> 'Grupo' circle",
                      cfg.crosspost_action_min, cfg.crosspost_action_max)
    #: the picker's own group payload arrives on the circle click — arm the
    #: response BEFORE it so the eligible groups can never be missed.
    try:
        async with page.expect_response(_graphql_filter(XPOST_GROUPS_OP),
                                        timeout=SHARE_STEP_TIMEOUT_MS) as rinfo:
            await circle.click()
        resp = await rinfo.value
        groups = parse_share_targets(await resp.text())
    except FlowError:
        raise
    except Exception as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "share_picker_parse")
        raise FlowError(f"share picker payload unreadable "
                        f"({type(e).__name__}: {e}). Evidence: {shot}") from e
    if not groups:
        shot = await dump_evidence(page, cfg.screenshot_dir, "share_no_targets")
        raise FlowError(f"share picker offered 0 groups for {title!r}. "
                        f"Evidence: {shot}")
    _tag_ranks(groups)
    first = groups[0]["name"]
    try:
        await page.wait_for_function(_PICKER_READY_JS, arg={"name": first},
                                     timeout=SHARE_STEP_TIMEOUT_MS)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "share_picker_skeleton")
        raise FlowError(f"share picker never listed {first!r} "
                        f"({len(groups)} in payload). Evidence: {shot}") from e
    log(f"share: hub opened, {len(groups)} groups in payload")
    return groups


async def _fill_share_search(page: Page, name: str, cfg: Config,
                             log: log_fn) -> None:
    """Paste the group name into 'Buscar grupos' (paste ritual: insertText
    fires the events FB's React input listens for, exactly like the composer)."""
    box = page.get_by_placeholder(SHARE_SEARCH_PLACEHOLDER).first
    try:
        await box.wait_for(state="visible", timeout=SHARE_STEP_TIMEOUT_MS)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "share_no_search_box")
        raise FlowError(f"'{SHARE_SEARCH_PLACEHOLDER}' input never appeared. "
                        f"Evidence: {shot}") from e
    await box.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    await page.evaluate("(t) => document.execCommand('insertText', false, t)",
                        name)
    log(f"share: searched {name[:44]!r} in '{SHARE_SEARCH_PLACEHOLDER}'")


async def _share_search(page: Page, query: str, name: str, cfg: Config,
                        log: log_fn) -> int:
    """Type `query` into 'Buscar grupos' and wait for the debounced typeahead
    (it answers with the SAME op as the picker boot). The response is armed
    BEFORE the input so the answer can never be missed; a search that answers
    nothing is NOT fatal — the DOM decides, and a missing typeahead only costs
    the log line. Returns the row count the payload promised."""
    promised = 0
    try:
        async with page.expect_response(_graphql_filter(XPOST_GROUPS_OP),
                                        timeout=SHARE_STEP_TIMEOUT_MS) as rinfo:
            await _fill_share_search(page, query, cfg, log)
        resp = await rinfo.value
        promised = len(parse_share_targets(await resp.text()))
    except FlowError:
        raise
    except Exception as e:
        log(f"share: typeahead response not observed for {query[:44]!r} "
            f"({type(e).__name__}) — judging the DOM directly")
    try:
        await page.wait_for_function(_PICKER_READY_JS, arg={"name": name},
                                     timeout=SHARE_STEP_TIMEOUT_MS)
    except PWTimeoutError:
        log(f"share: picker never listed {name[:44]!r} after the search "
            f"(payload promised {promised} rows)")
    return promised


async def pick_share_group(page: Page, group, cfg: Config,
                           log: log_fn = print) -> None:
    """A3: search the group in the picker and click its row, then read back
    that the SHARE COMPOSER opened. NEVER guesses between two identical rows
    (duplicate names exist) — an exact-name tie is a FlowError."""
    name = _obj_field(group, "name")
    if not name:
        raise FlowError("pick_share_group: group has no name")
    for attempt, query in enumerate((name, name[:20]), 1):
        promised = await _share_search(page, query, name, cfg, log)
        info = await page.evaluate(_STAMP_SHARE_ROW_JS, {"name": name})
        if str(info).startswith("ambiguous"):
            shot = await dump_evidence(page, cfg.screenshot_dir,
                                       "crossshare_ambiguous_row")
            raise FlowError(f"ambiguous duplicate {name!r} in the typeahead "
                            f"result ({info}) — refusing to guess which group. "
                            f"Evidence: {shot}")
        if str(info).startswith("ok"):
            break
        log(f"share: no picker row for {name!r} (search {attempt}/2, payload "
            f"said {promised} rows)")
    else:
        shot = await dump_evidence(page, cfg.screenshot_dir, "crossshare_no_row")
        raise FlowError(f"share_group_row_missing: no picker row for {name!r} "
                        f"after 2 searches. Evidence: {shot}")
    await page.locator('[data-ap-share-row="1"]').first.click()
    try:
        await page.wait_for_function(_COMPOSER_OPEN_JS,
                                     arg={"ph": SHARE_COMPOSER_PLACEHOLDER,
                                          "title": SHARE_COMPOSER_TITLE},
                                     timeout=SHARE_STEP_TIMEOUT_MS)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir, "crossshare_no_row")
        raise FlowError(f"share_group_row_missing: clicking {name!r} never "
                        f"opened the share composer. Evidence: {shot}") from e
    log(f"share: composer open for {name[:44]!r}")


async def _close_share_dialog(page: Page, cfg: Config, log: log_fn) -> None:
    """Close the share composer/ hub without submitting: its 'Cerrar' header
    button (probe: aria-label 'Cerrar'), Escape as fallback, then the same
    fade-aware 'no aria-modal dialog' wait the crosspost path uses."""
    closed = False
    try:
        closer = page.locator(
            '[role="dialog"] [role="button"][aria-label*="errar" i], '
            '[role="dialog"] [role="button"][aria-label*="lose" i]').first
        if await closer.count() and await closer.is_visible():
            await closer.click(timeout=8000)
            closed = True
    except PWTimeoutError:
        closed = False
    if not closed:
        await page.keyboard.press("Escape")
        log("share: closed the dialog with Escape (no 'Cerrar' button)")
    try:
        await page.wait_for_function(_DIALOG_GONE_JS, timeout=10000)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "crossshare_close_stuck")
        raise FlowError(f"share dialog survived the close (10s). "
                        f"Evidence: {shot}") from e


async def stage_or_publish_share(page: Page, cfg: Config, log: log_fn = print,
                                 description: str = "", group=None) -> str:
    """A4: the OPEN share composer -> stage (dry) or publish (live).

    Dry: dump evidence under 'crossshare_staged', close via 'Cerrar', verify
    the dialog is gone, return 'staged'. The Publicar locator is NEVER built
    on this path (a dry share must be structurally incapable of publishing).
    Live: arm the ComposerStoryCreateMutation response, click the folded
    'Publicar', wait for the dialog to go away, return 'published'."""
    name = _obj_field(group, "name")
    try:
        await page.wait_for_function(
            _PREVIEW_SETTLED_JS,
            arg={"pending": SHARE_PREVIEW_PENDING, "attach": SHARE_ATTACH_LABEL},
            timeout=SHARE_STEP_TIMEOUT_MS)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "crossshare_preview_stuck")
        raise FlowError(f"share link preview never settled (still "
                        f"'{SHARE_PREVIEW_PENDING}'). Evidence: {shot}") from e
    box = page.locator('div[role="dialog"] [contenteditable="true"]').first
    await _ensure_text_1to1(page, box, description, cfg, log)
    if cfg.dry_run:
        shot = await dump_evidence(page, cfg.screenshot_dir, "crossshare_staged")
        await _close_share_dialog(page, cfg, log)
        log(f"DRY RUN — share staged for {name[:44]!r}, "
            f"'{SHARE_PUBLISH_TEXT}' NOT clicked (evidence {shot})")
        return "staged"
    btn = page.get_by_role("button", name=SHARE_PUBLISH_TEXT, exact=True)
    if await btn.count() != 1 or not await btn.first.is_visible():
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "crossshare_no_publish")
        raise FlowError(f"'{SHARE_PUBLISH_TEXT}' button not unique/visible in "
                        f"the share composer ({await btn.count()}) — refusing "
                        f"to guess-click. Evidence: {shot}")
    await human_sleep(cfg, log, "share composer -> Publicar",
                      cfg.crosspost_action_min, cfg.crosspost_action_max)
    try:
        async with page.expect_response(_graphql_filter(XPOST_MUTATION_OP),
                                        timeout=SHARE_STEP_TIMEOUT_MS) as rinfo:
            await btn.first.click()
        await rinfo.value
    except Exception as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "crossshare_publish_unknown")
        raise FlowError(f"'{SHARE_PUBLISH_TEXT}' fired no "
                        f"{XPOST_MUTATION_OP} ({type(e).__name__}: {e}) — "
                        f"OUTCOME UNKNOWN, check manually. "
                        f"Evidence: {shot}") from e
    try:
        await page.wait_for_function(_DIALOG_GONE_JS,
                                     timeout=SHARE_STEP_TIMEOUT_MS)
    except PWTimeoutError as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "crossshare_publish_timeout")
        raise FlowError(f"share composer still open after "
                        f"'{SHARE_PUBLISH_TEXT}' — OUTCOME UNKNOWN, check "
                        f"manually before retrying. Evidence: {shot}") from e
    log(f"share: published to {name[:44]!r} ({XPOST_MUTATION_OP})")
    return "published"
