# 🛣️ Bangladesh Road Accident News Agents — Dhaka & Chittagong Highway

Three-agent pipeline that turns **online news reports about road accidents** into a clean,
geolocated and **clustered accident dataset**.

```
Online news portals (Google News + ~90 Bangladeshi news portals)
        │   articles mentioning a road accident
        ▼
┌─────────────────────┐   structured accident rows (deaths / injuries / vehicles / places / dates)
│  accident_agent_pro  │
│   (news agent)       │
└──────────┬──────────┘
           │   latitude + longitude added via OpenStreetMap (Nominatim, free, no API key)
           ▼
┌─────────────────────┐   crash coordinates snapped onto the real highway centreline (Overpass / OSM)
│   snap_to_highway    │
│    (snapping)        │
└──────────┬──────────┘
           │   deduplicated, reliable crash points
           ▼
┌─────────────────────┐   DBSCAN clustering on coordinates → ranked accident hotspots
│   n1_hotspots        │
│    (clustering)      │
└─────────────────────┘
```

---

## The three agents

### 1. `accident_agent_pro.py` — news collection & extraction agent

**Job:** go to the news portals and turn accident reports into structured data.

- Searches **Google News** plus ~90 Bangladeshi news portals (Dhaka, Chittagong / Cumilla, Rajshahi,
  Sylhet, Khulna, Barishal, Rangpur, Mymensingh …) for road accidents.
- Downloads the articles, extracts the accident facts with rule-based parsing that understands both
  **English and Bangla** (numbers written in Bengali digits, Bangla number words, English words,
  `AM/PM` times, dates in many formats …).
- Keeps the **exact place names as written in the article** (subdistrict / police station / village /
  road name), translates the small ones, and maps bigger districts to their division.
- Removes the same crash being reported by several news sites (coordinate + date, or place + day).
- Logs every article that was dropped and **why** (`rejected_articles.csv`).
- Optional `--geocode` pass adds **latitude / longitude** for every row through OpenStreetMap
  Nominatim (free, no API key).

```bash
# first run: collect ~200 highway accidents with coordinates
python accident_agent_pro.py --portals --highway --rows 200 --geocode

# bigger window / different start date
python accident_agent_pro.py --portals --rows 2000 --from-date 2020-01-01 --geocode

# only list the portals that are searched
python accident_agent_pro.py --list-portals
```

**Key outputs:** `dhaka_chattogram_highway_accidents.csv` (rows), `rejected_articles.csv`,
`duplicates_skipped.csv`, `geocode_cache.json`.

---

### 2. `snap_to_highway.py` — coordinate snapping agent

**Job:** take accident coordinates from the news articles and snap them onto a **real road
highway** (the actual centreline), instead of using a raw GPS point that may be off the road.

- Downloads the highway geometry (default: **N1** Dhaka ↔ Chittagong) from OpenStreetMap Overpass.
- For every accident, finds the **nearest point on the highway segment**.
- Writes `original_latitude` / `original_longitude` (as reported), the snapped
  `latitude` / `longitude`, `snap_distance_m`, and a `review` flag (`ok` / `CHECK` / `BAD COORDINATES`).

```bash
# snap reported coordinates onto the N1 highway
python snap_to_highway.py input.csv output.csv N1

# e.g.
python snap_to_highway.py dhaka_chattogram_highway_accidents_clean.csv snapped_n1.csv N1
```

Road data is cached to `road_cache_<REF>.json` after the first download, so later runs are offline.

---

### 3. `n1_hotspots.py` — hotspot clustering agent

**Job:** cluster the snapped accidents into **hotspot areas** using DBSCAN
(2 km maximum gap between crashes, ≥ 2 crashes to count as a hotspot).

- Cleans / de-duplicates again, keeps only points that stayed close to the highway when snapped.
- Runs **hold-out test**: finds hotspots from older crashes and checks how many later crashes land in
  them (sanity check of the clustering).
- Writes ranked hotspot table + cleaned events + **map** (`n1_hotspot_map.png`) + **interactive street
  map** (`n1_hotspot_streetmap.html` — open in browser) + **KML** (`n1_hotspots.kml` for Google My Maps /
  Google Earth).

```bash
pip install pandas numpy scikit-learn matplotlib
python n1_hotspots.py snapped_n1.csv

# optional tuning
python n1_hotspots.py snapped_n1.csv --eps-km 2 --min-events 2 --max-snap-m 1500 --top 7
```

**Key outputs** (written next to the input file):

- `n1_hotspots_ranked.csv` — ranked hotspots: crashes, deaths, injuries, severity, night share, centre point.
- `n1_events_clean.csv` — every crash with its assigned hotspot number.
- `n1_hotspot_map.png` — map of crashes + numbered hotspots.
- `n1_hotspot_streetmap.html` — interactive Leaflet map (street / satellite toggle, popups, Google Maps link).
- `n1_hotspots.kml` — KML for Google My Maps / Google Earth.

---

## Full pipeline (from news article to hotspot)

```bash
# 1. collect + extract + geocode accidents from news portals
python accident_agent_pro.py --portals --highway --rows 200 --geocode

# 2. snap coordinates onto the N1 highway
python snap_to_highway.py dhaka_chattogram_highway_accidents_clean.csv snapped_n1.csv N1

# 3. cluster into hotspots
python n1_hotspots.py snapped_n1.csv
```

Outputs of stage 3 (ranked, ready to use):

- `n1_hotspots_ranked.csv` — the hotspots with severity / deaths / injuries.
- `n1_hotspot_streetmap.html` / `n1_hotspots.kml` — maps to view / import the hotspots.

---

## Requirements

```bash
python -m venv accident_env
source accident_env/bin/activate
pip install requests trafilatura feedparser googlenewsdecoder deep-translator "selectolax>=0.4.12,<1"
pip install pandas numpy scikit-learn matplotlib
```

Notes:

- The news agent mainly uses `requests`, `trafilatura`, `feedparser`, `googlenewsdecoder` (+
  `selectolax`) and optionally `deep-translator`.
- The clustering agent uses `pandas`, `numpy`, `scikit-learn`, `matplotlib`.
- Geocoding / snapping hit free public APIs (Nominatim / Overpass) — respect their usage policy
  (Nominatim: max ~1 request/second; Overpass: use the cache after the first run).
- Google News may temporarily block a connection (HTTP 403). Wait a few hours or restart with
  `--search-workers 1 --search-delay 3` / `--skip-check`.

---

## Author

Built for research on road accidents reported by the news media in Dhaka & Chittagong divisions.
Coordinates are taken from the articles / OpenStreetMap and clustered with DBSCAN
(2 km neighbourhood, crash-weighted severity = deaths × 3 + injuries).
