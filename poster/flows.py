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

#: shared close-detector: the modal dialog is gone when no aria-modal
#: [role=dialog] remains (probe 2026-09-23: FB removes the node / its flag)
_DIALOG_GONE_JS = ("() => ![...document.querySelectorAll('[role=dialog]')]"
                   ".some(d => d.getAttribute('aria-modal') === 'true')")


def _norm(s: str) -> str:
    """Text-folding for name/label matching (accents + whitespace + case):
    group titles carry unicode emoji/acents and dialog rows vs graphql 'name'
    must compare equal regardless of NFC/NFD or line breaks."""
    return " ".join(unicodedata.normalize("NFC", str(s)).split()).casefold()


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


async def open_crosspost_dialog(page: Page, title: str, cfg: Config,
                                log: log_fn = print) -> tuple[list, int, str]:
    """Card '...' -> exact 'Publicar en más lugares' -> dialog. Returns
    (candidate groups, cap, listing id from the dialog payload). Raises
    FlowError with evidence otherwise."""
    sel = f'[role="button"][aria-label="{CROSSPOST_MORE_PREFIX}{title}"]'
    btn = page.locator(sel).first
    if not await btn.count():
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
        btn = hits[0]

    await human_sleep(cfg, log, "before opening the listing '...' menu")
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
        await item.first.click()
    resp = await rinfo.value
    try:
        groups, limit, listing_id = parse_crosspost_targets(await resp.text())
    except Exception as e:
        shot = await dump_evidence(page, cfg.screenshot_dir,
                                   "crosspost_dialog_parse")
        raise FlowError(f"dialog payload unreadable ({type(e).__name__}: {e}). "
                        f"Evidence: {shot}") from e
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
    rows carry no id — match folded first-line text, read back aria-checked
    after every trusted click (1:1 discipline)."""
    rows = page.locator(CROSSPOST_ROW_SEL)
    n = await rows.count()
    first_line: dict[str, int] = {}
    for i in range(n):
        key = _norm((await rows.nth(i).inner_text()).split("\n", 1)[0])
        first_line.setdefault(key, i)
    picked: list[int] = []
    for g in groups:
        i = first_line.get(_norm(g["name"]))
        if i is None or i in picked:
            shot = await dump_evidence(page, cfg.screenshot_dir,
                                       "crosspost_row_missing")
            raise FlowError(f"dialog row for {g['name']!r} not found/unique "
                            f"({n} rows). Evidence: {shot}")
        picked.append(i)
    for pos, (i, g) in enumerate(zip(picked, groups, strict=True), 1):
        await rows.nth(i).click()
        if await rows.nth(i).get_attribute("aria-checked") != "true":
            shot = await dump_evidence(page, cfg.screenshot_dir,
                                       "crosspost_check_fail")
            raise FlowError(f"row {g['name']!r} click left aria-checked false. "
                            f"Evidence: {shot}")
        log(f"crosspost: checked '{str(g['name'])[:44]}' ({pos}/{len(picked)})")
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
        await human_sleep(cfg, log, "all groups checked -> Publicar")
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
