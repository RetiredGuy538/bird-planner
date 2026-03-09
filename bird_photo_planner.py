#!/usr/bin/env python3
"""
Lake Koshkonong Bird Photography Session Planner
================================================
Double-click this file (or run: python3 bird_photo_planner.py)
It will fetch weather data and open your planner in the browser automatically.
Requires Python 3 (already installed on Mac and most computers).
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

# Central Time offset (UTC-6 standard, UTC-5 daylight)
# We detect DST automatically
def now_central():
    """Return current datetime in US Central Time, DST-aware."""
    import time as _time
    utc_now = datetime.now(timezone.utc)
    # US DST: second Sunday in March through first Sunday in November
    year = utc_now.year
    # Second Sunday in March
    march1 = datetime(year, 3, 1)
    dst_start = march1 + timedelta(days=(6 - march1.weekday()) % 7 + 7)
    dst_start = dst_start.replace(hour=2, tzinfo=timezone.utc)
    # First Sunday in November
    nov1 = datetime(year, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    dst_end = dst_end.replace(hour=2, tzinfo=timezone.utc)
    if dst_start <= utc_now < dst_end:
        return utc_now + timedelta(hours=-5)   # CDT
    else:
        return utc_now + timedelta(hours=-6)   # CST

# ── Location ──────────────────────────────────────────────────────────────────
LAT = 42.9136
LNG = -88.8601
TIMEZONE = "America/Chicago"

# ── Wind logic ────────────────────────────────────────────────────────────────
EXPOSED_DIRS  = {'W','NW','SW','WNW','WSW','NNW'}
SHELTERED_DIRS = {'E','NE','SE','ENE','ESE','NNE'}
EAGLE_DIRS    = {'E','NE','ENE','NNE'}

COMPASS = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
           'S','SSW','SW','WSW','W','WNW','NW','NNW']

def deg_to_compass(deg):
    return COMPASS[round(deg / 22.5) % 16]

def wind_impact(speed, dir_deg):
    d = deg_to_compass(dir_deg)
    sheltered = d in SHELTERED_DIRS
    exposed   = d in EXPOSED_DIRS
    if sheltered:
        eff = speed * 0.3
    elif exposed:
        eff = speed * 1.0
    else:
        eff = speed * 0.6
    return eff, d, exposed, sheltered

def parse_hhmm(time_str):
    """Parse 'HH:MM' or ISO datetime string, return (hour, minute) as floats -> decimal hour."""
    # Open-Meteo returns e.g. "2026-03-10T06:23" or "06:23"
    if 'T' in time_str:
        time_str = time_str.split('T')[1]
    hh, mm = time_str[:5].split(':')
    return int(hh) + int(mm) / 60.0

def light_period_sun(hour_decimal, sunrise_h, sunset_h):
    """
    Return period label and golden-hour proximity score (0-1) based on real sun times.
    Golden hour = 1 hr after sunrise, 1 hr before sunset.
    Civil twilight window = 30 min before sunrise / after sunset (usable shooting light).
    """
    twilight = 0.5          # 30 min civil twilight usable
    golden   = 1.0          # 1 hr golden hour window

    morn_start = sunrise_h - twilight
    morn_golden_end = sunrise_h + golden
    morn_end   = sunrise_h + 2.5   # good light fades ~2.5 hrs after sunrise

    eve_start  = sunset_h - 2.5    # good light builds ~2.5 hrs before sunset
    eve_golden_start = sunset_h - golden
    eve_end    = sunset_h + twilight

    # Outside all usable light windows → no score
    if hour_decimal < morn_start or hour_decimal > eve_end:
        return None, 0.0, False, False

    in_morning = morn_start <= hour_decimal <= morn_end
    in_evening = eve_start  <= hour_decimal <= eve_end

    # Determine period (morning takes priority if overlap)
    if in_morning and not (hour_decimal > morn_end):
        period = 'morning'
        # Golden hour proximity: peak at sunrise, fades over next hour
        if hour_decimal <= morn_golden_end:
            golden_score = 1.0 - abs(hour_decimal - sunrise_h) / golden
        else:
            golden_score = max(0.0, 1.0 - (hour_decimal - morn_golden_end) / 1.5)
        is_golden = sunrise_h <= hour_decimal <= morn_golden_end
        is_twilight = morn_start <= hour_decimal < sunrise_h
    elif in_evening:
        period = 'evening'
        if hour_decimal >= eve_golden_start:
            golden_score = 1.0 - abs(hour_decimal - sunset_h) / golden
        else:
            golden_score = max(0.0, 1.0 - (eve_golden_start - hour_decimal) / 1.5)
        is_golden = eve_golden_start <= hour_decimal <= sunset_h
        is_twilight = sunset_h < hour_decimal <= eve_end
    else:
        return None, 0.0, False, False

    return period, max(0.0, golden_score), is_golden, is_twilight

def score_hour(hour, temp, wind, wind_dir, cloud, precip, sunrise_h, sunset_h):
    hour_decimal = float(hour)  # on-the-hour reading
    period, golden_score, is_golden, is_twilight = light_period_sun(
        hour_decimal, sunrise_h, sunset_h)

    no_result = dict(score=0, grade='poor', notes=[], period=None,
                     eff_speed=round(wind*0.6,1), direction=deg_to_compass(wind_dir),
                     is_eagle=False, is_golden=False, sunrise_h=sunrise_h, sunset_h=sunset_h)

    if period is None:
        return no_result
    if precip > 40:
        r = no_result.copy(); r.update(period=period, notes=['Rain likely']); return r

    eff, direction, exposed, sheltered = wind_impact(wind, wind_dir)
    notes = []
    score = 0

    # Wind — 40 pts
    if eff < 3:    score += 40; notes.append('Near-calm winds')
    elif eff < 7:  score += 33; notes.append('Light breeze')
    elif eff < 10: score += 20
    elif eff < 14: score += 8;  notes.append('Moderate wind')
    else:                        notes.append('Windy — deck exposed')
    if sheltered: notes.append('Sheltered (easterly wind)')
    if exposed:   notes.append('Exposed (westerly wind)')

    # Light quality — 35 pts, weighted by golden hour proximity
    base_light = 0
    if cloud < 20:
        base_light = 35; notes.append('🌅 Golden hour' if is_golden else 'Bright sun')
    elif cloud < 45:
        base_light = 26; notes.append('Partly cloudy')
    elif cloud < 75:
        base_light = 14; notes.append('Mostly cloudy')
    else:
        base_light = 4;  notes.append('Overcast')

    # Golden hour multiplier: full points at peak, tapers off
    if is_golden:
        light_pts = round(base_light * (0.7 + 0.3 * golden_score))
        if cloud >= 20 and cloud < 65 and eff < 10:
            notes.append('Dramatic golden light possible')
    elif is_twilight:
        light_pts = round(base_light * 0.5)
        notes.append('Civil twilight — soft light')
    else:
        light_pts = round(base_light * max(0.3, golden_score))

    score += light_pts

    # Sun position bonus — 15 pts (peaks right at golden hour)
    score += round(15 * golden_score)

    # Dry — 10 pts
    if precip < 10:   score += 10
    elif precip < 25: score += 5

    # Eagle: morning + easterly + calm + clear enough to spot fish
    is_eagle = (period == 'morning' and direction in EAGLE_DIRS
                and eff < 8 and cloud < 50
                and hour_decimal >= sunrise_h - 0.25)  # at/near sunrise
    if is_eagle:
        notes.append('🦅 Eagle watch — easterly wind, calm water, low sun angle')

    grade = ('excellent' if score >= 75 else
             'good'      if score >= 55 else
             'fair'      if score >= 35 else 'poor')

    return dict(score=min(100, score), grade=grade, notes=notes, period=period,
                eff_speed=round(eff, 1), direction=direction, is_eagle=is_eagle,
                is_golden=is_golden, sunrise_h=sunrise_h, sunset_h=sunset_h)

# ── Fetch weather ─────────────────────────────────────────────────────────────
def fetch_weather():
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LNG}"
        f"&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,"
        f"cloud_cover,precipitation_probability,is_day"
        f"&daily=sunrise,sunset"
        f"&wind_speed_unit=mph&temperature_unit=fahrenheit"
        f"&timezone=America%2FChicago&forecast_days=7"
    )
    print(f"  Fetching: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "BirdPhotoPlanner/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

# ── Build HTML ────────────────────────────────────────────────────────────────
def fmt12(hour):
    if hour == 0:  return "12:00 AM"
    if hour == 12: return "12:00 PM"
    return f"{hour}:00 AM" if hour < 12 else f"{hour-12}:00 PM"

def fmt_date(ds):
    d = datetime.strptime(ds, "%Y-%m-%d")
    return d.strftime("%A, %b %-d")

def fmt_date_short(ds):
    d = datetime.strptime(ds, "%Y-%m-%d")
    return d.strftime("%b %-d")

WIND_ARROWS = ['↓','↙','←','↖','↑','↗','→','↘']
def wind_arrow(deg):
    return WIND_ARROWS[round(((deg + 180) % 360) / 45) % 8]

def build_html(weather_json):
    h = weather_json['hourly']
    d = weather_json['daily']
    n = len(h['time'])

    # Build sunrise/sunset lookup by date string
    sun = {}
    for i, ds in enumerate(d['time']):
        sun[ds] = {
            'rise': parse_hhmm(d['sunrise'][i]),
            'set':  parse_hhmm(d['sunset'][i]),
            'rise_str': d['sunrise'][i].split('T')[1][:5] if 'T' in d['sunrise'][i] else d['sunrise'][i][:5],
            'set_str':  d['sunset'][i].split('T')[1][:5]  if 'T' in d['sunset'][i]  else d['sunset'][i][:5],
        }

    def fmt_ampm(hhmm_str):
        hh, mm = hhmm_str.split(':')
        hh = int(hh); mm = int(mm)
        period = 'AM' if hh < 12 else 'PM'
        hh12 = hh if hh <= 12 else hh - 12
        if hh12 == 0: hh12 = 12
        return f"{hh12}:{mm:02d} {period}"

    # Parse every hour
    all_hours = []
    for i in range(n):
        dt = h['time'][i]
        ds = dt[:10]
        hr = int(dt[11:13])
        sun_day = sun.get(ds, {'rise': 6.0, 'set': 18.0})
        rec = dict(
            dt=dt, ds=ds, hour=hr,
            temp=round(h['temperature_2m'][i]),
            wind=round(h['wind_speed_10m'][i]),
            wind_dir=h['wind_direction_10m'][i],
            cloud=h['cloud_cover'][i],
            precip=h['precipitation_probability'][i],
            sunrise_h=sun_day['rise'],
            sunset_h=sun_day['set'],
        )
        rec['analysis'] = score_hour(
            hr, rec['temp'], rec['wind'], rec['wind_dir'],
            rec['cloud'], rec['precip'],
            rec['sunrise_h'], rec['sunset_h']
        )
        all_hours.append(rec)

    # Group by day
    days = {}
    for rec in all_hours:
        days.setdefault(rec['ds'], []).append(rec)

    # Top picks — select by score, then display chronologically
    light_hours = [r for r in all_hours if r['analysis']['period']]
    candidates  = sorted(light_hours, key=lambda r: r['analysis']['score'], reverse=True)
    seen = set()
    top_picks = []
    for c in candidates:
        key = f"{c['ds']}-{c['analysis']['period']}"
        if key in seen: continue
        seen.add(key)
        top_picks.append(c)
        if len(top_picks) >= 4: break

    # Eagle picks — add best if not already present
    eagle_cands = sorted([r for r in light_hours if r['analysis']['is_eagle']],
                         key=lambda r: r['analysis']['score'], reverse=True)
    if eagle_cands:
        ec = eagle_cands[0]
        if not any(p['dt'] == ec['dt'] for p in top_picks):
            top_picks.append(ec)
            top_picks = top_picks[:4]

    # Sort chronologically (soonest first)
    top_picks.sort(key=lambda r: r['dt'])

    # Current Central Time — used to filter out sessions that have already passed
    ct_now = now_central()
    now_ds = ct_now.strftime("%Y-%m-%d")
    now_hr = ct_now.hour

    # Remove sessions that are in the past (same day but hour already gone, or earlier days)
    top_picks = [
        r for r in top_picks
        if r['ds'] > now_ds or (r['ds'] == now_ds and r['hour'] >= now_hr)
    ]

    # If we filtered some out, backfill from candidates to keep up to 4 cards
    if len(top_picks) < 4:
        existing_keys = {f"{r['ds']}-{r['analysis']['period']}" for r in top_picks}
        for c in candidates:
            if len(top_picks) >= 4: break
            key = f"{c['ds']}-{c['analysis']['period']}"
            if key in existing_keys: continue
            # Only future sessions
            if c['ds'] < now_ds or (c['ds'] == now_ds and c['hour'] < now_hr): continue
            existing_keys.add(key)
            top_picks.append(c)
        top_picks.sort(key=lambda r: r['dt'])

    generated = ct_now.strftime("%A %b %-d, %Y at %-I:%M %p CT")

    # ── CSS & HTML shell ──────────────────────────────────────────────────────
    css = """
    :root{--gold:#c9a84c;--amber:#e8b84b;--pale:#f2e8d5;--mist:#8fa3b1;
      --good:#27ae60;--warn:#e67e22;--red:#c0392b;--bg:#0f1420;
      --card:#161d2e;--border:rgba(201,168,76,0.2);--text:#e8e0d0;--sub:#8fa3b1;}
    *{margin:0;padding:0;box-sizing:border-box;}
    body{background:var(--bg);color:var(--text);font-family:'Source Code Pro',monospace;min-height:100vh;}
    .sky-bg{position:fixed;top:0;left:0;right:0;bottom:0;
      background:radial-gradient(ellipse at 20% 30%,rgba(201,168,76,.06) 0%,transparent 50%),
        radial-gradient(ellipse at 80% 70%,rgba(61,107,125,.08) 0%,transparent 50%),
        linear-gradient(180deg,#0a0e18 0%,#0f1420 40%,#111828 100%);z-index:0;pointer-events:none;}
    .content{position:relative;z-index:1;max-width:1100px;margin:0 auto;padding:24px 20px 60px;}
    header{text-align:center;padding:40px 0 32px;border-bottom:1px solid var(--border);margin-bottom:32px;}
    .loc-badge{display:inline-flex;align-items:center;gap:8px;background:rgba(201,168,76,.1);
      border:1px solid var(--border);border-radius:20px;padding:4px 14px;font-size:11px;
      letter-spacing:.08em;color:var(--gold);margin-bottom:18px;text-transform:uppercase;}
    h1{font-family:'Playfair Display',serif;font-size:clamp(28px,5vw,52px);font-weight:400;
      color:var(--pale);line-height:1.1;margin-bottom:8px;}
    h1 em{font-style:italic;color:var(--gold);}
    .subtitle{font-size:12px;color:var(--sub);letter-spacing:.12em;text-transform:uppercase;}
    .legend{display:flex;gap:24px;justify-content:center;margin:20px 0 0;flex-wrap:wrap;}
    .legend-item{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--sub);letter-spacing:.06em;}
    .legend-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;}
    #status{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px 20px;
      margin-bottom:28px;font-size:12px;color:var(--sub);display:flex;align-items:center;gap:10px;}
    .status-dot{width:8px;height:8px;background:var(--good);border-radius:50%;flex-shrink:0;}
    .section-label{font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--gold);
      margin-bottom:14px;display:flex;align-items:center;gap:10px;}
    .section-label::after{content:'';flex:1;height:1px;background:var(--border);}
    .top-picks{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:16px;margin-bottom:40px;}
    .pick-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;
      position:relative;overflow:hidden;transition:transform .2s,border-color .2s;}
    .pick-card:hover{transform:translateY(-2px);border-color:rgba(201,168,76,.4);}
    .pick-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;}
    .pick-card.excellent::before{background:linear-gradient(90deg,#27ae60,#2ecc71);}
    .pick-card.good::before{background:linear-gradient(90deg,var(--gold),var(--amber));}
    .pick-card.fair::before{background:linear-gradient(90deg,var(--warn),#f39c12);}
    .pick-card.eagle::before{background:linear-gradient(90deg,#8e44ad,#3498db);}
    .pick-rank{position:absolute;top:16px;right:16px;font-family:'Playfair Display',serif;
      font-size:36px;color:rgba(201,168,76,.15);font-weight:700;line-height:1;}
    .pick-badge{display:inline-block;font-size:9px;letter-spacing:.12em;text-transform:uppercase;
      padding:2px 8px;border-radius:3px;margin-bottom:10px;font-weight:500;}
    .badge-excellent{background:rgba(39,174,96,.2);color:#2ecc71;}
    .badge-good{background:rgba(201,168,76,.2);color:var(--gold);}
    .badge-fair{background:rgba(230,126,34,.2);color:var(--warn);}
    .badge-eagle{background:rgba(142,68,173,.2);color:#9b59b6;}
    .pick-time{font-family:'Playfair Display',serif;font-size:20px;color:var(--pale);margin-bottom:4px;}
    .pick-date{font-size:11px;color:var(--sub);margin-bottom:14px;letter-spacing:.06em;}
    .pick-metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px;}
    .metric{background:rgba(255,255,255,.03);border-radius:6px;padding:8px 10px;}
    .metric-label{font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--sub);margin-bottom:3px;}
    .metric-value{font-size:14px;color:var(--pale);font-weight:500;}
    .good-val{color:#2ecc71;} .warn-val{color:var(--warn);} .bad-val{color:var(--red);}
    .score-bar-wrap{margin-bottom:10px;}
    .score-bar-label{display:flex;justify-content:space-between;font-size:10px;color:var(--sub);
      margin-bottom:4px;letter-spacing:.06em;text-transform:uppercase;}
    .score-bar{height:4px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden;}
    .score-bar-fill{height:100%;border-radius:2px;}
    .pick-notes{font-size:11px;color:var(--sub);line-height:1.5;border-top:1px solid var(--border);
      padding-top:10px;margin-top:2px;}
    .pick-notes strong{color:var(--gold);font-weight:500;}
    .day-block{background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:12px;overflow:hidden;}
    .day-header{display:flex;align-items:center;gap:16px;padding:14px 20px;cursor:pointer;
      user-select:none;transition:background .15s;}
    .day-header:hover{background:rgba(255,255,255,.02);}
    .day-name{font-family:'Playfair Display',serif;font-size:18px;color:var(--pale);min-width:130px;}
    .day-date-sub{font-size:11px;color:var(--sub);}
    .day-summary{flex:1;display:flex;gap:12px;flex-wrap:wrap;}
    .day-pill{font-size:10px;padding:3px 10px;border-radius:10px;letter-spacing:.06em;}
    .day-expand{font-size:18px;color:var(--sub);transition:transform .2s;margin-left:auto;flex-shrink:0;}
    .day-expand.open{transform:rotate(180deg);}
    .day-content{display:none;border-top:1px solid var(--border);}
    .day-content.open{display:block;}
    .hour-row{display:grid;grid-template-columns:80px 80px 100px 90px 80px 1fr;
      align-items:center;gap:12px;padding:9px 20px;border-bottom:1px solid rgba(255,255,255,.03);
      font-size:12px;transition:background .1s;}
    .hour-row:last-child{border-bottom:none;}
    .hour-row:hover{background:rgba(255,255,255,.02);}
    .hour-row.highlighted{background:rgba(201,168,76,.06);border-left:3px solid var(--gold);}
    .hour-row.eagle-hour{background:rgba(142,68,173,.06);border-left:3px solid #9b59b6;}
    .hour-row.session-start{background:rgba(39,174,96,.06);border-left:3px solid var(--good);}
    .score-chip{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;
      border-radius:4px;letter-spacing:.04em;}
    .chip-exc{background:rgba(39,174,96,.2);color:#2ecc71;}
    .chip-good{background:rgba(201,168,76,.2);color:var(--gold);}
    .chip-fair{background:rgba(230,126,34,.2);color:var(--warn);}
    .chip-poor{background:rgba(255,255,255,.05);color:var(--sub);}
    .chip-eagle{background:rgba(142,68,173,.2);color:#9b59b6;}
    .wind-good{color:#2ecc71;} .wind-warn{color:var(--warn);} .wind-bad{color:var(--red);}
    .hr-notes{color:var(--sub);font-size:11px;font-style:italic;}
    .blind-banner{background:linear-gradient(135deg,rgba(39,174,96,.1),rgba(201,168,76,.1));
      border:1px solid rgba(39,174,96,.3);border-radius:8px;padding:12px 20px;margin:10px 20px;
      font-size:11px;color:#2ecc71;display:flex;align-items:center;gap:10px;}
    .hour-header{display:grid;grid-template-columns:80px 80px 100px 90px 80px 1fr;gap:12px;
      padding:8px 20px;font-size:9px;letter-spacing:.12em;text-transform:uppercase;
      color:var(--sub);background:rgba(0,0,0,.2);}
    .key-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;margin-bottom:40px;}
    .key-card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;font-size:11px;}
    .key-card h4{font-family:'Playfair Display',serif;font-size:14px;color:var(--gold);margin-bottom:8px;font-weight:400;}
    .key-card li{color:var(--sub);margin-bottom:4px;line-height:1.5;list-style:none;padding-left:14px;position:relative;}
    .key-card li::before{content:'·';position:absolute;left:4px;color:var(--gold);}
    footer{text-align:center;font-size:10px;color:rgba(143,163,177,.5);letter-spacing:.08em;
      padding-top:20px;border-top:1px solid var(--border);}
    """

    # ── Top picks HTML ────────────────────────────────────────────────────────
    def pick_card(rank, rec):
        a = rec['analysis']
        eff, direction, exposed, sheltered = wind_impact(rec['wind'], rec['wind_dir'])
        eff = round(eff, 1)
        grade = 'eagle' if a['is_eagle'] else a['grade']
        badge_cls = 'badge-eagle' if a['is_eagle'] else f"badge-{a['grade']}"
        badge_txt = '🦅 Eagle Watch' if a['is_eagle'] else a['grade'].capitalize()
        score_color = ('#2ecc71' if a['score'] >= 75 else
                       '#c9a84c' if a['score'] >= 55 else '#e67e22')

        # Sunrise/sunset for this card's date
        sun_day = sun.get(rec['ds'], {})
        rise_str = fmt_ampm(sun_day.get('rise_str', '06:00'))
        set_str  = fmt_ampm(sun_day.get('set_str',  '18:00'))
        sun_line = f'&nbsp;&nbsp;🌅 {rise_str} &nbsp; 🌇 {set_str}'

        # session length
        day_hrs = days.get(rec['ds'], [])
        try:
            idx = next(i for i, r in enumerate(day_hrs) if r['dt'] == rec['dt'])
        except StopIteration:
            idx = 0
        sess_len = 0
        for r in day_hrs[idx:]:
            if r['analysis']['score'] >= 50: sess_len += 1
            else: break

        eff_cls = ('good-val' if eff < 7 else 'warn-val' if eff < 12 else 'bad-val')
        notes_html = ' · '.join(
            f"<strong>{n}</strong>" if '🦅' in n else n
            for n in a['notes']
        )
        return f"""
        <div class="pick-card {grade}">
          <div class="pick-rank">{rank}</div>
          <div class="pick-badge {badge_cls}">{badge_txt}</div>
          <div class="pick-time">{fmt12(rec['hour'])} — {a['period']}</div>
          <div class="pick-date">{fmt_date(rec['ds'])}{sun_line}</div>
          <div class="score-bar-wrap">
            <div class="score-bar-label"><span>Photo Score</span>
              <span style="color:{score_color}">{a['score']}/100</span></div>
            <div class="score-bar">
              <div class="score-bar-fill" style="width:{a['score']}%;background:{score_color}"></div>
            </div>
          </div>
          <div class="pick-metrics">
            <div class="metric"><div class="metric-label">Effective Wind</div>
              <div class="metric-value {eff_cls}">{eff} mph</div></div>
            <div class="metric"><div class="metric-label">Raw Wind</div>
              <div class="metric-value">{rec['wind']} mph {direction}</div></div>
            <div class="metric"><div class="metric-label">Cloud Cover</div>
              <div class="metric-value {'good-val' if rec['cloud']<40 else ''}">{rec['cloud']}%</div></div>
            <div class="metric"><div class="metric-label">Session Length</div>
              <div class="metric-value {'good-val' if sess_len>=3 else ''}">{sess_len}+ hr{'s' if sess_len!=1 else ''}</div></div>
          </div>
          <div class="pick-notes">{notes_html}</div>
        </div>"""

    picks_html = ''.join(pick_card(i+1, p) for i, p in enumerate(top_picks[:4]))

    # ── Day blocks ────────────────────────────────────────────────────────────
    def hour_row(rec):
        a = rec['analysis']
        if not a['period']: return ''
        eff, direction, exposed, sheltered = wind_impact(rec['wind'], rec['wind_dir'])
        eff = round(eff, 1)
        is_golden = a.get('is_golden', False)
        row_cls = ('eagle-hour' if a['is_eagle'] else
                   'session-start' if a['score'] >= 75 else
                   'highlighted' if a['score'] >= 55 else '')
        chip_cls = ('chip-eagle' if a['is_eagle'] else
                    'chip-exc'  if a['score'] >= 75 else
                    'chip-good' if a['score'] >= 55 else
                    'chip-fair' if a['score'] >= 35 else 'chip-poor')
        chip_txt = ('🦅 Eagle' if a['is_eagle'] else
                    'Excellent' if a['score'] >= 75 else
                    'Good'      if a['score'] >= 55 else
                    'Fair'      if a['score'] >= 35 else '—')
        wind_cls = ('wind-good' if eff < 7 else 'wind-warn' if eff < 12 else 'wind-bad')
        # Period icon: golden hour gets special treatment
        if is_golden and a['period'] == 'morning':
            period_icon = '🌅'
        elif is_golden and a['period'] == 'evening':
            period_icon = '🌇'
        elif a['period'] == 'morning':
            period_icon = '☀️'
        else:
            period_icon = '🌤'
        note_txt = ' · '.join(
            n for n in a['notes']
            if not any(x in n for x in ('Sheltered','Exposed','easterly'))
        )[:90]
        shield = ' ⛨' if sheltered else ''
        return f"""
        <div class="hour-row {row_cls}">
          <div>{period_icon} {fmt12(rec['hour'])}</div>
          <div><span class="score-chip {chip_cls}">{chip_txt}</span></div>
          <div class="{wind_cls}">{eff} mph{shield}</div>
          <div>{wind_arrow(rec['wind_dir'])} {direction} ({rec['wind']})</div>
          <div style="color:var(--sub)">{rec['cloud']}% ☁</div>
          <div class="hr-notes">{note_txt}</div>
        </div>"""

    def extended_sessions(hrs):
        sessions, cur = [], None
        for r in hrs:
            if r['analysis']['score'] >= 50:
                if not cur: cur = [r]
                else: cur.append(r)
            else:
                if cur and len(cur) >= 2: sessions.append(cur)
                cur = None
        if cur and len(cur) >= 2: sessions.append(cur)
        return sessions

    days_html = ''
    for ds, hrs in days.items():
        light = [r for r in hrs if r['analysis']['period']]
        if not light: continue
        best = max(r['analysis']['score'] for r in light)
        avg_wind = round(sum(r['wind'] for r in light) / len(light))
        has_eagle = any(r['analysis']['is_eagle'] for r in light)
        ext = extended_sessions(light)

        pill_color = ('#2ecc71' if best >= 75 else '#c9a84c' if best >= 55 else
                      '#e67e22' if best >= 35 else '#8fa3b1')
        pill_bg    = ('rgba(39,174,96,.15)' if best >= 75 else
                      'rgba(201,168,76,.15)' if best >= 55 else 'rgba(255,255,255,.05)')

        eagle_pill = (f'<span class="day-pill" style="background:rgba(142,68,173,.15);color:#9b59b6">🦅 Eagle watch</span>'
                      if has_eagle else '')
        blind_pill = (f'<span class="day-pill" style="background:rgba(39,174,96,.1);color:#2ecc71">🎭 {len(ext)} extended session{"s" if len(ext)>1 else ""}</span>'
                      if ext else '')

        # Sunrise/sunset for this day
        sun_day = sun.get(ds, {})
        rise_str = fmt_ampm(sun_day.get('rise_str','06:00'))
        set_str  = fmt_ampm(sun_day.get('set_str','18:00'))
        sun_pill = f'<span class="day-pill" style="background:rgba(201,168,76,.08);color:var(--gold)">🌅 {rise_str} &nbsp;🌇 {set_str}</span>'

        blind_banners = ''.join(
            f'<div class="blind-banner">🎭 <strong>Blind Setup Window:</strong> '
            f'{fmt12(s[0]["hour"])} – {fmt12(s[-1]["hour"]+1)} · '
            f'{len(s)} consecutive hrs score ≥50 · '
            f'Avg wind {round(sum(r["wind"] for r in s)/len(s))} mph</div>'
            for s in ext
        )

        rows = ''.join(hour_row(r) for r in light)
        dow = fmt_date(ds).split(',')[0]

        days_html += f"""
        <div class="day-block">
          <div class="day-header" onclick="toggleDay(this)">
            <div>
              <div class="day-name">{dow}</div>
              <div class="day-date-sub">{fmt_date_short(ds)}</div>
            </div>
            <div class="day-summary">
              <span class="day-pill" style="background:{pill_bg};color:{pill_color}">Best: {best}/100</span>
              {sun_pill}
              <span class="day-pill" style="background:rgba(143,163,177,.1);color:var(--sub)">Avg wind: {avg_wind} mph</span>
              {eagle_pill}{blind_pill}
            </div>
            <div class="day-expand">▾</div>
          </div>
          <div class="day-content">
            {blind_banners}
            <div class="hour-header">
              <span>Time</span><span>Score</span><span>Eff. Wind</span>
              <span>Direction</span><span>Clouds</span><span>Notes</span>
            </div>
            {rows}
          </div>
        </div>"""

    # ── Full page ─────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lake Koshkonong Bird Photography Planner</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=Source+Code+Pro:wght@300;400;500&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<div class="sky-bg"></div>
<div class="content">
  <header>
    <div class="loc-badge">📍 Lake Koshkonong · Fort Atkinson, WI · West-Facing Deck</div>
    <h1>Bird Photography<br><em>Session Planner</em></h1>
    <p class="subtitle">7-Day Forecast · Wind · Light · Eagle Conditions</p>
    <div class="legend">
      <div class="legend-item"><div class="legend-dot" style="background:#2ecc71"></div>Excellent session</div>
      <div class="legend-item"><div class="legend-dot" style="background:#c9a84c"></div>Good session</div>
      <div class="legend-item"><div class="legend-dot" style="background:#e67e22"></div>Fair session</div>
      <div class="legend-item"><div class="legend-dot" style="background:#9b59b6"></div>Eagle watch potential</div>
    </div>
  </header>
  <div id="status">
    <div class="status-dot"></div>
    Live data loaded · 7-day forecast for Lake Koshkonong · Generated {generated}
  </div>
  <div class="section-label">Best upcoming sessions</div>
  <div class="top-picks">{picks_html}</div>
  <div class="section-label" style="margin-top:16px">7-day hourly forecast</div>
  {days_html}
  <div class="section-label" style="margin-top:40px">Scoring guide</div>
  <div class="key-grid">
    <div class="key-card"><h4>Wind Logic</h4><ul>
      <li>Deck faces west over Lake Koshkonong</li>
      <li>Easterly winds (E, NE, ENE): deck shielded ~70% — effective wind much lower</li>
      <li>Western winds (W, NW, SW): full exposure — raw wind applies</li>
      <li>⛨ = sheltered indicator (easterly origin)</li>
    </ul></div>
    <div class="key-card"><h4>Blind Setup Advice</h4><ul>
      <li>Green banners = 2+ consecutive hours scoring ≥50</li>
      <li>Plan blind setup the evening before multi-hour morning windows</li>
      <li>Below 10 mph effective wind = optimal for stationary blind</li>
      <li>Sheltered easterly mornings = best for keeping blind up</li>
    </ul></div>
    <div class="key-card"><h4>Eagle Watch Conditions</h4><ul>
      <li>Easterly wind (calm, sheltered water surface)</li>
      <li>Morning hours — bald eagles hunt with sun behind them</li>
      <li>Clear to partly cloudy — bright water reveals fish</li>
      <li>Look toward sunrise-lit shoreline trees</li>
    </ul></div>
    <div class="key-card"><h4>Score Breakdown</h4><ul>
      <li>75–100: Excellent — set up blind tonight</li>
      <li>55–74: Good — worth setting up same morning</li>
      <li>35–54: Fair — casual observation, skip the blind</li>
      <li>0–34: Poor — wind or light unfavorable</li>
    </ul></div>
  </div>
  <footer>
    Data: Open-Meteo · Location: {LAT}°N, {abs(LNG):.4f}°W · Lake Koshkonong, Fort Atkinson WI<br>
    Re-run the script any time to refresh the forecast
  </footer>
</div>
<script>
function toggleDay(header) {{
  header.querySelector('.day-expand').classList.toggle('open');
  header.nextElementSibling.classList.toggle('open');
}}
// Auto-open today
document.querySelector('.day-header')?.click();
</script>
</body>
</html>"""

# ── Serve & open ──────────────────────────────────────────────────────────────
def main():
    import os
    print("=" * 60)
    print("  Lake Koshkonong Bird Photography Planner")
    print("=" * 60)

    # Check for --export flag (used to save index.html for GitHub Pages)
    export_mode = '--export' in sys.argv

    print("\n[1/2] Fetching weather data from Open-Meteo...")
    try:
        weather = fetch_weather()
    except urllib.error.URLError as e:
        print(f"\n  ❌ Network error: {e.reason}")
        print("  Make sure you are connected to the internet and try again.")
        input("\nPress Enter to exit.")
        sys.exit(1)
    except Exception as e:
        print(f"\n  ❌ Unexpected error: {e}")
        input("\nPress Enter to exit.")
        sys.exit(1)

    print("  ✅ Weather data received!")

    html = build_html(weather)
    html_bytes = html.encode('utf-8')

    # Always save index.html alongside the script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(script_dir, 'index.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n  ✅ Saved: {out_path}")

    if export_mode:
        print("\n  Export complete! Upload index.html to GitHub Pages.")
        print("  (See setup instructions for details.)")
        return

    print("\n[2/2] Opening browser...")

    # Tiny local HTTP server
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)

        def log_message(self, fmt, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    url  = f"http://127.0.0.1:{port}"

    threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    print(f"  ✅ Opening: {url}")
    print("\n  The planner will open in your browser momentarily.")
    print("  This window must stay open while you use the page.")
    print("  Press Ctrl+C (or close this window) when done.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped. Goodbye! 🦅")

if __name__ == '__main__':
    main()
