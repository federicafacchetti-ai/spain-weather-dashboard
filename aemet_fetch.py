#!/usr/bin/env python3
"""
AEMET daily alert fetcher for the Spain city list.

- Reads the API key from ~/.aemet_key (or env AEMET_API_KEY, or ./.aemet_key)
- Fetches today's active CAP alerts for Spain
- Filters to alerts overlapping the 155 cities in cities.json
- Writes / appends:
    data/today.json         → snapshot of today's active alerts per city
    data/history.jsonl      → append-only historical repository (one JSON per alert × city hit)
    data/weekly.json        → hours per city × level per ISO week
- Regenerates dashboard.html with all three tabs

Designed to run daily at 08:00 Madrid time via launchd (see com.glovo.aemet.plist).

Usage:
    python3 aemet_fetch.py                # normal daily run
    python3 aemet_fetch.py --dry-run      # fetch + parse but don't write
    python3 aemet_fetch.py --seed         # first run seed (empty history OK)
"""
from __future__ import annotations
import argparse, json, os, sys, tarfile, urllib.request, urllib.parse, ssl
import datetime as dt
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)

CITIES_JSON = HERE / "cities.json"
TODAY_JSON = DATA_DIR / "today.json"
HISTORY_JSONL = DATA_DIR / "history.jsonl"
WEEKLY_JSON = DATA_DIR / "weekly.json"
RED_ALERTS_CSV = DATA_DIR / "red_alerts_today.csv"
DASHBOARD_HTML = HERE / "index.html"  # renamed for GitHub Pages compatibility
LAST_RUN_LOG = DATA_DIR / "last_run.log"

AEMET_BASE = "https://opendata.aemet.es/opendata/api/avisos_cap/ultimoelaborado/area/esp"

# CAP XML namespace
CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}

# Normalise AEMET area names to match the tracker's province names.
# AEMET tends to use accents; tracker sometimes uses regional forms (Gerona/Girona etc).
PROVINCE_ALIASES = {
    # tracker → set of AEMET forms it should also match
    "Gerona":            ["Girona", "Gerona"],
    "Lérida":            ["Lleida", "Lérida"],
    "La Coruña":         ["A Coruña", "La Coruña", "Coruña, A"],
    "Orense":            ["Ourense", "Orense"],
    "Guipúzcoa":         ["Gipuzkoa", "Guipúzcoa"],
    "Vizcaya":           ["Bizkaia", "Vizcaya"],
    "Álava":             ["Araba/Álava", "Álava", "Alava", "Araba"],
    "Baleares":          ["Illes Balears", "Baleares", "Mallorca", "Menorca", "Ibiza", "Formentera"],
    "Las Palmas":        ["Las Palmas", "Gran Canaria", "Fuerteventura", "Lanzarote"],
    "Santa Cruz de Tenerife": ["Santa Cruz de Tenerife", "Tenerife", "La Palma", "La Gomera", "El Hierro"],
    "Castellón":         ["Castellón", "Castelló", "Castelló/Castellón"],
    "Valencia":          ["Valencia", "València", "València/Valencia"],
    "Alicante":          ["Alicante", "Alacant", "Alacant/Alicante"],
    "Cádiz":             ["Cádiz", "Cadiz"],
    "Málaga":            ["Málaga", "Malaga"],
    "Córdoba":           ["Córdoba", "Cordoba"],
    "Jaén":              ["Jaén", "Jaen"],
    "Almería":           ["Almería", "Almeria"],
    "León":              ["León", "Leon"],
    "Cantabria":         ["Cantabria"],
    "Asturias":          ["Asturias", "Principado de Asturias"],
    "La Rioja":          ["La Rioja", "Rioja, La"],
    "Navarra":           ["Navarra", "Nafarroa"],
    "Madrid":            ["Madrid"],
    # For provinces without special forms, we auto-match name == name below.
}

LEVEL_MAP = {
    # CAP <severity> → local level
    "Extreme": "RED", "Severe": "ORANGE", "Moderate": "YELLOW", "Minor": "GREEN",
    # AEMET sometimes uses <parameter valueName="AEMET-Meteoalerta nivel"> with es labels
    "rojo": "RED", "naranja": "ORANGE", "amarillo": "YELLOW", "verde": "GREEN",
}

EVENT_TYPE_MAP_ES = {
    # Approximate mapping from AEMET event names to short types
    "Altas temperaturas": "Heat",
    "Bajas temperaturas": "Cold",
    "Lluvias": "Rain",
    "Precipitaciones": "Rain",
    "Tormentas": "Storm",
    "Nieve": "Snow",
    "Viento": "Wind",
    "Aludes": "Avalanche",
    "Costeros": "Coastal",
    "Fenómenos costeros": "Coastal",
    "Polvo en suspensión": "Dust",
    "Nieblas": "Fog",
    "Deshielo": "Thaw",
}


def log(msg: str):
    ts = dt.datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {msg}"
    print(line)
    with LAST_RUN_LOG.open("a") as f:
        f.write(line + "\n")


def read_api_key() -> str:
    for candidate in (os.environ.get("AEMET_API_KEY"),
                      (HERE / ".aemet_key").read_text().strip() if (HERE / ".aemet_key").exists() else None,
                      (Path.home() / ".aemet_key").read_text().strip() if (Path.home() / ".aemet_key").exists() else None):
        if candidate:
            return candidate
    log("ERROR: no API key. Set AEMET_API_KEY or put it in .aemet_key")
    sys.exit(2)


def _make_ssl_context():
    r"""
    macOS Python often ships without a working system CA store. Prefer certifi's
    CA bundle if available (pip install certifi). Fall back to the system default;
    if that also fails at connect time, run:
      /Applications/Python\ 3.X/Install\ Certificates.command
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch_bytes(url: str, api_key: str | None = None,
                max_retries: int = 4, base_delay: int = 60) -> bytes:
    """Fetch a URL with retries. Handles AEMET's 429 rate-limits gracefully by
    waiting and re-trying with exponential backoff (60s, 120s, 240s, 480s)."""
    import time
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("api_key", api_key)
    req.add_header("Accept", "application/json")
    req.add_header("cache-control", "no-cache")
    ctx = _make_ssl_context()
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 and attempt < max_retries:
                # Respect Retry-After header if present, otherwise exponential backoff
                retry_after = 0
                try:
                    retry_after = int(e.headers.get("Retry-After") or 0)
                except Exception:
                    pass
                delay = max(retry_after, base_delay * (2 ** attempt))
                log(f"HTTP 429 rate-limited by AEMET — retrying in {delay}s (attempt {attempt+1}/{max_retries})")
                time.sleep(delay)
                continue
            raise
    raise last_err  # re-raise after exhausting retries


def fetch_alerts_tar(api_key: str) -> bytes:
    """Two-step AEMET API: first call returns pointer, second call gets the TAR."""
    meta = fetch_bytes(AEMET_BASE, api_key)
    meta_json = json.loads(meta.decode("utf-8"))
    if meta_json.get("estado") != 200:
        log(f"AEMET meta status {meta_json.get('estado')}: {meta_json.get('descripcion')}")
        return b""
    data_url = meta_json["datos"]
    log(f"AEMET datos URL: {data_url}")
    return fetch_bytes(data_url)


def parse_cap_xml(xml_bytes: bytes) -> list[dict]:
    """Parse one CAP alert into a list of (one per info block)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out = []
    sender = root.findtext("cap:sender", default="", namespaces=CAP_NS)
    sent = root.findtext("cap:sent", default="", namespaces=CAP_NS)
    identifier = root.findtext("cap:identifier", default="", namespaces=CAP_NS)
    for info in root.findall("cap:info", CAP_NS):
        # CAP 1.2 stores language in a child <language> element, not an attribute.
        # Also check xml:lang as a fallback for AEMET quirks. Keep Spanish only.
        lang = (info.findtext("cap:language", default="", namespaces=CAP_NS)
                or info.get("{http://www.w3.org/XML/1998/namespace}lang", "")
                or info.get("lang", ""))
        if lang and not lang.lower().startswith("es"):
            continue
        event = info.findtext("cap:event", default="", namespaces=CAP_NS)
        onset = info.findtext("cap:onset", default="", namespaces=CAP_NS)
        expires = info.findtext("cap:expires", default="", namespaces=CAP_NS)
        severity = info.findtext("cap:severity", default="", namespaces=CAP_NS)
        # AEMET-specific parameter with the coloured level
        level = LEVEL_MAP.get(severity, "UNKNOWN")
        for param in info.findall("cap:parameter", CAP_NS):
            name = (param.findtext("cap:valueName", default="", namespaces=CAP_NS) or "").lower()
            val  = (param.findtext("cap:value", default="", namespaces=CAP_NS) or "").lower().strip()
            if "nivel" in name:
                lm = LEVEL_MAP.get(val)
                if lm: level = lm
        for area in info.findall("cap:area", CAP_NS):
            area_desc = area.findtext("cap:areaDesc", default="", namespaces=CAP_NS)
            geocodes = [g.findtext("cap:value", default="", namespaces=CAP_NS)
                        for g in area.findall("cap:geocode", CAP_NS)]
            out.append({
                "identifier": identifier,
                "sender": sender,
                "sent": sent,
                "event": event,
                "event_type_short": next((v for k, v in EVENT_TYPE_MAP_ES.items() if event.startswith(k)), event),
                "level": level,
                "severity_raw": severity,
                "onset": onset,
                "expires": expires,
                "area_desc": area_desc,
                "geocodes": geocodes,
            })
    return out


def parse_alerts_from_tar(tar_bytes: bytes) -> list[dict]:
    alerts = []
    with tarfile.open(fileobj=BytesIO(tar_bytes), mode="r:*") as tar:
        for member in tar.getmembers():
            if not member.isfile(): continue
            if not member.name.endswith(".xml"): continue
            xml_bytes = tar.extractfile(member).read()
            alerts.extend(parse_cap_xml(xml_bytes))
    return alerts


def _norm(s: str) -> str:
    if not s: return ""
    return s.strip().lower().replace("province of ", "").replace("provincia de ", "")


def build_province_alias_map(cities: list[dict]) -> dict[str, set[str]]:
    """tracker-province → set of lowercased AEMET area-desc forms"""
    aliases = defaultdict(set)
    for c in cities:
        prov = c["provincia"]
        if not prov: continue
        forms = PROVINCE_ALIASES.get(prov, [prov])
        for f in forms:
            aliases[prov].add(_norm(f))
    return aliases


def match_alert_to_provinces(area_desc: str, aliases_by_prov: dict[str, set[str]]) -> list[str]:
    """Return list of tracker provinces whose alias appears in the AEMET area_desc."""
    if not area_desc: return []
    ad = _norm(area_desc)
    hits = []
    for prov, forms in aliases_by_prov.items():
        for f in forms:
            if f in ad:
                hits.append(prov); break
    return hits


def _madrid_today_iso() -> str:
    """Today's date in Europe/Madrid, YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()
    except Exception:
        # Fallback: UTC + 2h offset in summer, +1h winter — good enough
        return (dt.datetime.utcnow() + dt.timedelta(hours=2)).date().isoformat()


def hours_between(iso_a: str, iso_b: str) -> float:
    try:
        a = dt.datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def isoweek_of(iso_dt: str) -> str:
    try:
        d = dt.datetime.fromisoformat(iso_dt.replace("Z", "+00:00")).date()
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    except Exception:
        return ""


def run(dry_run: bool = False, seed: bool = False, debug_code: str | None = None):
    api_key = read_api_key()
    cities = json.loads(CITIES_JSON.read_text())
    aliases = build_province_alias_map(cities)
    prov_to_cities = defaultdict(list)
    for c in cities:
        prov_to_cities[c["provincia"]].append(c)

    log(f"Cities loaded: {len(cities)} across {len(aliases)} provinces")
    log("Fetching AEMET CAP TAR…")
    try:
        tar_bytes = fetch_alerts_tar(api_key)
    except Exception as e:
        log(f"Fetch failed: {e}")
        if not seed:
            sys.exit(3)
        tar_bytes = b""

    alerts = parse_alerts_from_tar(tar_bytes) if tar_bytes else []
    total_parsed = len(alerts)
    # Filter out GREEN and UNKNOWN levels — AEMET publishes many "no phenomenon" bulletins
    alerts = [a for a in alerts if a["level"] in ("RED", "ORANGE", "YELLOW")]
    log(f"Alerts parsed: {total_parsed} total → {len(alerts)} actionable (Y/O/R only)")

    # Explode: one row per (alert × affected tracker-city)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    hits = []
    for a in alerts:
        provs = match_alert_to_provinces(a["area_desc"], aliases)
        for p in provs:
            for city in prov_to_cities[p]:
                hits.append({
                    "captured_at": now,
                    "alert_id": a["identifier"],
                    "city_code": city["code"],
                    "city_name": city["name"],
                    "provincia": p,
                    "comunidad": city["comunidad"],
                    "ops_subregion": city["ops_subregion"],
                    "com_region": city["com_region"],
                    "level": a["level"],
                    "event": a["event"],
                    "event_type": a["event_type_short"],
                    "onset": a["onset"],
                    "expires": a["expires"],
                    "hours": round(hours_between(a["onset"], a["expires"]), 2),
                    "iso_week": isoweek_of(a["onset"]) or isoweek_of(now),
                    "area_desc": a["area_desc"],
                })
    log(f"City-alert hits (all Y/O/R): {len(hits)}")

    if debug_code:
        dc = debug_code.upper()
        matches = [h for h in hits if h["city_code"] == dc]
        print(f"\n=== DEBUG {dc}: {len(matches)} raw hits before today-filter/dedup ===")
        for h in matches:
            print(f"  {h['level']:6s} {h['event_type']:8s} onset={h['onset']} expires={h['expires']} area='{h['area_desc']}'")
        print("=== end debug ===\n")

    # Build TODAY snapshot: only alerts whose active window includes today's date (Madrid).
    # Then dedupe by (city, level, event_type) so the UI shows max ~3 badges per city.
    today_madrid = _madrid_today_iso()
    today_active = []
    for h in hits:
        onset_date = (h.get("onset") or "")[:10]
        expires_date = (h.get("expires") or "")[:10]
        if not onset_date or not expires_date: continue
        if onset_date <= today_madrid <= expires_date:
            today_active.append(h)
    # Dedupe — one row per (city, level). Max 3 rows per city (Y/O/R).
    # We keep event_type on the row for tooltip/detail but the UI only shows the colour.
    seen = set(); today_dedup = []
    for h in today_active:
        k = (h["city_code"], h["level"])
        if k in seen: continue
        seen.add(k); today_dedup.append(h)
    log(f"Today active (after date + dedup filter, max 3/city): {len(today_dedup)}")

    if dry_run:
        log("Dry-run mode; not writing anything.")
        for h in hits[:5]:
            print(json.dumps(h, ensure_ascii=False))
        return

    # today.json snapshot (replaces) — only alerts active today, deduped by city×level×type
    TODAY_JSON.write_text(json.dumps({"captured_at": now, "hits": today_dedup}, ensure_ascii=False, indent=2))

    # ── Red alerts CSV for today ─────────────────────────────────────────────
    # 3-column file for pasting into Google Sheets:
    #   City, City Code, Alert
    # Contains ONLY cities with an active RED alert today (deduped, one row/city).
    red_seen = set()
    red_rows = []
    for h in today_dedup:
        if h["level"] != "RED": continue
        if h["city_code"] in red_seen: continue
        red_seen.add(h["city_code"])
        red_rows.append((h["city_name"], h["city_code"], "RED"))
    red_rows.sort(key=lambda r: r[0].lower())
    import csv
    with RED_ALERTS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["City", "City Code", "Alert"])
        for row in red_rows:
            w.writerow(row)
    log(f"Red-alert CSV written: {len(red_rows)} cities → {RED_ALERTS_CSV.name}")
    # history.jsonl append (dedup by alert_id+city_code)
    seen_keys = set()
    if HISTORY_JSONL.exists():
        for line in HISTORY_JSONL.open():
            try:
                r = json.loads(line)
                seen_keys.add((r.get("alert_id"), r.get("city_code")))
            except Exception:
                pass
    added = 0
    with HISTORY_JSONL.open("a") as f:
        for h in hits:
            key = (h["alert_id"], h["city_code"])
            if key in seen_keys: continue
            f.write(json.dumps(h, ensure_ascii=False) + "\n")
            seen_keys.add(key); added += 1
    log(f"History appended: {added} new rows (dedup by alert_id×city_code)")

    # Weekly rollup — hours per (iso_week, city, level)
    weekly = defaultdict(lambda: defaultdict(lambda: {"RED": 0.0, "ORANGE": 0.0, "YELLOW": 0.0, "count": 0}))
    if HISTORY_JSONL.exists():
        for line in HISTORY_JSONL.open():
            try:
                r = json.loads(line)
                wk = r.get("iso_week") or ""
                cc = r.get("city_code") or ""
                lvl = r.get("level") or ""
                if not wk or not cc or lvl not in ("RED", "ORANGE", "YELLOW"): continue
                weekly[wk][cc][lvl] += float(r.get("hours", 0) or 0)
                weekly[wk][cc]["count"] += 1
            except Exception:
                pass
    weekly_out = {wk: {cc: {**vals, "city_name": next((c["name"] for c in cities if c["code"] == cc), cc)}
                      for cc, vals in city_rows.items()}
                  for wk, city_rows in sorted(weekly.items())}
    WEEKLY_JSON.write_text(json.dumps(weekly_out, ensure_ascii=False, indent=2))
    log(f"Weekly rollup: {sum(len(v) for v in weekly_out.values())} city-weeks")

    # Regenerate the dashboard HTML with embedded data (single-file, no server needed)
    render_dashboard(cities, today_dedup, weekly_out, now)
    log("Dashboard regenerated.")


def render_dashboard(cities, today_hits, weekly, captured_at):
    """Regenerate dashboard.html with all data embedded."""
    # Read history (compact for embedding)
    history = []
    if HISTORY_JSONL.exists():
        for line in HISTORY_JSONL.open():
            try:
                history.append(json.loads(line))
            except Exception: pass

    embed = {
        "captured_at": captured_at,
        "cities": cities,
        "today": today_hits,
        "history": history,
        "weekly": weekly,
    }

    html = DASHBOARD_TEMPLATE.replace("__DATA__", json.dumps(embed, ensure_ascii=False))
    DASHBOARD_HTML.write_text(html)


DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<title>Spain Weather Alert Dashboard</title>
<style>
:root{--red:#E8341A;--orange:#F07D1A;--yellow:#F5C842;--green:#2ECC71;--navy:#0D1B2A;--navy2:#162232;--navy3:#1E2F44;--slate:#7A93B0;--light:#C8D8E8;--white:#F0F4F8;}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--navy);color:var(--white);font-family:Arial,sans-serif;font-size:13px;min-height:100vh}
.header{background:linear-gradient(135deg,var(--navy) 0%,var(--navy3) 100%);border-bottom:1px solid rgba(255,255,255,.08);padding:18px 28px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
.header-left{flex:1;min-width:280px}
h1{font-size:1.5rem;font-weight:800;letter-spacing:-.01em}
h1 span{color:var(--yellow)}
.sub{color:var(--slate);font-size:.85rem;margin-top:4px}
.refresh-hint{background:rgba(245,200,66,.08);border:1px solid rgba(245,200,66,.3);border-radius:6px;padding:8px 14px;color:var(--yellow);font-size:.75rem;line-height:1.4;max-width:320px}
.refresh-hint b{color:var(--white)}
.refresh-hint code{background:rgba(0,0,0,.3);padding:2px 6px;border-radius:3px;font-family:'DM Mono',monospace;font-size:.72rem;color:var(--white)}
.refresh-btn{background:var(--yellow);color:var(--navy);border:none;border-radius:6px;padding:10px 18px;font-size:.85rem;font-weight:700;cursor:pointer;font-family:inherit;letter-spacing:.02em}
.refresh-btn:hover{background:#F0BC30}
.refresh-btn:disabled{opacity:.6;cursor:wait}
.refresh-status{color:var(--slate);font-size:.72rem;margin-top:6px}
.tabs{display:flex;gap:2px;background:var(--navy2);padding:0 28px;border-bottom:1px solid rgba(255,255,255,.05)}
.tab{padding:12px 24px;cursor:pointer;color:var(--slate);border-bottom:2px solid transparent;font-weight:500}
.tab.active{color:var(--white);border-color:var(--yellow)}
.tab:hover{color:var(--white)}
main{padding:22px 28px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}
.card{background:var(--navy2);border:1px solid rgba(255,255,255,.06);border-radius:6px;padding:12px}
.card.red{border-left:4px solid var(--red)}
.card.orange{border-left:4px solid var(--orange)}
.card.yellow{border-left:4px solid var(--yellow)}
.card.green{border-left:4px solid var(--green);opacity:.55}
.city-name{font-weight:700;font-size:1rem}
.city-meta{color:var(--slate);font-size:.75rem;margin-top:2px}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.7rem;font-weight:600;margin-top:6px;margin-right:4px}
.pill.red{background:var(--red);color:#fff}
.pill.orange{background:var(--orange);color:#fff}
.pill.yellow{background:var(--yellow);color:#000}
.pill.green{background:var(--green);color:#000}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid rgba(255,255,255,.08)}
th{background:var(--navy3);position:sticky;top:0}
tr:hover{background:rgba(255,255,255,.02)}
.filter{display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.filter input,.filter select{background:var(--navy3);color:var(--white);border:1px solid rgba(255,255,255,.1);padding:6px 10px;border-radius:4px;font-size:.85rem}
.stat{display:inline-block;background:var(--navy3);padding:8px 14px;border-radius:4px;margin-right:10px}
.stat-value{font-weight:700;font-size:1.1rem}
.stat-label{color:var(--slate);font-size:.7rem;text-transform:uppercase}
.footer{color:var(--slate);font-size:.75rem;padding:20px 28px}
</style>
</head><body>
<div class="header">
  <div class="header-left">
    <h1>Spain <span>Weather</span> Alert Dashboard</h1>
    <div class="sub">AEMET official alerts · 155 tracked cities · <span id="last-update">—</span></div>
  </div>
  <div id="refresh-area"></div>
</div>
<div class="tabs">
  <div class="tab active" data-tab="today">Today</div>
  <div class="tab" data-tab="history">Historical Repository</div>
  <div class="tab" data-tab="weekly">Weekly Summary</div>
</div>
<main id="today" class="pane">
  <div id="today-stats"></div>
  <div class="filter">
    <input type="text" id="today-search" placeholder="Search city…">
    <select id="today-level"><option value="">All levels</option><option value="ALARM">All alarms (R+O+Y)</option><option value="RED">Red</option><option value="ORANGE">Orange</option><option value="YELLOW">Yellow</option><option value="GREEN">Green (no alert)</option></select>
    <select id="today-region"><option value="">All OPS subregions</option></select>
  </div>
  <div class="grid" id="today-grid"></div>
</main>
<main id="history" class="pane" style="display:none">
  <div class="filter">
    <input type="text" id="hist-search" placeholder="Search city…">
    <select id="hist-level"><option value="">All levels</option><option value="RED">Red</option><option value="ORANGE">Orange</option><option value="YELLOW">Yellow</option></select>
    <select id="hist-day"><option value="">All days</option></select>
    <select id="hist-week"><option value="">All weeks</option></select>
    <select id="hist-type"><option value="">All types</option></select>
  </div>
  <div id="hist-count" class="sub" style="margin-bottom:10px"></div>
  <table><thead><tr><th>Captured</th><th>City</th><th>Provincia</th><th>Level</th><th>Type</th><th>Event</th><th>Day</th><th>Start</th><th>End</th><th>Hrs</th><th>Week</th></tr></thead><tbody id="hist-body"></tbody></table>
</main>
<main id="weekly" class="pane" style="display:none">
  <div class="filter">
    <select id="wk-week"><option value="">All weeks</option></select>
    <input type="text" id="wk-search" placeholder="Search city…">
  </div>
  <table><thead><tr><th>Week</th><th>City</th><th>Provincia</th><th>OPS subregion</th><th>Red hrs</th><th>Orange hrs</th><th>Yellow hrs</th><th>No alert hrs</th><th>Alerts</th></tr></thead><tbody id="wk-body"></tbody></table>
</main>
<div class="footer">Data source: AEMET Open Data · Regenerated by <code>aemet_fetch.py</code> · Local file, no server required</div>
<script>
const DATA = __DATA__;
const $=s=>document.querySelector(s), $$=s=>Array.from(document.querySelectorAll(s));

// Tab switching
$$('.tab').forEach(t=>t.onclick=()=>{
  $$('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  $$('.pane').forEach(p=>p.style.display='none');
  $('#'+t.dataset.tab).style.display='';
});

$('#last-update').textContent = 'Last updated ' + new Date(DATA.captured_at).toLocaleString('en-GB',{timeZone:'Europe/Madrid'}) + ' Madrid';

// Refresh area: if served via http://localhost, show a working button.
// If opened via file://, show a text hint pointing at the .command file.
(function setupRefresh(){
  const area = $('#refresh-area');
  const isServed = location.protocol === 'http:' || location.protocol === 'https:';
  if (isServed) {
    area.innerHTML = `
      <button class="refresh-btn" id="refresh-btn">🔄 Refresh now</button>
      <div class="refresh-status" id="refresh-status">Auto-refresh: 08:00 daily</div>`;
    $('#refresh-btn').onclick = async () => {
      const btn = $('#refresh-btn'), status = $('#refresh-status');
      btn.disabled = true; btn.textContent = 'Refreshing… (up to ~15s)';
      status.textContent = 'Fetching AEMET…';
      try {
        const r = await fetch('/refresh', {method: 'POST'});
        const j = await r.json();
        if (j.ok) {
          status.textContent = 'Done. Reloading…';
          setTimeout(()=>location.reload(), 300);
        } else {
          btn.disabled = false; btn.textContent = '🔄 Refresh now';
          const detail = (j.stderr_tail||j.error||j.stdout_tail||'').slice(-500);
          status.textContent = 'Failed — ' + detail;
        }
      } catch (e) {
        btn.disabled = false; btn.textContent = '🔄 Refresh now';
        status.textContent = 'Network error: ' + e.message;
      }
    };
  } else {
    area.innerHTML = `
      <div class="refresh-hint">
        <b>Manual refresh:</b> double-click <code>Refresh Dashboard.command</code> in the folder — or double-click <code>Start Dashboard.command</code> to run the dashboard as a proper server with a working Refresh button. Auto-refresh runs every day at 08:00.
      </div>`;
  }
})();

// Populate OPS subregion filter
const regions = [...new Set(DATA.cities.map(c=>c.ops_subregion))].filter(Boolean).sort();
const opts = regions.map(r=>`<option value="${r}">${r}</option>`).join('');
$('#today-region').insertAdjacentHTML('beforeend', opts);

// ---------- TODAY ----------
function renderToday(){
  const q=$('#today-search').value.toLowerCase(), lvl=$('#today-level').value, reg=$('#today-region').value;
  const hitsByCity = {};
  DATA.today.forEach(h=>{ (hitsByCity[h.city_code]=hitsByCity[h.city_code]||[]).push(h); });
  const grid = $('#today-grid'); grid.innerHTML='';
  let counts = {RED:0,ORANGE:0,YELLOW:0,GREEN:0};
  const filtered = DATA.cities.filter(c=>{
    if(reg && c.ops_subregion!==reg) return false;
    if(q && !(`${c.name} ${c.code} ${c.provincia}`.toLowerCase().includes(q))) return false;
    const hits = hitsByCity[c.code]||[];
    const topLvl = hits.length ? hits.map(h=>h.level).sort((a,b)=>['RED','ORANGE','YELLOW'].indexOf(a)-['RED','ORANGE','YELLOW'].indexOf(b))[0] : 'GREEN';
    if(lvl === 'ALARM') { if(!['RED','ORANGE','YELLOW'].includes(topLvl)) return false; }
    else if(lvl && topLvl!==lvl) return false;
    return true;
  });
  filtered.forEach(c=>{
    const hits = hitsByCity[c.code]||[];
    const topLvl = hits.length ? hits.map(h=>h.level).sort((a,b)=>['RED','ORANGE','YELLOW'].indexOf(a)-['RED','ORANGE','YELLOW'].indexOf(b))[0] : 'GREEN';
    counts[topLvl]=(counts[topLvl]||0)+1;
    // Show one coloured pill per active level (max 3): Yellow / Orange / Red
    const levelOrder = {RED:0, ORANGE:1, YELLOW:2};
    const uniqueLevels = [...new Set(hits.map(h=>h.level))].sort((a,b)=>levelOrder[a]-levelOrder[b]);
    const pills = uniqueLevels.map(l=>`<span class="pill ${l.toLowerCase()}">${l.charAt(0)+l.slice(1).toLowerCase()}</span>`).join('');
    grid.insertAdjacentHTML('beforeend', `
      <div class="card ${topLvl.toLowerCase()}">
        <div class="city-name">${c.name} <span style="color:var(--slate);font-weight:400">· ${c.code}</span></div>
        <div class="city-meta">${c.provincia} · ${c.ops_subregion}</div>
        <div>${pills || '<span class="pill green">No alert</span>'}</div>
      </div>`);
  });
  $('#today-stats').innerHTML = `
    <span class="stat"><span class="stat-value" style="color:var(--red)">${counts.RED||0}</span><br><span class="stat-label">Red</span></span>
    <span class="stat"><span class="stat-value" style="color:var(--orange)">${counts.ORANGE||0}</span><br><span class="stat-label">Orange</span></span>
    <span class="stat"><span class="stat-value" style="color:var(--yellow)">${counts.YELLOW||0}</span><br><span class="stat-label">Yellow</span></span>
    <span class="stat"><span class="stat-value" style="color:var(--green)">${counts.GREEN||0}</span><br><span class="stat-label">Clear</span></span>`;
}
['today-search','today-level','today-region'].forEach(id=>$('#'+id).oninput=renderToday);
$('#today-region').onchange=renderToday; $('#today-level').onchange=renderToday;
renderToday();

// ---------- HISTORY ----------
// Helper: derive YYYY-MM-DD from an ISO onset string
const dayOf = h => (h.onset||'').slice(0,10);
// Populate day + week + type filters
const days = [...new Set(DATA.history.map(dayOf))].filter(Boolean).sort().reverse();
$('#hist-day').insertAdjacentHTML('beforeend', days.map(d=>`<option value="${d}">${d}</option>`).join(''));
const weeks = [...new Set(DATA.history.map(h=>h.iso_week))].filter(Boolean).sort().reverse();
const wkLabelHist = w => { const m = w.match(/^(\d{4})-W(\d{2})$/); return m ? `W${m[2]} · ${m[1]}` : w; };
$('#hist-week').insertAdjacentHTML('beforeend', weeks.map(w=>`<option value="${w}">${wkLabelHist(w)}</option>`).join(''));
const types = [...new Set(DATA.history.map(h=>h.event_type))].filter(Boolean).sort();
$('#hist-type').insertAdjacentHTML('beforeend', types.map(t=>`<option value="${t}">${t}</option>`).join(''));
function renderHist(){
  const q=$('#hist-search').value.toLowerCase(), lvl=$('#hist-level').value, day=$('#hist-day').value, wk=$('#hist-week').value, tp=$('#hist-type').value;
  const rows = DATA.history.filter(h=>{
    if(lvl && h.level!==lvl) return false;
    if(day && dayOf(h)!==day) return false;
    if(wk && h.iso_week!==wk) return false;
    if(tp && h.event_type!==tp) return false;
    if(q && !(`${h.city_name} ${h.city_code} ${h.provincia}`.toLowerCase().includes(q))) return false;
    return true;
  }).sort((a,b)=>b.onset.localeCompare(a.onset));
  $('#hist-count').textContent = `${rows.length.toLocaleString()} rows`;
  $('#hist-body').innerHTML = rows.slice(0,2000).map(h=>`
    <tr>
      <td>${(h.captured_at||'').slice(0,16).replace('T',' ')}</td>
      <td>${h.city_name} <span style="color:var(--slate)">${h.city_code}</span></td>
      <td>${h.provincia}</td>
      <td><span class="pill ${h.level.toLowerCase()}">${h.level}</span></td>
      <td>${h.event_type||''}</td>
      <td>${h.event||''}</td>
      <td>${dayOf(h)}</td>
      <td>${(h.onset||'').slice(0,16).replace('T',' ')}</td>
      <td>${(h.expires||'').slice(0,16).replace('T',' ')}</td>
      <td>${h.hours}</td>
      <td>${h.iso_week||''}</td>
    </tr>`).join('') + (rows.length>2000 ? `<tr><td colspan="11" style="color:var(--slate)">…${rows.length-2000} more, refine filters to see them</td></tr>` : '');
}
['hist-search','hist-level','hist-day','hist-week','hist-type'].forEach(id=>$('#'+id).oninput=renderHist);
['hist-level','hist-day','hist-week','hist-type'].forEach(id=>$('#'+id).onchange=renderHist);
renderHist();

// ---------- WEEKLY ----------
const wkKeys = Object.keys(DATA.weekly).sort().reverse();
// Show as "W28 · 2026" in the dropdown for readability; keep full "YYYY-Www" as the value
const wkLabel = w => { const m = w.match(/^(\d{4})-W(\d{2})$/); return m ? `W${m[2]} · ${m[1]}` : w; };
$('#wk-week').insertAdjacentHTML('beforeend', wkKeys.map(w=>`<option value="${w}">${wkLabel(w)}</option>`).join(''));
function renderWeekly(){
  const q=$('#wk-search').value.toLowerCase(), wkSel=$('#wk-week').value;
  const rows=[];
  // Also include cities that had ZERO alerts in a week, so "No alert hrs" is meaningful for them too.
  wkKeys.forEach(wk=>{
    if(wkSel && wk!==wkSel) return;
    const wkData = DATA.weekly[wk] || {};
    DATA.cities.forEach(city=>{
      const v = wkData[city.code] || {RED:0, ORANGE:0, YELLOW:0, count:0};
      if(q && !(`${city.name} ${city.code} ${city.provincia}`.toLowerCase().includes(q))) return;
      // 168 hours per week; naive no-alert hours = 168 - sum of alert hours (clamped at 0)
      const alertHrs = v.RED + v.ORANGE + v.YELLOW;
      const noAlert = Math.max(0, 168 - alertHrs);
      rows.push({wk, code:city.code, city_name:city.name, provincia:city.provincia, ops:city.ops_subregion,
                 red:v.RED, orange:v.ORANGE, yellow:v.YELLOW, noAlert, count:v.count});
    });
  });
  // Sort: within a week, cities with most alert hours first; hide zero-alert rows unless a week is selected
  const showZero = !!wkSel;
  const filtered = showZero ? rows : rows.filter(r => (r.red + r.orange + r.yellow) > 0);
  filtered.sort((a,b)=> a.wk===b.wk ? (b.red+b.orange+b.yellow) - (a.red+a.orange+a.yellow) : b.wk.localeCompare(a.wk));
  $('#wk-body').innerHTML = filtered.map(r=>`
    <tr>
      <td>${r.wk}</td>
      <td>${r.city_name} <span style="color:var(--slate)">${r.code}</span></td>
      <td>${r.provincia}</td>
      <td>${r.ops||''}</td>
      <td style="color:var(--red)">${r.red.toFixed(1)}</td>
      <td style="color:var(--orange)">${r.orange.toFixed(1)}</td>
      <td style="color:var(--yellow)">${r.yellow.toFixed(1)}</td>
      <td style="color:var(--green)">${r.noAlert.toFixed(1)}</td>
      <td>${r.count}</td>
    </tr>`).join('');
}
['wk-search','wk-week'].forEach(id=>$('#'+id).oninput=renderWeekly);
$('#wk-week').onchange=renderWeekly;
renderWeekly();
</script></body></html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true", help="First run — OK if AEMET returns nothing")
    ap.add_argument("--debug", metavar="CITY_CODE", default=None,
                    help="Dump every hit for the given city code to stdout so we can debug missing alerts")
    args = ap.parse_args()
    run(dry_run=args.dry_run, seed=args.seed, debug_code=args.debug)
