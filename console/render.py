"""HTML rendering: shared shell, reusable components, one page per domain.

Self-contained output - no server, no CDN, no build step. Each page carries
the whole stylesheet so a single file can be mailed or dropped on a share and
still look right.
"""

from __future__ import annotations

import html
import json
import os

from .sources import freshness

# --------------------------------------------------------------------------- #
# Design tokens - shared with print-fleet-dashboard and entra-tenant-docs so
# the whole toolkit reads as one product in both light and dark.
# --------------------------------------------------------------------------- #

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --accent: #2a78d6; --accent-deep: #1c5cab;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --spark: #c3c2b7;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --accent: #3987e5; --accent-deep: #86b6ef;
    --spark: #52514e;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19;
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --baseline: #383835;
  --border: rgba(255,255,255,0.10);
  --accent: #3987e5; --accent-deep: #86b6ef;
  --spark: #52514e;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--accent-deep); }
.wrap { max-width: 1120px; margin: 0 auto; padding: 20px 20px 48px; }

nav.top { border-bottom: 1px solid var(--border); background: var(--surface); }
nav.top .inner { max-width: 1120px; margin: 0 auto; padding: 0 20px;
  display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
nav.top .brand { font-weight: 650; font-size: 14px; margin-right: 12px; padding: 12px 0; }
nav.top a { display: inline-block; padding: 12px 10px; font-size: 13px;
  color: var(--ink-2); text-decoration: none; border-bottom: 2px solid transparent; }
nav.top a:hover { color: var(--ink); }
nav.top a.on { color: var(--ink); font-weight: 600; border-bottom-color: var(--accent); }
nav.top a.off { color: var(--muted); }

header.page { margin: 20px 0 16px; }
header.page h1 { font-size: 21px; font-weight: 650; margin: 0 0 3px; }
header.page .sub { color: var(--ink-2); font-size: 13px; }
.stamp { display: inline-flex; align-items: center; gap: 6px; margin-top: 9px;
  padding: 4px 10px; border: 1px solid var(--border); border-radius: 999px;
  background: var(--surface); font-size: 12.5px; color: var(--ink-2); }
.dot { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.dot.fresh { background: var(--good); }
.dot.aging { background: var(--warning); }
.dot.stale { background: var(--critical); }
.dot.unknown { background: var(--muted); }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px; margin-bottom: 18px; }
.card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px; }
.card .k { font-size: 12px; color: var(--ink-2); margin-bottom: 2px; }
.card .v { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
.card .d { font-size: 12px; color: var(--muted); }

section { background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; }
section h2 { font-size: 15px; font-weight: 650; margin: 0 0 4px; }
section .note { font-size: 12.5px; color: var(--muted); margin: 0 0 10px; }

table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { text-align: left; color: var(--ink-2); font-weight: 600; font-size: 12px;
  padding: 6px 10px 6px 0; border-bottom: 1px solid var(--baseline); }
td { padding: 7px 10px 7px 0; border-bottom: 1px solid var(--grid); vertical-align: top; }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.scroll { overflow-x: auto; }

.badge { display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
  font-weight: 600; white-space: nowrap; }
.badge svg { width: 12px; height: 12px; flex: none; }
.muted { color: var(--muted); }

.meter { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 90px; }
.supply { display: grid; grid-template-columns: 62px 1fr; gap: 8px; align-items: center;
  font-size: 12px; padding: 1px 0; }
.supply .nm { color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meter .track { flex: 1; height: 8px; border-radius: 4px; overflow: hidden; }
.meter .fill { height: 100%; border-radius: 4px; }
.meter .lbl { font-size: 12px; color: var(--ink-2); font-variant-numeric: tabular-nums;
  white-space: nowrap; min-width: 38px; text-align: right; }
.bar-row { display: grid; grid-template-columns: minmax(150px, 230px) 1fr 52px;
  gap: 10px; align-items: center; padding: 5px 0; }
.bar-row .n { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-row .bar { height: 12px; border-radius: 3px 4px 4px 3px; background: var(--accent); min-width: 2px; }
.bar-row .c { font-size: 12.5px; font-variant-numeric: tabular-nums; color: var(--ink-2); text-align: right; }

.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.banner { display: flex; gap: 10px; align-items: flex-start; margin: 0 0 14px;
  padding: 10px 14px; border: 1px solid var(--border); border-left: 4px solid var(--warning);
  border-radius: 6px; background: var(--surface); font-size: 13.5px; }
.banner > svg { width: 16px; height: 16px; flex: none; margin-top: 2px; color: var(--warning); }
.banner.serious { border-left-color: var(--serious); }
.banner.serious > svg { color: var(--serious); }
.banner.critical { border-left-color: var(--critical); }
.banner.critical > svg { color: var(--critical); }
.banner .why { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }
footer .refresh-note { display: block; margin-top: 4px; }
.rules { width: 100%; border-collapse: collapse; }
.rules .help { display: block; font-size: 12px; color: var(--muted); }
.rules td.ctl { white-space: nowrap; vertical-align: top; padding-top: 9px; }
.rules input.num { width: 66px; }
.unit { color: var(--muted); font-size: 12px; margin-left: 6px; }
input[type=text], input[type=number], select {
  font: inherit; font-size: 13px; color: var(--ink); background: var(--surface);
  border: 1px solid var(--border); border-radius: 5px; padding: 4px 6px; }
input.wide { width: 320px; max-width: 100%; }
.sendctl { margin: 4px 0 20px; }
.sendctl .opt { display: block; margin: 2px 0; cursor: pointer; }
.tabtoggle { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; }
.btn { font: inherit; font-weight: 600; color: #fff; background: var(--accent-deep);
  border: 0; border-radius: 6px; padding: 8px 14px; cursor: pointer; }
.btn:hover { background: var(--accent); }
.savebox { margin-top: 20px; }
.savemsg { margin-left: 10px; font-size: 13px; color: var(--ink-2); }
.livebar { margin: 0 0 14px; display: flex; gap: 10px; align-items: center; }
/* display:flex beats the hidden attribute's own display:none, so a page
   opened as a FILE would show a Refresh button that cannot do anything. */
.livebar[hidden] { display: none !important; }
.livemsg { color: var(--muted); font-size: 13px; }
.rows { margin: 6px 0 10px; }
.row { display: flex; gap: 10px; align-items: center; padding: 2px 0; flex-wrap: wrap; }
.row input[data-row="name"] { width: 200px; }
.row input[data-row="value"] { width: 200px; font-variant-numeric: tabular-nums; }
.rowmsg { color: var(--critical); font-size: 12px; }
.rowinfo { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.btn.small { padding: 5px 11px; font-size: 12.5px; background: transparent;
  color: var(--accent-deep); border: 1px solid var(--border); }
.btn.small:hover { background: var(--surface); color: var(--ink); }
.savebox textarea { width: 100%; margin-top: 4px; padding: 8px; color: var(--ink);
  background: var(--page); border: 1px solid var(--border); border-radius: 6px;
  font: 12px/1.5 ui-monospace, Consolas, "Courier New", monospace; }
.kv { display: grid; grid-template-columns: 150px 1fr; gap: 6px 14px; font-size: 13.5px; }
.kv .k { color: var(--ink-2); }
.tile { display: block; background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 15px 16px; text-decoration: none; color: inherit; }
.tile:hover { border-color: var(--accent); }
.tile .top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.tile .title { font-size: 14px; font-weight: 650; }
.tile .headline { font-size: 26px; font-weight: 650; margin: 6px 0 1px;
  font-variant-numeric: tabular-nums; }
.tile .sub { font-size: 12.5px; color: var(--ink-2); }
.tile .foot { margin-top: 10px; padding-top: 9px; border-top: 1px solid var(--grid);
  font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.tile.off { opacity: .72; }
.tile.off .headline { color: var(--muted); font-size: 15px; font-weight: 600; }
.tile .spark { margin-top: 8px; display: flex; align-items: center; gap: 10px; }
.tile .spark > svg { flex: 1; height: 26px; }   /* the sparkline only - not the badge's icon */

.trend-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.trend { background: var(--page); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
.trend .k { font-size: 12px; color: var(--ink-2); }
.trend .v { font-size: 20px; font-weight: 650; font-variant-numeric: tabular-nums;
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.trend .v .badge { font-size: 11.5px; }
.trend > svg { display: block; width: 100%; height: 34px; margin-top: 6px; }   /* sparkline only */

.chg { display: flex; gap: 8px; padding: 5px 0; font-size: 13px; align-items: baseline;
  border-bottom: 1px solid var(--grid); }
.chg:last-child { border-bottom: none; }
.chg .kind { flex: none; width: 74px; font-size: 11.5px; font-weight: 600;
  color: var(--ink-2); text-transform: uppercase; letter-spacing: .3px; }
.chg .cat { color: var(--muted); }
.chg .act { display: block; font-size: 12px; color: var(--ink-2); margin-top: 2px; }
.chg .act::before { content: "\\2192\\00a0"; color: var(--accent-deep); }
.chg-ts { margin: 13px 0 2px; font-size: 12px; color: var(--muted);
  font-variant-numeric: tabular-nums; }

details { margin-top: 8px; }
summary { cursor: pointer; font-size: 12.5px; color: var(--accent-deep); }
details pre { background: var(--page); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; font-size: 12px; overflow-x: auto; margin: 8px 0 0; }
ul.plain { margin: 6px 0 0; padding-left: 18px; font-size: 13px; }
ul.plain li { margin: 2px 0; }
.empty { padding: 22px 0; text-align: center; color: var(--muted); font-size: 13px; }
footer { color: var(--muted); font-size: 12px; margin-top: 22px; }
@media (max-width: 700px) { .bar-row { grid-template-columns: 1fr 1fr 44px; } }
"""

ICONS = {
    "check": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M2.5 8.5l3.5 3.5 7-8"/></svg>',
    "warn": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M8 1.8L15 14H1L8 1.8z"/><path d="M8 6v4"/><circle cx="8" cy="12.2" r=".6" fill="currentColor"/></svg>',
    "dash": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="8" cy="8" r="6"/><path d="M5 8h6"/></svg>',
    "clock": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.5 1.5"/></svg>',
    "eye": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>',
}

# Set by build.py when a VERSION file ships beside it (release bundles do).
SUITE_VERSION = ""
# Set by build.py from refresh-status.json: one sentence about how this machine
# keeps itself fresh (and, when it applies, that it stays signed in between
# refreshes). Every page's footer carries it, so that state is never hidden.
REFRESH_NOTE = ""

PAGES = [
    ("index",     "Overview"),
    ("identity",  "Identity"),
    ("security",  "Security"),
    ("licensing", "Licensing"),
    ("fleet",     "Print fleet"),
    ("changes",   "What changed"),
    ("alerts",    "Alerts"),
]


def esc(v):
    return html.escape("" if v is None else str(v))


def money(amount, currency="$"):
    """Whole-dollar money with thousands separators, e.g. '$4,752'. Amounts are
    monthly/annual sums where cents are noise; None renders as an em dash."""
    if amount is None:
        return "&mdash;"
    try:
        return "%s%s" % (esc(currency), format(int(round(float(amount))), ","))
    except (TypeError, ValueError):
        return esc(amount)


def badge(color, icon, label):
    return ('<span class="badge" style="color:var(--%s)">%s%s</span>'
            % (color, ICONS.get(icon, ""), esc(label)))


def muted_badge(label, icon="dash"):
    return '<span class="badge muted">%s%s</span>' % (ICONS[icon], esc(label))


STATUS_BADGE = {
    "ok":       lambda: badge("good", "check", "OK"),
    "warning":  lambda: badge("warning", "warn", "Warning"),
    "error":    lambda: badge("critical", "warn", "Error"),
    "offline":  lambda: muted_badge("Offline"),
    "enabled":  lambda: badge("good", "check", "Enabled"),
    "disabled": lambda: muted_badge("Disabled"),
    "enabledForReportingButNotEnforced": lambda: badge("warning", "eye", "Report-only"),
}


def status_badge(state):
    fn = STATUS_BADGE.get(str(state))
    return fn() if fn else esc(state)


def meter(percent, fill_var="accent"):
    """Severity-aware meter; percent may be None (unknown)."""
    if percent is None:
        return '<span class="muted">&mdash;</span>'
    fill = "critical" if percent < 10 else ("warning" if percent < 20 else fill_var)
    return (
        '<div class="meter"><div class="track" style="background:color-mix(in oklab, '
        'var(--%s) 18%%, var(--surface))"><div class="fill" style="width:%d%%;'
        'background:var(--%s)"></div></div><div class="lbl">%d%%</div></div>'
        % (fill, max(0, min(100, int(percent))), fill, percent)
    )


def bar_rows(rows, value_key="count", label_key="label", suffix=""):
    if not rows:
        return '<p class="empty">Nothing to show.</p>'
    top = max(1, max(int(r.get(value_key) or 0) for r in rows))
    out = []
    for r in rows:
        v = int(r.get(value_key) or 0)
        out.append(
            '<div class="bar-row"><div class="n" title="%s">%s</div>'
            '<div><div class="bar" style="width:%d%%"></div></div>'
            '<div class="c">%s%s</div></div>'
            % (esc(r.get(label_key)), esc(r.get(label_key)),
               round(100.0 * v / top), esc(v), suffix)
        )
    return "".join(out)


def sparkline(values, width=210, height=34):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pad = 3
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + i * (width - 2 * pad) / (n - 1)
        y = height - pad - (v - lo) * (height - 2 * pad) / span
        pts.append("%.1f,%.1f" % (x, y))
    line = " ".join(pts)
    area = "%d,%d %s %.1f,%d" % (pad, height - pad, line, width - pad, height - pad)
    lx, ly = pts[-1].split(",")
    return (
        '<svg viewBox="0 0 %d %d" preserveAspectRatio="none" aria-hidden="true" '
        'style="display:block;width:100%%;height:%dpx">'
        '<polygon points="%s" fill="var(--accent)" opacity="0.1"></polygon>'
        '<polyline points="%s" fill="none" stroke="var(--accent)" stroke-width="2" '
        'vector-effect="non-scaling-stroke"></polyline>'
        '<circle cx="%s" cy="%s" r="2.5" fill="var(--accent)" stroke="var(--surface)" '
        'stroke-width="2"></circle></svg>' % (width, height, height, area, line, lx, ly)
    )


def fmt_metric(value, unit):
    """A metric value with its unit: '87%', '$1,240', '14'."""
    if value is None:
        return "&mdash;"
    if unit == "%":
        return esc("%g%%" % round(float(value), 1))
    if unit and unit != "%":            # a currency prefix
        return money(value, unit)
    try:
        return esc(format(int(round(float(value))), ","))
    except (TypeError, ValueError):
        return esc(value)


def delta_badge(metric):
    """How a metric moved since the previous point, read against which way is
    good: improving -> good/check, worsening -> warning, flat or neutral ->
    muted. Always a glyph plus words, never colour alone."""
    d = metric.get("delta")
    if d is None:
        return muted_badge("first reading", "clock")
    if abs(d) < 1e-9:
        return muted_badge("no change")
    sign = "+" if d > 0 else "−"
    mag = abs(d)
    unit = metric.get("unit") or ""
    if unit == "%":
        shown = "%g pts" % round(mag, 1)
    elif unit:
        shown = money(mag, unit)
    else:
        shown = format(int(round(mag)), ",")
    label = "%s%s since last" % (sign, shown)
    tone = metric.get("tone")
    if tone == "good":
        return badge("good", "check", label)
    if tone == "warning":
        return badge("warning", "warn", label)
    return muted_badge(label)


def trend_card(metric):
    """One small-multiple: label, current value with its delta, sparkline."""
    return ('<div class="trend"><div class="k">%s</div>'
            '<div class="v">%s %s</div>%s</div>'
            % (esc(metric["label"]), fmt_metric(metric["current"], metric["unit"]),
               delta_badge(metric), sparkline(metric["values"], width=220, height=34)))


def freshness_chip(state, age, prefix="Data"):
    return ('<span class="stamp"><span class="dot %s"></span>%s %s</span>'
            % (esc(state), esc(prefix), esc(age)))


def nav(current, available):
    links = ['<span class="brand">IT Console</span>']
    for key, label in PAGES:
        cls = "on" if key == current else ("" if available.get(key, True) else "off")
        title = "" if available.get(key, True) else ' title="not configured"'
        links.append('<a class="%s" href="%s.html"%s>%s</a>'
                     % (cls, key, title, esc(label)))
    return '<nav class="top"><div class="inner">%s</div></nav>' % "".join(links)


# The Refresh button, and the script that makes it work. Both are inert
# unless the console is being SERVED - a page opened as a file cannot start
# anything on your computer, and a button that does nothing is worse than no
# button. Served, window.CONSOLE_KEY exists and the bar appears.
LIVE_BAR = """<div class="livebar" id="livebar" hidden>
<button type="button" class="btn" id="refresh-btn">Refresh now</button>
<span class="livemsg" id="refresh-msg"></span></div>
<script>
(function () {
  var bar = document.getElementById('livebar');
  var btn = document.getElementById('refresh-btn');
  var msg = document.getElementById('refresh-msg');
  if (!bar || !window.CONSOLE_KEY) { return; }   /* opened as a file: stay hidden */
  bar.hidden = false;
  function say(t) { msg.textContent = t; }
  btn.addEventListener('click', function () {
    btn.disabled = true;
    say('Starting...');
    fetch('/api/refresh', { method: 'POST', headers: { 'X-Console-Key': window.CONSOLE_KEY } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        say(d.message || '');
        if (d.ok) { setTimeout(function () { location.href = 'status.html'; }, 600); }
        else { btn.disabled = false; }
      })
      .catch(function () { say('Could not reach the console service. Is its window still open?'); btn.disabled = false; });
  });
})();
</script>"""


def shell(title, current, available, body, generated, subtitle=""):
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title><style>%s</style></head><body>
%s
<div class="wrap">
<header class="page"><h1>%s</h1><div class="sub">%s</div>%s</header>
%s
<footer>Built by <a href="https://github.com/JonathanT10/it-ops-console">it-ops-console</a>%s
at %s (UTC) from the tools' own output. Each page is self-contained.%s</footer>
</div></body></html>""" % (
        esc(title), CSS, nav(current, available), esc(title), subtitle, LIVE_BAR, body,
        (" (suite v%s)" % esc(SUITE_VERSION)) if SUITE_VERSION else "", esc(generated),
        ('<span class="refresh-note">%s</span>' % esc(REFRESH_NOTE)) if REFRESH_NOTE else ""
    )


def write_page(out_dir, name, content):
    path = os.path.join(out_dir, "%s.html" % name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path
