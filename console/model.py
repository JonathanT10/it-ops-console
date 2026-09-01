"""Turn raw feeds into the numbers each page shows.

Everything here is a pure function of the loaded feeds, so the same inputs
always render the same console - and a missing feed yields an empty section
rather than an exception.
"""

from __future__ import annotations

from .sources import parse_ts, utcnow


def _g(d, *keys, default=None):
    """Nested get that tolerates missing keys and None along the way."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# --------------------------------------------------------------------------- #
# Identity (entra-tenant-docs tenant.json)
# --------------------------------------------------------------------------- #

def identity_model(feed, run_summary_feed=None):
    """tenant.json carries collected state; the CA gap analysis is derived data
    and lives in run-summary.json, so it rides in from there when available."""
    if not feed.ok:
        return None
    d = feed.data
    pols = _list(_g(d, "ConditionalAccess", "Policies"))
    gaps = _list(d.get("CaGaps"))
    if not gaps and run_summary_feed is not None and run_summary_feed.ok:
        gaps = _list(run_summary_feed.data.get("CaGaps"))
    roles = _list(d.get("Roles"))
    role_rows = sorted(
        ({"role": r.get("Role"), "count": len(_list(r.get("Members")))} for r in roles),
        key=lambda r: -r["count"],
    )
    intune = d.get("Intune") or {}
    return {
        "tenant": _g(d, "Organization", "DisplayName") or "Tenant",
        "tenant_id": d.get("TenantId"),
        "members": _g(d, "UserCounts", "Members", default=0),
        "enabled_members": _g(d, "UserCounts", "EnabledMembers", default=0),
        "guests": _g(d, "UserCounts", "Guests", default=0),
        "ca_policies": pols,
        "ca_enabled": sum(1 for p in pols if p.get("State") == "enabled"),
        "ca_report_only": sum(1 for p in pols
                              if p.get("State") == "enabledForReportingButNotEnforced"),
        "ca_disabled": sum(1 for p in pols if p.get("State") == "disabled"),
        "named_locations": _list(_g(d, "ConditionalAccess", "NamedLocations")),
        "gaps": gaps,
        "gaps_failing": [g for g in gaps if g.get("Result") == "fail"],
        "roles": role_rows,
        "role_detail": roles,
        "groups": d.get("Groups") or {},
        "auth_methods": _list(d.get("AuthMethods")),
        "user_settings": d.get("UserSettings") or {},
        "applications": _list(d.get("Applications")),
        "intune": intune if intune.get("Available") else None,
    }


# --------------------------------------------------------------------------- #
# Security (entra-security-snapshot JSON, plus CA gaps from identity)
# --------------------------------------------------------------------------- #

def security_model(feed, identity=None):
    if not feed.ok:
        return None
    d = feed.data
    cov = d.get("MfaCoverage") or {}
    guests = d.get("Guests") or {}
    legacy = d.get("LegacyAuth") or {}
    return {
        "tenant_id": d.get("TenantId"),
        "mfa": {
            "registered": cov.get("MfaRegistered"),
            "total": cov.get("EnabledMembersInReport"),
            "not_registered": cov.get("NotRegistered"),
            "percent": cov.get("CoveragePercent"),
        } if cov else None,
        "role_summary": _list(d.get("RoleSummary")),
        "admins_without_mfa": _list(d.get("AdminsWithoutMfa")),
        "stale_members": _list(d.get("StaleMembers")),
        "guests_total": guests.get("Total"),
        "guests_stale": len(_list(guests.get("Stale"))),
        "guests_pending": len(_list(guests.get("PendingAcceptance"))),
        "legacy_available": bool(legacy.get("Available")),
        "legacy_summary": _list(legacy.get("Summary")),
        "legacy_users": _list(legacy.get("Users")),
        "attention": _list(d.get("NeedsAttention")),
        "stale_days": d.get("StaleDays"),
        # CA gap analysis rides along from the tenant docs feed when present.
        "gaps": (identity or {}).get("gaps") if identity else [],
    }


# --------------------------------------------------------------------------- #
# Licensing (m365-license-waste-report JSON)
# --------------------------------------------------------------------------- #

def licensing_model(feed):
    if not feed.ok:
        return None
    d = feed.data
    skus = _list(d.get("SkuSummary"))
    cands = _list(d.get("ReclaimCandidates"))
    disabled = [c for c in cands if str(c.get("Reason", "")).startswith("DISABLED")]
    stale = [c for c in cands if not str(c.get("Reason", "")).startswith("DISABLED")]
    return {
        "licensed_users": d.get("LicensedUsers"),
        "stale_days": d.get("StaleDays"),
        "skus": sorted(skus, key=lambda s: -int(s.get("Unassigned") or 0)),
        "consumption_skus": sorted(_list(d.get("ConsumptionSkus")),
                                   key=lambda s: str(s.get("Sku") or "")),
        "unassigned_total": sum(int(s.get("Unassigned") or 0) for s in skus),
        "disabled_holders": disabled,
        "stale_holders": stale,
        "candidates": cands,
        # Costing rides in from the license tool when a price list was used.
        # Present only when -PriceList was passed; HasPrices distinguishes
        # "priced" from "price file exists but is still blank".
        "costing": d.get("Costing"),
    }


# --------------------------------------------------------------------------- #
# Print fleet (fleet.db)
# --------------------------------------------------------------------------- #

STALE_DEVICE_HOURS = 48


def fleet_model(feed):
    if not feed.ok:
        return None
    rows = []
    now = utcnow()
    for entry in feed.data["devices"]:
        dev, snap = entry["device"], entry["snapshot"]
        last_seen = parse_ts(dev.get("last_seen"))
        offline = last_seen is None or (now - last_seen).total_seconds() / 3600 > STALE_DEVICE_HOURS
        status = "offline" if offline else (snap or {}).get("status") or "unknown"
        supplies = []
        for s in entry["supplies"]:
            lvl, mx = s.get("level"), s.get("max_capacity")
            pct = None
            if mx and lvl is not None and lvl >= 0:
                pct = round(100.0 * lvl / mx)
            supplies.append({
                "description": s.get("description"),
                "type": s.get("supply_type"),
                "percent": pct,
                "raw": lvl,
            })
        vols = entry["volumes"]
        deltas = []
        for prev, cur in zip(vols, vols[1:]):
            p, c = prev.get("pages"), cur.get("pages")
            if p is not None and c is not None and c >= p:
                deltas.append({"day": cur["day"], "pages": c - p})
        rows.append({
            "name": dev.get("name") or dev.get("ip"),
            "ip": dev.get("ip"),
            "model": dev.get("model"),
            "serial": dev.get("serial"),
            "status": status,
            "detail": (snap or {}).get("detail"),
            "page_count": (snap or {}).get("page_count"),
            "last_seen": dev.get("last_seen"),
            "last_seen_dt": last_seen,
            "supplies": supplies,
            "volumes": deltas,
            "low_supplies": [s for s in supplies
                             if s["percent"] is not None and s["percent"] < 20],
        })
    order = {"error": 0, "offline": 1, "warning": 2, "ok": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 4), r["name"] or ""))
    week = sum(sum(v["pages"] for v in r["volumes"][-7:]) for r in rows)
    return {
        "devices": rows,
        "total": len(rows),
        "online": sum(1 for r in rows if r["status"] != "offline"),
        "attention": [r for r in rows if r["status"] in ("error", "offline", "warning")],
        "week_pages": week,
        "low_supply_count": sum(len(r["low_supplies"]) for r in rows),
    }


# --------------------------------------------------------------------------- #
# Change log (entra-tenant-docs history)
# --------------------------------------------------------------------------- #

def changes_model(feed, run_summary_feed=None):
    """Recompute the change feed from archived snapshots.

    The console deliberately derives this itself rather than trusting a
    pre-baked list, so the 'what changed' page stays correct even if the
    export that produced run-summary.json is older than the history folder.
    """
    if not feed.ok:
        return None
    snaps = feed.data
    events = []
    for prev, cur in zip(snaps, snaps[1:]):
        ts = cur.get("GeneratedUtc")
        events.extend(_diff_snapshots(prev, cur, ts))
    events.sort(key=lambda e: str(e["ts"]), reverse=True)
    return {
        "events": events,
        "snapshot_count": len(snaps),
        "first": snaps[0].get("GeneratedUtc") if snaps else None,
        "last": snaps[-1].get("GeneratedUtc") if snaps else None,
    }


def _index(rows, key):
    out = {}
    for r in _list(rows):
        if isinstance(r, dict) and r.get(key) is not None:
            out[str(r[key])] = r
    return out


def _diff_snapshots(prev, cur, ts):
    ev = []

    def add(cat, kind, item, detail=""):
        ev.append({"ts": ts, "category": cat, "kind": kind,
                   "item": str(item), "detail": str(detail)})

    # Conditional Access
    p = _index(_g(prev, "ConditionalAccess", "Policies"), "Name")
    c = _index(_g(cur, "ConditionalAccess", "Policies"), "Name")
    for k, v in c.items():
        if k not in p:
            add("Conditional Access", "added", k, "state: %s" % v.get("State"))
        elif p[k].get("State") != v.get("State"):
            add("Conditional Access", "changed", k,
                "%s -> %s" % (p[k].get("State"), v.get("State")))
    for k in p:
        if k not in c:
            add("Conditional Access", "removed", k)

    # Role assignments (role|principal pairs)
    def roleset(s):
        out = {}
        for r in _list(s.get("Roles")):
            for m in _list(r.get("Members")):
                who = m.get("UserPrincipalName") or m.get("DisplayName")
                out["%s|%s" % (r.get("Role"), who)] = (r.get("Role"), m.get("DisplayName"), who)
        return out
    pr, cr = roleset(prev), roleset(cur)
    for k, v in cr.items():
        if k not in pr:
            add("Role assignments", "added", v[1] or v[2], v[0])
    for k, v in pr.items():
        if k not in cr:
            add("Role assignments", "removed", v[1] or v[2], v[0])

    # Licenses purchased
    pl, cl = _index(prev.get("Licenses"), "Sku"), _index(cur.get("Licenses"), "Sku")
    for k, v in cl.items():
        if k not in pl:
            add("Licenses", "added", k, "%s purchased" % v.get("Purchased"))
        elif pl[k].get("Purchased") != v.get("Purchased"):
            add("Licenses", "changed", k,
                "purchased %s -> %s" % (pl[k].get("Purchased"), v.get("Purchased")))
    for k in pl:
        if k not in cl:
            add("Licenses", "removed", k)

    # Applications
    pa, ca = _index(prev.get("Applications"), "AppId"), _index(cur.get("Applications"), "AppId")
    for k, v in ca.items():
        if k not in pa:
            add("App registrations", "added", v.get("Name"))
    for k, v in pa.items():
        if k not in ca:
            add("App registrations", "removed", v.get("Name"))

    # Intune (only when both snapshots carry it)
    pi, ci = prev.get("Intune") or {}, cur.get("Intune") or {}
    if pi.get("Available") and ci.get("Available"):
        for label, field, keyname in (
            ("Intune compliance", "CompliancePolicies", "Name"),
            ("Intune configuration", "ConfigurationProfiles", "Name"),
        ):
            a, b = _index(pi.get(field), keyname), _index(ci.get(field), keyname)
            for k in b:
                if k not in a:
                    add(label, "added", k)
                elif a[k] != b[k]:
                    add(label, "changed", k, "settings or assignments changed")
            for k in a:
                if k not in b:
                    add(label, "removed", k)
    return ev
