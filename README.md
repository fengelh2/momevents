# momEvents

Cultural events calendar for Essen, Düsseldorf, and Recklinghausen — museums, opera, ballet, concerts, theatre.

**Live page:** https://fengelh2.github.io/momevents/

Auto-rebuilds nightly via GitHub Actions. State (favorites, "new since last visit" badges) lives in browser localStorage; no backend.

## Local development

```sh
pip install -r requirements.txt
python tools/rebuild_calendar.py
```

Output: `projects/momEvents/.tmp/events.html`. Serve locally:

```sh
cd projects/momEvents/.tmp && python -m http.server 18080
# then open http://localhost:18080/events.html
```

## Architecture

The scraper is a thin parametric layer over per-venue config in `projects/momEvents/config/venues.yaml`. Each venue declares `kind: ical | html_list | static` plus selectors / regex helpers. See [projects/momEvents/CLAUDE.md](projects/momEvents/CLAUDE.md) for the goal, gotchas, and onboarding workflow.

24 venues across 3 cities at last count.
