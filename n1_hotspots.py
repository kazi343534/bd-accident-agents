"""
N1 (Dhaka-Chittagong Highway) accident hotspot finder.

Usage:
    pip install pandas numpy scikit-learn matplotlib
    python n1_hotspots.py n1_snapped.csv

Optional settings (all have defaults):
    python n1_hotspots.py n1_snapped.csv --eps-km 2 --min-events 2 --max-snap-m 1500

Outputs (written next to the input file):
    n1_hotspots_ranked.csv   ranked hotspots with deaths, injuries, night share
    n1_events_clean.csv      every crash after cleaning, with its hotspot number
    n1_hotspot_map.png       map of crashes and numbered hotspots
"""
import argparse
import math
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN

p = argparse.ArgumentParser()
p.add_argument("csv")
p.add_argument("--eps-km", type=float, default=2.0, help="max gap between crashes in one hotspot (km)")
p.add_argument("--min-events", type=int, default=2, help="crashes needed to call something a hotspot")
p.add_argument("--max-snap-m", type=float, default=1500, help="ignore points moved more than this when snapped to road")
p.add_argument("--death-weight", type=float, default=3.0, help="a death counts this many injuries in the severity score")
p.add_argument("--top", type=int, default=7, help="show only the top N hotspots on the map and in the tables")
a = p.parse_args()

out_dir = os.path.dirname(os.path.abspath(a.csv))
R = 6371.0

# ---------------------------------------------------------------- load
df = pd.read_csv(a.csv, encoding="utf-8-sig")
df.columns = [c.strip().lstrip("﻿") for c in df.columns]
n_raw = len(df)

# only the Dhaka-Chittagong highway
df = df[df["road_name"].astype(str).str.contains("Dhaka-Chittagong", na=False)].copy()
n_n1 = len(df)


# ---------------------------------------------------------------- clean
def parse_date(s):
    s = str(s).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return pd.to_datetime(s[:10], errors="coerce")
    if re.match(r"\d{1,2}/\d{1,2}/\d{4}", s):
        return pd.to_datetime(s.split()[0], format="%d/%m/%Y", errors="coerce")
    return pd.NaT


def parse_injuries(s):
    nums = [int(x) for x in re.findall(r"\d+", str(s))]
    return max(nums) if nums else 0  # "11-12" -> 12


def parse_hour(s):
    m = re.match(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?", str(s), re.I)
    if not m:
        return np.nan
    h = int(m.group(1))
    ap = (m.group(3) or "").upper()
    if ap == "PM" and h < 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return h


df["date"] = df["accident_date"].map(parse_date)
df["injuries"] = df["injury_count"].map(parse_injuries)
df["deaths"] = pd.to_numeric(df["death_count"], errors="coerce").fillna(0).astype(int)
df["hour"] = df["accident_time"].map(parse_hour)

# The same crash is often reported by several news sites. Treat rows as one crash if they
# share a coordinate and date, or the same place name and day/month.
df["_ck"] = df["latitude"].round(3).astype(str) + "|" + df["longitude"].round(3).astype(str)
df["_dk"] = df["date"].dt.strftime("%Y-%m-%d").fillna("NA")
df["_pk"] = df["village_area"].astype(str).str.lower().str.replace(r"[^a-z]", "", regex=True).str[:18]
df["_md"] = df["date"].dt.strftime("%m-%d").fillna("NA")
dup = df.duplicated(["_ck", "_dk"]) | (df.duplicated(["_pk", "_md"]) & (df["_md"] != "NA"))
n_dup = int(dup.sum())
ev = df[~dup].copy().reset_index(drop=True)

# trust only points that did not move far when snapped to the road
ev["trusted"] = ev["snap_distance_m"] <= a.max_snap_m
ev["severity"] = ev["deaths"] * a.death_weight + ev["injuries"]


# ---------------------------------------------------------------- hotspots
def cluster(d):
    X = np.radians(d[["latitude", "longitude"]].values)
    return DBSCAN(eps=a.eps_km / R, min_samples=a.min_events, metric="haversine").fit_predict(X)


ev["hotspot"] = -1
T = ev[ev["trusted"]]
ev.loc[T.index, "hotspot"] = cluster(T)

rows = []
for c, g in ev[ev["hotspot"] >= 0].groupby("hotspot"):
    rows.append(dict(
        hotspot=c,
        lat=round(g.latitude.mean(), 5), lon=round(g.longitude.mean(), 5),
        crashes=len(g), deaths=int(g.deaths.sum()), injuries=int(g.injuries.sum()),
        severity=int(g.severity.sum()),
        district=g.district.mode().iloc[0],
        police_station=g.police_station.mode().iloc[0],
        main_place=g.village_area.mode().iloc[0],
        night_share=round(((g.hour >= 18) | (g.hour < 6)).mean(), 2),
        top_vehicle=g.vehicle_type.astype(str).str.split(r",| and ").explode().str.strip().mode().iloc[0],
    ))
H = pd.DataFrame(rows).sort_values(["severity", "crashes"], ascending=False).reset_index(drop=True)
H.insert(0, "rank", H.index + 1)
n_found = len(H)
H = H.head(a.top).copy()  # keep only the top N for map and tables
rank_of = dict(zip(H["hotspot"], H["rank"]))
ev["hotspot_rank"] = ev["hotspot"].map(rank_of)


# ---------------------------------------------------------------- hold-out test
def hav_km(a_, b_):
    a_, b_ = np.radians(a_), np.radians(b_)
    d = a_[:, None, :] - b_[None, :, :]
    h = np.sin(d[..., 0] / 2) ** 2 + np.cos(a_[:, None, 0]) * np.cos(b_[None, :, 0]) * np.sin(d[..., 1] / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(h))


dd = ev[ev["trusted"] & ev["date"].notna()].sort_values("date")
hold = None
if len(dd) >= 10:
    cut = dd["date"].quantile(0.6)
    train, test = dd[dd["date"] <= cut], dd[dd["date"] > cut]
    lab = cluster(train)
    cent = train.assign(c=lab)[lambda x: x.c >= 0].groupby("c")[["latitude", "longitude"]].mean().values
    if len(cent) and len(test):
        hit = hav_km(test[["latitude", "longitude"]].values, cent).min(axis=1) <= a.eps_km
        hold = (int(hit.sum()), len(test), len(cent), cut.date())

# ---------------------------------------------------------------- save tables
H.to_csv(os.path.join(out_dir, "n1_hotspots_ranked.csv"), index=False)
ev.drop(columns=[c for c in ev.columns if c.startswith("_")]).to_csv(os.path.join(out_dir, "n1_events_clean.csv"), index=False)

# ---------------------------------------------------------------- map
fig, ax = plt.subplots(figsize=(8, 9))
k = math.cos(math.radians(23))
bad, good = ev[~ev["trusted"]], ev[ev["trusted"]]
ax.scatter(bad.longitude, bad.latitude, s=22, facecolors="none", edgecolors="#9aa3a6", label="location needs checking")
ax.scatter(good.longitude, good.latitude, s=22, c="#1f4e5f", label="reliable location")
for _, h in H.iterrows():
    ax.scatter(h.lon, h.lat, s=140 + 40 * h.severity, facecolors="#c2410c", alpha=0.18, edgecolors="#c2410c", linewidths=1.5)
    ax.annotate(str(int(h["rank"])), (h.lon, h.lat), ha="center", va="center", fontsize=11, fontweight="bold", color="#7c2d12",
                bbox=dict(boxstyle="circle,pad=0.2", fc="white", ec="#c2410c"))
for name, lo, la in [("Dhaka", 90.4125, 23.8103), ("Comilla", 91.1809, 23.4607), ("Feni", 91.3976, 23.0159), ("Chattogram", 91.8123, 22.3569)]:
    ax.plot(lo, la, "s", color="black", ms=4)
    ax.annotate(name, (lo, la), xytext=(6, -3), textcoords="offset points", fontsize=9, color="#444")
ax.set_aspect(1 / k)
ax.set_xlabel("longitude")
ax.set_ylabel("latitude")
ax.set_title("N1 crash hotspots (numbers = rank, size = severity)")
ax.legend(loc="lower left", fontsize=8)
ax.grid(alpha=0.2)
fig.tight_layout()
fig.savefig(os.path.join(out_dir, "n1_hotspot_map.png"), dpi=150)

# ---------------------------------------------------------------- street map (opens in your browser)
import html as _html
import json as _json

hs = []
for _, h in H.iterrows():
    hs.append(dict(
        rank=int(h["rank"]), lat=float(h.lat), lon=float(h.lon),
        title=_html.escape(str(h.main_place)),
        info=(f"<b>#{int(h['rank'])} {_html.escape(str(h.main_place))}</b><br>{_html.escape(str(h.district))} / "
              f"{_html.escape(str(h.police_station))}<br>{int(h.crashes)} crashes, {int(h.deaths)} deaths, {int(h.injuries)} injured"
              f"<br>At night: {int(round(h.night_share * 100))}%<br>"
              f"<a target='_blank' href='https://www.google.com/maps?q={h.lat},{h.lon}'>Open in Google Maps</a>"),
        sev=int(h.severity)))
pts = []
for _, r in ev.iterrows():
    pts.append(dict(lat=float(r.latitude), lon=float(r.longitude), ok=bool(r.trusted),
                    info=_html.escape(f"{r.village_area} | {r.deaths} dead, {r.injuries} injured | {r.accident_date}")))

page = """<!doctype html><html><head><meta charset="utf-8"><title>N1 crash hotspots</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#m{height:100%;margin:0}.rk{background:#c2410c;color:#fff;border:2px solid #fff;border-radius:50%;
text-align:center;font:700 14px/26px sans-serif;width:26px;height:26px}.lg{background:#fff;padding:8px 10px;font:12px sans-serif;
line-height:1.6;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3)}</style></head><body><div id="m"></div><script>
var H=__H__, P=__P__;
var m=L.map('m');
var streets=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,
 attribution:'Tiles &copy; Esri'}).addTo(m);
var sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,
 attribution:'Tiles &copy; Esri'});
L.control.layers({'Streets':streets,'Satellite':sat}).addTo(m);
var all=[];
P.forEach(function(p){L.circleMarker([p.lat,p.lon],{radius:4,color:p.ok?'#1f4e5f':'#7b8588',weight:1.5,
 fillColor:p.ok?'#1f4e5f':'#fff',fillOpacity:p.ok?0.9:0.5}).bindPopup(p.info).addTo(m);all.push([p.lat,p.lon]);});
H.forEach(function(h){
 L.circle([h.lat,h.lon],{radius:2000,color:'#c2410c',weight:2,fillColor:'#c2410c',fillOpacity:0.15}).addTo(m);
 L.marker([h.lat,h.lon],{icon:L.divIcon({className:'',html:'<div class="rk">'+h.rank+'</div>',iconSize:[26,26],iconAnchor:[13,13]})})
  .bindPopup(h.info).bindTooltip(h.title).addTo(m);});
m.fitBounds(all,{padding:[30,30]});
var lg=L.control({position:'bottomleft'});lg.onAdd=function(){var d=L.DomUtil.create('div','lg');
d.innerHTML='<b>N1 crash hotspots</b><br><span style="color:#c2410c">&#9679;</span> numbered = hotspot rank (2 km zone)<br>'+
'<span style="color:#1f4e5f">&#9679;</span> reliable location<br><span style="color:#7b8588">&#9675;</span> location needs checking';return d;};lg.addTo(m);
</script></body></html>""".replace("__H__", _json.dumps(hs)).replace("__P__", _json.dumps(pts))
with open(os.path.join(out_dir, "n1_hotspot_streetmap.html"), "w", encoding="utf-8") as f:
    f.write(page)

# ---------------------------------------------------------------- KML for Google My Maps / Google Earth
def _kml_point(name, desc, lat, lon):
    return (f"<Placemark><name>{name}</name><description><![CDATA[{desc}]]></description>"
            f"<Point><coordinates>{lon},{lat},0</coordinates></Point></Placemark>")

kml = ['<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>N1 crash hotspots</name>']
for _, h in H.iterrows():
    kml.append(_kml_point(_html.escape(f"#{int(h['rank'])} {h.main_place}"),
                          f"{h.district} / {h.police_station}<br>{int(h.crashes)} crashes, {int(h.deaths)} deaths, {int(h.injuries)} injured",
                          h.lat, h.lon))
kml.append("</Document></kml>")
with open(os.path.join(out_dir, "n1_hotspots.kml"), "w", encoding="utf-8") as f:
    f.write("".join(kml))

# ---------------------------------------------------------------- print summary
print(f"\nRows in file: {n_raw}   N1 rows: {n_n1}   duplicate reports removed: {n_dup}")
print(f"Distinct crashes: {len(ev)}   with reliable location (<= {a.max_snap_m:.0f} m snap): {int(ev.trusted.sum())}")
print(f"Hotspots found: {n_found}. Showing the top {len(H)} by severity.")
print(f"Settings: hotspot = {a.min_events}+ crashes within {a.eps_km} km; score = deaths x{a.death_weight:g} + injuries\n")
show = H[["rank", "main_place", "district", "crashes", "deaths", "injuries", "severity", "night_share"]].copy()
show["main_place"] = show["main_place"].str.slice(0, 45)
print(show.to_string(index=False))
if hold:
    print(f"\nHold-out test: hotspots found from crashes up to {hold[3]} ({hold[2]} hotspots) "
          f"caught {hold[0]} of {hold[1]} later crashes ({hold[0] / hold[1]:.0%}).")
else:
    print("\nHold-out test skipped: not enough dated crashes with reliable locations.")
print(f"\nFiles written to: {out_dir}\n  n1_hotspots_ranked.csv\n  n1_events_clean.csv\n  n1_hotspot_map.png\n"
      "  n1_hotspot_streetmap.html   <- double-click to open the street map in your browser (needs internet)\n"
      "  n1_hotspots.kml             <- import into Google My Maps or Google Earth")
