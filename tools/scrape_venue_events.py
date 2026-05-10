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
        elif kind == "static":
            events = _scrape_static(venue_row)
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
    pattern = venue_row["ical_pattern"]
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
    sess = session or requests
    sel = venue_row.get("selectors") or {}
    listing_url = venue_row["calendar_url"]

    resp = sess.get(listing_url, headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    # Pass bytes (not resp.text) so BS4 detects the charset from the document
    # itself. Some German municipal sites (essen.de) don't set Content-Type
    # charset, and `requests` falls back to ISO-8859-1, which mangles UTF-8
    # umlauts ("jüdischer" → "jÃ¼discher").
    soup = BeautifulSoup(resp.content, "html.parser")

    item_sel = sel.get("item")
    if not item_sel:
        log.warning("%s: html_list missing selectors.item", venue_row["id"])
        return []

    items = soup.select(item_sel)
    log.debug("%s: matched %d items via %r", venue_row["id"], len(items), item_sel)

    out: list[Event] = []
    for it in items:
        ev = _assemble_from_html_item(it, listing_url, venue_row)
        if ev is not None:
            out.append(ev)
    return out


def _assemble_from_html_item(item, listing_url: str, venue_row: dict) -> Optional[Event]:
    sel = venue_row["selectors"]

    # When the date is embedded in the title block, preserve line breaks so we can
    # split off venue/location lines after extracting the date.
    title_separator = "\n" if venue_row.get("date_from_title") else " "
    raw_title = _select_text(item, sel.get("title"), separator=title_separator)
    if not raw_title:
        return None

    # Optional skip filter — drop non-event entries like "Museum closed" cards
    for pat in venue_row.get("skip_if_title_matches") or []:
        if re.search(pat, raw_title):
            return None

    # Optional title cleanup: strip suffixes (literal) and regex patterns
    title = _clean_title(raw_title)
    for suffix in venue_row.get("title_strip_suffixes") or []:
        title = re.sub(re.escape(suffix), "", title, flags=re.IGNORECASE).strip()
    for pattern in venue_row.get("title_strip_regex") or []:
        title = re.sub(pattern, "", title).strip(" -–—,.\n\t")

    # Resolve dates. Three modes:
    #   1. date_start + date_end selectors          (Folkwang-style)
    #   2. date selector                            (Red Dot-style, single field with range)
    #   3. date_from_title regex                    (Ruhr Museum-style, embedded in title)
    start = end = None
    date_text = ""
    if sel.get("date_start") or sel.get("date_end"):
        start_t = _select_text(item, sel.get("date_start"))
        end_t = _select_text(item, sel.get("date_end"))
        if start_t:
            start = _parse_one(start_t, venue_row.get("date_format"))
        if end_t:
            end = _parse_one(end_t, venue_row.get("date_format"))
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
                start = _parse_one(start_str, venue_row.get("date_format"))
                end = _parse_one(end_str, venue_row.get("date_format"))
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
                    end = _parse_one(date_text, venue_row.get("date_format"))
                    today = datetime.now(timezone.utc)
                    start = today.replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    start = _parse_one(date_text, venue_row.get("date_format"))
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
        start, end = _parse_date_range(date_text, venue_row.get("date_format"))

    if start is None:
        log.debug("%s: failed to parse date %r (raw_title=%r)", venue_row["id"], date_text, raw_title)
        return None

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
    )


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
    if not selector:
        return ""
    el = node.select_one(selector)
    if el is None:
        return ""
    return (el.get(attr) or "").strip()


# ─── date parsing ────────────────────────────────────────────────────────────


_DATE_PARSER_KW = dict(
    languages=["de", "en"],
    settings={
        "PREFER_DATES_FROM": "future",
        # German convention is DD.MM.YYYY. Without this, dateparser interprets
        # "12.3.2026" as December 3 (US MM.DD) instead of March 12 — causing
        # exhibitions to appear months later than they actually open.
        "DATE_ORDER": "DMY",
    },
)


def _parse_date_range(text: str, explicit_format: Optional[str] = None) -> tuple[Optional[datetime], Optional[datetime]]:
    """Parse a German date string, possibly a range, into (start, end).

    Handles patterns like:
        "21. Juni 2026"
        "Sa, 14.05.2026 19:30"
        "14.05.–30.06.2026"      (range — exhibition run)
        "14.05.2026 — 30.06.2026"
        "ab 18.05.2026"
        "noch bis 17.08.2026"

    Returns tz-aware datetimes (Europe/Berlin → UTC). end is None for single-day events.
    """
    if not text:
        return (None, None)
    text = text.strip()

    # Range patterns — try a few separators
    for sep in [" – ", " — ", " - ", "–", "—", " bis ", " – bis "]:
        if sep in text:
            left, right = text.split(sep, 1)
            l = _parse_one(left.strip(), explicit_format)
            r = _parse_one(right.strip(), explicit_format)
            if l and r:
                return (l, r)

    # Compact range "14.05.–30.06.2026" (no spaces, en-dash with year only on right)
    m = re.match(r"^(\d{1,2}\.\d{1,2}\.)[–—-](\d{1,2}\.\d{1,2}\.\d{4})$", text)
    if m:
        l_str = m.group(1) + m.group(2).split(".")[-1]   # tack year on
        l = _parse_one(l_str, explicit_format)
        r = _parse_one(m.group(2), explicit_format)
        if l and r:
            return (l, r)

    single = _parse_one(text, explicit_format)
    return (single, None)


def _parse_one(text: str, explicit_format: Optional[str] = None) -> Optional[datetime]:
    text = text.strip().rstrip(".")
    if not text:
        return None

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

    parsed = dateparser.parse(text, **_DATE_PARSER_KW)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ─── category inference ──────────────────────────────────────────────────────


_CATEGORY_KEYWORDS = [
    ("vernissage", ["vernissage", "ausstellungseröffnung", "eröffnung"]),
    # Note: bare "oper" deliberately excluded — too many false positives
    # ("Cooper", "Hopper", "Kooperation"). Real operas reach this category via
    # stage_resolver (Aalto, Opernhaus → default_category: opera) or via the
    # featured highlights keyword list.
    ("opera", ["opera", "operette"]),
    ("ballet", ["ballett", "tanztheater", "schwanensee", "nussknacker", "tanz"]),
    ("concert", ["sinfonie", "konzert", "orchester", "philharmonisch", "kammermusik", "rezital", "liederabend"]),
    ("theatre", ["premiere", "schauspiel", "aufführung", "vorstellung"]),
    ("museum_exhibition", ["ausstellung", "exhibition"]),
]


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
    2. global title keyword match (Mozart/Sinfonie/Schwanensee/etc.)
    3. stage_default from stage_resolver rule
    4. venue's base category if not 'mixed'
    5. 'other'
    """
    t = title.lower()
    overrides = venue_row.get("category_keyword_overrides") or {}
    for cat, kws in overrides.items():
        if any(kw.lower() in t for kw in (kws or [])):
            return cat
    for cat, kws in _CATEGORY_KEYWORDS:
        if any(kw in t for kw in kws):
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
