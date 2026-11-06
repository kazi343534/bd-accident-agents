"""
Snap accident coordinates onto a real Bangladesh highway centreline (OpenStreetMap).

Usage:  python3 snap_to_highway.py input.csv output.csv [HIGHWAY_REF]
        HIGHWAY_REF defaults to N1 (Dhaka-Chittagong). Use N2 for Dhaka-Sylhet, etc.

The road data is saved to road_cache_<REF>.json after the first successful
download, so later runs work without the OpenStreetMap servers.
"""
import csv, json, math, os, sys, time, urllib.parse, urllib.request

OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]
REVIEW_KM = 2.0   # snaps longer than this are flagged for manual check


def load_ways(js):
    """Accepts our own cache, an Overpass JSON export, or a GeoJSON export."""
    if isinstance(js, list):                       # our own cache format
        return [[tuple(p) for p in w] for w in js]
    ways = []
    for el in js.get("elements", []):              # Overpass JSON (out geom)
        if el.get("geometry"):
            ways.append([(p["lat"], p["lon"]) for p in el["geometry"]])
    for ft in js.get("features", []):              # GeoJSON from overpass-turbo
        g = ft.get("geometry") or {}
        lines = [g["coordinates"]] if g.get("type") == "LineString" else \
                g.get("coordinates", []) if g.get("type") == "MultiLineString" else []
        for ln in lines:
            ways.append([(c[1], c[0]) for c in ln])
    if not ways:
        raise SystemExit("The road file has no road geometry in it.")
    return ways


def fetch_road(ref):
    cache = f"road_cache_{ref}.json"
    if os.path.exists(cache):
        print(f"Using saved road data: {cache}")
        return load_ways(json.load(open(cache)))
    query = f"""
[out:json][timeout:180];
way["ref"="{ref}"]["highway"~"^(trunk|primary)$"](20.5,88.0,26.7,92.7);
out geom;
"""
    last = None
    for attempt in range(2):                      # two passes over all servers
        for url in OVERPASS:
            print(f"Downloading {ref} road data from {url} (up to 150 s)...", flush=True)
            try:
                req = urllib.request.Request(
                    url, urllib.parse.urlencode({"data": query}).encode(),
                    headers={"User-Agent": "accident-snapper/1.0 (research script)",
                             "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=150) as r:
                    js = json.load(r)
                ways = load_ways(js) if js.get("elements") else []
                if ways:
                    json.dump(ways, open(cache, "w"))
                    print(f"  OK, saved to {cache}")
                    return ways
                last = "no road found with that ref"
                print("  server returned no roads for this ref")
            except Exception as e:
                last = e
                print(f"  failed: {e}", flush=True)
        if attempt == 0:
            print("All servers failed; waiting 20 s and trying again...", flush=True)
            time.sleep(20)
    raise SystemExit(f"Could not download {ref} from OpenStreetMap: {last}")


def snap(lat, lon, ways):
    """Nearest point on any road segment. Returns (lat, lon, distance_m)."""
    kx = 111320 * math.cos(math.radians(lat))
    ky = 110540
    best = (None, None, float("inf"))
    for w in ways:
        for (la1, lo1), (la2, lo2) in zip(w, w[1:]):
            ax, ay = (lo1 - lon) * kx, (la1 - lat) * ky
            bx, by = (lo2 - lon) * kx, (la2 - lat) * ky
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0 if L2 == 0 else max(0, min(1, -(ax * dx + ay * dy) / L2))
            px, py = ax + t * dx, ay + t * dy
            d = math.hypot(px, py)
            if d < best[2]:
                best = (lat + py / ky, lon + px / kx, d)
    return best


def main(src, dst, ref="N1"):
    ways = fetch_road(ref)
    print(f"Loaded {len(ways)} {ref} road segments")
    with open(src, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header = [h.strip() for h in rows[0]]
    if header and header[-1] == "":
        header[-1] = "source_url"
    low = {h.lower(): h for h in header}
    lat_c, lon_c = low.get("latitude"), low.get("longitude")
    if not lat_c or not lon_c:
        raise SystemExit(f"Could not find latitude/longitude columns. Found: {header}")
    d_c, t_c = low.get("accident_date"), low.get("accident_time")
    out = []
    for r in rows[1:]:
        if not any(x.strip() for x in r):
            continue
        r = (r + [""] * len(header))[:len(header)]
        rec = dict(zip(header, r))
        if d_c and t_c and "M" in rec[d_c].upper() and "/" in rec[t_c]:
            rec[d_c], rec[t_c] = rec[t_c], rec[d_c]
        try:
            la, lo = float(rec[lat_c]), float(rec[lon_c])
            sla, slo, d = snap(la, lo, ways)
            rec.update(original_latitude=la, original_longitude=lo,
                       snap_distance_m=round(d),
                       review="CHECK" if d > REVIEW_KM * 1000 else "ok")
            rec[lat_c], rec[lon_c] = round(sla, 6), round(slo, 6)
        except ValueError:
            rec.update(original_latitude="", original_longitude="",
                       snap_distance_m="", review="BAD COORDINATES")
        out.append(rec)
    fields = header + ["original_latitude", "original_longitude",
                       "snap_distance_m", "review"]
    with open(dst, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(out)
    far = sum(1 for r in out if r["review"] != "ok")
    print(f"Wrote {len(out)} rows to {dst}; {far} flagged for review")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
