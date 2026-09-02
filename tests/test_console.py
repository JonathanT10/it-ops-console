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
    check("load: a configured-but-never-produced feed is 'missing', not an error",
          feeds["licensing"].missing and feeds["licensing"].error is None
          and feeds["licensing"].status_note == "nothing collected yet"
          and "missing.json" in feeds["licensing"].hint)
    check("load: unconfigured feed is not an error",
          not feeds["fleet"].ok and feeds["fleet"].error is None
          and feeds["fleet"].status_note == "not configured")
    check("load: timestamp picked up from payload", feeds["tenant"].state == "fresh")

    # A config written on Windows (backslash separators) must load on POSIX -
    # setup.ps1 writes exactly this shape and the console claims plain Python.
    win_cfg = os.path.join(tmp, "sources-win.ini")
    with open(win_cfg, "w") as fh:
        fh.write("[console]\nbase_path = %s\n\n[sources]\n"
                 "tenant = sub\\tenant.json\nsecurity =\nlicensing =\n"
                 "fleet =\nhistory =\nrun_summary =\n" % feeds_dir)
    os.makedirs(os.path.join(feeds_dir, "sub"), exist_ok=True)
    with open(os.path.join(feeds_dir, "sub", "tenant.json"), "w") as fh:
        json.dump(good, fh)
    _cfg2, feeds2 = load_all(win_cfg)
    check("load: windows-style backslash paths load on posix", feeds2["tenant"].ok)

    # Windows PowerShell 5.1's Set-Content -Encoding UTF8 writes a BOM, and
    # setup.ps1 writes sources.ini exactly that way. configparser must not
    # choke on it.
    bom_cfg = os.path.join(tmp, "sources-bom.ini")
    with open(bom_cfg, "wb") as fh:
        fh.write(b"\xef\xbb\xbf")
        fh.write(("[console]\nbase_path = %s\n\n[sources]\n"
                  "tenant = tenant.json\nsecurity =\nlicensing =\n"
                  "fleet =\nhistory =\nrun_summary =\n" % feeds_dir).encode("utf-8"))
    _cfg3, feeds3 = load_all(bom_cfg)
    check("load: a BOM'd config (PowerShell 5.1 Set-Content) loads", feeds3["tenant"].ok)


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
    check("licensing: no costing when tool omitted it", m["costing"] is None)


def _licensing_feed(costing=None, extra_sku=None):
    sku = {"Sku": "SPB", "Purchased": 100, "Assigned": 80, "Unassigned": 20}
    if extra_sku:
        sku.update(extra_sku)
    data = {
        "GeneratedUtc": iso(NOW), "StaleDays": 90, "LicensedUsers": 80,
        "SkuSummary": [sku],
        "ConsumptionSkus": [],
        "ReclaimCandidates": [
            {"Reason": "DISABLED ACCOUNT", "DisplayName": "Dee", "UserPrincipalName": "d@x",
             "Licenses": "SPB", "MonthlyCost": 33.0 if costing else None}],
    }
    if costing is not None:
        data["Costing"] = costing
    return Feed("licensing", "Licensing", "x", data=data, ts=NOW)


def test_licensing_costing_render():
    avail = {k: False for k in ("index", "identity", "security", "licensing", "fleet", "changes")}
    avail["licensing"] = True
    gen = "2026-01-01 00:00:00"

    # Priced: dollars lead on the page and the overview tile.
    costing = {"Currency": "$", "HasPrices": True, "PricedSkuCount": 1,
               "UnpricedSkuCount": 1, "UnpricedSkus": ["VISIOCLIENT"],
               "UnusedSeatsMonthly": 660.0, "UnusedSeatsAnnual": 7920.0,
               "ReclaimableMonthly": 33.0, "ReclaimableAnnual": 396.0}
    feed = _licensing_feed(costing, extra_sku={"MonthlyPrice": 33.0, "UnusedMonthlyCost": 660.0})
    m = model.licensing_model(feed)
    page = pages.build_licensing(m, feed, avail, gen)
    check("costing: annual unused dollars on page", "$7,920" in page)
    check("costing: reclaimable dollars on page", "$396" in page)
    check("costing: $/mo column present", "$/mo unused" in page)
    check("costing: unpriced SKU nudge names the SKU", "VISIOCLIENT" in page and "no price yet" in page)
    ov = pages.build_overview({"licensing": m, "identity": None, "security": None,
                               "fleet": None, "changes": None},
                              {"licensing": feed, "tenant": feed, "security": feed,
                               "history": feed, "fleet": feed}, avail, gen)
    check("costing: overview tile leads with dollars", "$7,920" in ov)

    # Price file present but empty: nudge to fill it, no dollar figures.
    empty = {"Currency": "$", "HasPrices": False, "PricedSkuCount": 0,
             "UnpricedSkuCount": 1, "UnpricedSkus": ["SPB"],
             "UnusedSeatsMonthly": 0, "UnusedSeatsAnnual": 0,
             "ReclaimableMonthly": 0, "ReclaimableAnnual": 0}
    fe = _licensing_feed(empty)
    pe = pages.build_licensing(model.licensing_model(fe), fe, avail, gen)
    check("costing: empty price file nudges to add prices", "add per-seat prices there and refresh" in pe.lower()
          or "type a per-seat price" in pe.lower())
    check("costing: empty price file shows no annual dollar card", "/ year</div>" not in pe or "$0" in pe)

    # No costing at all: seat-count view, first-run hint, no crash.
    fn = _licensing_feed(None)
    pn = pages.build_licensing(model.licensing_model(fn), fn, avail, gen)
    check("costing: no-costing page still renders seats", "#Unassigned" in pn or "Unassigned" in pn)
    check("costing: no-costing page hints at prices.ini", "prices.ini" in pn)


def test_next_steps():
    """Every finding carries a plain-English next step, wherever it appears."""
    # The mapping itself: every CA gap id the analysis can emit has an action,
    # and unknown kinds degrade to empty rather than exploding.
    ids = ("mfa-all-users", "block-legacy-auth", "admin-mfa", "baseline-exists",
           "breakglass-exclusion", "guest-protection", "risk-policies", "device-grants",
           "report-only-lingering", "unused-locations")
    check("next_step: every CA gap id has an action", all(pages.CA_GAP_ACTION.get(i) for i in ids))
    check("next_step: unknown gap id still gets a generic action",
          "Conditional Access" in pages.next_step("ca-gap", "no-such-check"))
    check("next_step: unknown kind is empty, not an error", pages.next_step("nonsense") == "")
    check("next_step: fleet distinguishes offline / warning / error",
          "network" in pages.next_step("fleet", "offline")
          and "Restock" in pages.next_step("fleet", "warning")
          and "panel" in pages.next_step("fleet", "error"))

    avail = {k: True for k in ("index", "identity", "security", "licensing", "fleet", "changes")}
    gen = "2026-01-01 00:00:00"

    # Identity: a failing gap carries its id-specific action; a passing one carries none.
    tenant = Feed("tenant", "Identity", "x", data={
        "GeneratedUtc": iso(NOW), "Organization": {"DisplayName": "T"},
        "UserCounts": {"Members": 1, "EnabledMembers": 1, "Guests": 0},
        "ConditionalAccess": {"Policies": [], "NamedLocations": []},
        "CaGaps": [
            {"Id": "breakglass-exclusion", "Title": "Break-glass accounts excluded",
             "Severity": "warning", "Result": "fail", "Detail": "no exclusions"},
            {"Id": "mfa-all-users", "Title": "MFA required for all users",
             "Severity": "critical", "Result": "pass", "Detail": "satisfied"}],
        "Roles": [], "Groups": {}, "AuthMethods": [], "UserSettings": {},
        "Applications": [], "Intune": {"Available": False}}, ts=NOW)
    ident = model.identity_model(tenant)
    page = pages.build_identity(ident, tenant, avail, gen)
    check("next_step: failing gap shows its specific action",
          "break-glass (emergency) accounts" in page)
    check("next_step: passing gap shows no action",
          "requires MFA for all users on all cloud apps" not in page)

    # Overview: the Needs-a-human item for that gap carries the same action line,
    # and an admin-without-MFA item carries the MFA action.
    sec = Feed("security", "Security", "x", data={
        "GeneratedUtc": iso(NOW), "StaleDays": 90,
        "AdminsWithoutMfa": [{"DisplayName": "Root Admin", "UserPrincipalName": "r@x",
                              "Roles": "Global Administrator"}],
        "RoleSummary": [], "StaleMembers": [], "Guests": {"Total": 0},
        "LegacyAuth": {"Available": False}, "NeedsAttention": []}, ts=NOW)
    secm = model.security_model(sec, ident)
    ov = pages.build_overview(
        {"identity": ident, "security": secm, "licensing": None, "fleet": None, "changes": None},
        {"tenant": tenant, "security": sec, "licensing": sec, "history": sec, "fleet": sec},
        avail, gen)
    check("next_step: overview items carry an action line", 'class="act"' in ov)
    check("next_step: overview admin-without-MFA carries the MFA action",
          "aka.ms/mfasetup" in ov)
    check("next_step: overview CA gap carries its specific action",
          "break-glass (emergency) accounts" in ov)

    # Security page notes carry their actions.
    sp = pages.build_security(secm, sec, avail, gen)
    check("next_step: security admins note carries the action", "aka.ms/mfasetup" in sp)
    check("next_step: security stale note carries the action", "disable the account" in sp)

    # Fleet: the Needs-attention table gains a What-to-do column with the
    # status-specific line (an offline device here).
    old = iso(NOW - timedelta(days=5))
    fleet = Feed("fleet", "Print fleet", "x", data={"devices": [{
        "device": {"id": 1, "ip": "10.0.0.9", "name": "Lobby", "model": "M", "serial": "S",
                   "first_seen": old, "last_seen": old},
        "snapshot": {"id": 1, "device_id": 1, "ts": old, "reachable": 0, "status": "offline",
                     "detail": "No SNMP response", "uptime_seconds": None, "page_count": None},
        "supplies": [], "volumes": []}]}, ts=NOW)
    fm = model.fleet_model(fleet)
    fp = pages.build_fleet(fm, fleet, avail, gen)
    check("next_step: fleet table has a What-to-do column", "<th>What to do</th>" in fp)
    check("next_step: offline device gets the power/network line",
          "powered on and on the network" in fp)


def _sec_snap(ts, mfa, admins, stale, guests=10, legacy=None):
    d = {"GeneratedUtc": ts, "StaleDays": 90,
         "MfaCoverage": {"CoveragePercent": mfa} if mfa is not None else None,
         "AdminsWithoutMfa": [{"DisplayName": "a%d" % i} for i in range(admins)],
         "StaleMembers": [{"DisplayName": "s%d" % i} for i in range(stale)],
         "RoleSummary": [], "Guests": {"Total": guests}, "NeedsAttention": [],
         "LegacyAuth": {"Available": legacy is not None,
                        "Summary": [{"ClientApp": "IMAP4", "SignIns": legacy}] if legacy is not None else []}}
    return d


def _lic_snap(ts, unassigned, reclaim, users=100, costing=None):
    d = {"GeneratedUtc": ts, "StaleDays": 90, "LicensedUsers": users,
         "SkuSummary": [{"Sku": "SPB", "Purchased": 200, "Assigned": 200 - unassigned,
                         "Unassigned": unassigned}],
         "ConsumptionSkus": [],
         "ReclaimCandidates": [{"Reason": "DISABLED ACCOUNT", "DisplayName": "d%d" % i}
                               for i in range(reclaim)]}
    if costing:
        d["Costing"] = costing
    return d


def test_trends_model():
    day = lambda n, h=12: iso(NOW - timedelta(days=n, hours=-h) - timedelta(hours=12))  # noqa: E731
    # Three days of history plus the current snapshot; day 0 appears TWICE in
    # history (an early run and a later one) to prove the per-day collapse.
    hist = Feed("security_history", "Security trend", "h", data=[
        _sec_snap(day(3), 80.0, 5, 9, legacy=40),
        _sec_snap(day(2), 85.0, 4, 8, legacy=30),
        _sec_snap(day(1), 88.0, 4, 6, legacy=20),                          # stale flat from here
        _sec_snap(iso(NOW - timedelta(hours=6)), 89.0, 4, 6, legacy=12),   # earlier today
    ], ts=NOW)
    cur = Feed("security", "Security", "c", data=_sec_snap(iso(NOW), 91.0, 3, 6, legacy=10), ts=NOW)
    t = model.trends_model(hist, cur, None, None)
    s = t["security"]
    check("trends: one point per day, latest wins (4 days from 5 snapshots)", s["points"] == 4)
    bk = s["by_key"]
    check("trends: today's point is the later run", bk["mfa_percent"]["current"] == 91.0)
    check("trends: MFA up reads as good", bk["mfa_percent"]["tone"] == "good"
          and abs(bk["mfa_percent"]["delta"] - 3.0) < 1e-9)
    check("trends: admins-without-MFA down reads as good",
          bk["admins_no_mfa"]["tone"] == "good" and bk["admins_no_mfa"]["delta"] == -1)
    check("trends: flat metric is muted", bk["stale"]["tone"] == "muted" and bk["stale"]["delta"] == 0)
    check("trends: neutral metric never judged", bk["guests"]["tone"] == "muted")
    check("trends: legacy sign-ins summed and trending down",
          bk["legacy_signins"]["current"] == 10 and bk["legacy_signins"]["tone"] == "good")
    check("trends: licensing half is None without data", t["licensing"] is None)

    # A metric getting WORSE reads as warning.
    worse = model.trends_model(
        Feed("security_history", "Security trend", "h", data=[_sec_snap(day(1), 90.0, 2, 3)], ts=NOW),
        Feed("security", "Security", "c", data=_sec_snap(iso(NOW), 85.0, 4, 3), ts=NOW), None, None)
    wb = worse["security"]["by_key"]
    check("trends: MFA down reads as warning", wb["mfa_percent"]["tone"] == "warning")
    check("trends: admins up reads as warning", wb["admins_no_mfa"]["tone"] == "warning")

    # A metric absent from older snapshots charts only its trailing run, and a
    # single point has no delta.
    partial = model.trends_model(
        Feed("security_history", "Security trend", "h", data=[
            _sec_snap(day(2), None, 2, 3), _sec_snap(day(1), 70.0, 2, 3)], ts=NOW),
        Feed("security", "Security", "c", data=_sec_snap(iso(NOW), 72.0, 2, 3), ts=NOW), None, None)
    pb = partial["security"]["by_key"]
    check("trends: metric missing from old snapshots charts its trailing run only",
          pb["mfa_percent"]["values"] == [70.0, 72.0])
    single = model.trends_model(None, cur, None, None)
    check("trends: current alone is one point with no delta",
          single["security"]["points"] == 1 and single["security"]["by_key"]["mfa_percent"]["delta"] is None)

    # Licensing: dollar metrics ride in only when priced, and carry the currency.
    cost1 = {"Currency": "$", "HasPrices": True, "UnusedSeatsMonthly": 500.0, "ReclaimableMonthly": 66.0}
    cost2 = {"Currency": "$", "HasPrices": True, "UnusedSeatsMonthly": 420.0, "ReclaimableMonthly": 33.0}
    lt = model.trends_model(None, None,
                            Feed("licensing_history", "Licensing trend", "h",
                                 data=[_lic_snap(day(1), 20, 3, costing=cost1)], ts=NOW),
                            Feed("licensing", "Licensing", "c",
                                 data=_lic_snap(iso(NOW), 16, 2, costing=cost2), ts=NOW))
    lb = lt["licensing"]["by_key"]
    check("trends: unused seats down reads as good", lb["unassigned"]["tone"] == "good"
          and lb["unassigned"]["delta"] == -4)
    check("trends: dollar metric present with currency and falling",
          lb["unused_monthly"]["unit"] == "$" and lb["unused_monthly"]["delta"] == -80.0
          and lb["unused_monthly"]["tone"] == "good")
    unpriced = model.trends_model(None, None, None,
                                  Feed("licensing", "Licensing", "c", data=_lic_snap(iso(NOW), 16, 2), ts=NOW))
    check("trends: no dollar metrics without prices",
          "unused_monthly" not in unpriced["licensing"]["by_key"])


def test_trends_render():
    avail = {k: True for k in ("index", "identity", "security", "licensing", "fleet", "changes")}
    gen = "2026-01-01 00:00:00"
    d1 = iso(NOW - timedelta(days=1)); d0 = iso(NOW)
    hist = Feed("security_history", "Security trend", "h", data=[_sec_snap(d1, 88.0, 3, 5)], ts=NOW)
    cur = Feed("security", "Security", "c", data=_sec_snap(d0, 91.0, 2, 5), ts=NOW)
    t = model.trends_model(hist, cur, None, None)
    secm = model.security_model(cur, None)

    page = pages.build_security(secm, cur, avail, gen, trend=t["security"])
    check("trends render: security page has Posture over time", "Posture over time" in page)
    check("trends render: MFA card shows value and improving delta",
          "91%" in page and "+3 pts since last" in page)
    check("trends render: admins card shows falling count", "−1 since last" in page)
    check("trends render: sparklines drawn", "<polyline" in page)

    # One point: the calm note, no chart.
    t1 = model.trends_model(None, cur, None, None)
    p1 = pages.build_security(secm, cur, avail, gen, trend=t1["security"])
    check("trends render: single point says trends appear after the second refresh",
          "after the second refresh" in p1 and "<polyline" not in p1)

    # No trend at all (feed not configured): no section, no crash.
    p0 = pages.build_security(secm, cur, avail, gen, trend=None)
    check("trends render: no trend data renders no section", "Posture over time" not in p0)

    # Overview tile: sparkline + delta with two points, nothing with one.
    ov = pages.build_overview(
        {"security": secm, "identity": None, "licensing": None, "fleet": None, "changes": None,
         "trends": t},
        {"security": cur, "tenant": cur, "licensing": cur, "history": cur, "fleet": cur}, avail, gen)
    check("trends render: security tile carries sparkline + delta",
          'class="spark"' in ov and "+3 pts since last" in ov)
    ov1 = pages.build_overview(
        {"security": secm, "identity": None, "licensing": None, "fleet": None, "changes": None,
         "trends": t1},
        {"security": cur, "tenant": cur, "licensing": cur, "history": cur, "fleet": cur}, avail, gen)
    check("trends render: single-point tile has no sparkline", 'class="spark"' not in ov1)

    # Licensing page: Waste over time with the dollar line when priced.
    c1 = {"Currency": "$", "HasPrices": True, "UnusedSeatsMonthly": 500.0, "ReclaimableMonthly": 66.0,
          "UnusedSeatsAnnual": 6000.0, "ReclaimableAnnual": 792.0, "PricedSkuCount": 1,
          "UnpricedSkuCount": 0, "UnpricedSkus": []}
    c2 = dict(c1, UnusedSeatsMonthly=420.0, UnusedSeatsAnnual=5040.0)
    lh = Feed("licensing_history", "Licensing trend", "h", data=[_lic_snap(d1, 20, 3, costing=c1)], ts=NOW)
    lc = Feed("licensing", "Licensing", "c", data=_lic_snap(d0, 16, 2, costing=c2), ts=NOW)
    lt = model.trends_model(None, None, lh, lc)
    lp = pages.build_licensing(model.licensing_model(lc), lc, avail, gen, trend=lt["licensing"])
    check("trends render: licensing page has Waste over time", "Waste over time" in lp)
    check("trends render: dollar trend card shows falling spend", "−$80 since last" in lp)


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

    # A configured-but-never-collected feed reads as a normal state with a
    # turn-it-on hint - never as a raw "file not found" error.
    tmp2 = tempfile.mkdtemp(prefix="console-missing-")
    fdir = os.path.join(tmp2, "feeds2"); os.makedirs(fdir, exist_ok=True)
    cfg2 = os.path.join(tmp2, "sources2.ini")
    with open(cfg2, "w") as fh:
        fh.write("[console]\nbase_path = %s\n\n[sources]\n"
                 "tenant =\nsecurity =\nlicensing =\nrun_summary =\nhistory =\n"
                 "fleet = fleet.db\n" % fdir)
    _c2, f2 = load_all(cfg2)
    page = pages.build_fleet(None, f2["fleet"], {"index": True}, "now")
    check("render: never-collected fleet page uses calm words",
          "Nothing collected here yet." in page and "Printers are optional" in page)
    check("render: never-collected fleet page has no raw file-not-found error",
          "file not found" not in page.lower())
    check("render: the hint still names the path for whoever debugs",
          "fleet.db" in page)

    # Release bundles stamp a VERSION; the footer must carry it - and a plain
    # git checkout must not invent one.
    from console import render as _r
    try:
        _r.SUITE_VERSION = "9.9.9-test"
        page_v = pages.build_fleet(None, f2["fleet"], {"index": True}, "now")
        check("render: suite version appears in the footer when set",
              "suite v9.9.9-test" in page_v)
    finally:
        _r.SUITE_VERSION = ""
    page_nv = pages.build_fleet(None, f2["fleet"], {"index": True}, "now")
    check("render: no suite version invented without a VERSION file",
          "suite v" not in page_nv)


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
        test_licensing_costing_render()
        test_next_steps()
        test_trends_model()
        test_trends_render()
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
