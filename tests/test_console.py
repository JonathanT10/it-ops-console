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

HERE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE_DIR)
sys.path.insert(0, ROOT_DIR)

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
    ovm = {"identity": ident, "security": secm, "licensing": None, "fleet": None, "changes": None}
    ovf = {"tenant": tenant, "security": sec, "licensing": sec, "history": sec, "fleet": sec}
    # The overview list is the alert rules now, so it needs their verdict.
    ovm["fired"] = A.evaluate(A.default_config(), ovm, ovf, now=NOW)
    ov = pages.build_overview(ovm, ovf, avail, gen)
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
    # Anchor "today" at noon UTC so the intra-day fixtures below (an earlier run
    # and a later run on the same day) can never straddle midnight - a test that
    # passes at 15:00 and fails at 00:04 is a test of the clock, not the code.
    base = NOW.replace(hour=12, minute=0, second=0, microsecond=0)
    day = lambda n: iso(base - timedelta(days=n))  # noqa: E731
    # Three days of history plus the current snapshot; today appears TWICE
    # (an early run and a later one) to prove the per-day collapse.
    hist = Feed("security_history", "Security trend", "h", data=[
        _sec_snap(day(3), 80.0, 5, 9, legacy=40),
        _sec_snap(day(2), 85.0, 4, 8, legacy=30),
        _sec_snap(day(1), 88.0, 4, 6, legacy=20),                           # stale flat from here
        _sec_snap(iso(base - timedelta(hours=6)), 89.0, 4, 6, legacy=12),   # today, 06:00
    ], ts=NOW)
    cur = Feed("security", "Security", "c", data=_sec_snap(iso(base), 91.0, 3, 6, legacy=10), ts=NOW)
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


def _discovery_feed(**over):
    """The document the printer collector writes beside fleet.db."""
    d = {
        "GeneratedUtc": iso(NOW), "RescanHours": 24, "MaxAddresses": 1024,
        "LastScanUtc": iso(NOW - timedelta(hours=2)), "ScannedThisRun": True,
        "Ranges": [
            {"Name": "Office", "Spec": "10.0.10.0/24", "Addresses": 254,
             "LastScanUtc": iso(NOW - timedelta(hours=2)), "Found": 3, "Problem": None},
            {"Name": "New VLAN", "Spec": "10.0.40.0/24", "Addresses": 254,
             "LastScanUtc": None, "Found": 0, "Problem": None},
            {"Name": "Old VLAN", "Spec": "10.0.0.0/8", "Addresses": 0,
             "LastScanUtc": iso(NOW - timedelta(hours=2)), "Found": 0,
             "Problem": "'10.0.0.0/8' is 16,777,214 addresses, and the limit is 1,024."},
        ],
        "Ignored": ["10.0.10.99"],
        "FoundThisScan": [{"Ip": "10.0.10.34", "Name": "Lobby MFP", "Range": "Office"}],
        "NonPrinters": 6,
        "Problems": ["Old VLAN: '10.0.0.0/8' is 16,777,214 addresses, and the limit is 1,024."],
    }
    d.update(over)
    return Feed("fleet_discovery", "Printer discovery", "fleet-discovery.json", data=d, ts=NOW)


def test_sample_does_not_rot():
    """The bundled sample must look the same in a year as it does today.

    Its fleet database is a snapshot of one moment, and "offline" is decided by
    comparing last_seen against the clock. Rendered against today's clock, every
    printer in the demo turns offline a few days after the sample was made - and
    the rules that depend on those statuses stop firing on a DATE rather than on
    a change. This caught exactly that, two days after the sample was made.
    """
    cfg, feeds = load_all(os.path.join(ROOT_DIR, "sample", "sources.ini"))
    as_of = model.fleet_as_of(feeds["fleet"])
    check("sample: the fleet feed knows its own moment", as_of is not None)

    m = model.fleet_model(feeds["fleet"], now=as_of)
    kinds = {d["status"] for d in m["devices"]}
    check("sample: rendered as of its own moment, the demo still has live printers",
          "ok" in kinds or "warning" in kinds)
    check("sample: and it is not all one status",
          len(kinds) > 1 and m["online"] > 0)

    # A year from now must render exactly the same as today.
    later = model.fleet_model(feeds["fleet"], now=as_of + timedelta(days=365))
    check("sample: pinned to its moment it does not drift with the calendar",
          [d["status"] for d in m["devices"]] == [d["status"] for d in
              model.fleet_model(feeds["fleet"], now=as_of)["devices"]])
    check("sample: and the wall clock is what would have rotted it",
          all(d["status"] == "offline" for d in later["devices"]))

    # A real feed still uses the real clock - this is a sample-only courtesy.
    live = model.fleet_model(feeds["fleet"])
    check("sample: a real feed is still judged against the real clock",
          all(d["status"] == "offline" for d in live["devices"]))


def test_discovery_model():
    m = model.discovery_model(_discovery_feed())
    by = {p["name"]: p for p in m["places"]}
    check("discovery: every configured place gets a row", len(m["places"]) == 3)
    check("discovery: a scanned place reports what it found",
          by["Office"]["found"] == 3 and by["Office"]["addresses"] == 254
          and by["Office"]["last_scan"] is not None)
    check("discovery: a place added since the last scan says so rather than reading as empty",
          by["New VLAN"]["last_scan"] is None
          and by["New VLAN"]["last_scan_text"] == "not looked at yet")
    check("discovery: a place that could not be scanned carries the reason",
          "16,777,214" in by["Old VLAN"]["problem"])
    check("discovery: the reason is repeated where a person will see it",
          len(m["problems"]) == 1 and "Old VLAN" in m["problems"][0])
    check("discovery: what the last scan turned up is kept",
          m["found_this_scan"] == [{"ip": "10.0.10.34", "name": "Lobby MFP", "range": "Office"}])
    check("discovery: things that answered but were not printers are counted, not listed",
          m["non_printers"] == 6)
    check("discovery: the settings the page edits come through",
          m["rescan_hours"] == 24 and m["ignored"] == ["10.0.10.99"]
          and m["max_addresses"] == 1024)
    check("discovery: a place that could not be scanned counts as no addresses",
          m["addresses_total"] == 508)

    # A collector that has never scanned, and one that has never run at all.
    quiet = model.discovery_model(_discovery_feed(ScannedThisRun=False, FoundThisScan=[],
                                                  LastScanUtc=None))
    check("discovery: a run that did not scan is not a run that found nothing",
          quiet["scanned_this_run"] is False and quiet["last_scan"] is None
          and quiet["found_this_scan"] == [])
    check("discovery: no discovery file at all is a normal state, not an error",
          model.discovery_model(None) is None
          and model.discovery_model(Feed("fleet_discovery", "x", "p",
                                         error="no such file")) is None)

    # The console reads keys the collector writes; the bundled sample is the
    # only place those two agree in this repo, so it has to stay in step.
    with open(os.path.join(ROOT_DIR, "sample", "feeds",
                           "fleet-discovery.json"), encoding="utf-8") as fh:
        sample = model.discovery_model(Feed("fleet_discovery", "x", "p",
                                            data=json.load(fh), ts=NOW))
    check("discovery: the bundled sample fills in every field the page uses",
          sample is not None and sample["places"] and sample["ignored"]
          and sample["rescan_hours"] and sample["max_addresses"]
          and sample["found_this_scan"] and sample["non_printers"]
          and sample["problems"] and sample["last_scan"] is not None
          and all(p["spec"] and p["name"] for p in sample["places"]))


def test_where_we_look_page():
    """The Print fleet tab's editor: it has to show what the collector did,
    and produce settings that say the same thing."""
    d = model.discovery_model(_discovery_feed())
    feed = Feed("fleet", "Print fleet", None)
    html = pages.build_fleet(None, feed, {"index": True, "fleet": True}, "now", discovery=d)
    check("where we look: the section is on the page even with no printers yet",
          "Where we look" in html and 'data-rowsec="ranges"' in html)
    check("where we look: every place has a row with its name and its address",
          html.count('<input type="text" data-row="name"') == len(d["places"]) + 2
          and 'value="10.0.10.0/24"' in html and 'value="Old VLAN"' in html)
    check("where we look: a row says what the last look found",
          "254 addresses" in html and "3 found" in html)
    check("where we look: a place not looked at yet says so on its row",
          "not looked at yet" in html)
    check("where we look: what could not be scanned is said twice - once loudly",
          html.count("16,777,214") >= 2 and 'class="banner warning"' in html)
    check("where we look: the newly found printer is named",
          "Lobby MFP" in html and "10.0.10.34" in html)
    check("where we look: the scan is described before it is offered",
          "one SNMP request goes to every address" in html and "1,024 addresses is refused" in html)
    check("where we look: the two settings are editable",
          'data-sec="discovery" data-key="rescan_hours"' in html
          and 'data-sec="discovery" data-key="ignore"' in html
          and 'value="10.0.10.99"' in html)
    check("where we look: it says what to do with what you changed",
          "Save settings" in html and "Apply Settings" in html)
    check("where we look: exactly one script, and it is the console's own",
          html.count("<script>") == 1 and "PLACE_PATTERN" in html)
    check("where we look: nothing about SNMP credentials is on the page",
          "public" not in html and "[snmp]" not in html)

    # With no discovery file at all the section is still offered, with defaults.
    plain = pages.build_fleet(None, feed, {"index": True, "fleet": True}, "now")
    check("where we look: offered before discovery has ever run",
          "Where we look" in plain and 'data-rowsec="ranges"' in plain
          and 'data-key="rescan_hours" value="24"' in plain)
    check("where we look: and nothing is claimed to have been found",
          "not looked at yet" not in plain and "16,777,214" not in plain)

    # A hostile name from the config file must not become markup.
    nasty = model.discovery_model(_discovery_feed(Ranges=[
        {"Name": "<script>alert(1)</script>", "Spec": "10.0.10.0/24", "Addresses": 1,
         "LastScanUtc": None, "Found": 0, "Problem": "<img src=x onerror=alert(1)>"}]))
    bad = pages.build_fleet(None, feed, {"index": True, "fleet": True}, "now", discovery=nasty)
    check("where we look: a name out of the config file is escaped",
          "<script>alert(1)</script>" not in bad and "<img src=x onerror" not in bad
          and "&lt;script&gt;" in bad)


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

def _refresh_feed(**over):
    base = {
        "GeneratedUtc": iso(NOW - timedelta(hours=3)), "Scheduled": True, "Final": True, "Ok": True,
        "Message": "Everything ran.", "SignIn": {"Mode": "user", "Ok": True, "Detail": "", "Dropped": []},
        "Steps": [], "Schedule": {"Mode": "while-signed-in", "Time": "07:00", "RunAs": "X\\sam"},
        "KeepSignedIn": True, "Certificate": None,
    }
    base.update(over)
    return Feed("refresh_status", "Automatic refresh", "x", data=base, ts=NOW - timedelta(hours=3))


def test_refresh_model():
    """Footer note always states the schedule; banners only when a person must act."""
    check("refresh: no feed -> no model", model.refresh_model(None) is None)
    check("refresh: unconfigured feed -> no model",
          model.refresh_model(Feed("refresh_status", "Automatic refresh", None)) is None)

    ok = model.refresh_model(_refresh_feed())
    check("refresh: happy path has no banner", ok["banners"] == [])
    check("refresh: note states the schedule", "every day at 07:00 while you're signed in" in ok["note"])
    check("refresh: note says it stays signed in when that is the choice",
          "stays signed in to Microsoft Graph" in ok["note"])
    no_keep = model.refresh_model(_refresh_feed(KeepSignedIn=False))
    check("refresh: note omits stays-signed-in when it does not apply",
          "stays signed in" not in no_keep["note"])
    off = model.refresh_model(_refresh_feed(Schedule={"Mode": "off", "Time": "", "RunAs": ""}))
    check("refresh: off mode names the desktop shortcut", "Refresh IT Ops Data" in off["note"])

    failed = model.refresh_model(_refresh_feed(
        Ok=False, SignIn={"Mode": "none", "Ok": False,
                          "Detail": "Nobody finished the Microsoft sign-in window within 5 minutes.",
                          "Dropped": ["Nobody finished the Microsoft sign-in window within 5 minutes."]}))
    check("refresh: failed sign-in -> one banner", len(failed["banners"]) == 1)
    check("refresh: failed sign-in banner says what to do",
          "couldn't sign in" in failed["banners"][0]["text"]
          and 'Double-click "Refresh IT Ops Data"' in failed["banners"][0]["text"])
    check("refresh: failed sign-in banner carries the detail",
          "Nobody finished" in failed["banners"][0]["detail"])

    # A desktop click that could not sign in is not the schedule's problem.
    desk = model.refresh_model(_refresh_feed(Scheduled=False, Ok=False,
                                             SignIn={"Mode": "none", "Ok": False, "Detail": "x", "Dropped": ["x"]}))
    check("refresh: an unscheduled run never raises a banner", desk["banners"] == [])

    dropped = model.refresh_model(_refresh_feed(SignIn={
        "Mode": "user", "Ok": True, "Detail": "Signed in with read-only access",
        "Dropped": ["Signing in as the registered app failed: AADSTS700027 bad key."]}))
    check("refresh: a passed-over rung is reported even though the run worked",
          len(dropped["banners"]) == 1 and "couldn't use its usual sign-in" in dropped["banners"][0]["text"]
          and "AADSTS700027" in dropped["banners"][0]["text"])

    soon = model.refresh_model(_refresh_feed(
        Schedule={"Mode": "unattended", "Time": "06:30", "RunAs": "SYSTEM"}, KeepSignedIn=False,
        SignIn={"Mode": "app", "Ok": True, "Detail": "", "Dropped": []},
        Certificate={"Thumbprint": "AB", "Present": True, "Expires": "2026-09-14", "DaysLeft": 12}))
    check("refresh: unattended note names the app route and the expiry",
          "as the registered app" in soon["note"] and "expires 2026-09-14" in soon["note"])
    check("refresh: certificate within 30 days -> warning banner",
          len(soon["banners"]) == 1 and soon["banners"][0]["tone"] == "warning"
          and "expires in 12 days" in soon["banners"][0]["text"])
    far = model.refresh_model(_refresh_feed(
        Schedule={"Mode": "unattended", "Time": "06:30", "RunAs": "SYSTEM"},
        SignIn={"Mode": "app", "Ok": True, "Detail": "", "Dropped": []},
        Certificate={"Thumbprint": "AB", "Present": True, "Expires": "2028-09-01", "DaysLeft": 729}))
    check("refresh: certificate far off -> no banner", far["banners"] == [])
    gone = model.refresh_model(_refresh_feed(
        Ok=False, Schedule={"Mode": "unattended", "Time": "06:30", "RunAs": "SYSTEM"},
        SignIn={"Mode": "none", "Ok": False, "Detail": "The automatic-refresh certificate expired on 2026-08-20.",
                "Dropped": ["The automatic-refresh certificate expired on 2026-08-20 - re-run setup."]},
        Certificate={"Thumbprint": "AB", "Present": True, "Expires": "2026-08-20", "DaysLeft": -13}))
    check("refresh: expired certificate -> sign-in banner plus a serious certificate banner",
          len(gone["banners"]) == 2 and gone["banners"][1]["tone"] == "serious"
          and "expired on 2026-08-20" in gone["banners"][1]["text"])


def test_refresh_render():
    from console import render as _r
    feeds = {k: Feed(k, k, None) for k in
             ("tenant", "run_summary", "security", "licensing", "history", "fleet")}
    models = {k: None for k in ("identity", "security", "licensing", "fleet",
                                "changes", "discovery")}
    available = {"index": True, "identity": False, "security": False,
                 "licensing": False, "fleet": False, "changes": False}

    models["refresh"] = model.refresh_model(_refresh_feed())
    page = pages.build_overview(models, feeds, available, "now")
    check("refresh render: happy path shows no banner", 'class="banner' not in page)

    models["refresh"] = model.refresh_model(_refresh_feed(
        Ok=False, SignIn={"Mode": "none", "Ok": False, "Detail": "<b>raw</b> detail",
                          "Dropped": ["<b>raw</b> detail"]}))
    page = pages.build_overview(models, feeds, available, "now")
    check("refresh render: failed sign-in banner on the overview",
          'class="banner warning"' in page and "couldn&#x27;t sign in" in page)
    check("refresh render: banner detail is escaped", "<b>raw</b>" not in page and "&lt;b&gt;raw" in page)
    check("refresh render: banner is placed before the tiles",
          page.index('class="banner') < page.index('class="tiles"'))

    # A refresh_status that has gone old must not be listed as a stale feed -
    # every real feed is old too and already says so.
    old_feed = Feed("refresh_status", "Automatic refresh", "x", data=_refresh_feed().data,
                    ts=NOW - timedelta(days=20))
    feeds2 = dict(feeds); feeds2["refresh_status"] = old_feed
    page = pages.build_overview(models, feeds2, available, "now")
    check("refresh render: old refresh_status not listed under Stale data",
          "Automatic refresh</span>" not in page)

    try:
        _r.REFRESH_NOTE = "Automatic refresh: every day at 07:00 while you're signed in."
        page = pages.build_fleet(None, feeds["fleet"], {"index": True}, "now")
        check("refresh render: footer note appears on every page when set",
              'class="refresh-note">Automatic refresh: every day at 07:00' in page)
    finally:
        _r.REFRESH_NOTE = ""
    page = pages.build_fleet(None, feeds["fleet"], {"index": True}, "now")
    check("refresh render: no footer note without a refresh feed", 'class="refresh-note"' not in page)


# --------------------------------------------------------------------------- #
# Alerts (console/alerts.py)
# --------------------------------------------------------------------------- #

from console import alerts as A  # noqa: E402


def _sample_models():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg, feeds = load_all(os.path.join(here, "sample", "sources.ini"))
    models = {}
    models["identity"] = model.identity_model(feeds["tenant"], feeds.get("run_summary"))
    models["security"] = model.security_model(feeds["security"], models["identity"])
    models["licensing"] = model.licensing_model(feeds["licensing"])
    # As of the sample's own moment, not today's - otherwise these rules stop
    # firing on a date rather than on a change.
    models["fleet"] = model.fleet_model(feeds["fleet"], now=model.fleet_as_of(feeds["fleet"]))
    models["changes"] = model.changes_model(feeds["history"], feeds.get("run_summary"))
    models["refresh"] = model.refresh_model(feeds.get("refresh_status"))
    return models, feeds


def _by_rule(fired, rule_id):
    return [a for a in fired if a["rule"] == rule_id]


def test_alerts_catalog_and_config(tmp):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "alerts.example.ini"), encoding="utf-8") as fh:
        on_disk = fh.read()
    check("alerts: alerts.example.ini is exactly what the catalog renders (no drift)",
          on_disk == A.render_example_ini())
    cfg = A.load_config(os.path.join(here, "alerts.example.ini"))
    check("alerts: the example loads with no problems", cfg["problems"] == [] and cfg["exists"])
    check("alerts: every rule has an ini line", all(("\n%s = " % r.id) in on_disk for r in A.CATALOG))
    check("alerts: every rule id is unique within its tab", len({r.key for r in A.CATALOG}) == len(A.CATALOG))
    check("alerts: every rule has label, help and a known severity",
          all(r.label and r.help and r.severity in A.SEVERITIES for r in A.CATALOG))
    check("alerts: defaults come through", cfg["rules"]["security.mfa_coverage_below"] == 90
          and cfg["rules"]["changes.app_registrations"] is False and cfg["send"]["when"] == "changes")

    missing = A.load_config(os.path.join(tmp, "nope.ini"))
    check("alerts: a missing ini is all defaults and not configured",
          not missing["exists"] and missing["rules"]["security.admin_without_mfa"] is True
          and not A.channels_configured(missing, env={})["any"])

    bad = os.path.join(tmp, "bad.ini")
    with open(bad, "w") as fh:
        fh.write("[send]\nwhen = sometimes\ndigest_day = Funday\n[teams]\nwebhook = https://x/y\n"
                 "[security]\nnotify = no\nadmin_without_mfa = maybe\nmfa_coverage_below = lots\n"
                 "stale_accounts_above =\nno_such_rule = yes\n[kitchen]\nsink = yes\n"
                 "[email]\nsmtp_server = relay\nfrom = a@b\nto = c@d; e@f, g@h\nport = x\n")
    b = A.load_config(bad)
    probs = "\n".join(b["problems"])
    check("alerts: unknown rule reported, not fatal", "no_such_rule" in probs)
    check("alerts: unknown section reported", "[kitchen]" in probs)
    check("alerts: bad when/digest_day reported and defaulted",
          "not 'changes' or 'every-refresh'" in probs and b["send"]["when"] == "changes"
          and "not a weekday" in probs and b["send"]["digest_day"] == "")
    check("alerts: bad switch value reported, default kept",
          "admin_without_mfa = 'maybe'" in probs and b["rules"]["security.admin_without_mfa"] is True)
    check("alerts: bad number reported, default kept",
          "mfa_coverage_below = 'lots'" in probs and b["rules"]["security.mfa_coverage_below"] == 90)
    check("alerts: blank number means off", b["rules"]["security.stale_accounts_above"] == 0)
    check("alerts: tab notify=no read", b["tabs"]["security"]["notify"] is False)
    check("alerts: email recipients split on ; and ,", b["email"]["to"] == ["c@d", "e@f", "g@h"])
    check("alerts: bad port reported, 25 kept", "port = 'x'" in probs and b["email"]["port"] == 25)
    ch = A.channels_configured(b, env={})
    check("alerts: both channels detected", ch["teams"] and ch["email"] and ch["any"])
    env_only = A.load_config(os.path.join(tmp, "nope.ini"))
    check("alerts: webhook from the environment counts",
          A.channels_configured(env_only, env={"ITOPS_TEAMS_WEBHOOK": "https://x"})["teams"])
    check("alerts: setting text reads plainly",
          A.rule_setting_text(A.RULES_BY_KEY["security.mfa_coverage_below"], 90) == "90%"
          and A.rule_setting_text(A.RULES_BY_KEY["licensing.unused_monthly_cost_above"], 0) == "off"
          and A.rule_setting_text(A.RULES_BY_KEY["licensing.unused_monthly_cost_above"], 500) == "$500"
          and A.rule_setting_text(A.RULES_BY_KEY["security.admin_without_mfa"], True) == "on")


def test_alerts_rules():
    models, feeds = _sample_models()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = A.load_config(os.path.join(here, "alerts.example.ini"))
    now = parse_ts("2026-09-02T12:00:00Z")
    fired = A.evaluate(cfg, models, feeds, now=now)
    check("rules: every alert has the common shape",
          all(set(("key", "tab", "rule", "severity", "title", "detail", "action", "transient", "tab_label")) <= set(a)
              for a in fired))
    check("rules: sorted critical first", [a["severity"] for a in fired] == sorted(
        [a["severity"] for a in fired], key=lambda s: A.SEVERITY_RANK[s]))
    check("rules: admins without MFA -> one alert each, keyed by UPN",
          len(_by_rule(fired, "admin_without_mfa")) == 2
          and all("/admin_without_mfa/" in a["key"] and "@" in a["key"] for a in _by_rule(fired, "admin_without_mfa")))
    check("rules: the alert carries the console's next step",
          _by_rule(fired, "admin_without_mfa")[0]["action"] == pages.next_step("admin-no-mfa"))
    exp = _by_rule(fired, "app_credential_expired")
    check("rules: expired app credential is critical and names the app",
          len(exp) == 1 and exp[0]["severity"] == "critical" and "HR Sync" in exp[0]["title"])
    soon = _by_rule(fired, "app_credential_expiring_days")
    check("rules: expiring credential says how many days", len(soon) == 1 and "expires in 16 days" in soon[0]["title"])
    check("rules: no critical CA gap in the sample; both warning gaps are found",
          not _by_rule(fired, "ca_gap_critical") and len(_by_rule(fired, "ca_gap_warning")) == 2
          and all(a["action"] for a in _by_rule(fired, "ca_gap_warning")))
    check("rules: unused seats 26 > 25 fires", len(_by_rule(fired, "unused_seats_above")) == 1)
    check("rules: disabled licensed holder fires with the cost",
          len(_by_rule(fired, "disabled_account_licensed")) == 1
          and "$48/month" in _by_rule(fired, "disabled_account_licensed")[0]["detail"])
    check("rules: unused cost rule is off by default (0)", not _by_rule(fired, "unused_monthly_cost_above"))
    check("rules: printer error critical, offline warning",
          _by_rule(fired, "device_error")[0]["severity"] == "critical"
          and _by_rule(fired, "device_offline")[0]["severity"] == "warning")
    check("rules: supplies below 10% -> one per supply, info", len(_by_rule(fired, "supply_below_percent")) == 4
          and all(a["severity"] == "info" for a in _by_rule(fired, "supply_below_percent")))
    check("rules: legacy auth in use fires once with the top protocols",
          len(_by_rule(fired, "legacy_auth_signins")) == 1
          and "Authenticated SMTP" in _by_rule(fired, "legacy_auth_signins")[0]["detail"])
    stale = _by_rule(fired, "data_stale_days")
    check("rules: stale-data alerts skip the history folders",
          all("history" not in a["key"] for a in stale) and all("Change log" not in a["title"] for a in stale))
    check("rules: MFA coverage 93.4 is above 90 - quiet", not _by_rule(fired, "mfa_coverage_below"))

    # Turning knobs changes what fires, and a silenced tab fires nothing.
    cfg2 = A.load_config(os.path.join(here, "alerts.example.ini"))
    cfg2["rules"]["identity.ca_gap_warning"] = False
    cfg2["rules"]["licensing.unused_seats_above"] = 30
    cfg2["rules"]["fleet.supply_below_percent"] = 5.5
    cfg2["rules"]["security.mfa_coverage_below"] = 95
    cfg2["rules"]["licensing.unused_monthly_cost_above"] = 100
    cfg2["tabs"]["fleet"]["notify"] = False
    f2 = A.evaluate(cfg2, models, feeds, now=now)
    check("rules: warning CA gaps off -> none", not _by_rule(f2, "ca_gap_warning"))
    check("rules: raising the seats line silences it", not _by_rule(f2, "unused_seats_above"))
    check("rules: MFA line at 95 fires with the numbers",
          len(_by_rule(f2, "mfa_coverage_below")) == 1 and "93.4%" in _by_rule(f2, "mfa_coverage_below")[0]["title"]
          and "your line is 95%" in _by_rule(f2, "mfa_coverage_below")[0]["title"])
    check("rules: unused cost above $100 fires with the monthly figure",
          len(_by_rule(f2, "unused_monthly_cost_above")) == 1 and "$396" in _by_rule(f2, "unused_monthly_cost_above")[0]["title"])
    check("rules: a silenced tab fires nothing", not [a for a in f2 if a["tab"] == "fleet"])

    # Changes are events: only recent ones, reported as transient.
    ch_models = dict(models)
    ch_models["changes"] = {"events": [
        {"ts": "2026-09-01T09:00:00Z", "category": "Conditional Access", "kind": "changed", "item": "Block legacy", "detail": "enabled -> disabled"},
        {"ts": "2026-09-01T09:00:00Z", "category": "Role assignments", "kind": "added", "item": "Pat Admin", "detail": "Global Administrator"},
        {"ts": "2026-08-01T09:00:00Z", "category": "Role assignments", "kind": "added", "item": "Old News", "detail": "Reader"},
        {"ts": "2026-09-01T09:00:00Z", "category": "App registrations", "kind": "added", "item": "New App", "detail": ""},
    ], "snapshot_count": 3, "first": None, "last": None}
    f3 = A.evaluate(cfg, ch_models, feeds, now=now)
    check("rules: recent CA change and role grant fire, transient",
          len(_by_rule(f3, "conditional_access")) == 1 and len(_by_rule(f3, "role_assignments")) == 1
          and all(a["transient"] for a in _by_rule(f3, "role_assignments") + _by_rule(f3, "conditional_access")))
    check("rules: a change older than the window is not reported",
          all("Old News" not in a["title"] for a in f3))
    check("rules: app registration changes are off by default", not _by_rule(f3, "app_registrations"))
    check("rules: change alerts carry the audit-log next step",
          "audit log" in _by_rule(f3, "conditional_access")[0]["action"])

    # Refresh rules read refresh-status.json directly.
    rf = dict(feeds)
    rf["refresh_status"] = Feed("refresh_status", "Automatic refresh", "x", data={
        "GeneratedUtc": iso(NOW), "Scheduled": True,
        "SignIn": {"Mode": "none", "Ok": False, "Detail": "Nobody finished the window.", "Dropped": ["Nobody finished the window."]},
        "Steps": [{"Step": "sign-in", "Status": "FAILED", "Detail": "x"}, {"Step": "print-fleet-collector", "Status": "FAILED", "Detail": "exit code 1"},
                  {"Step": "console build", "Status": "ok", "Detail": ""}],
        "Schedule": {"Mode": "unattended", "Time": "06:30", "RunAs": "SYSTEM"}, "KeepSignedIn": False,
        "Certificate": {"Thumbprint": "AB", "Present": True, "Expires": "2026-09-14", "DaysLeft": 12}}, ts=NOW)
    f4 = A.evaluate(cfg, models, rf, now=now)
    check("rules: could not sign in -> critical", _by_rule(f4, "could_not_sign_in")[0]["severity"] == "critical")
    check("rules: collector failed -> one per failed step, never sign-in or build",
          [a["key"] for a in _by_rule(f4, "collector_failed")] == ["refresh/collector_failed/print-fleet-collector"])
    check("rules: certificate within 30 days -> warning with the days",
          "expires in 12 days" in _by_rule(f4, "certificate_expiring_days")[0]["title"])
    rf["refresh_status"].data["SignIn"] = {"Mode": "user", "Ok": True, "Detail": "", "Dropped": ["Signing in as the registered app failed: AADSTS700027."]}
    rf["refresh_status"].data["Certificate"]["DaysLeft"] = -2
    f5 = A.evaluate(cfg, models, rf, now=now)
    check("rules: fell back -> warning; expired certificate -> critical even though the warning line is 30",
          _by_rule(f5, "could_not_sign_in")[0]["severity"] == "warning"
          and _by_rule(f5, "certificate_expiring_days")[0]["severity"] == "critical")
    cfg3 = A.load_config(os.path.join(here, "alerts.example.ini")); cfg3["rules"]["refresh.certificate_expiring_days"] = 0
    f6 = A.evaluate(cfg3, models, rf, now=now)
    check("rules: certificate rule at 0 is off entirely", not _by_rule(f6, "certificate_expiring_days"))


def test_overview_panel():
    """The overview's Needs-a-human list IS the alert rules - one source of truth."""
    models, feeds = _sample_models()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = A.load_config(os.path.join(here, "alerts.example.ini"))
    now = parse_ts("2026-09-02T12:00:00Z")
    avail = {"index": True, "identity": True, "security": True, "licensing": True,
             "fleet": True, "changes": True, "alerts": True}

    def panel(config, extra_models=None):
        m = dict(models)
        m.update(extra_models or {})
        m["fired"] = A.evaluate(config, m, feeds, now=now)
        html = pages.build_overview(m, feeds, avail, "now")
        chunk = html[html.index("Needs a human"):html.index("Stale data")] if "Stale data" in html \
            else html[html.index("Needs a human"):]
        return html, chunk

    html, chunk = panel(cfg)
    check("panel: it is built from the alerts, not a second hand-written list",
          not hasattr(pages, "urge") and "The same things the console would tell you about" in chunk)
    check("panel: severity order - the critical rows come first",
          chunk.index("Printer error: Service Bay") < chunk.index("Printer offline: Warehouse"))
    check("panel: findings only the catalog knows about now appear",
          all(t in chunk for t in ("Expired app credential: HR Sync",
                                   "Legacy authentication is still in use",
                                   "26 paid seats are unassigned")))
    check("panel: everything the old hand-written list showed is still there",
          all(t in chunk for t in ("Admin without MFA: Directory Sync Service Account",
                                   "Printer error: Service Bay", "Printer offline: Warehouse",
                                   "CA gap: Break-glass accounts excluded",
                                   "CA gap: No lingering report-only policies",
                                   "Disabled account still licensed: Ellis Frank")))
    check("panel: each row carries the same next step as its page",
          "aka.ms/mfasetup" in chunk and pages.next_step("disabled-licensed")[:40] in chunk)
    check("panel: it points at where to change what counts", 'href="alerts.html">Alerts</a>' in chunk)
    check("panel: informational findings stay on their own page (toner is not here)",
          "Toner" not in chunk and "Magenta" not in chunk)
    check("panel: the refresh's own troubles are not duplicated here",
          "could not sign in" not in chunk and "automatic-refresh certificate" not in chunk
          and "did not complete" not in chunk and "data is" not in chunk)

    # turning a rule off takes it off the front page too - the whole point
    off = A.load_config(os.path.join(here, "alerts.example.ini"))
    off["rules"]["security.admin_without_mfa"] = False
    _h, chunk_off = panel(off)
    check("panel: a rule turned off leaves the panel",
          "Admin without MFA" not in chunk_off and "Printer error: Service Bay" in chunk_off)
    quiet = A.load_config(os.path.join(here, "alerts.example.ini"))
    quiet["tabs"]["fleet"]["notify"] = False
    _h, chunk_quiet = panel(quiet)
    check("panel: a silenced tab leaves the panel", "Printer" not in chunk_quiet)
    moved = A.load_config(os.path.join(here, "alerts.example.ini"))
    moved["rules"]["licensing.unused_seats_above"] = 40
    _h, chunk_moved = panel(moved)
    check("panel: moving a threshold moves the panel", "paid seats are unassigned" not in chunk_moved)

    # change events are history, not open items
    ch = dict(models)
    ch["changes"] = {"events": [{"ts": "2026-09-01T09:00:00Z", "category": "Role assignments",
                                 "kind": "added", "item": "Pat Admin", "detail": "Global Administrator"}],
                     "snapshot_count": 2, "first": None, "last": None}
    _h, chunk_ch = panel(cfg, ch)
    check("panel: a change event is not an open item", "Pat Admin" not in chunk_ch)

    # one noisy rule cannot crowd out the rest
    many = dict(models)
    many["security"] = dict(models["security"], admins_without_mfa=[
        {"DisplayName": "Admin %d" % i, "UserPrincipalName": "a%d@x" % i, "Roles": "Global Administrator"}
        for i in range(9)])
    _h, chunk_many = panel(cfg, many)
    check("panel: at most five rows from one rule, then a pointer to its page",
          chunk_many.count("Admin without MFA:") == 5
          and '+4 more like this on the <a href="security.html">Security page</a>' in chunk_many)
    check("panel: the pointer sits under the last row of its own rule",
          chunk_many.index("+4 more like this") > chunk_many.index("Admin without MFA: Admin 4")
          and chunk_many.index("+4 more like this") < chunk_many.index("Printer offline"))

    empty = dict(models)
    empty["fired"] = []
    check("panel: nothing firing means no panel at all",
          "Needs a human" not in pages.build_overview(empty, feeds, avail, "now"))


def test_alerts_state():
    a1 = {"key": "security/admin_without_mfa/a@x", "tab": "security", "rule": "admin_without_mfa", "severity": "critical", "title": "A", "detail": "", "action": "", "transient": False}
    a2 = {"key": "fleet/device_offline/10.0.0.1", "tab": "fleet", "rule": "device_offline", "severity": "warning", "title": "B", "detail": "", "action": "", "transient": False}
    ev = {"key": "changes/role_assignments/x", "tab": "changes", "rule": "role_assignments", "severity": "warning", "title": "E", "detail": "", "action": "", "transient": True}
    t0 = parse_ts("2026-09-01T07:00:00Z")
    state = A.empty_state()
    d = A.diff_state([a1, a2, ev], state)
    check("state: everything is new on the first run", len(d["new"]) == 3 and not d["worse"] and not d["cleared"])
    # nothing was sent (say the webhook was down): they must stay 'new'
    A.apply_state(state, [a1, a2, ev], d, notified=False, now=t0)
    d = A.diff_state([a1, a2, ev], state)
    check("state: untold alerts stay new next time", len(d["new"]) == 3)
    check("state: first_seen recorded even when untold", state["alerts"][a1["key"]]["first_seen"] == "2026-09-01T07:00:00Z")
    A.apply_state(state, [a1, a2, ev], d, notified=True, now=parse_ts("2026-09-01T08:00:00Z"))
    d = A.diff_state([a1, a2, ev], state)
    check("state: told alerts are 'still', nothing new", not d["new"] and len(d["still"]) == 3)
    check("state: first_seen kept across runs", state["alerts"][a1["key"]]["first_seen"] == "2026-09-01T07:00:00Z")
    worse = dict(a2, severity="critical")
    d = A.diff_state([a1, worse], state)
    check("state: severity rise -> worse; nothing cleared while it is still firing",
          [x["key"] for x in d["worse"]] == [a2["key"]] and d["cleared"] == [])
    check("state: event absence is not a 'cleared'", all(c["key"] != ev["key"] for c in d["cleared"]))
    d2 = A.diff_state([a1], state)
    check("state: vanished state is cleared with its old severity",
          [c["key"] for c in d2["cleared"]] == [a2["key"]] and d2["cleared"][0]["severity"] == "warning")
    A.apply_state(state, [a1], d2, notified=True, now=parse_ts("2026-09-02T07:00:00Z"))
    check("state: cleared and aged-out entries are pruned", set(state["alerts"]) == {a1["key"]})


def test_alerts_page():
    models, feeds = _sample_models()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = A.load_config(os.path.join(here, "sample", "alerts.ini"))
    fired = A.evaluate(cfg, models, feeds, now=parse_ts("2026-09-02T12:00:00Z"))
    state = {"alerts": {}, "last_sent": "2026-08-31T07:01:12Z", "last_digest": "2026-08-31",
             "history": [{"when": "2026-08-31T07:01:12Z", "title": "IT Ops Console weekly summary: 12 open", "channels": ["teams"]}]}
    am = pages.alerts_page_model(fired, cfg, state, A.channels_configured(cfg, env={}))
    avail = {"index": True, "alerts": True}
    html = pages.build_alerts(am, avail, "now")
    check("alerts page: renders the four sections",
          all(h in html for h in ("Where alerts go", "Firing now", "Change what the console flags", "alerts.ini")))
    check("alerts page: says Teams is connected and when messages go",
          "connected - a Workflows URL is set" in html and "only when an alert appears, gets worse, or clears" in html)
    check("alerts page: shows the last message and history", "2026-08-31 07:01 UTC" in html and "weekly summary: 12 open" in html)
    check("alerts page: firing rows carry severity badge, title and next step",
          'class="badge" style="color:var(--critical)"' in html and "Admin without MFA:" in html
          and "aka.ms/mfasetup" in html)
    check("alerts page: every rule is a control, one table per tab",
          all(lbl in html for lbl in ("An admin has no MFA", "MFA coverage falls below"))
          and html.count('class="rules"') == len(A.TABS)
          and html.count('data-kind="switch"') == sum(1 for r in A.CATALOG if r.kind == "switch") + len(A.TABS)
          and html.count('data-kind="number"') == sum(1 for r in A.CATALOG if r.kind == "number"))
    check("alerts page: an on rule is ticked, an off rule is not",
          'data-key="admin_without_mfa" data-kind="switch" checked' in html
          and 'data-key="app_registrations" data-kind="switch">' in html)
    check("alerts page: a threshold carries its number and unit",
          'data-key="mfa_coverage_below" data-kind="number" min="0" step="1" value="90"' in html
          and 'data-key="unused_seats_above" data-kind="number" min="0" step="1" value="25"' in html)
    check("alerts page: the send settings are controls too",
          'data-key="when" data-kind="radio" value="changes" checked' in html
          and 'data-key="digest_day"' in html and 'data-key="console_link"' in html)
    check("alerts page: a save box and the two-step instruction",
          'id="settings-text"' in html and 'id="save-btn"' in html
          and "Apply Settings" in html and "<noscript>" in html)
    # console-site is a folder people copy onto shares, so the page must never
    # carry the Workflows URL - and the editor must not offer to change it.
    check("alerts page: the editor never carries where alerts go",
          "EXAMPLE-not-a-real-url" not in html and 'data-sec="teams"' not in html
          and 'data-sec="email"' not in html and 'data-key="webhook"' not in html)
    check("alerts page: no setup banner when a channel exists", "not sent anywhere yet" not in html)

    none = A.load_config(os.path.join(here, "nope-alerts.ini"))
    am2 = pages.alerts_page_model(fired, none, None, A.channels_configured(none, env={}))
    html2 = pages.build_alerts(am2, avail, "now")
    check("alerts page: no channel -> plain setup banner naming the file and the test command",
          "not sent anywhere yet" in html2 and "nope-alerts.ini" in html2 and "notify.py --test" in html2)
    check("alerts page: missing ini explained, defaults in force", "No alerts.ini yet" in html2)
    bad = dict(none); bad["problems"] = ["[security] 'typo' is not a rule this console knows - see alerts.example.ini for the list."]
    bad["exists"] = True
    bad["tabs"] = {t: {"notify": (t != "fleet")} for t, _ in A.TABS}
    am3 = pages.alerts_page_model([], bad, None, A.channels_configured(bad, env={}))
    html3 = pages.build_alerts(am3, avail, "now")
    check("alerts page: unusable lines listed", "could not be used as written" in html3 and "&#x27;typo&#x27;" in html3)
    check("alerts page: a silenced tab shows an unticked tab box",
          'data-sec="fleet" data-key="notify" data-kind="switch">' in html3
          and 'data-sec="security" data-key="notify" data-kind="switch" checked' in html3)
    check("alerts page: nothing firing says so calmly", "Nothing - every rule that is on is quiet." in html3)
    hostile = [{"key": "k", "tab": "security", "rule": "admin_without_mfa", "severity": "critical",
                "title": "<script>alert(1)</script>", "detail": "<b>x</b>", "action": "", "transient": False}]
    am4 = pages.alerts_page_model(hostile, none, None, A.channels_configured(none, env={}))
    h4 = pages.build_alerts(am4, avail, "now")
    check("alerts page: alert text escaped",
          "<script>alert(1)</script>" not in h4 and "&lt;script&gt;alert(1)" in h4
          and h4.count("<script>") == 1)   # only the editor's own


def _all_pages(models, feeds, available):
    gen = "2026-01-01 00:00:00"
    return {
        "index": pages.build_overview(models, feeds, available, gen),
        "identity": pages.build_identity(models["identity"], feeds["tenant"], available, gen),
        "security": pages.build_security(models["security"], feeds["security"], available, gen),
        "licensing": pages.build_licensing(models["licensing"], feeds["licensing"], available, gen),
        "fleet": pages.build_fleet(models["fleet"], feeds["fleet"], available, gen,
                                   discovery=models.get("discovery")),
        "changes": pages.build_changes(models["changes"], feeds["history"], available, gen),
        "alerts": pages.build_alerts(models.get("alerts"), available, gen),
    }


def test_render_empty_console():
    """Nothing configured at all - every page must still render."""
    feeds = {k: Feed(k, k, None) for k in
             ("tenant", "run_summary", "security", "licensing", "history", "fleet")}
    models = {k: None for k in ("identity", "security", "licensing", "fleet",
                                "changes", "discovery")}
    available = {"index": True, "identity": False, "security": False,
                 "licensing": False, "fleet": False, "changes": False}
    out = _all_pages(models, feeds, available)
    check("render: all 7 pages render with zero feeds", len(out) == 7)
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
    check("render: seven pages written", len(names) == 7)
    with open(os.path.join(site, "index.html"), encoding="utf-8") as fh:
        idx = fh.read()
    check("render: overview links every domain",
          all(('%s.html' % p) in idx for p in
              ("identity", "security", "licensing", "fleet", "changes")))
    check("render: freshness dot rendered", 'class="dot' in idx)
    check("render: sample footer states the automatic-refresh schedule",
          'class="refresh-note">Automatic refresh: every day at 07:00' in idx)
    check("render: sample overview has no banner (its last refresh worked)", 'class="banner' not in idx)
    with open(os.path.join(site, "fleet.html"), encoding="utf-8") as fh:
        fleet = fh.read()
    check("render: fleet page has meters", 'class="meter"' in fleet)
    check("render: the built sample carries the places-to-look editor",
          "Where we look" in fleet and 'data-rowsec="ranges"' in fleet
          and 'value="10.0.10.0/24"' in fleet)
    check("render: the built sample never puts the SNMP community on a page",
          all("public" not in open(os.path.join(site, n), encoding="utf-8").read()
              for n in names)) 


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
        test_sample_does_not_rot()
        test_discovery_model()
        test_where_we_look_page()
        test_changes_model()
        test_refresh_model()
        test_refresh_render()
        test_alerts_catalog_and_config(tmp)
        test_alerts_rules()
        test_overview_panel()
        test_alerts_state()
        test_alerts_page()
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
