"""Individual SHARE pipeline: one listing into ONE group at a time.

Where crosspost batches up to 20 groups inside a single dialog submission,
this pipeline walks the matrix the other way: for EVERY active listing, for
EVERY joined group, it opens the share hub afresh, reaches the group by NAME
SEARCH in 'Buscar grupos', stages the composer with the listing link attached
plus the listing description pasted 1:1, then stages (dry) or publishes (live)
that single share. N listings x M groups = N*M shares, uncapped unless --max.

Skeleton copied from poster/crosspost.py (argparse flags, identity ritual,
fetch_active_listings, AP_DRY_RUN fail-safe, Discord in `finally`).

IN-RUN MEMORY ONLY (user rule 2026-09-24): the only state is the done-set of
(listing_id, group_id) pairs of THIS run. The ledger is never read back for a
decision — a failed share marks its pair done so a broken group cannot cause a
retry storm; it does not make the group 'handled' for any future run.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import Error as PWError
from playwright.async_api import async_playwright

from . import flows as fl
from .config import Config, load_config
from .fb import adopt_identity, launch
from .flows import FlowError, human_sleep
from .groups_fetch import GroupsFetchError, fetch_joined_groups
from .listings import ListingsFetchError, fetch_active_listings
from .notify import build_share_payload, send_summary
from .results import STATUS_PUBLISHED, STATUS_STAGED, RunRecorder

SELLING_URL = "https://www.facebook.com/marketplace/you/selling/"
ITEM_URL = "https://www.facebook.com/marketplace/item/{}/"
#: last picker contents, seen by the most recent hub open. Written by THIS
#: module (flows stays I/O-free) and shown by --list as the groups column.
TARGETS_CACHE = Path(".local-capture/gui/share_targets.json")


# ---------------------------------------------------------------------------
# small pure helpers (unit-tested without a browser)
# ---------------------------------------------------------------------------

def _fold(s) -> str:
    """Whitespace-collapsed casefold — the only name comparison used here."""
    return " ".join(str(s or "").split()).casefold()


def normalize_listings(feed: list) -> list[dict]:
    """Active-feed rows -> {id,title,price,url}, deduped by folded title (the
    feed renders each card twice; same discipline as the crosspost card
    dedupe). The pipeline's listing SOURCE is that clean feed — 'Requieren
    atencion' listings are not in it."""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in feed or []:
        lid = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or lid).strip()
        key = _fold(title)
        if not lid or not key or key in seen:
            continue
        seen.add(key)
        out.append({"id": lid, "title": title,
                    "price": str(raw.get("price") or ""),
                    "url": str(raw.get("url") or ITEM_URL.format(lid))})
    return out


def filter_listings(listings: list[dict], terms) -> list[dict]:
    """--listing TERM (repeatable): title contains TERM (case-insensitive) or
    the listing id equals TERM — the crosspost hit() rule."""
    want = [str(t) for t in (terms or []) if str(t).strip()]
    if not want:
        return list(listings)
    out = []
    for lst in listings:
        if any(w.casefold() in lst["title"].casefold() or w == lst["id"]
               for w in want):
            out.append(lst)
    return out


def filter_groups(groups: list, only: str | None) -> list[dict]:
    """--group ID restricts the plan to that single joined group (its exact
    id, or its full name). No filter -> every joined group."""
    if not only:
        return list(groups)
    return [g for g in groups
            if str(g.get("id")) == str(only) or _fold(g.get("name")) == _fold(only)]


def ranked(group: dict, picker: list | None) -> dict:
    """Attach the picker's `rank` (kth occurrence of the folded name, payload
    order) to a joined-group dict before handing it to pick_share_group —
    duplicate group names are disambiguated by rank, never by name lookup."""
    rank = 0
    key = _fold(group.get("name"))
    for p in picker or []:
        if isinstance(p, dict) and _fold(p.get("name")) == key:
            try:
                rank = int(p.get("rank") or 0)
            except (TypeError, ValueError):
                rank = 0
            break
    return {**group, "rank": rank}


def write_targets_cache(listing: dict, picker: list | None, log) -> None:
    """Persist the hub's picker contents for the dashboard's --list groups
    column. Best-effort: a cache write must never break a share."""
    try:
        TARGETS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TARGETS_CACHE.write_text(json.dumps({
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "listing_id": str(listing.get("id") or ""),
            "listing_title": str(listing.get("title") or ""),
            "targets": [str(p.get("name") or "") for p in (picker or [])
                        if isinstance(p, dict)],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        log(f"[share] share_targets cache write failed: {e}")


def read_targets_cache() -> dict | None:
    try:
        data = json.loads(TARGETS_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


async def _open_share_hub(page, listing: dict, cfg: Config, log, on_groups):
    """Call fl.open_share_hub, passing the optional `on_groups` cache callback
    ONLY when the landed signature accepts it (plan B3 mentions it; the frozen
    signature is the 4-arg form). Introspected per call so the module works
    against either shape."""
    fn = fl.open_share_hub
    takes_cb = False
    try:
        takes_cb = "on_groups" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        takes_cb = False
    extra: dict = {"on_groups": on_groups} if takes_cb else {}
    return await fn(page, listing, cfg, log, **extra)


# ---------------------------------------------------------------------------
# the per-listing loop
# ---------------------------------------------------------------------------

async def run_listing(page, cfg: Config, recorder: RunRecorder, listing: dict,
                      plan: list, log, desc_cache: dict, done: set,
                      is_last_listing: bool = False) -> int:
    """Share ONE listing into each group of `plan`, one dialog per share.

    Sleep categories (user rules): the ACTION category (AP_CROSSPOST_ACTION_*)
    before each share, and the SHARE-GAP category (AP_SHARE_GAP_*, 2-3s)
    BETWEEN consecutive shares — group to group and listing to listing — and
    NEVER after the last share of the whole run. `is_last_listing` is how this
    function knows its final group is the run's final share.

    A FlowError fails THIS share only; the run moves on. Every attempt (ok or
    not) marks its (listing_id, group_id) pair done for this run.
    """
    lid = str(listing["id"])
    todo = [g for g in plan if (lid, str(g["id"])) not in done]
    if not todo:
        return 0

    # The description lives on the ITEM page: fetch it ONCE per listing BEFORE
    # any dialog. Fetching it mid-loop (as the plan pseudocode sketched) would
    # navigate away from the freshly-opened composer and destroy it.
    if lid not in desc_cache:
        try:
            desc_cache[lid] = await fl.fetch_listing_description(page, cfg, log,
                                                                 listing)
        except (FlowError, PWError, TimeoutError, RuntimeError, OSError) as e:
            log(f"[share] {listing['title'][:40]!r}: description unavailable "
                f"({e}) — skipping this listing")
            for g in todo:
                recorder.share_skipped(
                    listing, g, f"listing description unavailable: {str(e)[:140]}")
                done.add((lid, str(g["id"])))
            return 0

    shared = 0
    for idx, g in enumerate(todo):
        await human_sleep(cfg, log, "before share",
                          cfg.crosspost_action_min, cfg.crosspost_action_max)
        try:
            # a fresh selling page per share: dialog/X closes leave lazy state
            # stale and re-navigating is what a human doing share 2 would do.
            await page.goto(SELLING_URL, wait_until="domcontentloaded",
                            timeout=60000)
            picker = await _open_share_hub(page, listing, cfg, log,
                                           lambda groups: write_targets_cache(
                                               listing, groups, log))
            if picker:
                write_targets_cache(listing, picker, log)
            ranked_g = ranked(g, picker)
            await fl.pick_share_group(page, ranked_g, cfg, log)
            await fl.stage_or_publish_share(page, cfg, log, desc_cache[lid],
                                            ranked_g)
            recorder.share(listing, g, ok=True)
            shared += 1
            log(f"[share] {listing['title'][:40]!r} -> {g['name'][:34]!r} OK")
        except (FlowError, PWError, TimeoutError, RuntimeError, OSError) as e:
            recorder.share(listing, g, False, error=str(e)[:200])
            log(f"[share] {listing['title'][:40]!r} -> {g['name'][:34]!r} "
                f"FAILED: {e}")
        done.add((lid, str(g["id"])))
        last_of_run = is_last_listing and idx + 1 >= len(todo)
        if not last_of_run:
            await human_sleep(cfg, log, "before next share",
                              cfg.share_gap_min, cfg.share_gap_max)
    return shared


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

    sent = False

    def finish(aborted: str | None = None) -> None:
        """Write the run log + send the ONE Discord embed. Latched, so it can
        be called from every early exit AND from `finally` without ever
        double-posting."""
        nonlocal sent
        if sent:
            return
        sent = True
        summary = recorder.summary_share(aborted=aborted)
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        logf = cfg.log_dir / f"share_{stamp}.log"
        tail = ([f"ABORT: {aborted}"] if aborted else
                [f"{r.status} {r.listing_title!r} -> {r.group_name!r}"
                 for r in recorder.share_results])
        logf.write_text("\n".join([
            f"run_id={summary['run_id']} dry_run={summary['dry_run']}",
            f"published={summary['published']} staged={summary['staged']}",
            f"failed={summary['failed']} skipped={summary['skipped']}",
            *log_lines, *tail,
        ]) + "\n", encoding="utf-8")
        print(f"[run] log saved: {logf}", flush=True)
        send_summary(cfg.discord_webhook_url,
                     build_share_payload(summary, cfg.identity_label),
                     log=log)

    mode = ("DRY RUN (will NOT publish)" if cfg.dry_run
            else "*** LIVE — WILL PUBLISH ***")
    print(f"[share] mode: {mode} | as: {cfg.post_as}="
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

            # ---- listings: the active feed is the source of truth here ----
            try:
                feed = await fetch_active_listings(page, cfg, log=log)
            except (ListingsFetchError, PWError, RuntimeError, OSError,
                    TimeoutError) as e:
                log(f"[share] listings feed unavailable ({type(e).__name__}: {e})")
                finish(f"run aborted: active-listings feed unavailable ({e})")
                return 1
            listings = normalize_listings(feed)
            if args.listing:
                listings = filter_listings(listings, args.listing)
                if not listings:
                    log(f"[share] --listing matched no active listings "
                        f"{args.listing}")
                    finish(f"run aborted: --listing matched no listings "
                           f"{args.listing}")
                    return 1
            cap = int(args.max or 0)
            if cap > 0:
                listings = listings[:cap]
            if not listings:
                log("[share] no active listings to share")
                finish("run aborted: no active listings")
                return 1

            log(f"{len(listings)} active listing(s); this run considers:")
            for lst in listings:
                log(f"  {lst['id']:>26}  {lst['title'][:48]:<48} "
                    f"{lst['price']:>10}")

            # ---- targets: the identity's JOINED groups, live ----
            try:
                groups = await fetch_joined_groups(page, av=cfg.identity_id,
                                                   log=log)
            except GroupsFetchError as e:
                if not args.list:
                    log(f"[share] abort: joined-groups fetch failed: {e}")
                    finish(f"run aborted: joined-groups fetch failed ({e})")
                    return 1
                log(f"[share] joined-groups fetch failed ({e}) — --list "
                    "continues without the group count")
                groups = []
            plan = filter_groups(groups, args.group)
            log(f"[share] plan: {len(listings)} listing(s) x "
                f"{len(plan)} group(s) = {len(listings) * len(plan)} share(s)"
                f"{' (--group filter)' if args.group else ''}")

            if args.list:
                cache = read_targets_cache()
                if cache:
                    log(f"[share] last share_targets cache: "
                        f"{len(cache.get('targets') or [])} picker group(s) "
                        f"from listing {str(cache.get('listing_title'))[:40]!r} "
                        f"@ {cache.get('ts')}")
                else:
                    log("[share] share_targets cache: empty (no share run yet)")
                log(f"[share] joined groups available: {len(groups)}")
                log("--list: listings + caches only, NO dialog opened")
                finish()
                return 0

            if not plan:
                log("[share] abort: no joined groups to share into "
                    "(or --group matched none)")
                finish("run aborted: no joined groups to share into")
                return 1

            # ---- the matrix: each share opens its own dialog ----
            desc_cache: dict = {}
            done: set = set()
            for i, lst in enumerate(listings):
                await run_listing(page, cfg, recorder, lst, plan, log,
                                  desc_cache, done,
                                  is_last_listing=(i == len(listings) - 1))

            ok = sum(1 for r in recorder.share_results
                     if r.status in (STATUS_PUBLISHED, STATUS_STAGED))
            log(f"done: {ok}/{len(recorder.share_results)} share(s) "
                f"{'staged (dry-run)' if cfg.dry_run else 'published'}, "
                f"{recorder.summary_share()['failed']} failed, "
                f"{recorder.summary_share()['skipped']} skipped")
            return 0 if ok else 1
        finally:
            # ONE Discord embed per run, whatever happened (crash included).
            finish()
            await ctx.close()


def _cli(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m poster.share",
        description="Share each active MARKETPLACE listing into EACH joined "
                    "group individually (listing -> Compartir -> Grupo -> "
                    "name search -> composer -> description): every listing x "
                    "every group = its own share, one dialog at a time. "
                    "Dry-run stages each composer then closes it — nothing is "
                    "ever submitted.")
    ap.add_argument("--dry-run", action="store_true",
                    help="force dry run (default unless .env says otherwise)")
    ap.add_argument("--live", action="store_true",
                    help="confirm LIVE intent (still requires AP_DRY_RUN=false"
                         " in .env — the repo fail-safe)")
    ap.add_argument("--list", action="store_true",
                    help="print the active listings + the last share_targets "
                         "cache + the joined-group count, open NO dialog")
    ap.add_argument("--listing", action="append", metavar="TERM",
                    help="only listings whose title contains TERM "
                         "(case-insensitive) or whose id equals TERM; "
                         "repeatable")
    ap.add_argument("--max", type=int, default=0,
                    help="max LISTINGS this run (0 = no cap: every active "
                         "listing x every targeted group)")
    ap.add_argument("--group", default=None, metavar="ID",
                    help="restrict the plan to ONE joined group (exact id or "
                         "exact name)")
    ap.add_argument("--env-file", default=None,
                    help="alternate .env path (or set AP_ENV_FILE)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _cli(argv)
    cfg = load_config(env_file=args.env_file)
    if args.dry_run:
        cfg.dry_run = True
    if args.list:
        # --list never opens a dialog; report dry so the LIVE banner cannot
        # mislead (the plan table publishes nothing regardless).
        cfg.dry_run = True
    elif args.live and cfg.dry_run:
        # repo fail-safe (same gate as poster.main --live / poster.crosspost):
        # going live needs the explicit AP_DRY_RUN=false in .env — a CLI flag
        # alone never authorises a real publish.
        print("[share] --live refused: AP_DRY_RUN is true in .env "
              "(edit .env explicitly to authorise publishing)")
        return 4
    if not cfg.dry_run:
        print("[share] LIVE mode: shares WILL really publish to groups.")
    return asyncio.run(main_async(cfg, args))


if __name__ == "__main__":
    sys.exit(main())
