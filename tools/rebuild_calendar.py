"""Orchestrator: scrape every venue, mark highlights, render events.html.

Run from repo root:
    python tools/rebuild_calendar.py
        --venues  projects/momEvents/config/venues.yaml
        --highlights projects/momEvents/config/highlights.yaml
        --out     projects/momEvents/.tmp/events.html
        --only-essen          # optional: filter to Essen-city sources

Per-venue scrape failure is logged and counted, NOT raised. The run fails (exit 2)
only if more than 5 venues fail OR the total event count drops > 30% vs. the
prior run report (TODO: trend tracking).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from rapidfuzz import fuzz

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import render_events_html  # noqa: E402
import scrape_venue_events  # noqa: E402

log = logging.getLogger("rebuild_calendar")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--venues", default=str(REPO_ROOT / "projects/momEvents/config/venues.yaml"))
    p.add_argument("--highlights", default=str(REPO_ROOT / "projects/momEvents/config/highlights.yaml"))
    p.add_argument("--out", default=str(REPO_ROOT / "projects/momEvents/.tmp/events.html"))
    p.add_argument("--only-essen", action="store_true", help="Only scrape sources with city == Essen")
    p.add_argument("--horizon-days", type=int, default=270)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    log.setLevel(logging.INFO)

    venues = scrape_venue_events.load_venues(args.venues)
    highlights = _load_highlights(args.highlights)

    if args.only_essen:
        venues = [v for v in venues if v.get("city") == "Essen"]

    # Reuse one HTTP session across all venues for connection pooling
    session = requests.Session()
    session.headers.update(scrape_venue_events.DEFAULT_HEADERS)

    all_events = []
    failed: list[tuple[str, str]] = []
    ok = 0
    skipped = 0
    for v in venues:
        if v.get("kind") == "unknown":
            skipped += 1
            continue
        try:
            evs = scrape_venue_events.scrape(v, session=session)
        except Exception as exc:  # belt-and-braces; scrape() already isolates
            log.error("scrape() raised for %s: %s", v["id"], exc)
            failed.append((v["id"], str(exc)))
            continue
        if not evs:
            failed.append((v["id"], "no events returned"))
        else:
            all_events.extend(evs)
            ok += 1

    log.info("scraped %d venues OK, %d failed, %d skipped (kind=unknown)", ok, len(failed), skipped)
    log.info("raw events: %d", len(all_events))

    # Mark featured events using highlights config
    featured = _compute_featured_set(all_events, highlights)
    log.info("flagged %d events as featured", len(featured))

    # Build venue_id → canonical display name map. Used by the renderer for
    # chip labels — without it, off-site events (e.g. TUP-organized events at
    # Gruga-Park / Café Central / ADA) would each produce their own chip that
    # silently filters to ALL events sharing that venue_id.
    venue_meta: dict[str, str] = {}
    for v in venues:
        # Primary venue row
        primary_id = v.get("id")
        primary_label = v.get("display_name") or v.get("name") or primary_id
        if primary_id:
            venue_meta.setdefault(primary_id, primary_label)
        # Each stage_resolver row may produce a different venue_id with its own label
        for stage in v.get("stage_resolver") or []:
            sid = stage.get("venue_id")
            slabel = stage.get("venue_name") or sid
            if sid:
                venue_meta.setdefault(sid, slabel)

    # Render
    n = render_events_html.render(
        events=all_events,
        out_path=args.out,
        featured=featured,
        title="Was ist los in Gabis Welt",
        header_eyebrow="Kultur in der Grünen Lunge",
        horizon_days=args.horizon_days,
        now=datetime.now(timezone.utc),
        venue_meta=venue_meta,
    )

    # Audit log — write chip-quality findings to .tmp/chip_audit.md so we can
    # eyeball phantom venues, near-duplicates, and configured-but-empty chips
    # without staring at the live page. User-driven design per chat 2026-05-11.
    try:
        _emit_chip_audit(all_events, venue_meta, Path(args.out).parent / "chip_audit.md")
    except Exception as exc:
        log.warning("chip audit failed: %s", exc)

    # Freshness check — warn if any configured venue produced 0 events. Most
    # often signals a broken scraper (site redesign, selectors stale, network
    # transient). Stays in the run log so failures don't slip past silently.
    _emit_freshness_warnings(venues, venue_events=_per_venue_counts(all_events))

    # Summary report
    print()
    print(f"Run {datetime.now(timezone.utc).isoformat(timespec='seconds')} — "
          f"{ok+len(failed)} venues, {ok} OK, {len(failed)} failed, {skipped} skipped")
    if failed:
        print("  Failed venues:")
        for vid, reason in failed:
            print(f"    - {vid}: {reason}")
    print(f"  Events: {len(all_events)} raw → {n} after future-only/horizon filter")
    print(f"  Featured: {len(featured)}")
    print(f"  Output: {args.out}")

    if len(failed) > 5:
        log.error("too many failed venues: %d", len(failed))
        return 2
    return 0


# ─── highlights logic ────────────────────────────────────────────────────────


def _load_highlights(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"featured_keywords": [], "featured_events": []}
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {
        "featured_keywords": data.get("featured_keywords") or [],
        "featured_events": data.get("featured_events") or [],
    }


def _per_venue_counts(events: list) -> dict:
    """Return {venue_id (source): n_events} — keyed by `source` (the row that
    scraped it) not `venue_id` (which can be split by aggregator)."""
    from collections import Counter as _C
    c = _C()
    for e in events:
        src = getattr(e, "source", None) or (e.get("source") if isinstance(e, dict) else None) or ""
        if src:
            c[src] += 1
    return dict(c)


def _emit_freshness_warnings(venues: list, venue_events: dict) -> None:
    """Warn on stderr/log when a configured venue scraped 0 events.

    Most failures here mean: site selector changed, source moved, transient
    network blip. Doesn't fail the build — just makes the silence audible.
    Static venues and known-thin venues (single static_events with 1 show)
    are exempt from the warning.
    """
    silent: list[tuple[str, str]] = []
    for v in venues:
        vid = v.get("id")
        kind = v.get("kind", "unknown")
        if not vid or kind == "unknown":
            continue
        # Static venues are intentionally hardcoded; not subject to scrape drift.
        if kind == "static":
            continue
        n = int(venue_events.get(vid, 0))
        if n == 0:
            silent.append((vid, kind))
    if silent:
        log.warning(
            "FRESHNESS: %d venue(s) returned 0 events — possible scraper drift: %s",
            len(silent),
            ", ".join(f"{vid}({kind})" for vid, kind in silent),
        )
    else:
        log.info("FRESHNESS: all configured venues returned ≥1 event")


def _emit_chip_audit(events: list, venue_meta: dict, out_path: Path) -> None:
    """Write a markdown report of chip-quality findings:

      1. Per-city chip roster (event count + canonical label).
      2. Single-event chips that aren't in venues.yaml (likely aggregator-
         spawned phantoms — candidates for skip_venue_substrings).
      3. Fuzzy-similar chip-label pairs within the same city (rapidfuzz
         token_set_ratio >= 80) — candidates for venue_id_overrides.

    This is a lightweight reactive audit. Read it after each push and add
    overrides to venues.yaml as needed.
    """
    from collections import Counter as _Counter
    try:
        from rapidfuzz import fuzz as _fuzz
    except ImportError:
        _fuzz = None

    # Group events by (city, venue_id) → count + most-common venue_name.
    chips: dict[tuple[str, str], dict] = {}
    for e in events:
        vid = getattr(e, "venue_id", None) or (e.get("venue_id") if isinstance(e, dict) else None) or ""
        city = getattr(e, "city", None) or (e.get("city") if isinstance(e, dict) else None) or ""
        vname = getattr(e, "venue_name", None) or (e.get("venue_name") if isinstance(e, dict) else None) or ""
        if not vid or not city or city == "__aggregator__":
            continue
        key = (city, vid)
        bucket = chips.setdefault(key, {"n": 0, "names": _Counter()})
        bucket["n"] += 1
        bucket["names"][vname] += 1

    # Resolve a canonical label per chip.
    chip_rows: list[dict] = []
    for (city, vid), bucket in chips.items():
        canonical = venue_meta.get(vid) or (bucket["names"].most_common(1)[0][0] if bucket["names"] else vid)
        chip_rows.append({
            "city": city, "vid": vid, "label": canonical, "n": bucket["n"],
            "in_config": vid in venue_meta,
        })

    # Section 1: roster by city, descending count.
    parts: list[str] = []
    parts.append(f"# Chip audit — {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    parts.append("")
    parts.append("Auto-generated by `tools/rebuild_calendar.py`. Surfaces chips")
    parts.append("that might be phantoms or duplicates; review periodically and")
    parts.append("add overrides to `venues.yaml` (`venue_id_overrides` /")
    parts.append("`skip_venue_substrings`) where needed.")
    parts.append("")
    parts.append("## Chips by city")
    parts.append("")
    by_city: dict[str, list] = {}
    for row in chip_rows:
        by_city.setdefault(row["city"], []).append(row)
    for city in sorted(by_city):
        rows = sorted(by_city[city], key=lambda r: (-r["n"], r["label"].lower()))
        parts.append(f"### {city}  _(chips: {len(rows)})_")
        parts.append("")
        parts.append("| events | venue_id | label | configured |")
        parts.append("|---:|---|---|:---:|")
        for r in rows:
            tick = "✓" if r["in_config"] else " "
            parts.append(f"| {r['n']} | `{r['vid']}` | {r['label']} | {tick} |")
        parts.append("")

    # Section 2: single-event chips that aren't in venues.yaml.
    phantoms = [r for r in chip_rows if r["n"] == 1 and not r["in_config"]]
    parts.append("## Single-event chips not in `venues.yaml` (phantom candidates)")
    parts.append("")
    if phantoms:
        parts.append("These are aggregator-spawned chips that didn't get suppressed")
        parts.append("by `MIN_EVENTS_PER_CHIP=2` only because the renderer keeps")
        parts.append("configured-but-thin venues. Single-event aggregator chips")
        parts.append("ARE filtered — these survived because a configured override")
        parts.append("mapped them onto another id, or the event happens to be the")
        parts.append("only one at a real venue. If a row below isn't a real venue,")
        parts.append("add it to that aggregator's `skip_venue_substrings`.")
        parts.append("")
        parts.append("| city | venue_id | label |")
        parts.append("|---|---|---|")
        for r in sorted(phantoms, key=lambda r: (r["city"], r["label"].lower())):
            parts.append(f"| {r['city']} | `{r['vid']}` | {r['label']} |")
        parts.append("")
    else:
        parts.append("_(none)_")
        parts.append("")

    # Section 3: fuzzy-similar label pairs within each city.
    parts.append("## Possible duplicate label pairs (within city)")
    parts.append("")
    parts.append("Pairs scoring rapidfuzz token_set_ratio >= 80. Likely candidates")
    parts.append("for `venue_id_overrides` in the source aggregator's config.")
    parts.append("")
    found_any = False
    if _fuzz is not None:
        for city, rows in sorted(by_city.items()):
            labels = [(r["label"], r["vid"]) for r in rows]
            pairs: list[tuple[int, str, str, str, str]] = []
            for i in range(len(labels)):
                for j in range(i + 1, len(labels)):
                    score = int(_fuzz.token_set_ratio(labels[i][0], labels[j][0]))
                    if score >= 80 and labels[i][0].lower() != labels[j][0].lower():
                        pairs.append((score, labels[i][0], labels[i][1], labels[j][0], labels[j][1]))
            if pairs:
                found_any = True
                parts.append(f"### {city}")
                parts.append("")
                parts.append("| score | label A (venue_id) | label B (venue_id) |")
                parts.append("|---:|---|---|")
                for score, la, va, lb, vb in sorted(pairs, key=lambda p: -p[0]):
                    parts.append(f"| {score} | {la} (`{va}`) | {lb} (`{vb}`) |")
                parts.append("")
    if not found_any:
        parts.append("_(none found)_")
        parts.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(parts), encoding="utf-8")
    log.info("chip audit written: %s", out_path)


def _compute_featured_set(events: list, highlights: dict) -> set:
    """Return a set of (venue_id, normalized_title) for events that match.

    Two paths:
        1. title contains any string from featured_keywords (case-insensitive substring)
        2. matches a featured_events entry: same venue_id + token_set_ratio >= 85
    """
    keywords = [kw.lower() for kw in highlights.get("featured_keywords", [])]
    manual = highlights.get("featured_events", [])

    today = datetime.now(timezone.utc).date()
    out: set = set()

    for ev in events:
        if isinstance(ev, dict):
            title = ev.get("title", "")
            venue_id = ev.get("venue_id", "")
            description = ev.get("description") or ""
            audience = ev.get("audience") or "general"
        else:
            title = getattr(ev, "title", "") or ""
            venue_id = getattr(ev, "venue_id", "") or ""
            description = getattr(ev, "description", "") or ""
            audience = getattr(ev, "audience", "general") or "general"

        # Don't pin kids/educational events as highlights, even if they keyword-match
        if audience != "general":
            continue

        haystack = f"{title} {description}".lower()

        matched = False
        for kw in keywords:
            if kw in haystack:
                matched = True
                break

        if not matched:
            for entry in manual:
                if entry.get("venue_id") != venue_id:
                    continue
                until = entry.get("until")
                if until and _coerce_date(until) < today:
                    continue
                if fuzz.token_set_ratio(title, entry.get("title_match", "")) >= 85:
                    matched = True
                    break

        if matched:
            out.add(render_events_html._featured_key(ev))

    return out


def _coerce_date(v):
    if isinstance(v, datetime):
        return v.date()
    return v


if __name__ == "__main__":
    sys.exit(main())
