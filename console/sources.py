"""Feed discovery and loading.

The console never collects anything itself - it reads what the tools already
wrote. Every feed is optional: a tool you don't run simply has no page, and
a feed that has gone stale says so out loud rather than quietly showing you
last month's numbers.
"""

from __future__ import annotations

import configparser
import json
import os
import sqlite3
from datetime import datetime, timezone

# How old a feed can get before the console starts complaining, in hours.
DEFAULT_FRESH_HOURS = 26      # a daily job that ran a bit late is still fine
DEFAULT_STALE_HOURS = 24 * 8  # a weekly job that missed its slot is not


def utcnow():
    return datetime.now(timezone.utc)


def parse_ts(value):
    """Parse the ISO timestamps our tools emit; return None if unparseable."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(str(value)[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_hours(ts):
    if ts is None:
        return None
    return (utcnow() - ts).total_seconds() / 3600.0


def freshness(ts, fresh_hours=DEFAULT_FRESH_HOURS, stale_hours=DEFAULT_STALE_HOURS):
    """('fresh'|'aging'|'stale'|'unknown', human age string)."""
    h = age_hours(ts)
    if h is None:
        return "unknown", "no timestamp"
    if h < 0:
        h = 0
    if h < 1:
        human = "%d min ago" % max(1, int(h * 60))
    elif h < 48:
        human = "%dh ago" % int(h)
    else:
        human = "%dd ago" % int(h / 24)
    if h <= fresh_hours:
        return "fresh", human
    if h <= stale_hours:
        return "aging", human
    return "stale", human


class Feed:
    """One tool's output: what it is, whether it loaded, and how old it is."""

    def __init__(self, key, label, path, data=None, error=None, ts=None, missing=False):
        self.key = key
        self.label = label
        self.path = path
        self.data = data
        self.error = error
        self.ts = ts
        self.missing = missing   # configured, but its tool has never written it
        self.state, self.age = freshness(ts)

    @property
    def ok(self):
        return self.data is not None and self.error is None

    @property
    def status_note(self):
        if self.error:
            return self.error
        if self.missing:
            # "Never collected" is a normal state for an optional tool, and it
            # must not read like a failure - the raw path stays available in
            # the hint for whoever is actually debugging.
            return "nothing collected yet"
        if not self.path:
            return "not configured"
        return self.age

    @property
    def hint(self):
        if self.missing:
            return "This page fills in once its tool has run. It reads: %s" % self.path
        return ""


def _load_json(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def load_tenant(path):
    """entra-tenant-docs tenant.json"""
    d = _load_json(path)
    return d, parse_ts(d.get("GeneratedUtc"))


def load_run_summary(path):
    """entra-tenant-docs run-summary.json"""
    d = _load_json(path)
    return d, parse_ts(d.get("GeneratedUtc"))


def load_security(path):
    """entra-security-snapshot -JsonPath"""
    d = _load_json(path)
    return d, parse_ts(d.get("GeneratedUtc"))


def load_licensing(path):
    """m365-license-waste-report -JsonPath"""
    d = _load_json(path)
    return d, parse_ts(d.get("GeneratedUtc"))


def load_history(path):
    """entra-tenant-docs history/ folder -> snapshots oldest first."""
    snaps = []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        try:
            snaps.append(_load_json(os.path.join(path, name)))
        except (ValueError, OSError):
            continue
    if not snaps:
        raise ValueError("no readable snapshots in history folder")
    snaps.sort(key=lambda s: str(s.get("GeneratedUtc") or ""))
    return snaps, parse_ts(snaps[-1].get("GeneratedUtc"))


def load_fleet(path):
    """print-fleet-dashboard fleet.db -> devices with their latest snapshot.

    Reads the published schema (devices / snapshots / supplies). Uses
    devices.last_seen for 'last successful contact', which is the distinction
    that matters when a device has gone quiet.
    """
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        devices = []
        newest = None
        for d in conn.execute("SELECT * FROM devices ORDER BY name"):
            snap = conn.execute(
                "SELECT * FROM snapshots WHERE device_id = ? ORDER BY ts DESC LIMIT 1",
                (d["id"],),
            ).fetchone()
            supplies = []
            if snap:
                supplies = [dict(r) for r in conn.execute(
                    "SELECT * FROM supplies WHERE snapshot_id = ? ORDER BY slot",
                    (snap["id"],),
                )]
                ts = parse_ts(snap["ts"])
                if ts and (newest is None or ts > newest):
                    newest = ts
            # Day-over-day page volume for the last 14 days.
            volumes = [dict(r) for r in conn.execute(
                """SELECT substr(ts, 1, 10) AS day, MAX(page_count) AS pages
                   FROM snapshots WHERE device_id = ? AND page_count IS NOT NULL
                   GROUP BY day ORDER BY day DESC LIMIT 15""",
                (d["id"],),
            )]
            devices.append({
                "device": dict(d),
                "snapshot": dict(snap) if snap else None,
                "supplies": supplies,
                "volumes": list(reversed(volumes)),
            })
        if not devices:
            raise ValueError("fleet database has no devices yet")
        return {"devices": devices}, newest
    finally:
        conn.close()


LOADERS = {
    "tenant":            ("Identity",         load_tenant),
    "run_summary":       ("Last run",         load_run_summary),
    "security":          ("Security",         load_security),
    "licensing":         ("Licensing",        load_licensing),
    "history":           ("Change log",       load_history),
    "fleet":             ("Print fleet",      load_fleet),
    # run-all archives the two JSON feeds above after each successful run;
    # these folders of snapshots become the "over time" trend lines. Both
    # files carry GeneratedUtc, so the tenant-docs history loader fits as-is.
    "security_history":  ("Security trend",  load_history),
    "licensing_history": ("Licensing trend", load_history),
}


def load_all(config_path):
    """Read sources.ini and load every configured feed. Never raises for a
    feed problem - a broken or missing feed becomes a Feed carrying its error,
    so the console can render honestly around it."""
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    # utf-8-sig: Windows PowerShell 5.1's Set-Content -Encoding UTF8 writes a
    # BOM, and setup.ps1 writes this file. Without it, configparser sees the
    # BOM as text glued to line 1 and fails with "no section headers".
    if not cfg.read(config_path, encoding="utf-8-sig"):
        raise SystemExit("Config not found: %s (copy sources.example.ini)" % config_path)

    # Relative paths are resolved against the config file's own folder, not the
    # current directory, so the console builds the same from anywhere - which
    # matters when a scheduled task runs it from C:\Windows\System32.
    cfg_dir = os.path.dirname(os.path.abspath(config_path))

    def _norm(p):
        # Configs written on Windows use backslashes. On POSIX a backslash is
        # an ordinary filename character, so a copied config fails with paths
        # that LOOK right in the error message - normalize instead.
        return p.replace("\\", os.sep) if os.sep == "/" else p

    base = _norm(cfg.get("console", "base_path", fallback="").strip())
    if base and not os.path.isabs(base):
        base = os.path.join(cfg_dir, base)

    feeds = {}
    for key, (label, loader) in LOADERS.items():
        raw = _norm(cfg.get("sources", key, fallback="").strip())
        if not raw:
            feeds[key] = Feed(key, label, None, error=None)
            continue
        path = raw if os.path.isabs(raw) else os.path.join(base or cfg_dir, raw)
        if not os.path.exists(path):
            # Configured but never produced: the tool simply has not run yet.
            # That is a normal state (printers are optional), distinct from a
            # file that exists and cannot be read - which stays a real error.
            feeds[key] = Feed(key, label, path, missing=True)
            continue
        try:
            data, ts = loader(path)
            feeds[key] = Feed(key, label, path, data=data, ts=ts)
        except Exception as e:  # noqa: BLE001 - one bad feed must not kill the console
            feeds[key] = Feed(key, label, path, error="%s: %s" % (type(e).__name__, e))
    return cfg, feeds
