"""Marketplace CROSSPOST pipeline: publish listings into joined groups.

The flow this automates (ground truth: recording
20260923T021450Z_marketplace_article_fetching_and_mass_pu + LIVE selector
probe .local-capture/crosspost_probe/report.md + first LIVE dry run
2026-09-23):

    /marketplace/you/selling -> card '...' menu -> 'Publicar en más lugares'
    -> dialog 'En tus grupos' (checkboxes, hard cap
    marketplace_crosspost_limit=20 per submission) -> select -> Publicar.

Publish-set ground truth = the PAGE'S CARDS (aria 'Más opciones para <title>'),
NOT the graphql fast-feed: the 2026-09-23 dry run proved the feed query
(poster/listings.py) omits 'Requieren atención' listings, while every visible
card opens the dialog. Listing ids arrive inside the dialog payload itself.

USER SPEC (2026-09-23, revised same day): ALL batches in ONE run, listing by
listing, ~2 min between batches (AP_CROSSPOST_GAP_*, capped 300s), and
tracking is IN-RUN ONLY — "no ledger or whatever": the set of groups already
given to this listing THIS run lives in this loop's memory (matched by the
dialog's own group ids). Each batch still APPENDS one simple ledger line for
the Discord summary and human audit, but no decision ever reads it back.
The dialog is re-read fresh every batch, so a group that vanished between
batches is simply not offered; one that reappears is skipped by the done set.

Exit codes: 0 >=1 batch staged/published · 1 nothing to do (no cards /
--listing matched nothing) · 2 not logged in · 3 identity switch failed.
A broken listings FEED never aborts (cards are the truth); a selling page
that renders NO cards after 30s is treated as 'nothing to do', reported loud.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import UTC, datetime

from playwright.async_api import Error as PWError
from playwright.async_api import async_playwright

from .config import Config, load_config
from .fb import adopt_identity, launch
from .flows import (
    FlowError,
    dismiss_crosspost_dialog,
    finish_crosspost_dialog,
    human_sleep,
    listing_card_titles,
    open_crosspost_dialog,
    select_crosspost_groups,
)
from .listings import ListingsFetchError, fetch_active_listings
from .notify import build_crosspost_payload, send_summary
from .results import RunRecorder

#: runaway guard: the in-run done set guarantees convergence (each batch
#: removes >=1 group from `remaining`), so hitting this means the dialog is
#: offering NEW groups every round — stop rather than loop on a moving feed.
MAX_BATCHES_PER_LISTING = 10



async def run_listing(page, cfg: Config, recorder: RunRecorder, title: str,
                      log, known_id: str = "") -> int:
    """All batches of ONE listing card (≤20 groups per dialog submission).
    A FlowError fails THIS batch and THIS listing only — the run moves on."""
    lid = known_id
    done: set[str] = set()   # groups this listing already received THIS RUN
    batches = 0
    while batches < MAX_BATCHES_PER_LISTING:
        # every batch starts from a freshly loaded selling page: dialog
        # closes can leave scroll/lazy state stale, and re-navigating is
        # exactly what a human doing batch 2 would do.
        await page.goto("https://www.facebook.com/marketplace/you/selling/",
                        wait_until="domcontentloaded", timeout=60000)
        try:
            groups, limit, resp_id = await open_crosspost_dialog(page, title,
                                                                 cfg, log)
        except FlowError as e:
            log(f"[listing] {title[:50]!r} FAILED opening dialog: {e}")
            recorder.crosspost({"id": lid or title, "title": title}, batches,
                               [], False, error=str(e)[:200])
            return batches
        if resp_id:
            if lid and resp_id != lid:
                log(f"[listing] {title[:40]!r}: id drift {lid} -> {resp_id} "
                    "(using newest)")
            lid = resp_id
        key = lid or title
        listing = {"id": key, "title": title}
        if not groups:
            log(f"[listing] {title[:50]!r}: dialog offered 0 groups")
            recorder.crosspost_skipped(listing, "dialog offers no groups")
            return 0
        remaining = [g for g in groups if g["id"] not in done]
        if not remaining:
            log(f"[listing] {title[:50]!r}: done after {batches} batch(es)"
                f" — all {len(groups)} dialog groups covered this run")
            return batches
        batch = remaining[:max(1, limit)]
        log(f"[listing] {title[:50]!r} batch {batches}: {len(batch)} of "
            f"{len(remaining)} to-go groups (cap {limit}, "
            f"{len(done)} done this run)")
        try:
            await select_crosspost_groups(page, batch, cfg, log)
            detail = await finish_crosspost_dialog(page,
                                                   publish=not cfg.dry_run,
                                                   cfg=cfg, log=log)
        except FlowError as e:
            await dismiss_crosspost_dialog(page, log)
            recorder.crosspost(listing, batches, [], False, error=str(e)[:200])
            log(f"[listing] {title[:50]!r} batch {batches} FAILED: {e}")
            return batches
        done.update(str(g["id"]) for g in batch)
        recorder.crosspost(listing, batches, batch, True)
        log(f"[listing] {title[:50]!r} batch {batches} -> {len(batch)} groups "
            f"| {detail}")
        batches += 1
        if len(remaining) <= len(batch):
            log(f"[listing] {title[:50]!r}: done after {batches} batch(es)"
                " — all dialog groups covered this run")
            return batches
        # user rule (2026-09-23): everything in one run, ~2 min between batches
        gap = random.uniform(cfg.crosspost_gap_min, cfg.crosspost_gap_max)
        log(f"[listing] sleep {gap:.1f}s before next batch")
        await asyncio.sleep(gap)
    log(f"[listing] {title[:50]!r}: stopped at hard guard "
        f"({MAX_BATCHES_PER_LISTING} batches) — dialog keeps changing?")
    return batches


async def main_async(cfg: Config, args) -> int:
    recorder = RunRecorder(
        ledger_path=cfg.ledger_file,
        run_id=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        dry_run=cfg.dry_run)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        line = f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_lines.append(line)

    def finish(aborted: str | None = None) -> None:
        summary = recorder.summary_crosspost(aborted=aborted)
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        logf = cfg.log_dir / f"crosspost_{stamp}.log"
        tail = ([f"ABORT: {aborted}"] if aborted else
                [f"{r.status} {r.listing_title!r} b{r.batch} x{r.count}"
                 for r in recorder.cross_results])
        logf.write_text("\n".join([
            f"run_id={summary['run_id']} dry_run={summary['dry_run']}",
            f"published={summary['published']} staged={summary['staged']}",
            f"failed={summary['failed']} skipped={summary['skipped']}",
            *log_lines, *tail,
        ]) + "\n", encoding="utf-8")
        print(f"[run] log saved: {logf}", flush=True)
        send_summary(cfg.discord_webhook_url,
                     build_crosspost_payload(summary, cfg.identity_label),
                     log=log)

    mode = ("DRY RUN (will NOT publish)" if cfg.dry_run
            else "*** LIVE — WILL PUBLISH ***")
    print(f"[xpost] mode: {mode} | as: {cfg.post_as}="
          f"{cfg.identity_label or '(unset)'} | profile: {cfg.profile_dir}")

    cfg.screenshot_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        ctx, page = await launch(cfg, p)
        try:
            state = await adopt_identity(ctx, page, cfg, log)
            if state == "login":
                finish("run aborted: not logged in (c_user never appeared)")
                return 2
            if state == "identity":
                finish(f"run aborted: not posting as "
                       f"{cfg.identity_label or cfg.post_as} "
                       "(identity switch failed)")
                return 3

            # best-effort metadata table (ids+prices of the plain-active
            # feed); a broken feed NEVER aborts — cards are the truth.
            feed: list = []
            try:
                feed = await fetch_active_listings(page, cfg, log=log)
            except (ListingsFetchError, PWError, RuntimeError, OSError,
                    TimeoutError) as e:
                # metadata-only source (cards are the truth) — NEVER aborts
                log(f"[xpost] listings feed unavailable ({type(e).__name__}: "
                    f"{e}) — continuing from page cards")
            feed_by_title = {str(l.get("title", "")).casefold(): l
                             for l in feed}

            titles = await listing_card_titles(page)
            # [live 2026-09-23] the selling page renders a card's menu
            # trigger MORE THAN ONCE for some listings (4 buttons, 2 real
            # items): dedupe by normalized title so one listing is never
            # batch-planned twice in the same run.
            seen_t: set[str] = set()
            uniq = []
            for t in titles:
                k = t.casefold()
                if k not in seen_t:
                    seen_t.add(k)
                    uniq.append(t)
            if len(uniq) != len(titles):
                log(f"[xpost] deduped {len(titles)} cards -> {len(uniq)} "
                    "distinct listing(s)")
            titles = uniq

            #: USER RULE (2026-09-23 recording notes): "Requieren atencion"
            #: listings are NOT ours to publish — only the active feed
            #: ("Todas las publicaciones"). Cards stay the anchor (we click
            #: them), but a card whose title is absent from the feed is
            #: skipped. If the feed itself broke we degrade loudly to
            #: all-cards rather than silently narrowing the run's scope.
            if feed:
                feed_titles = {str(l.get("title", "")).casefold()
                               for l in feed}
                keep = [t for t in titles if t.casefold() in feed_titles]
                for t in titles:
                    if t not in keep:
                        log(f"[xpost] skip card {t[:44]!r} — not in the "
                            "active feed ('Requieren atencion'?)")
                if not keep:
                    finish("run aborted: no card matches an active-feed "
                           "listing (flagged-only account?)")
                    return 1
                titles = keep
            else:
                log("[xpost] WARNING: active feed unavailable — running "
                    "with ALL cards incl. possible 'Requieren atencion'")
            if not titles:
                log("abort: no listing cards rendered on the selling page")
                finish("run aborted: no listing cards on "
                       "/marketplace/you/selling (page never hydrated?)")
                return 1

            log(f"{len(titles)} listing card(s); this run considers:")
            for t in titles:
                meta = feed_by_title.get(t.casefold()) or {}
                log(f"  {meta.get('id') or '(id from dialog)'!s:>26}  "
                    f"{t[:48]:<48} {meta.get('price') or ''!s:>10}")
            if args.list:
                log("--list: cards + feed only, no dialogs opened "
                    "(batch tracking is in-run only)")
                finish()
                return 0

            planned = titles
            if args.listing:
                want = [str(w) for w in args.listing]

                def hit(t: str) -> bool:
                    fid = str((feed_by_title.get(t.casefold()) or {}).get("id")
                              or "")
                    return any(w.casefold() in t.casefold() or (fid and w == fid)
                               for w in want)
                planned = [t for t in titles if hit(t)]
                if not planned:
                    finish(f"run aborted: --listing matched no cards {want}")
                    return 1
            cap = args.max or cfg.crosspost_max_listings
            if cap > 0:
                planned = planned[:cap]
            log(f"planned: {planned}")

            for i, t in enumerate(planned):
                if i:
                    gap = random.uniform(cfg.crosspost_gap_min,
                                         cfg.crosspost_gap_max)
                    log(f"[xpost] sleep {gap:.1f}s before next listing")
                    await asyncio.sleep(gap)
                await human_sleep(cfg, log, "before listing dialog round")
                meta = feed_by_title.get(t.casefold()) or {}
                await run_listing(page, cfg, recorder, t, log,
                                  known_id=str(meta.get("id") or ""))
        finally:
            await ctx.close()

    s = recorder.summary_crosspost()
    ok = s["published"] + s["staged"]
    log(f"done: {ok}/{s['attempted']} batch(es) "
        f"{'staged (dry-run)' if cfg.dry_run else 'published'}, "
        f"{s['failed']} failed, {s['skipped']} skipped")
    finish()
    return 0 if ok else 1


def _cli(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m poster.crosspost",
        description="Mass-publish MARKETPLACE LISTINGS into joined groups "
                    "('Publicar en más lugares'): all batches per listing "
                    "(cap 20 groups each), ~2 min apart. Dry-run checks the "
                    "boxes then clicks Cancelar — nothing is ever submitted.")
    ap.add_argument("--dry-run", action="store_true",
                    help="force dry run (default unless .env says otherwise)")
    ap.add_argument("--live", action="store_true",
                    help="confirm LIVE intent (still requires AP_DRY_RUN=false"
                         " in .env — the repo fail-safe)")
    ap.add_argument("--list", action="store_true",
                    help="print cards + coverage table, open NO dialogs")
    ap.add_argument("--listing", action="append", metavar="TERM",
                    help="only listings whose title contains TERM "
                         "(case-insensitive) or whose feed id equals TERM; "
                         "repeatable")
    ap.add_argument("--max", type=int, default=0,
                    help="max listings this run (0 = AP_CROSSPOST_MAX_LISTINGS)")
    ap.add_argument("--env-file", default=None,
                    help="alternate .env path (or set AP_ENV_FILE)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _cli(argv)
    cfg = load_config(env_file=args.env_file)
    if args.dry_run:
        cfg.dry_run = True
    elif args.live and cfg.dry_run:
        # repo fail-safe (same gate as poster.main --live): going live needs
        # the explicit AP_DRY_RUN=false in .env — a CLI flag alone never
        # authorises a real publish.
        print("[xpost] --live refused: AP_DRY_RUN is true in .env "
              "(edit .env explicitly to authorise publishing)")
        return 4
    if not cfg.dry_run:
        print("[xpost] LIVE mode: batches WILL really publish to groups.")
    return asyncio.run(main_async(cfg, args))


if __name__ == "__main__":
    sys.exit(main())
