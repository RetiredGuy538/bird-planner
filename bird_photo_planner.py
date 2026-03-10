#!/usr/bin/env python3
"""
Lake Koshkonong Bird Photography Session Planner  v2.0
======================================================
New in v2.0:
  * Solunar tables -- major/minor feeding windows from pure Python lunar math
  * Haikubox live detections -- "Recently on your deck" panel + peak activity chart
  * 🌕 Prime Session tier -- golden hour + calm wind + major solunar aligned
  * Updated scoring: light 40pts, golden hour 30pts, solunar up to +20pts (normalized to 100)

Double-click or run: python3 bird_photo_planner_v2.py
"""

import json
import math
import http.server
import threading
import webbrowser
import urllib.request
import urllib.error
import sys
from datetime import datetime, timezone, timedelta

# ── Central Time (DST-aware) ──────────────────────────────────────────────────
def _dst_bounds(year):
    march1   = datetime(year, 3,  1, tzinfo=timezone.utc)
    dst_start = march1 + timedelta(days=(6 - march1.weekday()) % 7 + 7)
    dst_start = dst_start.replace(hour=2)
    nov1      = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end   = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    dst_end   = dst_end.replace(hour=2)
    return dst_start, dst_end

def utc_to_central(dt_utc):
    ds, de = _dst_bounds(dt_utc.year)
    return dt_utc + timedelta(hours=-5 if ds <= dt_utc < de else -6)

def now_central():
    return utc_to_central(datetime.now(timezone.utc))

# ── Location & device ─────────────────────────────────────────────────────────
LAT          = 42.9136
LNG          = -88.8601
HAIKUBOX_ID  = "3485189C16A4"

# ── Wind ──────────────────────────────────────────────────────────────────────
EXPOSED_DIRS   = {'W','NW','SW','WNW','WSW','NNW'}
SHELTERED_DIRS = {'E','NE','SE','ENE','ESE','NNE'}
EAGLE_DIRS     = {'E','NE','ENE','NNE'}
COMPASS = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW']

def deg_to_compass(deg):
    return COMPASS[round(deg / 22.5) % 16]

def wind_impact(speed, dir_deg):
    d = deg_to_compass(dir_deg)
    if d in SHELTERED_DIRS: eff = speed * 0.3
    elif d in EXPOSED_DIRS: eff = speed * 1.0
    else:                   eff = speed * 0.6
    return eff, d, d in EXPOSED_DIRS, d in SHELTERED_DIRS

def parse_hhmm(s):
    if 'T' in s: s = s.split('T')[1]
    hh, mm = s[:5].split(':')
    return int(hh) + int(mm) / 60.0

# ── Solunar tables ────────────────────────────────────────────────────────────
def _jd(year, month, day, hour_utc=0.0):
    if month <= 2: year -= 1; month += 12
    A = int(year / 100)
    B = 2 - A + int(A / 4)
    return int(365.25*(year+4716)) + int(30.6001*(month+1)) + day + hour_utc/24.0 + B - 1524.5

def _moon_lon(jd):
    T  = (jd - 2451545.0) / 36525.0
    r  = lambda x: math.radians(x % 360)
    L0 = 218.3164477 + 481267.88123421 * T
    M  = 134.9633964 + 477198.8675055  * T
    Ms = 357.5291092 +  35999.0502909  * T
    F  =  93.2720950 + 483202.0175233  * T
    D  = 297.8501921 + 445267.1114034  * T
    lon = (L0
        + 6.288774*math.sin(r(M))
        + 1.274027*math.sin(r(2*D-M))
        + 0.658314*math.sin(r(2*D))
        + 0.213618*math.sin(r(2*M))
        - 0.185116*math.sin(r(Ms))
        - 0.114332*math.sin(r(2*F))
        + 0.058793*math.sin(r(2*D-2*M))
        + 0.053322*math.sin(r(2*D+M))
        + 0.045758*math.sin(r(2*D-Ms)))
    return lon % 360

def _lst(jd, lng):
    T    = (jd - 2451545.0) / 36525.0
    gmst = 280.46061837 + 360.98564736629*(jd-2451545.0) + 0.000387933*T**2
    return (gmst + lng) % 360

def solunar_windows(date_str, lat=LAT, lng=LNG):
    """Return list of {type, hour_ct, label} for date_str (YYYY-MM-DD)."""
    d       = datetime.strptime(date_str, "%Y-%m-%d")
    utc_ref = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)
    ct_ref  = utc_to_central(utc_ref)
    utc_off = (ct_ref.replace(tzinfo=None) - utc_ref.replace(tzinfo=None)).total_seconds() / 3600

    # Sample hour-angles every minute across the day
    jd_start = _jd(d.year, d.month, d.day, -utc_off)   # midnight CT in UTC
    ha_series = []
    for minute in range(1441):
        frac   = minute / 1440.0
        jd     = jd_start + frac
        lon    = _moon_lon(jd)
        lst    = _lst(jd, lng)
        ha     = (lst - lon) % 360
        hour_ct = minute / 60.0
        ha_series.append((hour_ct, ha))

    def crossings(target):
        hits = []
        for i in range(1, len(ha_series)):
            h0, a0 = ha_series[i-1]
            h1, a1 = ha_series[i]
            # Handle 360/0 wrap by unwrapping
            diff = a1 - a0
            if diff >  180: a1 -= 360
            if diff < -180: a1 += 360
            # Also handle target=0 wrap: re-centre around target
            a0c = ((a0 - target + 180) % 360) - 180
            a1c = a0c + (a1 - a0)
            if a0c * a1c <= 0 and a0c != a1c:
                t = h0 + (-a0c) / (a1c - a0c) * (h1 - h0)
                hits.append(t % 24)
        return hits

    windows = []
    seen    = set()
    def add(wtype, hs, label):
        for h in hs:
            key = round(h * 4)
            if key in seen: continue
            seen.add(key)
            windows.append({'type': wtype, 'hour_ct': h, 'label': label})

    add('major', crossings(0),   'Moon overhead')
    add('major', crossings(180), 'Moon underfoot')
    add('minor', crossings(90),  'Moonset')
    add('minor', crossings(270), 'Moonrise')

    windows.sort(key=lambda w: w['hour_ct'])
    return windows

def solunar_score(hour, windows):
    best_pts, best_lbl = 0, ''
    for w in windows:
        diff = min(abs(hour - w['hour_ct']), 24 - abs(hour - w['hour_ct']))
        if diff > 1.0: continue
        pts = round((20 if w['type'] == 'major' else 10) * max(0, 1 - diff))
        if pts > best_pts:
            best_pts = pts
            best_lbl = ('🌕 ' if w['type'] == 'major' else '🌙 ') + w['label']
    return best_pts, best_lbl

# ── Light / golden hour ───────────────────────────────────────────────────────
def light_period(hour, sunrise, sunset):
    morn_start = sunrise - 0.5
    morn_end   = sunrise + 2.5
    eve_start  = sunset  - 2.5
    eve_end    = sunset  + 0.5

    if hour < morn_start or hour > eve_end:
        return None, 0.0, False, False

    in_morn = morn_start <= hour <= morn_end
    in_eve  = eve_start  <= hour <= eve_end

    if in_morn:
        period   = 'morning'
        mge      = sunrise + 1.0
        gs       = (1.0 - abs(hour - sunrise)) if hour <= mge else max(0.0, 1.0 - (hour - mge)/1.5)
        is_gold  = sunrise <= hour <= mge
        is_twil  = morn_start <= hour < sunrise
    elif in_eve:
        period   = 'evening'
        egs      = sunset - 1.0
        gs       = (1.0 - abs(hour - sunset)) if hour >= egs else max(0.0, 1.0 - (egs - hour)/1.5)
        is_gold  = egs <= hour <= sunset
        is_twil  = sunset < hour <= eve_end
    else:
        return None, 0.0, False, False

    return period, max(0.0, gs), is_gold, is_twil

# ── Score ─────────────────────────────────────────────────────────────────────
def score_hour(hr, wind, wind_dir, cloud, precip, sunrise, sunset, sol_windows=None):
    period, gs, is_gold, is_twil = light_period(float(hr), sunrise, sunset)
    eff, direction, exposed, sheltered = wind_impact(wind, wind_dir)

    base = dict(score=0, grade='poor', notes=[], period=None,
                eff_speed=round(eff,1), direction=direction,
                is_eagle=False, is_golden=False, is_prime=False,
                solunar_pts=0, solunar_label='',
                sunrise_h=sunrise, sunset_h=sunset)

    if period is None: return base
    if precip > 40:
        r = base.copy(); r.update(period=period, notes=['Rain likely']); return r

    notes = []; raw = 0

    # Wind — 40 pts
    if   eff < 3:   raw += 40; notes.append('Near-calm winds')
    elif eff < 7:   raw += 33; notes.append('Light breeze')
    elif eff < 10:  raw += 20
    elif eff < 14:  raw += 8;  notes.append('Moderate wind')
    else:                       notes.append('Windy — deck exposed')
    if sheltered: notes.append('Sheltered (easterly wind)')
    if exposed:   notes.append('Exposed (westerly wind)')

    # Light quality — 40 pts
    if   cloud < 20: base_l = 40; notes.append('🌅 Golden hour' if is_gold else 'Bright sun')
    elif cloud < 45: base_l = 30; notes.append('Partly cloudy')
    elif cloud < 75: base_l = 16; notes.append('Mostly cloudy')
    else:            base_l = 5;  notes.append('Overcast')

    if is_gold:
        light_pts = round(base_l * (0.7 + 0.3 * gs))
        if 20 <= cloud < 65 and eff < 10: notes.append('Dramatic golden light possible')
    elif is_twil:
        light_pts = round(base_l * 0.5); notes.append('Civil twilight — soft light')
    else:
        light_pts = round(base_l * max(0.3, gs))
    raw += light_pts

    # Sun position — 30 pts
    raw += round(30 * gs)

    # Dry — 10 pts
    if   precip < 10: raw += 10
    elif precip < 25: raw += 5

    # Solunar — up to 20 pts
    sol_pts, sol_lbl = 0, ''
    if sol_windows:
        sol_pts, sol_lbl = solunar_score(float(hr), sol_windows)
        raw += sol_pts
        if sol_lbl: notes.append(sol_lbl)

    # Normalize: theoretical max = 140 → scale to 100
    score = round(min(100, raw * 100 / 140))

    is_eagle = (period == 'morning' and direction in EAGLE_DIRS
                and eff < 8 and cloud < 50 and float(hr) >= sunrise - 0.25)
    if is_eagle: notes.append('🦅 Eagle watch — easterly wind, calm water, low sun angle')

    is_prime = is_gold and eff < 8 and sol_pts >= 15
    if is_prime: notes.append('🌕 Prime session — golden hour + calm + major solunar aligned')

    grade = ('prime'     if is_prime else
             'eagle'     if is_eagle else
             'excellent' if score >= 75 else
             'good'      if score >= 55 else
             'fair'      if score >= 35 else 'poor')

    return dict(score=score, grade=grade, notes=notes, period=period,
                eff_speed=round(eff,1), direction=direction,
                is_eagle=is_eagle, is_golden=is_gold, is_prime=is_prime,
                solunar_pts=sol_pts, solunar_label=sol_lbl,
                sunrise_h=sunrise, sunset_h=sunset)

# ── Fetch data ────────────────────────────────────────────────────────────────
def fetch_weather():
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={LAT}&longitude={LNG}"
           f"&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,cloud_cover,precipitation_probability"
           f"&daily=sunrise,sunset"
           f"&wind_speed_unit=mph&temperature_unit=fahrenheit"
           f"&timezone=America%2FChicago&forecast_days=7")
    req = urllib.request.Request(url, headers={"User-Agent": "BirdPlanner/2.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

def fetch_haikubox(hours=24):
    url = f"https://api.haikubox.com/haikubox/{HAIKUBOX_ID}/detections?hours={hours}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BirdPlanner/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  ⚠️  Haikubox: {e}")
        return None

def process_haikubox(data):
    if not data or 'detections' not in data:
        return [], {h: 0 for h in range(24)}
    detections    = data['detections']
    species_map   = {}
    hourly_counts = {h: 0 for h in range(24)}
    for det in detections:
        cn = det.get('cn', 'Unknown')
        dt = det.get('dt', '')
        try:
            dt_utc = datetime.strptime(dt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            dt_ct  = utc_to_central(dt_utc)
            hourly_counts[dt_ct.hour] = hourly_counts.get(dt_ct.hour, 0) + 1
        except Exception:
            dt_ct = None
        if cn not in species_map:
            species_map[cn] = {'count': 0, 'last': dt_ct, 'spCode': det.get('spCode',''), 'sn': det.get('sn','')}
        species_map[cn]['count'] += 1
        if dt_ct and (not species_map[cn]['last'] or dt_ct > species_map[cn]['last']):
            species_map[cn]['last'] = dt_ct
    species_list = sorted(
        [{'name': k, **v} for k, v in species_map.items()],
        key=lambda x: x['count'], reverse=True)
    return species_list, hourly_counts

# ── Formatting helpers ────────────────────────────────────────────────────────
def fmt12(h):
    if h == 0:  return "12:00 AM"
    if h == 12: return "12:00 PM"
    return f"{h}:00 AM" if h < 12 else f"{h-12}:00 PM"

def fmt12f(h):
    """Decimal hour → '6:30 AM'."""
    h = h % 24; hh = int(h); mm = int(round((h-hh)*60))
    if mm == 60: hh += 1; mm = 0
    hh %= 24
    p = 'AM' if hh < 12 else 'PM'
    h12 = hh if 1 <= hh <= 12 else (12 if hh == 0 else hh-12)
    return f"{h12}:{mm:02d} {p}"

def fmt_ampm(s):
    hh, mm = s.split(':'); hh = int(hh); mm = int(mm)
    p = 'AM' if hh < 12 else 'PM'
    h12 = hh if 1 <= hh <= 12 else (12 if hh == 0 else hh-12)
    return f"{h12}:{mm:02d} {p}"

def fmt_date(ds):
    return datetime.strptime(ds, "%Y-%m-%d").strftime("%A, %b %-d")

def fmt_date_short(ds):
    return datetime.strptime(ds, "%Y-%m-%d").strftime("%b %-d")

WIND_ARROWS = ['↓','↙','←','↖','↑','↗','→','↘']
def wind_arrow(deg):
    return WIND_ARROWS[round(((deg+180)%360)/45)%8]

# ── HTML builder ──────────────────────────────────────────────────────────────
def build_html(weather_json, haikubox_data=None):
    h = weather_json['hourly']
    d = weather_json['daily']
    n = len(h['time'])

    # Sunrise/sunset by date
    sun = {}
    for i, ds in enumerate(d['time']):
        sun[ds] = {
            'rise': parse_hhmm(d['sunrise'][i]),
            'set':  parse_hhmm(d['sunset'][i]),
            'rise_str': (d['sunrise'][i].split('T')[1][:5] if 'T' in d['sunrise'][i] else d['sunrise'][i][:5]),
            'set_str':  (d['sunset'][i].split('T')[1][:5]  if 'T' in d['sunset'][i]  else d['sunset'][i][:5]),
        }

    # Solunar windows by date
    print("  Computing solunar tables for 7 days...")
    sol_by_day = {ds: solunar_windows(ds) for ds in d['time']}

    # All hours
    all_hours = []
    for i in range(n):
        dt = h['time'][i]; ds = dt[:10]; hr = int(dt[11:13])
        sd = sun.get(ds, {'rise': 6.0, 'set': 18.0})
        rec = dict(dt=dt, ds=ds, hour=hr,
                   temp=round(h['temperature_2m'][i]),
                   wind=round(h['wind_speed_10m'][i]),
                   wind_dir=h['wind_direction_10m'][i],
                   cloud=h['cloud_cover'][i],
                   precip=h['precipitation_probability'][i],
                   sunrise_h=sd['rise'], sunset_h=sd['set'])
        rec['analysis'] = score_hour(hr, rec['wind'], rec['wind_dir'],
                                     rec['cloud'], rec['precip'],
                                     rec['sunrise_h'], rec['sunset_h'],
                                     sol_by_day.get(ds, []))
        all_hours.append(rec)

    days = {}
    for rec in all_hours:
        days.setdefault(rec['ds'], []).append(rec)

    # Haikubox
    species_list, hourly_counts = process_haikubox(haikubox_data)

    # Top picks
    ct_now  = now_central()
    now_ds  = ct_now.strftime("%Y-%m-%d")
    now_hr  = ct_now.hour

    light_hrs  = [r for r in all_hours if r['analysis']['period']]
    candidates = sorted(light_hrs, key=lambda r: r['analysis']['score'], reverse=True)
    seen = set(); top_picks = []
    for c in candidates:
        key = f"{c['ds']}-{c['analysis']['period']}"
        if key in seen: continue
        seen.add(key); top_picks.append(c)
        if len(top_picks) >= 4: break

    # Ensure a prime or eagle slot if not already present
    for flag in ('is_prime', 'is_eagle'):
        sc = next((r for r in candidates if r['analysis'].get(flag)), None)
        if sc and not any(p['dt'] == sc['dt'] for p in top_picks):
            top_picks.append(sc); top_picks = top_picks[:4]

    top_picks.sort(key=lambda r: r['dt'])
    top_picks = [r for r in top_picks
                 if r['ds'] > now_ds or (r['ds'] == now_ds and r['hour'] >= now_hr)]
    if len(top_picks) < 4:
        existing = {f"{r['ds']}-{r['analysis']['period']}" for r in top_picks}
        for c in candidates:
            if len(top_picks) >= 4: break
            key = f"{c['ds']}-{c['analysis']['period']}"
            if key in existing: continue
            if c['ds'] < now_ds or (c['ds'] == now_ds and c['hour'] < now_hr): continue
            existing.add(key); top_picks.append(c)
        top_picks.sort(key=lambda r: r['dt'])

    generated = ct_now.strftime("%A %b %-d, %Y at %-I:%M %p CT")

    # ── CSS ───────────────────────────────────────────────────────────────────
    css = """
    :root{--gold:#c9a84c;--pale:#f2e8d5;--sub:#8fa3b1;--good:#27ae60;--warn:#e67e22;--red:#c0392b;
      --bg:#0f1420;--card:#161d2e;--border:rgba(201,168,76,0.2);--text:#e8e0d0;
      --prime:#a78bfa;--prime-bg:rgba(167,139,250,.12);}
    *{margin:0;padding:0;box-sizing:border-box;}
    body{background:var(--bg);color:var(--text);font-family:'Source Code Pro',monospace;min-height:100vh;}
    .sky-bg{position:fixed;inset:0;z-index:0;pointer-events:none;
      background:radial-gradient(ellipse at 20% 30%,rgba(201,168,76,.06) 0%,transparent 50%),
        radial-gradient(ellipse at 80% 70%,rgba(61,107,125,.08) 0%,transparent 50%),
        radial-gradient(ellipse at 65% 5%,rgba(167,139,250,.05) 0%,transparent 45%),
        linear-gradient(180deg,#0a0e18 0%,#0f1420 40%,#111828 100%);}
    .content{position:relative;z-index:1;max-width:1100px;margin:0 auto;padding:24px 20px 60px;}
    header{text-align:center;padding:40px 0 32px;border-bottom:1px solid var(--border);margin-bottom:32px;}
    .loc-badge{display:inline-flex;align-items:center;gap:8px;background:rgba(201,168,76,.1);
      border:1px solid var(--border);border-radius:20px;padding:4px 14px;font-size:11px;
      letter-spacing:.08em;color:var(--gold);margin-bottom:18px;text-transform:uppercase;}
    h1{font-family:'Playfair Display',serif;font-size:clamp(28px,5vw,52px);font-weight:400;
      color:var(--pale);line-height:1.1;margin-bottom:8px;}
    h1 em{font-style:italic;color:var(--gold);}
    .subtitle{font-size:12px;color:var(--sub);letter-spacing:.12em;text-transform:uppercase;}
    .legend{display:flex;gap:20px;justify-content:center;margin:20px 0 0;flex-wrap:wrap;}
    .legend-item{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--sub);}
    .legend-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;}
    #status{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px 20px;
      margin-bottom:28px;font-size:12px;color:var(--sub);display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
    .status-dot{width:8px;height:8px;background:var(--good);border-radius:50%;flex-shrink:0;}
    .sl{font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--gold);
      margin-bottom:14px;display:flex;align-items:center;gap:10px;}
    .sl::after{content:'';flex:1;height:1px;background:var(--border);}

    /* Picks */
    .top-picks{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;margin-bottom:40px;}
    .pick-card{background:var(--card);border:1px solid var(--border);border-radius:10px;
      padding:20px;position:relative;overflow:hidden;}
    .pick-card.excellent{border-color:rgba(39,174,96,.35);}
    .pick-card.good{border-color:rgba(201,168,76,.35);}
    .pick-card.eagle{border-color:rgba(142,68,173,.4);}
    .pick-card.prime{border-color:rgba(167,139,250,.5);background:linear-gradient(135deg,#161d2e,#1a1535);}
    .pick-rank{position:absolute;top:12px;right:14px;font-size:11px;color:var(--sub);}
    .pick-badge{display:inline-block;font-size:10px;font-weight:600;letter-spacing:.08em;
      text-transform:uppercase;padding:3px 10px;border-radius:20px;margin-bottom:12px;}
    .badge-excellent{background:rgba(39,174,96,.15);color:#2ecc71;}
    .badge-good{background:rgba(201,168,76,.15);color:var(--gold);}
    .badge-fair{background:rgba(230,126,34,.15);color:var(--warn);}
    .badge-eagle{background:rgba(142,68,173,.2);color:#9b59b6;}
    .badge-prime{background:var(--prime-bg);color:var(--prime);}
    .pick-time{font-size:22px;font-family:'Playfair Display',serif;color:var(--pale);
      font-weight:400;text-transform:capitalize;line-height:1.2;}
    .pick-date{font-size:22px;font-family:'Playfair Display',serif;color:var(--pale);
      font-weight:400;line-height:1.2;margin-bottom:2px;}
    .pick-sun{font-size:11px;color:var(--sub);margin:4px 0 14px;letter-spacing:.03em;}
    .sb-wrap{margin-bottom:14px;}
    .sb-lbl{display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-bottom:5px;}
    .sb{height:4px;background:rgba(255,255,255,.06);border-radius:2px;}
    .sb-fill{height:100%;border-radius:2px;}
    .pm{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px;}
    .m{background:rgba(0,0,0,.2);border-radius:6px;padding:8px 10px;}
    .ml{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--sub);margin-bottom:2px;}
    .mv{font-size:13px;color:var(--text);}
    .gv{color:#2ecc71;} .wv{color:var(--warn);} .bv{color:var(--red);}
    .pick-notes{font-size:11px;color:var(--sub);font-style:italic;line-height:1.5;margin-top:8px;}
    .sol-tag{display:inline-block;background:rgba(167,139,250,.12);color:var(--prime);
      font-size:10px;padding:2px 8px;border-radius:10px;margin-top:6px;border:1px solid rgba(167,139,250,.2);}

    /* Day blocks */
    .day-block{background:var(--card);border:1px solid var(--border);border-radius:10px;
      margin-bottom:10px;overflow:hidden;}
    .day-header{display:flex;align-items:center;gap:16px;padding:14px 20px;cursor:pointer;
      transition:background .15s;user-select:none;}
    .day-header:hover{background:rgba(255,255,255,.02);}
    .day-name{font-family:'Playfair Display',serif;font-size:18px;color:var(--pale);font-weight:400;}
    .day-sub{font-size:10px;color:var(--sub);letter-spacing:.08em;margin-top:2px;}
    .day-sum{display:flex;gap:8px;flex-wrap:wrap;flex:1;justify-content:flex-end;align-items:center;}
    .pill{font-size:10px;padding:3px 9px;border-radius:10px;letter-spacing:.04em;}
    .dexp{font-size:18px;color:var(--sub);transition:transform .2s;margin-left:auto;flex-shrink:0;}
    .dexp.open{transform:rotate(180deg);}
    .day-content{display:none;border-top:1px solid var(--border);}
    .day-content.open{display:block;}
    .hr-row{display:grid;grid-template-columns:80px 90px 100px 90px 80px 1fr;
      align-items:center;gap:12px;padding:9px 20px;border-bottom:1px solid rgba(255,255,255,.03);
      font-size:12px;transition:background .1s;}
    .hr-row:last-child{border-bottom:none;}
    .hr-row:hover{background:rgba(255,255,255,.02);}
    .hr-row.hl{background:rgba(201,168,76,.06);border-left:3px solid var(--gold);}
    .hr-row.eg{background:rgba(142,68,173,.06);border-left:3px solid #9b59b6;}
    .hr-row.pr{background:rgba(167,139,250,.07);border-left:3px solid var(--prime);}
    .hr-row.exc{background:rgba(39,174,96,.06);border-left:3px solid var(--good);}
    .chip{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px;}
    .c-exc{background:rgba(39,174,96,.2);color:#2ecc71;}
    .c-gd{background:rgba(201,168,76,.2);color:var(--gold);}
    .c-fr{background:rgba(230,126,34,.2);color:var(--warn);}
    .c-po{background:rgba(255,255,255,.05);color:var(--sub);}
    .c-eg{background:rgba(142,68,173,.2);color:#9b59b6;}
    .c-pr{background:var(--prime-bg);color:var(--prime);}
    .wg{color:#2ecc71;} .ww{color:var(--warn);} .wb{color:var(--red);}
    .hr-notes{color:var(--sub);font-size:11px;font-style:italic;}
    .blind-banner{background:linear-gradient(135deg,rgba(39,174,96,.1),rgba(201,168,76,.1));
      border:1px solid rgba(39,174,96,.3);border-radius:8px;padding:12px 20px;margin:10px 20px;
      font-size:11px;color:#2ecc71;display:flex;align-items:center;gap:8px;}
    .sol-banner{background:rgba(167,139,250,.05);border:1px solid rgba(167,139,250,.2);
      border-radius:8px;padding:10px 20px;margin:8px 20px 0;font-size:11px;
      color:var(--prime);display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
    .hr-header{display:grid;grid-template-columns:80px 90px 100px 90px 80px 1fr;gap:12px;
      padding:8px 20px;font-size:9px;letter-spacing:.12em;text-transform:uppercase;
      color:var(--sub);background:rgba(0,0,0,.2);}
    .kg{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;margin-bottom:40px;}
    .kc{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;font-size:11px;}
    .kc h4{font-family:'Playfair Display',serif;font-size:14px;color:var(--gold);margin-bottom:8px;font-weight:400;}
    .kc li{color:var(--sub);margin-bottom:4px;line-height:1.5;list-style:none;padding-left:14px;position:relative;}
    .kc li::before{content:'·';position:absolute;left:4px;color:var(--gold);}

    /* Haikubox */
    .hb-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:40px;}
    @media(max-width:680px){.hb-grid{grid-template-columns:1fr;}}
    .hb-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;}
    .hb-card h3{font-family:'Playfair Display',serif;font-size:16px;color:var(--pale);
      font-weight:400;margin-bottom:14px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;}
    .hb-meta{font-family:'Source Code Pro';font-size:10px;color:var(--sub);}
    .sp-group{margin-bottom:14px;}
    .sp-group:last-child{margin-bottom:0;}
    .sp-group-label{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--sub);
      margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid rgba(255,255,255,.06);
      display:flex;align-items:center;gap:6px;}
    .sp-group-icon{font-size:11px;}
    .sp-row{display:flex;align-items:center;padding:4px 0;font-size:12px;gap:8px;}
    .sp-rank{font-size:10px;color:var(--sub);width:18px;text-align:right;flex-shrink:0;}
    .sp-name{color:var(--text);white-space:nowrap;flex-shrink:0;min-width:130px;font-size:11px;}
    .sp-bar-wrap{flex:1;height:5px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden;}
    .sp-bar{height:100%;border-radius:3px;}
    .sp-cnt{color:var(--sub);font-size:10px;white-space:nowrap;width:24px;text-align:right;flex-shrink:0;}
    .sp-ago{color:var(--gold);font-size:10px;white-space:nowrap;flex-shrink:0;min-width:42px;text-align:right;}
    .act-chart{display:flex;align-items:flex-end;gap:2px;height:80px;margin-top:12px;}
    .act-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;}
    .act-bar{width:100%;border-radius:2px 2px 0 0;min-height:2px;}
    .act-lbl{font-size:8px;color:var(--sub);}
    footer{text-align:center;font-size:10px;color:rgba(143,163,177,.4);letter-spacing:.06em;
      padding-top:20px;border-top:1px solid var(--border);}
    """

    # ── Haikubox panel ────────────────────────────────────────────────────────
    def hb_panel():
        if not species_list:
            return f"""<div class="hb-grid">
              <div class="hb-card" style="grid-column:1/-1">
                <h3>🎙 Recently on your deck</h3>
                <div style="color:var(--sub);font-size:12px;font-style:italic;text-align:center;padding:20px 0">
                  Haikubox data unavailable · check your internet connection
                </div>
              </div></div>"""

        def ago(dt_ct):
            if not dt_ct: return ''
            mins = int((ct_now.replace(tzinfo=None) - dt_ct.replace(tzinfo=None)).total_seconds() / 60)
            return f"{mins}m ago" if mins < 60 else (f"{mins//60}h ago" if mins < 1440 else f"{mins//1440}d ago")

        # ── Species categories ────────────────────────────────────────────────
        # Each entry: (display label, emoji, bar color, set of species names)
        CATEGORIES = [
            ('Raptors & Owls', '🦅', '#a78bfa', {
                'Bald Eagle','Golden Eagle','Osprey','Red-tailed Hawk','Cooper\'s Hawk',
                'Sharp-shinned Hawk','Northern Harrier','Rough-legged Hawk','Merlin',
                'American Kestrel','Peregrine Falcon','Great Horned Owl','Barred Owl',
                'Short-eared Owl','Long-eared Owl','Snowy Owl','Eastern Screech-Owl',
                'Northern Saw-whet Owl','Great Gray Owl','Barn Owl',
            }),
            ('Waterfowl', '🦆', '#60a5fa', {
                'Mallard','Canada Goose','Wood Duck','Blue-winged Teal','Green-winged Teal',
                'Northern Pintail','American Wigeon','Gadwall','Northern Shoveler',
                'Ring-necked Duck','Lesser Scaup','Greater Scaup','Bufflehead',
                'Common Goldeneye','Hooded Merganser','Common Merganser','Red-breasted Merganser',
                'Trumpeter Swan','Tundra Swan','Snow Goose','Cackling Goose',
                'Redhead','Canvasback','Ruddy Duck','Long-tailed Duck',
            }),
            ('Waders & Shorebirds', '🦩', '#34d399', {
                'Great Blue Heron','Great Egret','Snowy Egret','Little Blue Heron',
                'Green Heron','Black-crowned Night-Heron','Yellow-crowned Night-Heron',
                'Sandhill Crane','Virginia Rail','Sora','American Coot','Common Gallinule',
                'Killdeer','American Woodcock','Wilson\'s Snipe','Greater Yellowlegs',
                'Lesser Yellowlegs','Spotted Sandpiper','Solitary Sandpiper','Dunlin',
                'Least Sandpiper','Semipalmated Sandpiper','American Avocet',
            }),
            ('Gulls & Terns', '🕊️', '#94a3b8', {
                'Ring-billed Gull','Herring Gull','Great Black-backed Gull',
                'Bonaparte\'s Gull','Franklin\'s Gull','Caspian Tern','Common Tern',
                'Forster\'s Tern','Black Tern','Double-crested Cormorant',
            }),
            ('Woodpeckers', '🐦', '#fb923c', {
                'Downy Woodpecker','Hairy Woodpecker','Red-bellied Woodpecker',
                'Red-headed Woodpecker','Pileated Woodpecker','Northern Flicker',
                'Yellow-bellied Sapsucker',
            }),
            ('Songbirds & Others', '🎵', '#c9a84c', {
                # catch-all — everything not in the above groups
            }),
        ]

        # Assign each detected species to a category
        assigned = set()
        grouped  = {label: [] for label, _, _, _ in CATEGORIES}
        for sp in species_list:
            for label, icon, color, members in CATEGORIES[:-1]:  # skip catch-all
                if sp['name'] in members:
                    grouped[label].append(sp)
                    assigned.add(sp['name'])
                    break

        # Everything unassigned → Songbirds & Others
        catchall_label = CATEGORIES[-1][0]
        for sp in species_list:
            if sp['name'] not in assigned:
                grouped[catchall_label].append(sp)

        # Overall max count (for bar scaling across all groups)
        max_cnt = species_list[0]['count'] if species_list else 1

        def sp_row(sp, rank, color):
            bar_pct = round(sp['count'] / max_cnt * 100)
            return f"""<div class="sp-row">
              <div class="sp-rank">#{rank}</div>
              <div class="sp-name">{sp['name']}</div>
              <div class="sp-bar-wrap"><div class="sp-bar" style="width:{bar_pct}%;background:{color};opacity:.8"></div></div>
              <div class="sp-cnt">{sp['count']}</div>
              <div class="sp-ago">{ago(sp['last'])}</div>
            </div>"""

        # Build grouped HTML — only show groups that have detections
        groups_html = ''
        overall_rank = 1
        for label, icon, color, _ in CATEGORIES:
            members = grouped.get(label, [])
            if not members:
                continue
            rows_html = ''.join(sp_row(sp, overall_rank + i, color) for i, sp in enumerate(members))
            overall_rank += len(members)
            groups_html += f"""<div class="sp-group">
              <div class="sp-group-label"><span class="sp-group-icon">{icon}</span>{label}</div>
              {rows_html}
            </div>"""

        # ── Activity chart ────────────────────────────────────────────────────
        mx = max(hourly_counts.values()) or 1
        bars = ''
        for hr in range(24):
            cnt  = hourly_counts.get(hr, 0)
            hpx  = max(2, round(cnt / mx * 74))
            now_ = hr == ct_now.hour
            clr  = ('var(--prime)' if now_ else 'rgba(201,168,76,.35)' if cnt > 0 else 'rgba(255,255,255,.06)')
            lbl  = str(hr) if hr % 6 == 0 else ''
            bars += f"""<div class="act-col">
              <div class="act-bar" style="height:{hpx}px;background:{clr}"></div>
              <div class="act-lbl">{lbl}</div>
            </div>"""

        total   = sum(hourly_counts.values())
        peak_hr = max(hourly_counts, key=hourly_counts.get)

        # Category summary pills for the activity card
        cat_summary = ' &nbsp;·&nbsp; '.join(
            f'<span style="color:{color}">{icon} {len(grouped[label])}</span>'
            for label, icon, color, _ in CATEGORIES
            if grouped.get(label)
        )

        return f"""<div class="hb-grid">
          <div class="hb-card">
            <h3>🎙 Recently on your deck <span class="hb-meta">last 24h · {total} detections · {len(species_list)} species</span></h3>
            {groups_html}
            <div style="margin-top:12px;font-size:10px;color:var(--sub)">
              <a href="https://birds.haikubox.com/listen/{HAIKUBOX_ID}" target="_blank"
                 style="color:var(--gold);text-decoration:none">Listen live on Haikubox →</a>
            </div>
          </div>
          <div class="hb-card">
            <h3>📊 Peak activity <span class="hb-meta">peak: {fmt12(peak_hr)}</span></h3>
            <div class="act-chart">{bars}</div>
            <div style="margin-top:12px;font-size:10px;color:var(--sub);line-height:1.9">
              {cat_summary}<br>
              {total} total detections · last 24 hrs
            </div>
          </div>
        </div>"""

    haikubox_html = hb_panel()

    # ── Pick cards ────────────────────────────────────────────────────────────
    def pick_card(rank, rec):
        a   = rec['analysis']
        eff, dir_, _, sh = wind_impact(rec['wind'], rec['wind_dir'])
        eff = round(eff, 1)
        isp = a['is_prime']; ise = a['is_eagle']
        g   = 'prime' if isp else 'eagle' if ise else a['grade']
        btxt = ('🌕 Prime Session' if isp else '🦅 Eagle Watch' if ise else a['grade'].capitalize())
        sc  = a['score']
        scol = '#a78bfa' if isp else '#2ecc71' if sc>=75 else '#c9a84c' if sc>=55 else '#e67e22'
        sd   = sun.get(rec['ds'], {})
        sun_line = f'🌅 {fmt_ampm(sd.get("rise_str","06:00"))} &nbsp; 🌇 {fmt_ampm(sd.get("set_str","18:00"))}'
        day_hrs = days.get(rec['ds'], [])
        try:    idx = next(i for i,r in enumerate(day_hrs) if r['dt']==rec['dt'])
        except: idx = 0
        sess_len = sum(1 for r in day_hrs[idx:] if r['analysis']['score']>=50)
        # Session length: count from this hour forward while score >= 50
        sess_len = 0
        for r in day_hrs[idx:]:
            if r['analysis']['score'] >= 50: sess_len += 1
            else: break
        sol_pts, sol_lbl = solunar_score(float(rec['hour']), sol_by_day.get(rec['ds'], []))
        sol_tag = f'<div class="sol-tag">{sol_lbl}</div>' if sol_lbl else ''
        ec  = 'gv' if eff<7 else 'wv' if eff<12 else 'bv'
        notes_html = ' · '.join(
            f"<strong>{nn}</strong>" if '🦅' in nn or '🌕' in nn else nn
            for nn in a['notes'] if not (sol_lbl and sol_lbl in nn and sol_tag))
        return f"""
        <div class="pick-card {g}">
          <div class="pick-rank">{rank}</div>
          <div class="pick-badge badge-{g}">{btxt}</div>
          <div class="pick-date">{fmt_date(rec['ds'])}</div>
          <div class="pick-time">{fmt12(rec['hour'])} — {a['period']}</div>
          <div class="pick-sun">{sun_line}</div>
          <div class="sb-wrap">
            <div class="sb-lbl"><span>Photo Score</span><span style="color:{scol}">{sc}/100</span></div>
            <div class="sb"><div class="sb-fill" style="width:{sc}%;background:{scol}"></div></div>
          </div>
          <div class="pm">
            <div class="m"><div class="ml">Effective Wind</div><div class="mv {ec}">{eff} mph</div></div>
            <div class="m"><div class="ml">Raw Wind</div><div class="mv">{rec['wind']} mph {dir_}</div></div>
            <div class="m"><div class="ml">Cloud Cover</div><div class="mv {'gv' if rec['cloud']<40 else ''}">{rec['cloud']}%</div></div>
            <div class="m"><div class="ml">Session Length</div><div class="mv {'gv' if sess_len>=3 else ''}">{sess_len}+ hr{'s' if sess_len!=1 else ''}</div></div>
          </div>
          {sol_tag}
          <div class="pick-notes">{notes_html}</div>
        </div>"""

    picks_html = ''.join(pick_card(i+1, p) for i,p in enumerate(top_picks[:4]))

    # ── Day blocks ────────────────────────────────────────────────────────────
    def hr_row(rec):
        a = rec['analysis']
        if not a['period']: return ''
        eff, dir_, exp, sh = wind_impact(rec['wind'], rec['wind_dir'])
        eff = round(eff,1)
        isp = a['is_prime']; ise = a['is_eagle']; isg = a['is_golden']
        sol_pts = a['solunar_pts']
        rcls = 'pr' if isp else 'eg' if ise else 'exc' if a['score']>=75 else 'hl' if a['score']>=55 else ''
        ccls = 'c-pr' if isp else 'c-eg' if ise else 'c-exc' if a['score']>=75 else 'c-gd' if a['score']>=55 else 'c-fr' if a['score']>=35 else 'c-po'
        ctxt = '🌕 Prime' if isp else '🦅 Eagle' if ise else 'Excellent' if a['score']>=75 else 'Good' if a['score']>=55 else 'Fair' if a['score']>=35 else '—'
        wcls = 'wg' if eff<7 else 'ww' if eff<12 else 'wb'
        icon = ('🌅' if isg and a['period']=='morning' else '🌇' if isg and a['period']=='evening'
                else '☀️' if a['period']=='morning' else '🌤')
        notes_txt = ' · '.join(
            nn for nn in a['notes']
            if not any(x in nn for x in ('Sheltered','Exposed','easterly')))[:95]
        shield = ' ⛨' if sh else ''
        sdot   = (' <span style="color:var(--prime);font-size:10px">🌕</span>' if sol_pts>=15 else
                  ' <span style="color:rgba(167,139,250,.5);font-size:10px">🌙</span>' if sol_pts>=7 else '')
        return f"""
        <div class="hr-row {rcls}">
          <div>{icon} {fmt12(rec['hour'])}</div>
          <div><span class="chip {ccls}">{ctxt}</span>{sdot}</div>
          <div class="{wcls}">{eff} mph{shield}</div>
          <div>{wind_arrow(rec['wind_dir'])} {dir_} ({rec['wind']})</div>
          <div style="color:var(--sub)">{rec['cloud']}% ☁</div>
          <div class="hr-notes">{notes_txt}</div>
        </div>"""

    def ext_sessions(hrs):
        sessions, cur = [], None
        for r in hrs:
            if r['analysis']['score'] >= 50:
                cur = (cur or []) + [r]
            else:
                if cur and len(cur)>=2: sessions.append(cur)
                cur = None
        if cur and len(cur)>=2: sessions.append(cur)
        return sessions

    days_html = ''
    for ds, hrs in days.items():
        light = [r for r in hrs if r['analysis']['period']]
        if not light: continue
        best     = max(r['analysis']['score'] for r in light)
        avg_wind = round(sum(r['wind'] for r in light)/len(light))
        has_eagle = any(r['analysis']['is_eagle'] for r in light)
        has_prime = any(r['analysis']['is_prime'] for r in light)
        ext       = ext_sessions(light)
        sol_day   = sol_by_day.get(ds, [])

        # Pill colors
        pc = ('#a78bfa' if has_prime else '#2ecc71' if best>=75 else '#c9a84c' if best>=55 else '#e67e22' if best>=35 else '#8fa3b1')
        pb = ('var(--prime-bg)' if has_prime else 'rgba(39,174,96,.15)' if best>=75 else 'rgba(201,168,76,.15)' if best>=55 else 'rgba(255,255,255,.05)')

        sd = sun.get(ds, {})
        sun_pill   = f'<span class="pill" style="background:rgba(201,168,76,.08);color:var(--gold)">🌅 {fmt_ampm(sd.get("rise_str","06:00"))} &nbsp;🌇 {fmt_ampm(sd.get("set_str","18:00"))}</span>'
        prime_pill = f'<span class="pill" style="background:var(--prime-bg);color:var(--prime)">🌕 Prime session</span>' if has_prime else ''
        eagle_pill = f'<span class="pill" style="background:rgba(142,68,173,.15);color:#9b59b6">🦅 Eagle watch</span>' if has_eagle else ''
        blind_pill = f'<span class="pill" style="background:rgba(39,174,96,.1);color:#2ecc71">🎭 {len(ext)} extended session{"s" if len(ext)>1 else ""}</span>' if ext else ''

        # Solunar pill for day header
        sol_parts = [('🌕 ' if w['type']=='major' else '🌙 ') + fmt12f(w['hour_ct']) for w in sol_day]
        sol_pill  = (f'<span class="pill" style="background:var(--prime-bg);color:var(--prime)">'
                     f'{" · ".join(sol_parts)}</span>') if sol_parts else ''

        blind_banners = ''.join(
            f'<div class="blind-banner">🎭 <strong>Blind Setup Window:</strong> '
            f'{fmt12(s[0]["hour"])} – {fmt12(s[-1]["hour"]+1)} · '
            f'{len(s)} consecutive hrs ≥50 · Avg wind {round(sum(r["wind"] for r in s)/len(s))} mph</div>'
            for s in ext)
        sol_banner = (f'<div class="sol-banner">🌕 Solunar windows: '
                      f'{" &nbsp;·&nbsp; ".join(sol_parts)}</div>') if sol_parts else ''

        rows = ''.join(hr_row(r) for r in light)
        dow  = fmt_date(ds).split(',')[0]
        days_html += f"""
        <div class="day-block">
          <div class="day-header" onclick="toggleDay(this)">
            <div><div class="day-name">{dow}</div><div class="day-sub">{fmt_date_short(ds)}</div></div>
            <div class="day-sum">
              <span class="pill" style="background:{pb};color:{pc}">Best: {best}/100</span>
              {sun_pill}{sol_pill}
              <span class="pill" style="background:rgba(143,163,177,.1);color:var(--sub)">Avg wind: {avg_wind} mph</span>
              {prime_pill}{eagle_pill}{blind_pill}
            </div>
            <div class="dexp">▾</div>
          </div>
          <div class="day-content">
            {sol_banner}{blind_banners}
            <div class="hr-header"><span>Time</span><span>Score</span><span>Eff. Wind</span>
              <span>Direction</span><span>Clouds</span><span>Notes</span></div>
            {rows}
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Lake Koshkonong Bird Photography Planner v2</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=Source+Code+Pro:wght@300;400;500&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<div class="sky-bg"></div>
<div class="content">
  <header>
    <div class="loc-badge">📍 Lake Koshkonong · Fort Atkinson WI · West-Facing Deck · v2.0</div>
    <h1>Bird Photography<br><em>Session Planner</em></h1>
    <p class="subtitle">7-Day Forecast · Wind · Light · Solunar Tables · Eagle · Haikubox Live</p>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot" style="background:#a78bfa"></div>🌕 Prime session</div>
      <div class="legend-item"><div class="legend-dot" style="background:#2ecc71"></div>Excellent</div>
      <div class="legend-item"><div class="legend-dot" style="background:#c9a84c"></div>Good</div>
      <div class="legend-item"><div class="legend-dot" style="background:#e67e22"></div>Fair</div>
      <div class="legend-item"><div class="legend-dot" style="background:#9b59b6"></div>Eagle watch</div>
    </div>
  </header>
  <div id="status">
    <div class="status-dot"></div>
    Live data · 7-day forecast · Solunar tables computed · Haikubox live · Generated {generated}
  </div>
  <div class="sl">Best upcoming sessions</div>
  <div class="top-picks">{picks_html}</div>
  <div class="sl">Haikubox · Lake Koshkonong</div>
  {haikubox_html}
  <div class="sl" style="margin-top:16px">7-day hourly forecast</div>
  {days_html}
  <div class="sl" style="margin-top:40px">Scoring guide</div>
  <div class="kg">
    <div class="kc"><h4>What's New in v2.0</h4><ul>
      <li>🌕 Solunar tables — major/minor feeding windows from lunar position math</li>
      <li>🌕 Prime Session tier — golden hour + calm + major solunar all aligned</li>
      <li>🎙 Haikubox panel — live detections from your Lake Koshkonong device</li>
      <li>Scoring: light 40 · golden hour 30 · solunar +20 · all normalized to 100</li>
    </ul></div>
    <div class="kc"><h4>Wind Logic</h4><ul>
      <li>Deck faces west over Lake Koshkonong</li>
      <li>Easterly winds (E, NE, ENE): shielded ~70% — effective wind much lower</li>
      <li>Western winds (W, NW, SW): full exposure — raw wind applies</li>
      <li>⛨ = sheltered (easterly wind direction)</li>
    </ul></div>
    <div class="kc"><h4>Solunar Science</h4><ul>
      <li>🌕 Major periods: moon overhead or underfoot (±1 hr) — peak feeding</li>
      <li>🌙 Minor periods: moonrise / moonset (±1 hr) — moderate feeding</li>
      <li>Aligned with golden hour = 🌕 Prime Session designation</li>
      <li>Calculated entirely from lunar position math — no external API</li>
    </ul></div>
    <div class="kc"><h4>Score Breakdown</h4><ul>
      <li>🌕 Prime: golden hour + calm + major solunar — rarest, best sessions</li>
      <li>75–100 Excellent — set up blind tonight</li>
      <li>55–74 Good — worth setting up same morning</li>
      <li>35–54 Fair — casual observation only</li>
      <li>Wind 40 + Light 40 + Sun 30 + Dry 10 + Solunar 20 → scaled to /100</li>
    </ul></div>
  </div>
  <footer>
    Data: Open-Meteo · Haikubox (Lake Koshkonong) · Solunar tables computed in Python<br>
    {LAT}°N, {abs(LNG):.4f}°W · Lake Koshkonong, Fort Atkinson WI · v2.0
  </footer>
</div>
<script>
function toggleDay(h){{
  h.querySelector('.dexp').classList.toggle('open');
  h.nextElementSibling.classList.toggle('open');
}}
document.querySelector('.day-header')?.click();
</script>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    import os
    print("=" * 60)
    print("  Lake Koshkonong Bird Photography Planner  v2.0")
    print("=" * 60)
    export_mode = '--export' in sys.argv

    print("\n[1/3] Fetching weather from Open-Meteo...")
    try:
        weather = fetch_weather()
    except urllib.error.URLError as e:
        print(f"\n  ❌ Network error: {e.reason}")
        input("\nPress Enter to exit."); sys.exit(1)
    except Exception as e:
        print(f"\n  ❌ Error: {e}")
        input("\nPress Enter to exit."); sys.exit(1)
    print("  ✅ Weather data received!")

    print("\n[2/3] Fetching Haikubox detections (last 24 hrs)...")
    hb = fetch_haikubox(hours=24)
    if hb:
        n = len(hb.get('detections', []))
        print(f"  ✅ {n} detections from Lake Koshkonong")
    else:
        print("  ⚠️  Haikubox unavailable — planner will work without it")

    print("\n[3/3] Building planner + computing solunar tables...")
    html       = build_html(weather, hb)
    html_bytes = html.encode('utf-8')

    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path   = os.path.join(script_dir, 'index.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n  ✅ Saved: {out_path}")

    if export_mode:
        print("\n  Export complete! Upload index.html to GitHub Pages.")
        return

    print("\n  Opening browser...")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
        def log_message(self, *a): pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    port   = server.server_address[1]
    threading.Timer(0.3, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"  ✅ Opening: http://127.0.0.1:{port}")
    print("\n  The planner opens in your browser momentarily.")
    print("  Keep this window open. Press Ctrl+C when done.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped. 🦅")

if __name__ == '__main__':
    main()
