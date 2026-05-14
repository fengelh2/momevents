"""Parametric scraper that dispatches on `kind` field in venues.yaml.

Supported kinds (current):
    ical        — uses tools.parse_ical to discover + fetch .ics URLs
    html_list   — CSS selectors against the listing page (with optional detail-page follow)
    unknown     — skipped (pending onboarding)

Future kinds:
    json_ld     — extruct on the listing or detail pages

The scraper assembles each event into the canonical schema documented in
projects/momEvents/CLAUDE.md. A single source row may produce events under
multiple `venue_id`s when the source row declares `produces_venue_ids:` plus
a `stage_resolver:` rule list.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import dateparser
import requests
import yaml
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
import parse_ical  # noqa: E402

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20
DEFAULT_HEADERS = parse_ical.DEFAULT_HEADERS


@dataclass
class Event:
    """Canonical event row. Matches the schema in projects/momEvents/CLAUDE.md."""

    title: str
    start: datetime
    end: Optional[datetime]
    venue_id: str
    venue_name: str
    city: str
    category: str
    url: str
    description: Optional[str] = None
    price: Optional[str] = None
    source: str = ""
    audience: str = "general"   # general | kids | educational  (drives display dimming)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat() if self.start else None
        d["end"] = self.end.isoformat() if self.end else None
        return d


# ─── public API ──────────────────────────────────────────────────────────────


def scrape(venue_row: dict, session: Optional[requests.Session] = None) -> list[Event]:
    """Dispatch one venue row to the right scraping path.

    On any failure, logs the error and returns []. Per-venue isolation is the
    workflow's responsibility (see projects/momEvents/workflows/rebuild_calendar.md).
    """
    kind = venue_row.get("kind", "unknown")
    venue_id = venue_row.get("id", "?")
    try:
        if kind == "ical":
            events = _scrape_ical(venue_row, session=session)
        elif kind == "html_list":
            events = _scrape_html_list(venue_row, session=session)
        elif kind == "detail_pages":
            events = _scrape_detail_pages(venue_row, session=session)
        elif kind == "static":
            events = _scrape_static(venue_row)
        elif kind == "playwright_html_list":
            events = _scrape_playwright_html_list(venue_row, session=session)
        elif kind == "tribe_rest":
            events = _scrape_tribe_rest(venue_row, session=session)
        elif kind == "et4_search":
            events = _scrape_et4_search(venue_row, session=session)
        elif kind == "unknown":
            log.info("skip %s: kind=unknown (pending onboarding)", venue_id)
            return []
        else:
            log.warning("skip %s: unknown kind=%r", venue_id, kind)
            return []
    except Exception as exc:
        log.error("scrape failed for %s: %s: %s", venue_id, type(exc).__name__, exc)
        return []
    log.info("scrape %s (%s): %d events", venue_id, kind, len(events))
    return events


# ─── detail-pages path ──────────────────────────────────────────────────────
# For venues whose listing page has only URLs (no item-level data), and all
# title/date info lives on per-event detail pages. Lindenbrauerei Unna fits.

def _scrape_detail_pages(venue_row: dict, session=None) -> list[Event]:
    """Discover detail URLs from the listing, then fetch each detail page
    and extract title + date from selectors there.

    Config:
      detail_url_pattern: regex to find detail URLs on listing
      selectors: {title, date} for the detail page
      title_strip_suffixes: optional cleanup
      date_extract_regex: optional, applied to extracted date text
    """
    sess = session or requests
    listing_url = venue_row["calendar_url"]
    pattern = venue_row.get("detail_url_pattern")
    if not pattern:
        log.warning("%s: detail_pages kind missing detail_url_pattern", venue_row["id"])
        return []
    sel = venue_row.get("selectors") or {}

    try:
        resp = sess.get(listing_url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("%s: listing fetch failed: %s", venue_row["id"], exc)
        return []

    matches = re.findall(pattern, resp.text)
    detail_urls = sorted({urljoin(listing_url, m) for m in matches})
    log.debug("%s: found %d detail URLs", venue_row["id"], len(detail_urls))

    out: list[Event] = []
    for url in detail_urls:
        try:
            r = sess.get(url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as exc:
            log.debug("  %s: detail fetch failed: %s", url, exc)
            continue
        page = BeautifulSoup(r.content, "html.parser")
        title = _select_text(page, sel.get("title", "title"))
        title = _clean_title(title)
        for suffix in venue_row.get("title_strip_suffixes") or []:
            title = re.sub(re.escape(suffix), "", title, flags=re.IGNORECASE).strip()
        date_text = _select_text(page, sel.get("date"))
        extract_re = venue_row.get("date_extract_regex")
        if extract_re and date_text:
            m = re.search(extract_re, date_text)
            if m:
                date_text = m.group(0)
        if not title or not date_text:
            continue
        for pat in venue_row.get("skip_if_title_matches") or []:
            if re.search(pat, title):
                title = ""
                break
        if not title:
            continue
        start = _parse_one(date_text, venue_row.get("date_format"),
                           date_prefer=venue_row.get("date_prefer", "future"))
        if start is None:
            log.debug("  %s: failed to parse date %r", url, date_text)
            continue
        out.append(
            Event(
                title=title,
                start=start,
                end=None,
                venue_id=venue_row["id"],
                venue_name=venue_row.get("display_name") or venue_row["name"],
                city=venue_row.get("city", ""),
                category=venue_row.get("category", "other"),
                url=url,
                description=None,
                source=venue_row["id"],
                audience=_infer_audience(title),
            )
        )
    return out


# ─── Tribe Events REST API path ─────────────────────────────────────────────
# WordPress + The Events Calendar (Tribe) plugin exposes a JSON API at
# /wp-json/tribe/events/v1/events. Used by Unna's central kultur-in-unna.de
# portal (388 events across 15+ venues — Hellweg-Museum, Stadthalle, ZIL,
# Bibliothek, etc.) and Lindenbrauerei's WordPress site.
# Backported from events-la 2026-05-11; adds split_by_venue mode so a
# multi-venue aggregator can fan out into separate venue_ids/chips.

import html as _html

# Marketing-ribbon suffixes Tribe sites bake into titles via <span> wrappers.
_TITLE_RIBBONS = re.compile(
    r"\s*(?:SELLING\s+FAST|SOLD\s+OUT|FEW\s+TICKETS\s+LEFT|"
    r"ON\s+SALE\s+NOW|JUST\s+ANNOUNCED|FINAL\s+WEEK|EXTENDED|NEW\s+DATE|"
    r"AUSVERKAUFT|RESTKARTEN)\s*$",
    re.IGNORECASE,
)


def _scrape_tribe_rest(venue_row: dict, session=None) -> list[Event]:
    """Walk a Tribe Events REST API, paginating until exhausted.

    Config:
      calendar_url: REST endpoint (typically `.../wp-json/tribe/events/v1/events?per_page=100`)
      filter_venue_substring: optional — keep events whose `venue.venue` contains this string
      split_by_venue: bool — when true, generate venue_id per event from a
        slugified venue.venue field (so a multi-venue aggregator fans out
        into separate chips, e.g. Hellweg-Museum vs Stadthalle Unna).
      venue_id_overrides: optional {tribe_venue_name: canonical_venue_id}
        for split_by_venue mode (lets aggregator events fold under the
        existing venue_id of a separately-onboarded venue).
      skip_venue_substrings: optional list of substrings; drop events whose
        venue.venue contains any of them (filter out community/private rooms).
      max_pages: safety cap (default 30)
    """
    sess = session or requests
    base_url = venue_row["calendar_url"]
    sep = "&" if "?" in base_url else "?"
    if "per_page=" not in base_url:
        base_url = f"{base_url}{sep}per_page=100"
        sep = "&"

    venue_filter = venue_row.get("filter_venue_substring")
    split_by_venue = bool(venue_row.get("split_by_venue", False))
    vid_overrides = venue_row.get("venue_id_overrides") or {}
    skip_substrings = [s.lower() for s in (venue_row.get("skip_venue_substrings") or [])]
    max_pages = int(venue_row.get("max_pages", 30))
    out: list[Event] = []
    seen_urls: set[str] = set()

    for page in range(1, max_pages + 1):
        page_url = f"{base_url}{sep}page={page}"
        try:
            r = sess.get(page_url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("%s: tribe_rest fetch failed page=%d: %s", venue_row["id"], page, exc)
            break
        events = data.get("events") or []
        if not events:
            break
        for raw in events:
            ev = _tribe_to_event(
                raw, venue_row,
                venue_filter=venue_filter,
                split_by_venue=split_by_venue,
                vid_overrides=vid_overrides,
                skip_substrings=skip_substrings,
            )
            if ev is None:
                continue
            key = (ev.venue_id, ev.url)
            if key in seen_urls:
                continue
            seen_urls.add(key)
            out.append(ev)
        if page >= int(data.get("total_pages") or 1):
            break

    log.info("%s: %d events from tribe_rest", venue_row["id"], len(out))
    return out


def _tribe_to_event(
    raw: dict, venue_row: dict,
    venue_filter: str | None = None,
    split_by_venue: bool = False,
    vid_overrides: dict | None = None,
    skip_substrings: list[str] | None = None,
) -> Optional[Event]:
    """Map one Tribe REST record to a canonical Event."""
    title = raw.get("title") or ""
    if not title:
        return None
    title = _clean_title(_tribe_html_decode(title))
    if not title:
        return None

    # Extract Tribe's venue object (a dict or list of dicts).
    venue_obj = raw.get("venue")
    if isinstance(venue_obj, dict):
        tribe_vname = venue_obj.get("venue") or ""
    elif isinstance(venue_obj, list) and venue_obj and isinstance(venue_obj[0], dict):
        tribe_vname = venue_obj[0].get("venue", "")
    else:
        tribe_vname = ""
    tribe_vname = _tribe_html_decode(tribe_vname)
    tribe_vname_lower = tribe_vname.lower()

    # Optional filters.
    if venue_filter and venue_filter.lower() not in tribe_vname_lower:
        return None
    if skip_substrings and any(s in tribe_vname_lower for s in skip_substrings):
        return None

    start = _parse_tribe_dt(raw.get("utc_start_date") or raw.get("start_date"))
    end = _parse_tribe_dt(raw.get("utc_end_date") or raw.get("end_date"))
    if start is None:
        return None

    url = raw.get("url") or venue_row.get("homepage", "#")

    # Resolve venue_id + venue_name.
    if split_by_venue and tribe_vname:
        # Aggregator mode — split events by their Tribe venue field.
        overrides = vid_overrides or {}
        if tribe_vname in overrides:
            venue_id = overrides[tribe_vname]
        else:
            venue_id = f"{venue_row['id']}-{_tribe_slug(tribe_vname)}"
        venue_name = tribe_vname
    else:
        venue_id = venue_row["id"]
        venue_name = tribe_vname or venue_row.get("display_name") or venue_row["name"]

    city = venue_row.get("city", "")
    # Category: trust the venue's declared category unless it's "mixed" (the
    # multi-venue aggregator case). For mixed sources, lean on the global
    # title-keyword inference + venue-name hints so Hellweg-Museum events
    # become museum_exhibition, Stadthalle concerts become concert, etc.
    base_cat = venue_row.get("category", "other")
    if base_cat == "mixed":
        # Title-keyword inference + per-venue hint map (museum/concert/etc.).
        hint = (venue_row.get("venue_category_hints") or {}).get(venue_name)
        if hint:
            category = hint
        else:
            category = _infer_category(title, venue_row) or "other"
    else:
        category = base_cat

    return Event(
        title=title,
        start=start,
        end=end,
        venue_id=venue_id,
        venue_name=venue_name,
        city=city,
        category=category,
        url=url,
        description=None,
        source=venue_row["id"],
        audience=_infer_audience(title),
    )


def _parse_tribe_dt(s) -> Optional[datetime]:
    """Parse Tribe REST datetime ('YYYY-MM-DD HH:MM:SS', UTC if from utc_*)."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _tribe_html_decode(s: str) -> str:
    """Decode HTML entities + strip embedded tags + trailing marketing ribbons."""
    if not s:
        return ""
    s = _html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for _ in range(3):  # several ribbons can stack
        new = _TITLE_RIBBONS.sub("", s)
        if new == s:
            break
        s = new.strip()
    return s


def _tribe_slug(s: str) -> str:
    """Slugify a venue name for use as a venue_id suffix."""
    s = _html.unescape(s).lower()
    s = re.sub(r"[ä]", "ae", s)
    s = re.sub(r"[ö]", "oe", s)
    s = re.sub(r"[ü]", "ue", s)
    s = re.sub(r"[ß]", "ss", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60]


# ─── Runtime-Playwright HTML list path ──────────────────────────────────────
# For JS-rendered sites where there's no public API to call directly (Toubiz
# widget on visit-duesseldorf, CTG queue-it wall, etc.) — render the page in
# a real headless Chromium, then run the same html_list selector logic
# against the rendered DOM. Only enable for venues that genuinely need it
# (browser startup is ~3-5s of overhead per venue).
#
# CI workflow installs Playwright Chromium via `playwright install`. Locally
# you can either install Playwright globally or skip these venues by leaving
# the dependency uninstalled — the parser gracefully bails if the package or
# browser isn't present.


def _scrape_playwright_html_list(venue_row: dict, session=None) -> list[Event]:
    """Render `calendar_url` in headless Chromium, then extract events using
    html_list-style selectors against the rendered DOM.

    Config (extends html_list):
      calendar_url:        page URL
      selectors.item/title/date/...: same as html_list
      wait_for_selector:   optional CSS selector to wait for before extraction
      scroll:              bool, default true — scroll to bottom to trigger
                           lazy-loaded content
      dismiss_cookies:     bool, default true — try common cookie-accept buttons
      timeout_ms:          page-load timeout (default 45000)
      extra_wait_ms:       fixed wait after load/scroll (default 3000)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("%s: playwright not installed; skipping", venue_row["id"])
        return []

    url = venue_row["calendar_url"]
    wait_for = venue_row.get("wait_for_selector")
    do_scroll = bool(venue_row.get("scroll", True))
    do_cookies = bool(venue_row.get("dismiss_cookies", True))
    timeout_ms = int(venue_row.get("timeout_ms", 45000))
    extra_wait_ms = int(venue_row.get("extra_wait_ms", 3000))

    html_text: Optional[str] = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                locale=venue_row.get("locale", "de-DE"),
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Cookie banners block lazy-load on many EU sites.
            if do_cookies:
                for selector in [
                    "button:has-text('Akzeptieren')",
                    "button:has-text('Alle akzeptieren')",
                    "button:has-text('Accept all')",
                    "button:has-text('Allow all')",
                    "[data-testid='uc-accept-all-button']",
                    "#uc-btn-accept-banner",
                    "button.uc-btn-accept",
                    "#CybotCookiebotDialogBodyButtonAccept",
                ]:
                    try:
                        page.click(selector, timeout=1500)
                        break
                    except Exception:
                        pass
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=timeout_ms)
                except Exception as exc:
                    log.warning("%s: wait_for_selector failed: %s", venue_row["id"], exc)
            if do_scroll:
                try:
                    for _ in range(8):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(700)
                except Exception:
                    pass
            page.wait_for_timeout(extra_wait_ms)
            html_text = page.content()
            browser.close()
    except Exception as exc:
        log.warning("%s: playwright render failed: %s", venue_row["id"], exc)
        return []

    if not html_text:
        return []

    # Run the html_list extraction logic against the rendered DOM.
    soup = BeautifulSoup(html_text, "html.parser")
    sel = venue_row.get("selectors") or {}
    item_sel = sel.get("item")
    if not item_sel:
        log.warning("%s: playwright_html_list missing selectors.item", venue_row["id"])
        return []
    items = soup.select(item_sel)
    log.debug("%s: rendered DOM has %d items", venue_row["id"], len(items))

    out: list[Event] = []
    prev_day = None
    for it in items:
        ev, prev_day = _assemble_from_html_item(
            it, url, venue_row,
            month_ctx=None,
            ctx_year=None,
            prev_day=prev_day,
        )
        if ev is not None:
            out.append(ev)
    log.info("%s: %d events from playwright_html_list", venue_row["id"], len(out))
    return out


# ─── et4 (Saxon) tourism platform search path ───────────────────────────────
# Used by visitessen.de (the Stadt Essen city-wide events aggregator) and
# probably other DACH-region tourism portals (Saxon GmbH / destination.one).
# The portal page embeds a short-lived JWT licensekey; we fetch the iframe
# HTML, extract the licensekey, then POST a search query to meta.et4.de.
# Response is JSON when template=ET2014A_LIGHT.json. Two-step but doable
# with plain requests — no Playwright needed at runtime.

import json as _et4_json


def _scrape_et4_search(venue_row: dict, session=None) -> list[Event]:
    """Scrape an et4 (Saxon) tourism portal search index.

    Config:
      calendar_url:   the iframe HTML page on pages.<host>.de that embeds the
                      licensekey (e.g. .../default/search/Event). Required.
      experience:     et4 experience name (e.g. "visitessen"). Required.
      api_endpoint:   defaults to https://meta.et4.de/rest.ashx/search/
      template:       defaults to ET2014A_LIGHT.json (returns JSON items)
      max_limit:      single-request limit (default 1000; the API caps).
      split_by_venue: bool — fan out events by `name` field (default true).
      venue_id_overrides: {tribe_venue_name: canonical_venue_id} so events
                      from this aggregator fold under existing venue rows
                      (e.g. Aalto, Grillo, Folkwang Museum).
      skip_venue_substrings: drop events whose `name` contains any.
      drop_cancelled: bool, default true — drop events with DETAILS_ABGESAGT.
    """
    sess = session or requests
    iframe_url = venue_row["calendar_url"]
    experience = venue_row["experience"]
    api_endpoint = venue_row.get("api_endpoint", "https://meta.et4.de/rest.ashx/search/")
    template = venue_row.get("template", "ET2014A_LIGHT.json")
    max_limit = int(venue_row.get("max_limit", 1000))
    split_by_venue = bool(venue_row.get("split_by_venue", True))
    vid_overrides = venue_row.get("venue_id_overrides") or {}
    skip_substrings = [s.lower() for s in (venue_row.get("skip_venue_substrings") or [])]
    drop_cancelled = bool(venue_row.get("drop_cancelled", True))

    # Step 1: fetch iframe page, extract fresh licensekey (JWT, ~21h validity).
    try:
        r1 = sess.get(iframe_url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
        r1.raise_for_status()
    except requests.RequestException as exc:
        log.warning("%s: et4 iframe fetch failed: %s", venue_row["id"], exc)
        return []
    m = re.search(r'"licensekey"\s*:\s*"([^"]+)"', r1.text)
    if not m:
        log.warning("%s: et4 licensekey not found in iframe page", venue_row["id"])
        return []
    licensekey = m.group(1)

    # Step 2: paginate the search API. et4 caps response size; pull pages of
    # min(max_limit, 200) until overallcount is reached.
    page_size = min(max_limit, 200)
    all_items: list[dict] = []
    offset = 0
    for _ in range(20):  # safety: at most 20 pages
        payload = {
            "offset": offset,
            "limit": page_size,
            "facets": False,
            "type": "Event",
            "experience": experience,
            "q": venue_row.get("q", "all:all -systag:has_abnormal_interval"),
            "template": template,
            "licensekey": licensekey,
            "maxresponsetime": "0",
        }
        try:
            r = sess.post(
                api_endpoint,
                json=payload,
                headers={
                    **DEFAULT_HEADERS,
                    "Content-Type": "application/json",
                    "Origin": "https://" + iframe_url.split("/")[2],
                },
                timeout=DEFAULT_TIMEOUT + 10,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("%s: et4 search offset=%d failed: %s", venue_row["id"], offset, exc)
            break
        items = data.get("items") or []
        all_items.extend(items)
        overallcount = int(data.get("overallcount") or 0)
        offset += len(items)
        if not items or offset >= overallcount:
            break

    log.debug("%s: et4 returned %d raw items", venue_row["id"], len(all_items))

    # Step 3: map to Event records, deduping by (venue_id, url, start).
    out: list[Event] = []
    seen: set[tuple] = set()
    for it in all_items:
        ev = _et4_to_event(
            it, venue_row,
            split_by_venue=split_by_venue,
            vid_overrides=vid_overrides,
            skip_substrings=skip_substrings,
            drop_cancelled=drop_cancelled,
        )
        if ev is None:
            continue
        key = (ev.venue_id, ev.title, ev.start)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)

    log.info("%s: %d events from et4_search", venue_row["id"], len(out))
    return out


def _et4_to_event(
    raw: dict, venue_row: dict,
    split_by_venue: bool = True,
    vid_overrides: dict | None = None,
    skip_substrings: list[str] | None = None,
    drop_cancelled: bool = True,
) -> Optional[Event]:
    """Map one et4 item to a canonical Event."""
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    # Strip " | Abgesagt" / " - Abgesagt" cancelled suffixes; also drop the row.
    for suffix in (" | Abgesagt", " - Abgesagt", " | ABGESAGT"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()

    # Attributes (k/v list) — check for cancellation flag.
    attrs_list = raw.get("attributes") or []
    attrs = {a.get("key"): a.get("value") for a in attrs_list if isinstance(a, dict)}
    if drop_cancelled and str(attrs.get("DETAILS_ABGESAGT", "")).lower() == "true":
        return None

    # Venue name (the `name` field on each et4 Event item is the venue).
    venue_name = (raw.get("name") or "").strip()
    venue_lower = venue_name.lower()
    if skip_substrings and any(s in venue_lower for s in skip_substrings):
        return None

    # Date/time from timeIntervals[0].
    intervals = raw.get("timeIntervals") or []
    if not intervals:
        return None
    start = _et4_parse_dt(intervals[0].get("start"))
    end = _et4_parse_dt(intervals[0].get("end"))
    if start is None:
        return None

    # Build the detail URL from URL_TITLE + id.
    url_slug = attrs.get("URL_TITLE", "")
    ev_id = raw.get("id", "")
    if url_slug and ev_id:
        # visitessen detail-page pattern:
        # https://pages.visitessen.de/de/visitessen/streaming/detail/Event/{id}/{slug}/
        base = "https://" + venue_row["calendar_url"].split("/")[2]
        url = f"{base}/de/visitessen/streaming/detail/Event/{ev_id}/{url_slug}/"
    else:
        url = venue_row.get("homepage", "#")

    # Resolve venue_id.
    overrides = vid_overrides or {}
    if split_by_venue and venue_name:
        if venue_name in overrides:
            venue_id = overrides[venue_name]
        else:
            venue_id = f"{venue_row['id']}-{_tribe_slug(venue_name)}"
    else:
        venue_id = venue_row["id"]
        if not venue_name:
            venue_name = venue_row.get("display_name") or venue_row["name"]

    # Category: map et4 categories[0] (German "Vortrag/Lesung", "Konzert", etc.)
    # into our schema.
    cats = raw.get("categories") or []
    raw_cat = cats[0] if cats else ""
    cat_map = venue_row.get("category_map") or _ET4_CATEGORY_MAP
    deny_cats = set(venue_row.get("deny_categories") or _ET4_DENY_CATEGORIES)
    if raw_cat in deny_cats:
        return None
    category = cat_map.get(raw_cat) or venue_row.get("category", "other")
    if category == "mixed":
        category = "other"
    # Keyword overlay still beats venue-level guess.
    category = _infer_category(title, venue_row, stage_default=category)

    # Use the event's own city (et4 data carries it per-item) so events at
    # neighbouring venues — Musiktheater im Revier Gelsenkirchen,
    # Ruhrfestspiele Recklinghausen, etc. — get tagged correctly instead of
    # all defaulting to the aggregator's host city.
    city = (raw.get("city") or venue_row.get("city") or "").strip()
    # Optional out-of-scope filter (Köln etc. per CLAUDE.md).
    skip_cities = [c.lower() for c in (venue_row.get("skip_cities") or [])]
    if skip_cities and city.lower() in skip_cities:
        return None

    return Event(
        title=title,
        start=start,
        end=end,
        venue_id=venue_id,
        venue_name=venue_name or venue_row["name"],
        city=city,
        category=category,
        url=url,
        description=None,
        source=venue_row["id"],
        audience=_infer_audience(title),
    )


def _et4_parse_dt(s) -> Optional[datetime]:
    """Parse an et4 ISO-8601 string (e.g. '2026-08-06T20:00:00+02:00')."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# Default et4-category → our-schema mapping. Override per-venue via
# `category_map` in venues.yaml if needed.
_ET4_CATEGORY_MAP = {
    "Ausstellung": "museum_exhibition",
    "Theater & Film": "theatre",
    "Schauspiel": "theatre",
    "Kinder- und Jugendtheater": "theatre",
    "Kabarett & Co.": "theatre",
    "Musical & Musiktheater": "theatre",
    "Oper": "opera",
    "Operette": "opera",
    "Ballett": "ballet",
    "Tanz": "ballet",
    "Konzert": "concert",
    "Klassik": "concert",
    "Jazz": "concert",
    "Pop/Rock": "concert",
    "Chormusik": "concert",
    "Lesung": "other",
    "Vortrag/Lesung": "other",
    "Vortrag": "other",
    "Brauchtum/Kultur": "other",
    "Führung": "other",
}

# Categories to drop entirely — not cultural content for mum's calendar.
_ET4_DENY_CATEGORIES = frozenset({
    "Markt", "Märkte", "Flohmarkt",
    "Sport",
    "Party", "Disco",
    "Messe",
    "Ausflug/Exkursion",   # mostly day-trip / hiking listings, not cultural
})


# ─── static path ─────────────────────────────────────────────────────────────


def _scrape_static(venue_row: dict) -> list[Event]:
    """Return hardcoded Event objects from the venue's `static_events` list.

    Use case: venues with no scrapeable calendar — Villa Hügel's permanent
    Krupp exhibition, Domschatz Essen's renovation closure notice. The user
    maintains these entries by hand in venues.yaml.
    """
    out: list[Event] = []
    for item in venue_row.get("static_events") or []:
        title = _clean_title(item.get("title") or "")
        start_raw = item.get("start")
        if not title or not start_raw:
            continue
        start = _coerce_to_dt(start_raw)
        end = _coerce_to_dt(item.get("end"))
        if start is None:
            log.warning("%s: static_events entry skipped (bad start=%r)", venue_row["id"], start_raw)
            continue
        category = item.get("category") or venue_row.get("category", "other")
        out.append(
            Event(
                title=title,
                start=start,
                end=end,
                venue_id=venue_row["id"],
                venue_name=venue_row.get("display_name") or venue_row["name"],
                city=venue_row.get("city", ""),
                category=category,
                url=item.get("detail_url") or venue_row.get("homepage", "#"),
                description=_clean_title(item.get("description") or "") or None,
                source=venue_row["id"],
                audience=item.get("audience", "general"),
            )
        )
    return out


def _coerce_to_dt(v) -> Optional[datetime]:
    """Coerce a YAML date/datetime/ISO-string into a tz-aware datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    from datetime import date as _date
    if isinstance(v, _date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


# ─── ical path ───────────────────────────────────────────────────────────────


def _scrape_ical(venue_row: dict, session=None) -> list[Event]:
    listing = venue_row["calendar_url"]
    pattern = venue_row.get("ical_pattern")
    # If no pattern is configured, treat calendar_url as a single .ics endpoint
    # (used by venues that publish ONE .ics with all events, e.g. Tribe Events
    # WordPress sites with `?ical=1` query). Skips the discovery step.
    if not pattern:
        ics_urls = [listing]
    else:
        ics_urls = parse_ical.discover_ics_urls(listing, pattern, session=session)

    # Build id → detail-URL map from the listing if a detail_pattern is configured.
    # This is how TUP Essen exposes its real event pages (the .ics URL points only
    # to the iCal endpoint; the human-readable page lives at a different path).
    detail_map = _build_detail_url_map(listing, venue_row, session=session)

    out: list[Event] = []
    for ics_url in ics_urls:
        try:
            raw_events = parse_ical.fetch_ics_events(ics_url, session=session)
        except requests.RequestException as exc:
            log.warning("  fetch %s failed: %s", ics_url, exc)
            continue
        for raw in raw_events:
            ev = _assemble_from_ical(raw, ics_url, venue_row, detail_map=detail_map)
            if ev is not None:
                out.append(ev)
    return out


def _build_detail_url_map(listing_url: str, venue_row: dict, session=None) -> dict[str, str]:
    """Fetch the listing page once and extract {event_id: detail_url} pairs."""
    detail_pattern = venue_row.get("detail_pattern")
    if not detail_pattern:
        return {}
    try:
        sess = session or requests
        resp = sess.get(listing_url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("could not fetch listing for detail map: %s", exc)
        return {}
    base = listing_url
    out: dict[str, str] = {}
    for m in re.finditer(detail_pattern, resp.text):
        event_id = m.group("id") if "id" in m.groupdict() else m.group(1)
        if event_id and event_id not in out:
            out[event_id] = urljoin(base, m.group(0))
    log.debug("detail map: %d entries from %s", len(out), listing_url)
    return out


def _assemble_from_ical(raw: dict, ics_url: str, venue_row: dict, detail_map: dict | None = None) -> Optional[Event]:
    """Map one .ics VEVENT to a canonical Event, applying stage routing."""
    if not raw.get("title") or raw.get("start") is None:
        return None

    # Drop tour engagements outside the city (e.g. "Gastspiel" or
    # "Recklinghausen") — they're real productions but mum can't attend them
    # at this venue. Configured per-venue via skip_if_location_matches.
    location_raw = raw.get("location") or ""
    for pat in venue_row.get("skip_if_location_matches") or []:
        if re.search(pat, location_raw):
            return None

    title = _clean_title(raw["title"])

    venue_id, venue_name, city, stage_default_category = _resolve_stage(
        location=raw.get("location") or "",
        title=title,
        venue_row=venue_row,
    )

    # URL resolution priority:
    #   1. .ics URL field (rarely populated)
    #   2. detail_map lookup by event_id (the right answer for TUP)
    #   3. fall back to the calendar listing
    detail_url = raw.get("url") or _detail_url_from_map(ics_url, detail_map) \
        or venue_row["calendar_url"]

    return Event(
        title=title,
        start=raw["start"],
        end=raw.get("end"),
        venue_id=venue_id,
        venue_name=venue_name,
        city=city,
        category=_infer_category(title, venue_row, stage_default=stage_default_category),
        url=detail_url,
        description=_clean_title(raw.get("description") or "") or None,
        price=None,
        source=venue_row["id"],
        audience=_infer_audience(title),
    )


def _clean_title(s: str) -> str:
    """Strip soft hyphens, collapse newlines and runs of whitespace."""
    if not s:
        return s
    # Remove SHY (soft hyphen) and other invisibles that break display
    s = s.replace("­", "").replace("​", "").replace("﻿", "")
    # iCal SUMMARY can have embedded newlines (e.g. conductor / orchestra); join with bullet
    s = re.sub(r"[\r\n]+", " · ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _detail_url_from_map(ics_url: str, detail_map: dict | None) -> str:
    """Look up the human-readable detail URL by extracting the event ID from the .ics URL."""
    if not detail_map:
        return ""
    m = re.search(r"/(\d+)/ical-", ics_url)
    if not m:
        return ""
    return detail_map.get(m.group(1), "")


# ─── stage routing ───────────────────────────────────────────────────────────


def _resolve_stage(location: str, title: str, venue_row: dict) -> tuple[str, str, str, Optional[str]]:
    """Map an event's location string to (venue_id, venue_name, city, default_category).

    `default_category` may be None — caller falls back to keyword inference or
    the venue's base category.

    Each resolver rule:
        - match: "Aalto"           # str OR list[str]; substring, case-insensitive
          venue_id: aalto-essen
          venue_name: "Aalto-Theater"
          city: Essen
          default_category: opera  # optional fallback when title keywords miss
    """
    resolvers = venue_row.get("stage_resolver") or []
    haystack = f"{location} {title}".lower()
    for rule in resolvers:
        patterns = rule["match"]
        if isinstance(patterns, str):
            patterns = [patterns]
        if any(p.lower() in haystack for p in patterns):
            # Prefer the actual LOCATION string for display — it's more specific
            # ("NATIONAL-BANK Pavillon" beats the rule-name "Philharmonie Essen").
            # The rule's venue_name is only a fallback when location is empty.
            display_name = location if location else rule.get("venue_name", venue_row["name"])
            return (
                rule["venue_id"],
                display_name,
                rule.get("city", venue_row["city"]),
                rule.get("default_category"),
            )

    # Use the location string as the display name when present (even short
    # ones like "Box" are informative). Else fall back to the venue's short
    # display name from `display_name`, or `name` if no short form is set.
    fallback_name = location if location else (venue_row.get("display_name") or venue_row["name"])
    return (venue_row["id"], fallback_name, venue_row["city"], None)


# ─── html_list path ──────────────────────────────────────────────────────────


def _scrape_html_list(venue_row: dict, session=None) -> list[Event]:
    """Fetch the listing page(s) and assemble events from each item.

    Supports multi-month pagination (`paginate_months: N` + `paginate_url_param`)
    for venues that show one month at a time and need explicit URL stepping.
    Each fetched page may carry its own month context (for venues like Theater
    Münster where the date day-number per item is paired with a page-level
    month name).
    """
    sess = session or requests
    sel = venue_row.get("selectors") or {}
    listing_url = venue_row["calendar_url"]

    item_sel = sel.get("item")
    if not item_sel:
        log.warning("%s: html_list missing selectors.item", venue_row["id"])
        return []

    # Build the list of URLs to fetch. Defaults to single calendar_url; if
    # paginate_months is set, append `?date=YYYY-MM` for the current month
    # plus N-1 future months.
    urls = _paginated_urls(venue_row)

    out: list[Event] = []
    total_items = 0   # selector matched this many across all pages
    for url, ctx_year in urls:
        try:
            resp = sess.get(url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("  %s: page fetch failed (%s): %s", venue_row["id"], url, exc)
            continue
        # Pass bytes (not resp.text) so BS4 detects the charset from the document
        soup = BeautifulSoup(resp.content, "html.parser")

        # Page-level month context (Theater Münster, Wolfgang-Borchert-Theater).
        # Combined with per-item day-number to produce a full date.
        month_ctx = None
        mc_sel = venue_row.get("month_context_selector")
        if mc_sel:
            mc_el = soup.select_one(mc_sel)
            if mc_el:
                month_ctx = mc_el.get_text(" ", strip=True)

        items = soup.select(item_sel)
        total_items += len(items)
        log.debug("%s [%s]: matched %d items", venue_row["id"], url[-30:], len(items))

        prev_day = None  # for date_day_carry_forward
        for it in items:
            ev, prev_day = _assemble_from_html_item(
                it, url, venue_row,
                month_ctx=month_ctx,
                ctx_year=ctx_year,
                prev_day=prev_day,
            )
            if ev is not None:
                out.append(ev)

    # Surface high item-to-event drop rates. A scraper that selects 9 items
    # but only emits 3 events almost always means silent date-parse failure
    # on a non-standard format (e.g. "27. Febr. 2026" — see Folkwang 2026-05).
    # Below the threshold the chip_audit.md log is enough; above, we want a
    # WARNING visible in the rebuild summary so the operator investigates.
    DROP_WARN_THRESHOLD = 0.30   # >30% of selected items dropped
    MIN_ITEMS_FOR_WARN = 3       # don't warn on tiny pages
    if total_items >= MIN_ITEMS_FOR_WARN and total_items > len(out):
        dropped = total_items - len(out)
        drop_rate = dropped / total_items
        if drop_rate > DROP_WARN_THRESHOLD:
            log.warning(
                "DROP: %s selected %d items, emitted only %d (%.0f%% dropped) "
                "— likely silent date-parse failure or selector mismatch",
                venue_row["id"], total_items, len(out), drop_rate * 100,
            )
    return out


def _paginated_urls(venue_row: dict) -> list[tuple[str, Optional[int]]]:
    """Return [(url, year_for_that_url), ...] — one entry per month for
    paginated venues, or just [(calendar_url, None)] for non-paginated."""
    base = venue_row["calendar_url"]
    months = venue_row.get("paginate_months")
    if not months:
        return [(base, None)]
    param = venue_row.get("paginate_url_param", "date")
    out: list[tuple[str, Optional[int]]] = []
    today = datetime.now(timezone.utc).date()
    y, m = today.year, today.month
    sep = "&" if "?" in base else "?"
    for _ in range(int(months)):
        url = f"{base}{sep}{param}={y:04d}-{m:02d}"
        out.append((url, y))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _assemble_from_html_item(
    item, listing_url: str, venue_row: dict,
    month_ctx: Optional[str] = None,
    ctx_year: Optional[int] = None,
    prev_day: Optional[str] = None,
) -> tuple[Optional[Event], Optional[str]]:
    """Build a single Event from one selector match.

    Returns (event_or_None, day_carry_forward_value) — the second value is
    used by the caller to remember the last seen day-number for venues where
    continuation rows have an empty day cell (Theater Münster).
    """
    sel = venue_row["selectors"]
    new_prev_day = prev_day  # default: pass through unchanged

    # When the date is embedded in the title block, preserve line breaks so we can
    # split off venue/location lines after extracting the date.
    title_separator = "\n" if venue_row.get("date_from_title") else " "
    raw_title = _select_text(item, sel.get("title"), separator=title_separator)
    if not raw_title:
        return None, new_prev_day

    # Optional skip filter — drop non-event entries like "Museum closed" cards
    for pat in venue_row.get("skip_if_title_matches") or []:
        if re.search(pat, raw_title):
            return None, new_prev_day

    # Optional title cleanup: strip suffixes (literal) and regex patterns
    title = _clean_title(raw_title)
    for suffix in venue_row.get("title_strip_suffixes") or []:
        title = re.sub(re.escape(suffix), "", title, flags=re.IGNORECASE).strip()
    for pattern in venue_row.get("title_strip_regex") or []:
        title = re.sub(pattern, "", title).strip(" -–—,.\n\t")

    # Resolve dates. Modes (first matching wins):
    #   0. month_context: page-level month + per-item day (Theater Münster, WBT)
    #   1. date_start + date_end selectors (Folkwang-style)
    #   2. date selector (Red Dot / Ruhr Museum extracts)
    #   3. date_from_title regex (Ruhr Museum exhibitions)
    start = end = None
    date_text = ""
    if month_ctx and sel.get("date_day"):
        # Construct date from day-number + page-level month + inferred year
        day_t = _select_text(item, sel.get("date_day")).strip(" .,")
        if not day_t and venue_row.get("date_day_carry_forward"):
            day_t = prev_day or ""
        if day_t:
            new_prev_day = day_t
            time_t = _select_text(item, sel.get("date_time")) if sel.get("date_time") else ""
            yr = ctx_year or datetime.now(timezone.utc).year
            date_text = f"{day_t}. {month_ctx} {yr} {time_t}".strip()
            start = _parse_one(date_text, venue_row.get("date_format"), date_prefer=venue_row.get("date_prefer", "future"))
    if start is not None:
        pass  # month_context mode already produced a start
    elif sel.get("date_start") or sel.get("date_end"):
        start_t = _select_text(item, sel.get("date_start"))
        end_t = _select_text(item, sel.get("date_end"))
        if start_t:
            start = _parse_one(start_t, venue_row.get("date_format"), date_prefer=venue_row.get("date_prefer", "future"))
        if end_t:
            end = _parse_one(end_t, venue_row.get("date_format"), date_prefer=venue_row.get("date_prefer", "future"))
        date_text = f"{start_t} – {end_t}".strip(" –")
    elif venue_row.get("date_from_title"):
        mode = venue_row.get("date_from_title_mode", "single")
        # Operate on the already-cleaned title (SHY chars + collapsed whitespace
        # gone) so the strip leaves a clean display string.
        haystack = title
        if mode == "range":
            # Two capture groups: (start, end). Year on start may be missing —
            # inferred from end. Used by Kunstpalast: "DIE GROSSE 5.7.–9.8.2026".
            pattern = venue_row.get(
                "date_from_title_pattern",
                r"(\d{1,2}\.\d{1,2}\.(?:\d{4})?)\s*[–—-]\s*(\d{1,2}\.\d{1,2}\.\d{4})",
            )
            m = re.search(pattern, haystack)
            if m and m.lastindex and m.lastindex >= 2:
                date_text = m.group(0)
                title = re.sub(re.escape(date_text), "", haystack).strip(" -–—,.\n\t")
                start_str, end_str = m.group(1), m.group(2)
                if not re.search(r"\d{4}", start_str):
                    yr = re.search(r"(\d{4})", end_str)
                    if yr:
                        start_str = start_str + yr.group(1)
                start = _parse_one(start_str, venue_row.get("date_format"), date_prefer=venue_row.get("date_prefer", "future"))
                end = _parse_one(end_str, venue_row.get("date_format"), date_prefer=venue_row.get("date_prefer", "future"))
        else:
            date_pattern = venue_row.get(
                "date_from_title_pattern",
                # Default: "Bis 10. Januar 2027" / "ab 10. Mai 2026" / "10.01.2027"
                r"(?:Bis|bis|Ab|ab|Vom|vom|Noch bis|noch bis)\s*\d{1,2}\.\s*\w+\s*\d{4}|\d{1,2}\.\d{1,2}\.\d{4}",
            )
            m = re.search(date_pattern, haystack)
            if m:
                date_text = m.group(0)
                title = re.sub(re.escape(date_text), "", haystack).strip(" -–—,.\n\t")
            if date_text:
                if re.match(r"^(?:Bis|bis|Noch bis|noch bis)\b", date_text):
                    end = _parse_one(date_text, venue_row.get("date_format"), date_prefer=venue_row.get("date_prefer", "future"))
                    today = datetime.now(timezone.utc)
                    start = today.replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    start = _parse_one(date_text, venue_row.get("date_format"), date_prefer=venue_row.get("date_prefer", "future"))
    else:
        date_text = _select_text(item, sel.get("date"))
        # Optional pre-extract: pull a clean date substring out of a noisy
        # text blob. Used by Ruhr Museum where the date selector returns
        # "Margarethenhöhe... Sonntag 10.5. 11:00 - 13:00" — regex pulls just
        # the day+time portion before dateparser sees it.
        extract_re = venue_row.get("date_extract_regex")
        if extract_re and date_text:
            if venue_row.get("date_find_all"):
                # Find every match; first = start, last = end. Used for venues
                # that show "DATE_A bis DATE_B - verlängert bis DATE_C" — we
                # want DATE_A → DATE_C, not DATE_A → DATE_B.
                matches = re.findall(extract_re, date_text)
                if matches:
                    date_text = matches[0] if len(matches) == 1 else f"{matches[0]} – {matches[-1]}"
            else:
                m = re.search(extract_re, date_text)
                if m:
                    date_text = m.group(0)
        start, end = _parse_date_range(date_text, venue_row.get("date_format"),
                                        date_prefer=venue_row.get("date_prefer", "future"))

    if start is None:
        log.debug("%s: failed to parse date %r (raw_title=%r)", venue_row["id"], date_text, raw_title)
        return None, new_prev_day

    # Title cleanup pass 2: take first non-empty line, in case selector pulled a multiline blob
    if "\n" in title or "  " in title:
        first_line = next((ln.strip() for ln in re.split(r"\n|\s{2,}", title) if ln.strip()), title)
        if first_line:
            title = first_line

    detail_link = _select_attr(item, sel.get("detail_link"), "href")
    detail_url = urljoin(listing_url, detail_link) if detail_link else listing_url

    description = _select_text(item, sel.get("description")) or None

    venue_id, venue_name, city, stage_default_category = _resolve_stage(
        location="", title=title, venue_row=venue_row
    )

    category = _infer_category(title, venue_row, stage_default=stage_default_category)

    return Event(
        title=title,
        start=start,
        end=end,
        venue_id=venue_id,
        venue_name=venue_name,
        city=city,
        category=category,
        url=detail_url,
        description=description,
        price=None,
        source=venue_row["id"],
        audience=_infer_audience(title),
    ), new_prev_day


def _select_text(node, selector: Optional[str], separator: str = " ") -> str:
    """Read text (or an attribute) from a CSS-selected element.

    The `@attr` suffix on a selector reads the attribute instead of the text,
    e.g. `meta[itemprop='startDate']@content` returns the meta tag's content.
    A selector starting with `@` (no element selector before it) reads the
    attribute from the *item* element itself — used by Ruhrfestspiele where
    the date list is on `<article ... data-days='["2026-05-04",...]'>`.
    """
    if not selector:
        return ""
    if selector.startswith("@"):
        return (node.get(selector[1:].strip()) or "")
    if "@" in selector:
        sel, attr = selector.rsplit("@", 1)
        el = node.select_one(sel.strip())
        return (el.get(attr.strip()) or "") if el is not None else ""
    el = node.select_one(selector)
    if el is None:
        return ""
    text = el.get_text(separator, strip=True)
    if separator == "\n":
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
        return "\n".join(ln for ln in lines if ln)
    return " ".join(text.split())


def _select_attr(node, selector: Optional[str], attr: str) -> str:
    """Read an attribute from a CSS-selected element.

    Supports the same `@attr` and `@attr-on-item` shortcuts as _select_text:
        - "h2 a"          → select h2 a, read attr `attr`
        - "h2 a@href"     → select h2 a, read attr `href` (overrides default)
        - "@href"         → read attr `href` from the item element itself
    """
    if not selector:
        return ""
    if selector.startswith("@"):
        return (node.get(selector[1:].strip()) or "").strip()
    if "@" in selector:
        sel, override_attr = selector.rsplit("@", 1)
        el = node.select_one(sel.strip())
        return (el.get(override_attr.strip()) or "").strip() if el is not None else ""
    el = node.select_one(selector)
    if el is None:
        return ""
    return (el.get(attr) or "").strip()


# ─── date parsing ────────────────────────────────────────────────────────────


_DATE_PARSER_LANGS = ["de", "en"]
_DATE_PARSER_BASE_SETTINGS = {
    # German convention is DD.MM.YYYY. Without this, dateparser interprets
    # "12.3.2026" as December 3 (US MM.DD) instead of March 12 — causing
    # exhibitions to appear months later than they actually open.
    "DATE_ORDER": "DMY",
}


def _date_parser_kw(date_prefer: str = "future") -> dict:
    """Build dateparser kwargs with the configured PREFER_DATES_FROM mode.

    Default is 'future' — for upcoming-events scraping, missing-year dates
    should resolve to the next future occurrence. 'current_period' is the
    auto-fallback for date ranges where future-bias produces end < start
    (e.g. "16. April bis 5. Juli" parsed today resolves to start=2027/end=2026).
    """
    return {
        "languages": _DATE_PARSER_LANGS,
        "settings": {**_DATE_PARSER_BASE_SETTINGS, "PREFER_DATES_FROM": date_prefer},
    }


# Back-compat name — old callers expect this.
_DATE_PARSER_KW = _date_parser_kw("future")


def _parse_date_range(text: str, explicit_format: Optional[str] = None,
                       date_prefer: str = "future") -> tuple[Optional[datetime], Optional[datetime]]:
    """Parse a German date string, possibly a range, into (start, end).

    Handles patterns like:
        "21. Juni 2026"
        "Sa, 14.05.2026 19:30"
        "14.05.–30.06.2026"      (range — exhibition run)
        "14.05.2026 — 30.06.2026"
        "ab 18.05.2026"
        "noch bis 17.08.2026"
        "16. April bis 5. Juli"  (no year → auto-fallback handles this)

    Returns tz-aware datetimes (Europe/Berlin → UTC). end is None for single-day events.

    Auto-fallback: if a parsed range produces end < start (almost always means
    no-year input + PREFER_DATES_FROM=future flipped the start to next year),
    retry the whole range with PREFER_DATES_FROM='current_period'.
    """
    if not text:
        return (None, None)
    text = text.strip()

    def _parse_with(left_text: str, right_text: str, prefer: str):
        return (
            _parse_one(left_text, explicit_format, date_prefer=prefer),
            _parse_one(right_text, explicit_format, date_prefer=prefer),
        )

    # Range patterns — try a few separators
    for sep in [" – ", " — ", " - ", "–", "—", " bis ", " – bis "]:
        if sep in text:
            left, right = text.split(sep, 1)
            l, r = _parse_with(left.strip(), right.strip(), date_prefer)
            if l and r:
                # Auto-fallback: end < start means no-year + future-bias bug.
                if r < l:
                    l2, r2 = _parse_with(left.strip(), right.strip(), "current_period")
                    if l2 and r2 and r2 >= l2:
                        return (l2, r2)
                return (l, r)

    # Compact range "14.05.–30.06.2026" (no spaces, en-dash with year only on right)
    m = re.match(r"^(\d{1,2}\.\d{1,2}\.)[–—-](\d{1,2}\.\d{1,2}\.\d{4})$", text)
    if m:
        l_str = m.group(1) + m.group(2).split(".")[-1]   # tack year on
        l = _parse_one(l_str, explicit_format, date_prefer=date_prefer)
        r = _parse_one(m.group(2), explicit_format, date_prefer=date_prefer)
        if l and r:
            return (l, r)

    single = _parse_one(text, explicit_format, date_prefer=date_prefer)
    return (single, None)


def _parse_one(text: str, explicit_format: Optional[str] = None,
                date_prefer: str = "future") -> Optional[datetime]:
    text = text.strip().rstrip(".")
    if not text:
        return None

    # Normalize non-standard German month abbreviations dateparser doesn't
    # recognize. Folkwang prints "Febr." (4-letter, between "Feb." and
    # "Februar") which silently fails. Other sites sometimes use "Janu.",
    # "Juli." with trailing dot variants. Map them to dateparser-friendly
    # forms BEFORE the main parse.
    text = re.sub(r"\bJanu\.", "Januar", text, flags=re.IGNORECASE)
    text = re.sub(r"\bFebr\.", "Februar", text, flags=re.IGNORECASE)

    if explicit_format:
        try:
            dt = datetime.strptime(text, explicit_format)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # ISO 8601 first. dateparser with DATE_ORDER='DMY' (set globally for German
    # numeric dates) actively rejects ISO format, so any "2026-05-23" coming
    # from a <time datetime="..."> attribute would silently fail otherwise.
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # Strip leading German prepositions that confuse dateparser
    for prefix in ("ab ", "noch bis ", "bis "):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]

    parsed = dateparser.parse(text, **_date_parser_kw(date_prefer))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ─── category inference ──────────────────────────────────────────────────────


_CATEGORY_KEYWORDS = [
    ("vernissage", ["vernissage", "ausstellungseröffnung", "eröffnung"]),
    # Note: bare "oper" deliberately excluded — too many false positives
    # ("Cooper", "Hopper", "Kooperation"). Real operas reach this category via:
    #  (a) stage_resolver default_category=opera (Aalto, Opernhaus Düsseldorf, etc.)
    #  (b) one of the rep production-name keywords below (word-boundary matched
    #      to avoid substring traps like "Aida" inside other words)
    #  (c) the featured highlights keyword list.
    # Backported + extended from events-la list 2026-05-11.
    ("opera", [
        "opera", "operette",
        # Verdi
        "falstaff", "aida", "rigoletto", "otello", "la traviata", "la traviata",
        "il trovatore", "nabucco", "don carlos", "macbeth", "simon boccanegra",
        "un ballo in maschera", "la forza del destino",
        # Puccini
        "tosca", "la bohème", "la boheme", "madama butterfly", "turandot",
        "gianni schicchi", "manon lescaut", "suor angelica", "il tabarro",
        # Mozart
        "magic flute", "zauberflöte", "zauberfloete",
        "don giovanni", "cosi fan tutte", "così fan tutte",
        "marriage of figaro", "le nozze di figaro", "le nozze",
        "die hochzeit des figaro",
        "idomeneo", "la clemenza di tito",
        "entführung aus dem serail", "entfuehrung aus dem serail",
        # Wagner — full Ring cycle + others
        "die walküre", "die walkuere", "rheingold", "siegfried",
        "götterdämmerung", "goetterdaemmerung", "ring des nibelungen",
        "tannhäuser", "tannhaeuser", "lohengrin", "parsifal",
        "tristan und isolde", "fliegender holländer", "fliegender hollaender",
        "die meistersinger",
        # R. Strauss
        "der rosenkavalier", "rosenkavalier", "salome", "elektra",
        "ariadne auf naxos", "capriccio", "arabella",
        "die frau ohne schatten",
        # J. Strauss / Lehár (operettas — explicit; "operette" alone catches generic)
        "die fledermaus", "fledermaus", "eine nacht in venedig",
        "der zigeunerbaron", "wiener blut",
        "lustige witwe", "land des lächelns", "land des laechelns",
        # Beethoven / Weber / Humperdinck
        "fidelio", "der freischütz", "freischuetz",
        "hänsel und gretel", "haensel und gretel", "königskinder", "koenigskinder",
        # Bizet / Rossini / Donizetti / Bellini
        "carmen", "les pêcheurs", "les pecheurs de perles",
        "barbiere di siviglia", "barber of seville",
        "elisir d'amore", "elixir of love", "lucia di lammermoor",
        "don pasquale", "fille du régiment", "fille du regiment",
        "norma",
        # Tchaikovsky / Mussorgsky / Borodin
        "eugen onegin", "eugene onegin", "jewgeni onegin",
        "pikowaja", "pique dame", "pikowaja dama",
        "boris godunov", "fürst igor", "fuerst igor", "prince igor",
        # Massenet / Gounod / Offenbach
        "werther", "manon",
        "faust",  # Gounod opera; ambiguous with Goethe play but most "Faust" events at opera houses ARE the opera
        "contes d'hoffmann", "hoffmanns erzählungen", "hoffmanns erzaehlungen",
        "orpheus in der unterwelt", "orphée aux enfers",
        "schöne helena", "schoene helena", "belle hélène", "belle helene",
        # Janáček / Berg / Britten / Bartók
        "jenufa", "jenůfa", "katja kabanowa", "káťa kabanová",
        "sache makropulos", "makropulos",
        "wozzeck", "lulu",
        "peter grimes", "billy budd", "death in venice", "tod in venedig",
        "midsummer night's dream",  # Britten opera (Shakespeare play also exists; rare conflict)
        "herzog blaubarts burg", "bluebeard",
        # Misc rep
        "die verkaufte braut", "verkaufte braut", "bartered bride",
        "rake's progress", "rakes progress",
        "cavalleria rusticana", "pagliacci",
    ]),
    ("ballet", [
        "ballett", "ballet", "tanztheater",
        "schwanensee", "swan lake",
        "nussknacker", "nutcracker",
        "dornröschen", "dornroeschen", "sleeping beauty",
        "giselle", "coppelia", "coppélia",
        "don quixote", "don quichotte",
        "la bayadère", "la bayadere",
        "la sylphide",
        "spartacus", "spartakus",
        "raymonda", "sylvia",
        "le sacre du printemps", "frühlingsopfer", "fruehlingsopfer", "rite of spring",
        "petrushka", "petruschka",
        "feuervogel", "firebird",
        "daphnis und chloé", "daphnis und chloe", "daphnis et chloé",
        "boléro", "bolero",
        "tanz", "tanzhommage",  # last-resort markers
    ]),
    ("concert", ["sinfonie", "symphonie", "konzert", "orchester", "philharmonisch",
                  "kammermusik", "rezital", "liederabend", "chormusik", "chor "]),
    ("theatre", ["premiere", "schauspiel", "aufführung", "vorstellung"]),
    ("museum_exhibition", ["ausstellung", "exhibition"]),
]


# German + English musicals — title may LITERALLY contain "Oper" or other
# opera-keyword bait, but they're musical theatre. Tag as theatre.
_MUSICAL_THEATRE_TITLES = (
    "phantom der oper", "phantom of the opera",
    "les misérables", "les miserables",
    "miss saigon",
    "evita",
    "jesus christ superstar",
    "tanz der vampire",
    "elisabeth",  # Wiener musical (not the Donizetti opera; check context)
    "rebecca",  # musical
    "der könig der löwen", "the lion king",
    "wicked",
    "hamilton",
    "hadestown",
    "cabaret",
    "chicago",
    "rent",
    "into the woods",
    "sweeney todd",
    "starlight express",
    "anatevka", "fiddler on the roof",
    "linie 1",
    "tabaluga",
    "ich war noch niemals in new york",
    "mamma mia",
    "der glöckner von notre dame",
    "die schöne und das biest", "beauty and the beast",
    "tarzan",
    "rocky horror",
    "we will rock you",
)


# ─── audience inference ──────────────────────────────────────────────────────
# Title keyword markers for events that should be visually de-emphasized.
# A 70-year-old culturally engaged user doesn't want kids' shows or backstage
# tours competing with the actual programme.

_AUDIENCE_KEYWORDS = {
    # ORDER MATTERS — first match wins. Hide-by-default classes (kids, active)
    # are listed before the dim-only "educational" class so a "Familien-Werkstatt"
    # gets classified as kids (hidden) rather than educational (dimmed).

    # Hidden by default; revealed by the "Auch Kurse & Familienprogramm zeigen" toggle.
    "kids": [
        "kinder",
        "kindertheater",
        "familienkonzert",
        "familien-werkstatt",
        "familienwerkstatt",
        "familienführung",
        "familien-",
        "babykonzert",
        "krabbel",
        "kita",
        "schulkonzert",
        "schul-",
        "schulvorstellung",
        "klassenzimmer",
        "ohren auf",
        "dinos",
        "vorlese",
        "junge konzerte",
        "junge zuhörer",
        "jazz für kinder",
    ],
    # Hidden by default; revealed by the same toggle. Hands-on / participatory
    # OR formal industry events (Spielzeitpräsentation = annual season-preview
    # press event, technically public but not a real performance).
    "active": [
        "open class",
        "tanzunterricht",
        "tanzkurs",
        "tanz für menschen",
        "mixed-abled",
        "bestimmungstag",
        "mitmach",
        "mit-mach",
        "workshop",
        "werkstatt",
        "opernwerkstatt",
        "spielzeitpräsentation",
        "spielzeitvorschau",
    ],
    # Visible but dimmed — passive things mum might enjoy (tours, lectures, open rehearsals).
    "educational": [
        "führung",                   # broad catch — Architekturführung, Theaterführung, Balletthausführung, ...
        "einführung",
        "blick hinter",
        "backstage",
        "einblicke",                 # preview / behind-the-scenes events
        "probenbesuch",
    ],
}


def _infer_audience(title: str) -> str:
    t = title.lower()
    for cls, kws in _AUDIENCE_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                return cls
    return "general"


def _infer_category(title: str, venue_row: dict, stage_default: Optional[str] = None) -> str:
    """Resolve event category. Priority:

    1. per-venue category_keyword_overrides — venue-curated, beats global keywords
       (used for production-name overrides like Aalto's "Relations" / "Ptah VI" being ballet)
    2. famous-musical override (Phantom der Oper / Tanz der Vampire / etc. → theatre,
       not opera/ballet, even though their titles literally contain those words)
    3. global title keyword match (word-boundary on short opera/ballet tokens
       to prevent "Aida" matching inside "Saida" or "tanz" matching inside
       "Akzeptanz" etc.)
    4. stage_default from stage_resolver rule
    5. venue's base category if not 'mixed'
    6. 'other'
    """
    t = title.lower()
    overrides = venue_row.get("category_keyword_overrides") or {}
    for cat, kws in overrides.items():
        if any(kw.lower() in t for kw in (kws or [])):
            return cat
    # 2. Famous musicals deny — title-contains check that beats opera/ballet.
    if any(m in t for m in _MUSICAL_THEATRE_TITLES):
        return "theatre"
    # 3. Title keyword match. For opera/ballet tokens, require word-boundary
    # match — otherwise short keywords ("aida", "tanz") false-positive inside
    # longer German words.
    for cat, kws in _CATEGORY_KEYWORDS:
        for kw in kws:
            if cat in ("opera", "ballet") and " " not in kw:
                if re.search(rf"\b{re.escape(kw)}\b", t):
                    return cat
            elif kw in t:
                return cat
    if stage_default:
        return stage_default
    base = (venue_row.get("category") or "").lower()
    if base and base != "mixed":
        return base
    return "other"


# ─── helpers for orchestrator ────────────────────────────────────────────────


def load_venues(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── CLI smoke test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--venue-id", required=True)
    p.add_argument("--venues-path", default="projects/momEvents/config/venues.yaml")
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args()

    venues = load_venues(args.venues_path)
    row = next((v for v in venues if v["id"] == args.venue_id), None)
    if row is None:
        print(f"venue id {args.venue_id!r} not in {args.venues_path}")
        sys.exit(1)

    events = scrape(row)
    print(f"\n{len(events)} events for {row['id']}\n")
    for ev in events[:args.limit]:
        print(f"  {ev.start!s:30s} | {ev.venue_id:22s} | {ev.category:18s} | {ev.title[:60]}")
