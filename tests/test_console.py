"""Test suite: python tests/test_console.py

Covers the parts that are easy to get quietly wrong - freshness classification,
feeds that are missing or corrupt, the per-domain maths, and that every page
renders for both a full and an empty console.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from console import model, pages                      # noqa: E402
from console.render import write_page                 # noqa: E402
from console.sources import Feed, freshness, load_all, parse_ts  # noqa: E402

FAILS = []


def check(label, cond):
    print("%s %s" % ("PASS" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #

def test_freshness():
    state, _ = freshness(NOW - timedelta(hours=2))
    check("freshness: recent is fresh", state == "fresh")
    state, _ = freshness(NOW - timedelta(days=3))
    check("freshness: 3 days is aging", state == "aging")
    state, _ = freshness(NOW - timedelta(days=30))
    check("freshness: 30 days is stale", state == "stale")
    state, human = freshness(None)
    check("freshness: no timestamp is unknown", state == "unknown" and human == "no timestamp")
    _, human = freshness(NOW - timedelta(hours=5))
    check("freshness: hours render as hours", human.endswith("h ago"))
    _, human = freshness(NOW - timedelta(days=4))
    check("freshness: days render as days", human.endswith("d ago"))


def test_parse_ts():
    check("parse: Z suffix", parse_ts("2026-08-31T12:00:00Z") is not None)
    check("parse: offset form", parse_ts("2026-08-31T12:00:00+00:00") is not None)
    check("parse: naive assumed UTC",
          parse_ts("2026-08-31T12:00:00").tzinfo is not None)
    check("parse: junk is None", parse_ts("not a date") is None)
    check("parse: empty is None", parse_ts("") is None)


# --------------------------------------------------------------------------- #
# Feed loading and degradation
# --------------------------------------------------------------------------- #

def test_loading(tmp):
    feeds_dir = os.path.join(tmp, "feeds")
    os.makedirs(feeds_dir)
    good = {"GeneratedUtc": iso(NOW - timedelta(hours=1)), "TenantId": "t",
            "Organization": {"DisplayName": "Contoso"},
            "UserCounts": {"Members": 10, "EnabledMembers": 9, "Guests": 2},
            "ConditionalAccess": {"Policies": [], "NamedLocations": []},
            "Roles": [], "Groups": {"Total": 3}, "Applications": []}
    with open(os.path.join(feeds_dir, "tenant.json"), "w") as fh:
        json.dump(good, fh)
    with open(os.path.join(feeds_dir, "broken.json"), "w") as fh:
        fh.write("{ this is not json")

    cfg_path = os.path.join(tmp, "sources.ini")
    with open(cfg_path, "w") as fh:
        fh.write("[console]\nbase_path = %s\n\n[sources]\n"
                 "tenant = tenant.json\nsecurity = broken.json\n"
                 "licensing = missing.json\nfleet =\nhistory =\nrun_summary =\n" % feeds_dir)

    _cfg, feeds = load_all(cfg_path)
    check("load: good feed ok", feeds["tenant"].ok)
    check("load: corrupt feed carries error, does not raise",
          not feeds["security"].ok and "JSONDecodeError" in (feeds["security"].error or ""))
    check("load: missing file reported", "file not found" in (feeds["licensing"].error or ""))
    check("load: unconfigured feed is not an error",
          not feeds["fleet"].ok and feeds["fleet"].error is None
          and feeds["fleet"].status_note == "not configured")
    check("load: timestamp picked up from payload", feeds["tenant"].state == "fresh")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

def test_identity_model():
    tenant = Feed("tenant", "Identity", "x", data={
        "GeneratedUtc": iso(NOW), "TenantId": "abc",
        "Organization": {"DisplayName": "Contoso"},
        "UserCounts": {"Members": 100, "EnabledMembers": 90, "Guests": 5},
        "ConditionalAccess": {"Policies": [
            {"Name": "A", "State": "enabled"},
            {"Name": "B", "State": "disabled"},
            {"Name": "C", "State": "enabledForReportingButNotEnforced"},
        ], "NamedLocations": []},
        "Roles": [{"Role": "Global Administrator", "Members": [{"DisplayName": "x"},
                                                               {"DisplayName": "y"}]},
                  {"Role": "Helpdesk", "Members": [{"DisplayName": "z"}]}],
        "Groups": {"Total": 4, "Dynamic": [{"Name": "d"}]},
        "Applications": [{"Name": "app"}],
        "Intune": {"Available": False},
    }, ts=NOW)
    rs = Feed("run_summary", "Last run", "x", data={
        "GeneratedUtc": iso(NOW),
        "CaGaps": [{"Title": "MFA for all users", "Result": "fail", "Severity": "critical",
                    "Detail": "none"},
                   {"Title": "Legacy auth", "Result": "pass", "Severity": "critical"}],
    }, ts=NOW)

    m = model.identity_model(tenant, rs)
    check("identity: CA counted by state",
          m["ca_enabled"] == 1 and m["ca_disabled"] == 1 and m["ca_report_only"] == 1)
    check("identity: roles sorted by size", m["roles"][0]["role"] == "Global Administrator")
    check("identity: gaps ride in from run-summary", len(m["gaps"]) == 2)
    check("identity: only failing gaps in gaps_failing", len(m["gaps_failing"]) == 1)
    check("identity: intune omitted when unavailable", m["intune"] is None)

    m2 = model.identity_model(tenant, None)
    check("identity: no run-summary means no gaps, not a crash", m2["gaps"] == [])
    check("identity: missing feed yields None",
          model.identity_model(Feed("tenant", "Identity", None)) is None)


def test_licensing_model():
    feed = Feed("licensing", "Licensing", "x", data={
        "GeneratedUtc": iso(NOW), "StaleDays": 90, "LicensedUsers": 50,
        "SkuSummary": [{"Sku": "A", "Purchased": 10, "Assigned": 8, "Unassigned": 2},
                       {"Sku": "B", "Purchased": 20, "Assigned": 5, "Unassigned": 15}],
        "ConsumptionSkus": [{"Sku": "FLOW_FREE", "Purchased": 10000, "Assigned": 40}],
        "ReclaimCandidates": [
            {"Reason": "DISABLED ACCOUNT", "DisplayName": "d"},
            {"Reason": "STALE (> 90 days)", "DisplayName": "s"},
            {"Reason": "NEVER SIGNED IN", "DisplayName": "n"}],
    }, ts=NOW)
    m = model.licensing_model(feed)
    check("licensing: unassigned excludes consumption SKUs", m["unassigned_total"] == 17)
    check("licensing: SKUs sorted by waste", m["skus"][0]["Sku"] == "B")
    check("licensing: disabled split out", len(m["disabled_holders"]) == 1)
    check("licensing: stale+never grouped", len(m["stale_holders"]) == 2)


def test_fleet_model(tmp):
    db = os.path.join(tmp, "fleet.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE devices (id INTEGER PRIMARY KEY, ip TEXT, name TEXT, model TEXT,
                            serial TEXT, first_seen TEXT, last_seen TEXT);
      CREATE TABLE snapshots (id INTEGER PRIMARY KEY, device_id INTEGER, ts TEXT,
                              reachable INTEGER, status TEXT, detail TEXT,
                              uptime_seconds INTEGER, page_count INTEGER);
      CREATE TABLE supplies (id INTEGER PRIMARY KEY, snapshot_id INTEGER, slot INTEGER,
                             description TEXT, supply_type TEXT, level INTEGER,
                             max_capacity INTEGER);
    """)
    fresh, old = iso(NOW - timedelta(hours=1)), iso(NOW - timedelta(days=6))
    conn.execute("INSERT INTO devices VALUES (1,'10.0.0.1','Live','M','S','x',?)", (fresh,))
    conn.execute("INSERT INTO devices VALUES (2,'10.0.0.2','Quiet','M','S','x',?)", (old,))
    conn.execute("INSERT INTO snapshots VALUES (1,1,?,1,'warning','Low toner',100,5000)", (fresh,))
    conn.execute("INSERT INTO snapshots VALUES (2,2,?,1,'ok','Idle',100,900)", (old,))
    # yesterday/today rows on device 1 so a volume delta exists
    conn.execute("INSERT INTO snapshots VALUES (3,1,?,1,'ok','Idle',100,4800)",
                 (iso(NOW - timedelta(days=1)),))
    conn.execute("INSERT INTO supplies VALUES (1,1,1,'Black Toner','toner',500,10000)")
    conn.execute("INSERT INTO supplies VALUES (2,1,2,'Drum','drum',-2,NULL)")
    conn.commit(); conn.close()

    from console.sources import load_fleet
    data, ts = load_fleet(db)
    m = model.fleet_model(Feed("fleet", "Print fleet", db, data=data, ts=ts))
    names = {d["name"]: d for d in m["devices"]}
    check("fleet: stale last_seen becomes offline", names["Quiet"]["status"] == "offline")
    check("fleet: recent device keeps its status", names["Live"]["status"] == "warning")
    check("fleet: online count excludes offline", m["online"] == 1 and m["total"] == 2)
    supplies = {s["description"]: s for s in names["Live"]["supplies"]}
    check("fleet: percent computed", supplies["Black Toner"]["percent"] == 5)
    check("fleet: unknown level stays None", supplies["Drum"]["percent"] is None)
    check("fleet: low supply flagged", len(names["Live"]["low_supplies"]) == 1)
    check("fleet: attention includes offline + warning", len(m["attention"]) == 2)


def test_changes_model():
    def snap(ts, policies, roles=None, apps=None):
        return {"GeneratedUtc": ts,
                "ConditionalAccess": {"Policies": policies},
                "Roles": roles or [], "Licenses": [], "Applications": apps or []}
    a = snap(iso(NOW - timedelta(days=2)),
             [{"Name": "MFA", "State": "enabledForReportingButNotEnforced"}],
             [{"Role": "GA", "Members": [{"DisplayName": "Ann", "UserPrincipalName": "a@x"}]}])
    b = snap(iso(NOW - timedelta(days=1)),
             [{"Name": "MFA", "State": "enabled"}, {"Name": "New", "State": "enabled"}],
             [{"Role": "GA", "Members": [{"DisplayName": "Ann", "UserPrincipalName": "a@x"},
                                         {"DisplayName": "Bob", "UserPrincipalName": "b@x"}]}],
             [{"AppId": "1", "Name": "NewApp"}])
    m = model.changes_model(Feed("history", "Change log", "x", data=[a, b], ts=NOW))
    kinds = {(e["category"], e["kind"], e["item"]) for e in m["events"]}
    check("changes: state flip detected",
          ("Conditional Access", "changed", "MFA") in kinds)
    check("changes: new policy detected",
          ("Conditional Access", "added", "New") in kinds)
    check("changes: role grant detected",
          ("Role assignments", "added", "Bob") in kinds)
    check("changes: app registration detected",
          ("App registrations", "added", "NewApp") in kinds)
    check("changes: unchanged things stay silent", len(m["events"]) == 4)
    check("changes: snapshot count reported", m["snapshot_count"] == 2)

    single = model.changes_model(Feed("history", "Change log", "x", data=[a], ts=NOW))
    check("changes: single snapshot means no events", single["events"] == [])


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _all_pages(models, feeds, available):
    gen = "2026-01-01 00:00:00"
    return {
        "index": pages.build_overview(models, feeds, available, gen),
        "identity": pages.build_identity(models["identity"], feeds["tenant"], available, gen),
        "security": pages.build_security(models["security"], feeds["security"], available, gen),
        "licensing": pages.build_licensing(models["licensing"], feeds["licensing"], available, gen),
        "fleet": pages.build_fleet(models["fleet"], feeds["fleet"], available, gen),
        "changes": pages.build_changes(models["changes"], feeds["history"], available, gen),
    }


def test_render_empty_console():
    """Nothing configured at all - every page must still render."""
    feeds = {k: Feed(k, k, None) for k in
             ("tenant", "run_summary", "security", "licensing", "history", "fleet")}
    models = {k: None for k in ("identity", "security", "licensing", "fleet", "changes")}
    available = {"index": True, "identity": False, "security": False,
                 "licensing": False, "fleet": False, "changes": False}
    out = _all_pages(models, feeds, available)
    check("render: all 6 pages render with zero feeds", len(out) == 6)
    check("render: every page is a full document",
          all(h.startswith("<!DOCTYPE html>") and h.rstrip().endswith("</html>")
              for h in out.values()))
    check("render: overview says not configured", "Not configured" in out["index"])
    check("render: domain page explains itself",
          "not configured" in out["identity"].lower())
    check("render: nav present on every page",
          all('nav class="top"' in h for h in out.values()))


def test_render_escaping():
    tenant = Feed("tenant", "Identity", "x", data={
        "GeneratedUtc": iso(NOW), "TenantId": "t",
        "Organization": {"DisplayName": "<script>alert(1)</script>"},
        "UserCounts": {"Members": 1, "EnabledMembers": 1, "Guests": 0},
        "ConditionalAccess": {"Policies": [
            {"Name": "<img src=x onerror=alert(1)>", "State": "enabled",
             "IncludeUsers": ["All"], "GrantControls": ["mfa"]}], "NamedLocations": []},
        "Roles": [], "Groups": {"Total": 0}, "Applications": [],
    }, ts=NOW)
    m = model.identity_model(tenant)
    html = pages.build_identity(m, tenant, {"index": True, "identity": True}, "now")
    check("render: hostile tenant name escaped", "<script>alert(1)</script>" not in html)
    check("render: hostile policy name escaped", "<img src=x onerror" not in html)
    check("render: escaped entity present instead", "&lt;script&gt;" in html)


def test_render_full(tmp):
    """The bundled sample must build end to end and produce every page."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rc = os.system("cd %s && python3 build.py --sample --out %s > /dev/null 2>&1"
                   % (here, os.path.join(tmp, "site")))
    check("render: sample build exits clean", rc == 0)
    site = os.path.join(tmp, "site")
    names = sorted(os.listdir(site)) if os.path.isdir(site) else []
    check("render: six pages written", len(names) == 6)
    with open(os.path.join(site, "index.html"), encoding="utf-8") as fh:
        idx = fh.read()
    check("render: overview links every domain",
          all(('%s.html' % p) in idx for p in
              ("identity", "security", "licensing", "fleet", "changes")))
    check("render: freshness dot rendered", 'class="dot' in idx)
    with open(os.path.join(site, "fleet.html"), encoding="utf-8") as fh:
        check("render: fleet page has meters", 'class="meter"' in fh.read())


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_freshness()
        test_parse_ts()
        test_loading(os.path.join(tmp, "load"))
        test_identity_model()
        test_licensing_model()
        test_fleet_model(tmp)
        test_changes_model()
        test_render_empty_console()
        test_render_escaping()
        test_render_full(tmp)
    print("")
    if FAILS:
        print("RESULT: %d FAILURES" % len(FAILS))
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
