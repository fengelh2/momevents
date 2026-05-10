# Project: momEvents

A weekly cultural-events calendar for one user (mum, lives in Essen). Aggregates upcoming events from ~30 venues in a 35-40km radius around Essen — including Düsseldorf, Duisburg, Dortmund, Wuppertal, Krefeld, Neuss — and explicitly **excluding Köln**.

## Goal

A simple, easy-to-read web page rebuilt nightly that lists upcoming museums exhibitions, vernissages, concerts, ballets, opera, philharmonic, and theatre near Essen. The user is non-technical and 60+. The page should be skimmable on a phone.

## Inputs

- `config/venues.yaml` — the master list of ~30 priority venues + 1-2 regional aggregators. Each entry declares URL, type of source (`ical` | `json_ld` | `html_list`), selectors / URL patterns, alias, and source priority for dedup.
- `config/venue_aliases.json` — hand-curated mapping of venue-name variants ("Aalto-Theater" / "Aalto Theater Essen" / ...) to a canonical `venue_id`. Critical for dedup.

## Outputs

- `events.html` — single static page, rebuilt nightly, grouped by week then by city. Hosted somewhere the user can bookmark (TBD: GitHub Pages, Cloudflare Pages, or local file).
- `events.ics` — generated as a free side-artifact. May be subscribed to in Google Calendar later if the user wants.

## Architecture (WAT)

This project is a thin layer over shared scrapers in `tools/`:

- `tools/scrape_venue_events.py` — **one parametric scraper** that dispatches on `kind` field in venues.yaml. Do NOT write 30 bespoke scripts; write one scraper + 30 small config blocks.
- `tools/parse_ical.py` — fetches and parses .ics URLs into the canonical Event schema.
- `tools/merge_events.py` — deduplicates on `(normalize_title(title), start_date, venue_id)` with `rapidfuzz.token_set_ratio >= 85` as fuzzy fallback. Keeps the entry from the highest-priority source.
- `tools/render_events_html.py` — renders the merged event list to a single static HTML page.
- `tools/publish_calendar.py` — pushes `events.html` + `events.ics` to wherever the user picks for hosting.

## Canonical Event schema

```yaml
title: str            # event title, German
start: datetime       # ISO 8601, with timezone (Europe/Berlin)
end: datetime | null  # ISO 8601, with timezone (optional, null for all-day exhibitions)
venue_id: str         # canonical id from venue_aliases.json
venue_name: str       # display name
city: str             # one of the 11 supported cities
category: str         # museum_exhibition | opera | ballet | concert | theatre | vernissage | other
url: str              # event detail page or venue calendar fallback
description: str | null
price: str | null
source: str           # which venue config row produced this row
```

## Onboarding order for any new venue (see `workflows/onboard_venue.md`)

1. Probe the venue's calendar/Spielplan page for `.ics` URLs first. Many German theatres on TYPO3 (Theater Essen confirmed) expose RFC 5545 iCal per event. **No HTML parsing needed if .ics exists.**
2. Probe for JSON-LD `@type: Event`. Empirically rare on German cultural sites (0/5 in spot checks) but cheap to extract via `extruct` if found.
3. Fall back to handwritten CSS selectors against the listing page. Add `requires_js: true` only if the page is empty without JavaScript (rare on municipal sites).

## Geographic scope (DO NOT widen unilaterally)

- **Include:** Essen, Düsseldorf, Duisburg, Bochum, Dortmund, Mülheim a. d. Ruhr, Oberhausen, Gelsenkirchen, Wuppertal, Krefeld, Neuss.
- **Exclude:** Köln (~70km, explicitly out of scope per user).

## Gotchas / non-obvious notes

- **Eventim and Ticketmaster are hostile to scraping** (Akamai/Cloudflare bot protection, JS-rendered listings). Do not use as primary sources. Reservix is OK as secondary backfill only.
- **JSON-LD on German cultural sites is the exception, not the rule.** Plan for HTML parsing as the default path.
- **Date strings are German-formatted** (`21. Juni 2026`, `Sa, 14.05.`). Parse via `dateparser` with `languages=['de']`.
- **Many "venues" are actually houses with multiple stages.** Theater und Philharmonie Essen (TUP) covers Aalto + Grillo + Philharmonie under one Spielplan — scrape once, split into three `venue_id`s. Same for Wuppertaler Bühnen (Oper + Schauspiel + Sinfonieorchester) and Theater Dortmund (Oper + Ballett + Schauspiel + Konzerthaus + Kinder/Jugendtheater).
- **Vernissages don't always appear on calendar pages.** Private galleries (Sies+Höke, Konrad Fischer) often only post vernissage dates on their "Aktuelles" / "News" page or via Instagram. These are best-effort.
