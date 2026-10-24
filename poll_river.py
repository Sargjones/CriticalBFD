#!/usr/bin/env python3
"""
CriticalBFD river logger.

Pulls recent readings for Water Survey of Canada station 02GB001
(Grand River at Brantford) and keeps one reading per hour.

Writes three files under river/:
  history.json      last 30 days, used by the homepage trend chart
  archive/YYYY.csv  every hourly reading, kept permanently (the receipts log)
  status.json       {"updated": ...} for the monitor board's freshness check

Standard library only, so the GitHub Action needs no installs.
Usage:  python scripts/poll_river.py [--hours N] [--source FILE]
"""
import argparse, csv, json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone

STATION = "02GB001"
API = ("https://api.weather.gc.ca/collections/hydrometric-realtime/items"
       "?f=json&STATION_NUMBER={stn}&sortby=-DATETIME&limit={limit}")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "river")
WINDOW_DAYS = 30
FIELDS = ["time", "flow_m3s", "level_m"]


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(hours, source=None):
    if source:  # local file, for testing
        with open(source) as f:
            return json.load(f)
    # Readings arrive every 5 minutes: 12 per hour, plus slack.
    url = API.format(stn=STATION, limit=min(10000, hours * 13 + 20))
    req = urllib.request.Request(url, headers={"User-Agent": "CriticalBFD river logger (criticalbfd.ca)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def hourly(features):
    """One reading per UTC hour: the top-of-hour reading if present, else the earliest in that hour."""
    best = {}
    for f in features:
        p = f.get("properties") or {}
        if p.get("DATETIME") is None or p.get("DISCHARGE") is None:
            continue
        t = utc(p["DATETIME"])
        hour = t.replace(minute=0, second=0, microsecond=0)
        if hour not in best or t < best[hour][0]:
            lvl = p.get("LEVEL")
            best[hour] = (t, {"time": iso(t),
                              "flow_m3s": round(float(p["DISCHARGE"]), 2),
                              "level_m": None if lvl is None else round(float(lvl), 3)})
    return [best[h][1] for h in sorted(best)]


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_if_changed(path, text):
    old = None
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
    if old != text:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as f:
            f.write(text)
        return True
    return False


def update_archive(rows):
    by_year = {}
    for r in rows:
        by_year.setdefault(r["time"][:4], []).append(r)
    for year, new in by_year.items():
        path = os.path.join(ROOT, "archive", f"{year}.csv")
        merged = {}
        if os.path.exists(path):
            with open(path, newline="") as f:
                for r in csv.DictReader(f):
                    merged[r["time"]] = r
        for r in new:
            merged[r["time"]] = {k: ("" if r[k] is None else r[k]) for k in FIELDS}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
            w.writeheader()
            for t in sorted(merged):
                w.writerow(merged[t])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="how far back to look (max about 720)")
    ap.add_argument("--source", help="read API JSON from a file instead of the web")
    a = ap.parse_args()

    try:
        data = fetch(a.hours, a.source)
    except Exception as e:
        # Don't fail the run: status.json simply stops updating, and the
        # monitor board marks the feed stale, which is the honest signal.
        print(f"::warning::Could not reach the hydrometric API: {e}")
        return 0

    rows = hourly(data.get("features", []))
    if not rows:
        print("::warning::API answered but returned no usable readings")
        return 0

    hist_path = os.path.join(ROOT, "history.json")
    hist = read_json(hist_path, {})
    merged = {r["time"]: r for r in hist.get("readings", [])}
    merged.update({r["time"]: r for r in rows})
    cutoff = iso(datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS))
    readings = [merged[t] for t in sorted(merged) if t >= cutoff]

    latest = readings[-1]["time"] if readings else rows[-1]["time"]
    out = {
        "station": STATION,
        "name": "Grand River at Brantford",
        "source": "Water Survey of Canada real-time hydrometric data (provisional)",
        "units": {"flow": "m3/s", "level": "m"},
        "latest": latest,
        "readings": readings,
    }
    changed = write_if_changed(hist_path, json.dumps(out, separators=(",", ":")) + "\n")
    update_archive(rows)
    if changed:
        # "updated" is the newest reading's time, not the run time, so a
        # frozen gauge shows as stale even while the job keeps running.
        write_if_changed(os.path.join(ROOT, "status.json"),
                         json.dumps({"updated": latest, "feed": "river"}) + "\n")
    print(f"{len(rows)} hourly readings fetched; latest {latest}; "
          f"{len(readings)} in the 30-day window")
    return 0


if __name__ == "__main__":
    sys.exit(main())
