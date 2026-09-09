# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Vitals: private, local-first views of Garmin readings and experimental scores."""
import argparse
import calendar
import json
from datetime import date, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import DATA as BASE, DEMO
from recovery import readiness, MODEL_VERSION
from scores import series as score_series, strain_band
import healthspan as hs

STATIC = {
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icon.png": ("icon.png", "image/png"),
    "/manifest.json": ("manifest.json", "application/manifest+json"),
}


ICONS = {
    "today": '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    "history": '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18m-13 4h2m4 0h2"/>',
    "health": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "setup": '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
}
BANDS = {"GREEN": "Green", "AMBER": "Amber", "RED": "Red"}
_RDY = {}


def esc(value):
    return escape(str(value), quote=True) if value is not None else ""


def hm(hours):
    if hours is None:
        return "—"
    minutes = round(hours * 60)
    sign = "−" if minutes < 0 else ""
    minutes = abs(minutes)
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"


def number(value, fmt=".0f", unit=""):
    return "—" if value is None else f"{value:{fmt}}{unit}".replace("-", "−")


def recovery_score(g):
    z = g.get("z")
    # Anonymous paired-device fit; display calibration, not a percentile.
    return None if z is None else max(3.0, min(98.0, 60.0 + 17.2 * z))


def rdy_fresh():
    stamp = tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                         for p in BASE.glob("*.json")))
    if _RDY.get("stamp") != stamp:
        _RDY.clear()
        _RDY["stamp"] = stamp


def rdy(day):
    if day not in _RDY:
        _RDY[day] = readiness(day)
    return _RDY[day]


def daily_data():
    rdy_fresh()
    # Full history is required for sleep debt and recent-strain context.
    scores = score_series()
    path = BASE / "garmin_hrv_resp.json"
    raw = json.loads(path.read_text()) if path.exists() else {}
    days = sorted(d for d in set(scores) | set(raw)
                  if valid_date(d) and d <= date.today().isoformat()
                  and (raw.get(d) or any(scores.get(d, {}).get(k) is not None
                                        for k in ("sleep_score", "strain"))))
    return scores, raw, days[-1] if days else None


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except (ValueError, TypeError):
        return False


def want_day(path):
    value = parse_qs(urlparse(path).query).get("d", [None])[0]
    return value if valid_date(value) else None


def icon(name):
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true">{ICONS[name]}</svg>')


def nav(active):
    links = []
    for href, key, title in (("/", "today", "Today"), ("/history", "history", "History"),
                             ("/health", "health", "Health"), ("/setup", "setup", "Setup")):
        current = ' aria-current="page"' if key == active else ""
        links.append(f'<a href="{href}"{current}>{icon(key)}<span>{title}</span></a>')
    return '<nav class="navigation" aria-label="Main navigation">' + "".join(links) + '</nav>'


def page(title, body, active):
    status = "Demo / synthetic data" if DEMO else "Local / private data"
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="theme-color" content="#f6f7f5"><meta name="color-scheme" content="light">'
            f'<title>{esc(title)} · Vitals</title><link rel="stylesheet" href="/static/style.css">'
            '<link rel="icon" href="/icon.png"><link rel="apple-touch-icon" href="/icon.png">'
            '<link rel="manifest" href="/manifest.json">'
            '<meta name="apple-mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-title" content="Vitals">'
            '</head><body><a class="skip-link" href="#main">Skip to content</a>'
            '<aside class="sidebar"><a class="brand" href="/" aria-label="Vitals home">'
            f'<span class="brand-mark">{icon("health")}</span>Vitals<span class="brand-dot">.</span></a>'
            f'<div class="environment {"demo" if DEMO else ""}"><span class="status-dot" aria-hidden="true"></span>{status}</div>'
            f'{nav(active)}<div class="sidebar-note"><b>Your data. Your device.</b>'
            '<p>No account here.<br>No cloud dashboard.<br>Just your own context.</p></div></aside>'
            f'<main id="main" class="main">{body}<footer>Vitals · Made for perspective, not prescriptions.'
            '<span>Wearable estimates are not medical advice.</span></footer></main></body></html>')


def heading(kicker, title, description="", actions=""):
    return (f'<header class="page-header"><div><p class="eyebrow">{esc(kicker)}</p>'
            f'<h1>{esc(title)}</h1>'
            + (f'<p class="subtitle">{esc(description)}</p>' if description else "")
            + f'</div>{actions}</header>')


def badge(g):
    band = g.get("band")
    tone = band.lower() if band in BANDS else "neutral"
    label = BANDS.get(band, "No estimate")
    return f'<span class="badge {tone}"><span aria-hidden="true">●</span> {label}</span>'


def rows(items):
    return '<dl class="readings">' + "".join(
        f'<div><dt>{esc(label)}</dt><dd>{esc(value if value is not None else "—")}</dd></div>'
        for label, value in items) + '</dl>'


def disclosure(title, content):
    return f'<details class="disclosure"><summary>{esc(title)}</summary><div class="details-body">{content}</div></details>'


def date_controls(day, latest):
    selected, today = date.fromisoformat(day), date.today()
    previous = selected - timedelta(days=1) if selected > date.min else None
    following = selected + timedelta(days=1) if selected < today else None
    def arrow(target, label, glyph):
        return (f'<a class="button icon-button" href="/?d={target.isoformat()}" aria-label="{label}">{glyph}</a>'
                if target else f'<span class="button icon-button disabled" aria-disabled="true" aria-label="{label}">{glyph}</span>')
    return ('<div class="date-toolbar"><div class="day-arrows">'
            + arrow(previous, "Previous day", "←") + arrow(following, "Next day", "→")
            + '</div><form action="/" method="get" class="date-form">'
            f'<label class="sr-only" for="day">Choose a date</label><input id="day" name="d" type="date" '
            f'value="{day}" max="{today.isoformat()}" required><button class="button" type="submit">Go</button></form>'
            + ('<a class="text-link" href="/">Back to latest →</a>' if latest and day != latest else
               '<a class="text-link" href="/history">Browse history →</a>') + '</div>')


def score_card(kind, label, value, unit, description, extra="", override=False):
    return (f'<article class="score-card {kind}{" overridden" if override else ""}">'
            f'<div class="score-top"><h2>{esc(label)}</h2><span class="score-symbol" aria-hidden="true">{icon("health")}</span></div>'
            f'<div class="score-value">{esc(value)}<span>{esc(unit)}</span></div>'
            f'<p>{esc(description)}</p>{extra}</article>')


def need_bar(asleep, need):
    if asleep is None or not need:
        return '<p class="muted">Sleep duration or estimated need is not available.</p>'
    difference = need - asleep
    label = f"{hm(difference)} h below estimated need" if difference > 0 else "Estimated need met"
    fraction = min(100, 100 * asleep / need)
    return (f'<div class="sleep-comparison"><div><strong>{hm(asleep)} <span>h asleep</span></strong>'
            f'<span>{hm(need)} h estimated need</span></div><div class="meter" aria-hidden="true">'
            f'<span style="width:{fraction:.1f}%"></span></div><p>{label}</p></div>')


def trend_panel(day, scores, raw):
    end = date.fromisoformat(day)
    dates = [(end - timedelta(days=k)).isoformat() for k in range(13, -1, -1)
             if end.toordinal() > k]
    recovery = {d: rdy(d) if d in raw or d in scores else {} for d in dates}
    series = [
        ("Recovery", "HRV-derived · 0–100", "recovery", 100,
         [recovery_score(recovery[d]) for d in dates]),
        ("Sleep", "Estimated score · 0–100", "sleep", 100,
         [scores.get(d, {}).get("sleep_score") for d in dates]),
        ("Strain", "Closed cycles · 0–21", "strain", 21,
         [None if scores.get(d, {}).get("strain_partial") else scores.get(d, {}).get("strain") for d in dates]),
    ]
    charts = []
    for label, scale, kind, maximum, values in series:
        pieces = []
        run = []
        for index, value in enumerate(values):
            if value is None:
                if run:
                    pieces.append('<polyline points="' + " ".join(run) + '"/>')
                    run = []
                continue
            x = 35 + index * 290 / max(1, len(dates) - 1)
            y = 130 - 110 * max(0, min(maximum, value)) / maximum
            run.append(f"{x:.1f},{y:.1f}")
            pieces.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5"/>')
        if run:
            pieces.append('<polyline points="' + " ".join(run) + '"/>')
        grid = "".join(f'<line x1="35" x2="325" y1="{y}" y2="{y}"/><text x="25" y="{y + 5}" text-anchor="end">{number(v)}</text>'
                       for y, v in ((20, maximum), (75, maximum / 2), (130, 0)))
        available = [v for v in values if v is not None]
        average = number(sum(available) / len(available), ".1f" if kind == "strain" else ".0f") if available else "—"
        charts.append(f'<div class="trend-chart {kind}"><div class="trend-title"><h3>{label}</h3>'
                      f'<span>{average} <small>avg</small></span></div><p>{scale}</p>'
                      f'<svg viewBox="0 0 350 155" role="img" aria-label="{label} over 14 days. Values and date links below.">'
                      f'<g class="chart-grid">{grid}</g><g class="chart-line">{"".join(pieces)}</g></svg>'
                      f'<div class="chart-dates"><span>{date.fromisoformat(dates[0]):%d %b}</span><span>{end:%d %b}</span></div></div>')
    links = "".join(f'<a href="/?d={d}" aria-label="View {date.fromisoformat(d):%d %B %Y}"'
                    + (' aria-current="date"' if d == day else '')
                    + f'>{date.fromisoformat(d):%d}<span>{date.fromisoformat(d):%b}</span></a>'
                    for d in dates)
    return ('<section class="panel trend-panel"><div class="section-heading"><div><p class="eyebrow">The bigger picture</p>'
            '<h2>Your last 14 days</h2></div><a class="text-link" href="/history">Full history →</a></div>'
            '<div class="trend-grid">' + "".join(charts) + '</div>'
            '<p class="muted chart-note">Fixed scales. Gaps mean missing readings; open strain cycles are not plotted. Recovery line shows the HRV estimate, not overrides.</p>'
            + '<div class="trend-days" aria-label="Browse these days">' + links + '</div>'
            + disclosure("Daily values & date links", daily_table(dates, scores, raw)) + '</section>')


def daily_table(dates, scores, raw):
    body = []
    for day in reversed(dates):
        s = scores.get(day, {})
        g = rdy(day) if day in raw or day in scores else {}
        band = BANDS.get(g.get("band"), "—")
        partial = "*" if s.get("strain_partial") else ""
        body.append(f'<tr><th scope="row"><a href="/?d={day}">{date.fromisoformat(day):%d %b}</a></th>'
                    f'<td>{band}</td><td>{number(s.get("sleep_score"))}</td>'
                    f'<td>{number(s.get("strain"), ".1f")}{partial}</td></tr>')
    return ('<div class="table-wrap"><table><caption>Daily recovery band, sleep score /100 and strain /21. * Partial strain.</caption>'
            '<thead><tr><th scope="col">Date</th><th scope="col">Recovery</th><th scope="col">Sleep</th><th scope="col">Strain</th></tr></thead>'
            '<tbody>' + "".join(body) + '</tbody></table></div>')


def screen_today(want=None):
    scores, raw, latest = daily_data()
    if not latest and not want:
        return page("Welcome", heading("Your personal health workspace", "A clearer view of your everyday.",
                    "Recovery, sleep and movement. Together, on your own device.") + setup_content(), "today")
    day = want if want and want <= date.today().isoformat() else latest or date.today().isoformat()
    selected = date.fromisoformat(day)
    s, reading = scores.get(day, {}), raw.get(day, {}) or {}
    g = rdy(day) if day in scores or day in raw else {}
    browsing = latest and day != latest
    title = "Your day, in perspective."
    body = heading("Daily overview" if not browsing else "From your history", title,
                   f"{selected:%A, %d %B %Y} · Readings, not a verdict.")
    body += date_controls(day, latest)
    if day == latest and (date.today() - selected).days > 1:
        body += (f'<div class="notice">Showing your latest local reading, from {selected:%d %B %Y}. '
                 '<a href="/setup">Sync to bring it up to date →</a></div>')
    if not reading and all(s.get(key) is None for key in ("sleep_score", "strain", "asleep_h")):
        body += ('<section class="panel empty-state"><h2>No readings for this day</h2>'
                 '<p>This date is a gap in your local data, not a recovery assessment.</p>'
                 '<a class="button primary" href="/history">Find a recorded day</a></section>')
        return page("Today", body, "today")
    z = g.get("z")
    override = bool(g.get("veto") or g.get("vetoes") or g.get("override")
                    or (z is None and g.get("band") == "RED"))
    # Readiness may be supplied by an explicitly configured external model.
    if z is not None and g.get("band") in BANDS:
        expected = "GREEN" if z >= 0 else "RED" if z < -1 else "AMBER"
        override = override or g["band"] != expected
    rec_extra = '<div class="recovery-status">' + badge(g)
    rec_extra += ('<span>Override: ' + esc(g.get("reason") or "band differs from the HRV-only estimate") + '</span>'
                  if override else '<span>Relative to your recent baseline.</span>')
    rec_extra += '</div>'
    if z is None:
        context = (g.get("reason") or "Override applies; no HRV-only score is available." if override else
                   "More baseline history may be needed. Raw readings remain below.")
        rec_extra = '<div class="recovery-status">' + badge(g) + f'<span>{esc(context)}</span></div>'
    strain = s.get("strain")
    body += '<section class="score-grid" aria-label="Daily scores">'
    body += score_card("recovery", "Recovery", number(recovery_score(g)), "/ 100",
                       "HRV-derived estimate, not a clinical readiness percentage.", rec_extra, override)
    body += score_card("sleep", "Sleep", number(s.get("sleep_score")), "/ 100",
                       "Estimated from duration, efficiency and consistency.",
                       f'<div class="score-foot">{hm(s.get("asleep_h"))} h asleep <span>of {hm(s.get("need_h"))} h need</span></div>')
    body += score_card("strain", "Strain so far" if s.get("strain_partial") else "Day strain",
                       number(strain, ".1f"), "/ 21", "Activity-load estimate for this sleep-to-sleep cycle.",
                       f'<div class="score-foot">{esc((strain_band(strain) or "No estimate").capitalize())}<span>'
                       + ('Partial · still accumulating' if s.get("strain_partial") else 'No complete cycle available' if strain is None else 'Closed cycle') + '</span></div>')
    body += '</section><section class="context-grid" aria-label="Underlying readings">'
    hrv = g.get("hrv") if g.get("hrv") is not None else reading.get("hrv_last_night")
    body += ('<article class="panel"><div class="section-heading"><h2>Behind your recovery</h2><span class="tag">Measured</span></div>'
             f'<div class="reading-hero">{number(hrv)} <span>ms HRV last night</span></div>'
             + rows([("30-day HRV baseline", number(g.get("baseline_hrv"), ".0f", " ms")),
                     ("Baseline nights", g.get("baseline_n")),
                     ("Resting heart rate", number(reading.get("rhr"), ".0f", " bpm")),
                     ("Skin temperature deviation", number(g.get("skin_temp_dev_c", reading.get("skin_temp_dev_c")), "+.1f", " °C"))])
             + '</article><article class="panel"><div class="section-heading"><h2>Sleep, in context</h2><span class="tag">Measured + modeled</span></div>'
             + need_bar(s.get("asleep_h"), s.get("need_h"))
             + rows([("Recorded sleep efficiency", number(s.get("efficiency_garmin"), ".0f", "%")),
                     ("Estimated consistency", number(s.get("consistency"), ".0f", "%")),
                     ("Garmin sleep score", number(s.get("garmin_sleep_score"), ".0f", " / 100"))]) + '</article></section>')
    body += trend_panel(day, scores, raw)
    body += disclosure("How these daily estimates work", '<p>Recovery maps your HRV deviation from its baseline onto a 0–100 display scale '
                       '(60 + 17.2 × z, limited to 3–98). It is not a percentile. A readiness override can change the band without changing that HRV-only number.</p>'
                       '<p>Sleep combines duration against estimated need, calibrated efficiency and clock-time consistency. Strain uses activity training load '
                       'and active calories across a sleep-to-sleep cycle. These are experimental estimates fitted on one person’s paired-device data, '
                       'not official Garmin or WHOOP scores and not generalized clinical models.</p>'
                       + rows([("Recovery model", MODEL_VERSION), ("HRV deviation", number(z, "+.2f", " SD")),
                               ("Baseline sleep need", hm(s.get("baseline_h")) + " h"),
                               ("Sleep debt component", hm(s.get("debt_h")) + " h"),
                               ("Recent strain component", hm(s.get("strain_h")) + " h"),
                               ("Nap adjustment", hm(s.get("nap_h")) + " h"),
                               ("Training load", number(s.get("training_load"), ".1f")),
                               ("Activities in cycle", s.get("n_activities"))]))
    return page("Today", body, "today")


def screen_history(want=None):
    scores, raw, latest = daily_data()
    today = date.today()
    selected = date.fromisoformat(want) if want and want <= today.isoformat() else date.fromisoformat(latest) if latest else today
    first = selected.replace(day=1)
    previous = first - timedelta(days=1) if first > date.min else None
    count = calendar.monthrange(first.year, first.month)[1]
    following = first + timedelta(days=count)
    arrows = (f'<a class="button icon-button" href="/history?d={previous.isoformat()}" aria-label="Previous month">←</a>'
              if previous else '<span class="button icon-button disabled">←</span>')
    arrows += f'<h2>{first:%B %Y}</h2>'
    arrows += (f'<a class="button icon-button" href="/history?d={following.isoformat()}" aria-label="Next month">→</a>'
               if following <= today else '<span class="button icon-button disabled" aria-disabled="true" aria-label="Next month">→</span>')
    body = heading("Your personal archive", "Every day tells a little more.",
                   "Find patterns, revisit a day, and see the gaps as clearly as the readings.")
    body += ('<section class="panel calendar-panel"><div class="calendar-toolbar"><div class="month-nav">' + arrows + '</div>'
             '<form action="/history" method="get" class="date-form"><label class="sr-only" for="history-day">Jump to a date</label>'
             f'<input id="history-day" type="date" name="d" value="{selected.isoformat()}" max="{today.isoformat()}" required>'
             '<button class="button" type="submit">Go</button></form></div><div class="calendar" aria-label="Month calendar, Monday first">')
    body += "".join(f'<div class="weekday">{d}</div>' for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
    body += '<div class="calendar-pad" aria-hidden="true"></div>' * first.weekday()
    dates = []
    for n in range(1, count + 1):
        current = first.replace(day=n)
        ds = current.isoformat()
        if current > today:
            body += f'<div class="calendar-day future" aria-label="{ds}, future date"><span class="calendar-number">{n}</span></div>'
            continue
        dates.append(ds)
        s = scores.get(ds, {})
        present = bool(raw.get(ds) or any(s.get(key) is not None for key in ("sleep_score", "strain", "asleep_h")))
        g = rdy(ds) if present else {}
        band = BANDS.get(g.get("band"), "No estimate" if present else "No reading")
        tone = g.get("band", "").lower() if g.get("band") in BANDS else "neutral"
        label = f'{ds}, {band}, sleep {number(s.get("sleep_score"))}, strain {number(s.get("strain"), ".1f")}'
        cls = "calendar-day" + (" selected" if current == selected else "") + (" gap" if not present else "")
        body += (f'<a class="{cls}" href="/?d={ds}" aria-label="{esc(label)}"><span class="calendar-number">{n}'
                 + ('<span class="today-dot" aria-label="Today"></span>' if current == today else '') + '</span>'
                 f'<span class="calendar-band {tone}"><span class="desktop-band">{band}</span>'
                 f'<span class="mobile-band" aria-hidden="true">{band[0] if band in BANDS.values() else "—"}</span></span>'
                 f'<span class="calendar-values">Sleep <b>{number(s.get("sleep_score"))}</b><br>Strain <b>{number(s.get("strain"), ".1f")}'
                 f'{"*" if s.get("strain_partial") else ""}</b></span></a>')
    body += ('</div><p class="calendar-key">G Green · A Amber · R Red · — No estimate or no reading. '
             'Bands include overrides. * Partial strain.</p></section>')
    body += '<section class="panel history-values"><div class="section-heading"><h2>Daily readings</h2><a class="text-link" href="/">Latest day →</a></div>'
    body += daily_table(dates, scores, raw) + '</section>'
    if not latest:
        body += '<div class="notice">No local readings yet. <a href="/setup">Connect your data or try the demo →</a></div>'
    return page("History", body, "history")


def metric_value(metric, value):
    if value is None:
        return "—"
    return (hm(value) if metric.get("hm") else metric["fmt"].format(value)) + metric["unit"]


def screen_health():
    _, _, latest = daily_data()
    long = hs.report(180, as_of=latest)
    recent = hs.report(30, as_of=latest)
    short_metrics = {m["key"]: m for m in recent["metrics"]}
    available = long.get("available_metrics", sum(m["value"] is not None for m in long["metrics"]))
    observed = long.get("observed_days", 0)
    body = heading("Long-term perspective", "The habits behind the numbers.",
                   "Nine wearable metrics. Your recent 30 days beside a six-month view.")
    body += ('<div class="coverage-strip"><div><span class="coverage-number">' + str(available) + '<span> / 9</span></span>'
             '<span>metrics available</span></div><div><strong>' + str(observed) + '</strong><span>observed dates · six-month lookback</span></div>'
             '<div><strong>' + (f'{date.fromisoformat(latest):%d %b %Y}' if latest else 'No readings yet') + '</strong><span>window ending</span></div></div>')
    if not available:
        body += '<div class="notice">There are no inputs for the long-term model. Missing data is not a favorable result. <a href="/setup">Add your readings →</a></div>'
    body += '<section class="health-grid" aria-label="Long-term raw metrics">'
    for m in long["metrics"]:
        v6, v30 = m["value"], short_metrics.get(m["key"], {}).get("value")
        if v6 is None or v30 is None:
            context = "Not enough data to compare both windows."
        else:
            difference = v30 - v6
            context = ("Same reported value in both windows." if metric_value(m, v6) == metric_value(m, v30) else
                       ("Higher" if difference > 0 else "Lower") + " in the recent window.")
        method = ("Latest recorded estimate on or before the window end; not an average." if m["key"] == "vo2max" else
                  "Weekly recorded activity totals, normalized to observed days. Recording gaps can undercount activity." if m["key"] in ("zone13", "zone45", "strength") else
                  "Calculated from recorded sleep clock times; a modeled consistency value, not a direct sensor reading." if m["key"] == "consistency" else
                  "100% minus the average recorded body-fat percentage; not a direct muscle-mass measurement." if m["key"] == "lean_pct" else
                  "Average of available daily readings in each trailing window; missing values are excluded.")
        evidence = (f'<p>{method}</p><p><b>Model reference:</b> {esc(metric_value(m, m["ref"]))}. '
                    'A research comparison point, not a personal prescription.</p>'
                    f'<p><b>Evidence:</b> {esc(m["cite"])}</p>'
                    '<p>Population associations do not establish an individual outcome or causal benefit.</p>'
                    + rows([("Experimental six-month contribution", number(m.get("years"), "+.2f", " modeled years"))]))
        body += (f'<article class="panel health-metric"><p class="eyebrow">{esc(m["group"])}</p><h2>{esc(m["label"])}</h2>'
                 f'<div class="metric-current">{esc(metric_value(m, v30))}</div><p class="metric-label">Recent 30 days</p>'
                 f'<div class="metric-baseline"><span>Six-month context</span><strong>{esc(metric_value(m, v6))}</strong></div>'
                 f'<p class="metric-context">{esc(context)}</p>' + disclosure("Method & evidence", evidence) + '</article>')
    body += '</section><section class="panel experiment"><div class="section-heading"><div><p class="eyebrow">Experimental · secondary context</p>'
    body += '<h2>A model, not your biological age.</h2></div><span class="tag">Not clinical</span></div>'
    body += ('<p>This research-inspired summary converts published hazard-ratio associations into an age-like scale. '
             'Its original paired-device calibration comes from one person and has not been generalized or clinically validated.</p>')
    bio, delta = long.get("bio_age"), long.get("delta_years")
    if not available:
        body += '<p class="muted">No modeled result: none of the nine inputs are available.</p>'
    else:
        body += rows([("Experimental modeled age", number(bio, ".1f", " years") if bio is not None else "Unavailable — chronological age not configured"),
                      ("Six-month model offset", number(delta, "+.2f", " years")),
                      ("Metrics included", f"{available} of 9; missing metrics excluded")])
    d30 = recent.get("delta_years")
    comparison = number(d30 - delta, "+.2f", " modeled years") if d30 is not None and delta is not None else "Unavailable"
    body += disclosure("Model method & window comparison", '<p>The model divides log hazard ratios by 0.0953, caps each contribution, '
                       f'and applies a configured gain of {hs.SHRINK:.2f}. Activity curves saturate; sleep and strength have non-monotonic relationships. '
                       'Correlated inputs can double-count associations.</p>'
                       + rows([("30-day model offset", number(d30, "+.2f", " years")), ("30-day minus six-month model", comparison)])
                       + '<p>This compares two overlapping window models. It is not a literal rate of biological aging or years gained during a month. '
                       'Different coverage can also change the comparison.</p><p>No blood markers, blood pressure, methylation, diet, smoking, '
                       'or family history enter this model. It cannot assess individual health risk.</p>')
    body += '</section>'
    return page("Health", body, "health")


def setup_content():
    return ('<div class="setup-grid"><section class="panel setup-card"><span class="step-number">01</span>'
            '<h2>Take a look around</h2><p>No Garmin account needed. Explore generated sleep, recovery and activity readings, clearly marked as synthetic on every page.</p>'
            '<pre><code>uv run app.py --demo</code></pre><p class="muted">Stop an existing server with Ctrl+C first, or use <code>--port 8768</code>. '
            'Demo mode ignores your personal configuration and uses temporary synthetic data.</p></section>'
            '<section class="panel setup-card"><span class="step-number">02</span><h2>Bring your own history</h2>'
            '<p>Sync six months of Garmin data, then launch your private dashboard. Authentication happens in your terminal, never in a web form.</p>'
            '<pre><code>uv run sync.py --days 180\nuv run app.py</code></pre>'
            '<p class="muted">Open <code>http://localhost:8767</code>. Credentials are not stored in the source code. '
            'Session tokens live in the private data directory.</p></section></div>'
            '<section class="panel"><h2>Already have exported data?</h2><p>Import the normalized JSON files described in the repository’s '
            '<a href="https://github.com/gzaripov/garmin-vitals#normalized-data-contract">README data contract</a>. The default directory is <code>./data</code>; '
            'set <code>VITALS_DATA_DIR</code> to choose another directory. No private repository is required.</p>'
            + rows([("Daily readings", "garmin_hrv_resp.json"), ("Sleep timing", "garmin_intraday.json"),
                    ("Recorded activities", "garmin_activities.json"), ("Long-term inputs", "garmin_healthspan.json")])
            + '</section><section class="panel privacy-panel"><h2>Local means local</h2><p>The dashboard binds to '
            '<code>127.0.0.1</code> by default. It makes no external font, analytics or tracking requests. Sync contacts Garmin only when you run it.</p>'
            '<p><strong>Optional LAN access has no authentication.</strong> <code>uv run app.py --lan</code> binds to all network interfaces: '
            'anyone who can reach the port can see your readings. Use only on a trusted network; do not publish or tunnel it to the internet.</p>'
            '<p class="muted">For all launch options: <code>uv run app.py --help</code>.</p></section>')


def screen_setup():
    return page("Setup", heading("Make it yours", "Your data. A clearer picture.",
                "A few terminal commands. No dashboard account. No subscription.") + setup_content(), "setup")


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in STATIC:
                filename, content_type = STATIC[path]
                return self._send((Path(__file__).parent / "static" / filename).read_bytes(), content_type)
            if path not in ("/", "/history", "/health", "/setup"):
                body = page("Not found", heading("404", "That page is not here.")
                            + '<a class="button primary" href="/">Back to Today</a>', "")
                return self._send(body.encode(), "text/html; charset=utf-8", 404)
            if path == "/":
                body = screen_today(want_day(self.path))
            elif path == "/history":
                body = screen_history(want_day(self.path))
            elif path == "/health":
                body = screen_health()
            else:
                body = screen_setup()
            self._send(body.encode(), "text/html; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            body = page("Unable to load", heading("Data unavailable", "We couldn’t read the local data.")
                        + '<section class="panel"><p>Check that your imported files match the README data schema, '
                        'or run sync again from your terminal. No data has been changed.</p>'
                        '<a class="button primary" href="/setup">Open setup instructions</a></section>', "")
            self._send(body.encode(), "text/html; charset=utf-8", 500)

    def _send(self, data, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vitals: a private, local Garmin dashboard.")
    parser.add_argument("--demo", action="store_true", help="use temporary synthetic data; ignore personal configuration")
    parser.add_argument("--port", type=int, default=8767, help="local HTTP port (default: 8767)")
    parser.add_argument("--lan", action="store_true", help="listen on all interfaces; WARNING: no authentication, exposes health data to your network")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    if args.lan:
        print("WARNING: LAN mode has no authentication. Anyone who can reach this port can read your health data.")
    print(f"Vitals — {'Demo / synthetic data' if DEMO else 'Local / private data'}")
    print(f"http://localhost:{args.port} (bound to {host})")
    try:
        ThreadingHTTPServer((host, args.port), H).serve_forever()
    except KeyboardInterrupt:
        print("\nVitals stopped.")
