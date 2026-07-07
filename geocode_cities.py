#!/usr/bin/env python3
"""
One-time script: enrich cities.json with lat/lon from OpenStreetMap Nominatim.
Run once when the city list changes. Rate-limited to 1 request/sec per Nominatim ToS.

Usage:
    python3 geocode_cities.py            # geocode cities missing lat/lon
    python3 geocode_cities.py --force    # re-geocode all cities

Takes ~3 minutes for 155 cities.
"""
from __future__ import annotations
import argparse, json, ssl, sys, time
import urllib.request, urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
CITIES_JSON = HERE / "cities.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "spain-weather-dashboard/1.0 (Glovo ES Supply Ops)"


def _ssl_ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def geocode(query: str) -> tuple[float, float] | None:
    params = urllib.parse.urlencode({
        "q": query, "countrycodes": "es",
        "format": "json", "limit": 1,
    })
    req = urllib.request.Request(f"{NOMINATIM}?{params}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl_ctx()) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"    error: {e}")
        return None
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Re-geocode even cities that already have lat/lon")
    args = ap.parse_args()

    cities = json.loads(CITIES_JSON.read_text())
    todo = [c for c in cities if args.force or c.get("lat") is None or c.get("lon") is None]
    print(f"Cities loaded: {len(cities)} · to geocode: {len(todo)}")
    if not todo:
        print("Nothing to do. Use --force to re-geocode all cities.")
        return

    updated, missed = 0, []
    for i, c in enumerate(todo, 1):
        # Try increasingly relaxed queries
        candidates = [
            f"{c['name']}, {c['provincia']}, Spain",
            f"{c['name']}, {c['comunidad']}, Spain",
            f"{c['name']}, Spain",
        ]
        result = None
        for q in candidates:
            result = geocode(q)
            if result: break
            time.sleep(1.1)
        if result is None:
            print(f"  [{i}/{len(todo)}] {c['code']:>4} {c['name']} → NOT FOUND")
            missed.append(c['code'])
        else:
            c["lat"], c["lon"] = round(result[0], 6), round(result[1], 6)
            updated += 1
            print(f"  [{i}/{len(todo)}] {c['code']:>4} {c['name']:<30} → {c['lat']:.4f}, {c['lon']:.4f}")
        time.sleep(1.1)  # Respect Nominatim: 1 req/sec + margin

    CITIES_JSON.write_text(json.dumps(cities, ensure_ascii=False, indent=2))
    print(f"\nDone. Updated: {updated}. Missed: {len(missed)}{' → ' + ', '.join(missed) if missed else ''}.")
    if missed:
        print("Missed cities will keep using province-level matching until you geocode them manually.")


if __name__ == "__main__":
    main()
