"""The six pages. Each renders only its own domain - that's the whole point."""

from __future__ import annotations

from .render import (bar_rows, badge, esc, freshness_chip, meter, muted_badge,
                     shell, sparkline, status_badge)


def _empty(msg, hint=""):
    h = '<div class="muted" style="margin-top:6px;font-size:12.5px">%s</div>' % esc(hint) if hint else ""
    return '<section><div class="empty">%s%s</div></section>' % (esc(msg), h)


def _feed_empty(label, feed, how_to_turn_on):
    """The empty page for a feed, in words matched to WHY it is empty.

    Never collected is a normal state and must not read like a failure; a feed
    that exists but cannot be read is a real error and stays one."""
    if feed is not None and feed.error:
        return _empty("%s data could not be read." % label, feed.error)
    if feed is not None and getattr(feed, "missing", False):
        return _empty("Nothing collected here yet.",
                      how_to_turn_on + " " + feed.hint)
    return _empty("%s data is not configured." % label,
                  (feed.status_note if feed else ""))


def _cards(items):
    out = []
    for it in items:
        out.append('<div class="card"><div class="k">%s</div><div class="v">%s</div>'
                   '<div class="d">%s</div></div>'
                   % (esc(it.get("k")), esc(it.get("v")), esc(it.get("d", ""))))
    return '<div class="cards">%s</div>' % "".join(out)


def _table(headers, rows, empty="Nothing to show."):
    if not rows:
        return '<p class="empty">%s</p>' % esc(empty)
    head = "".join('<th%s>%s</th>' % (' class="num"' if h.startswith("#") else "",
                                      esc(h.lstrip("#"))) for h in headers)
    body = []
    for r in rows:
        cells = "".join('<td%s>%s</td>' % (' class="num"' if str(h).startswith("#") else "", c)
                        for h, c in zip(headers, r))
        body.append("<tr>%s</tr>" % cells)
    return '<div class="scroll"><table><tr>%s</tr>%s</table></div>' % (head, "".join(body))


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

def build_overview(models, feeds, available, generated):
    tiles = []

    def tile(key, title, headline, sub, feed, extra=""):
        # `key` is the PAGE name; `feed` is the loaded feed that backs it -
        # they are deliberately different (identity <- tenant, changes <- history).
        f = feed
        if f is None or not f.ok:
            note = (f.status_note if f else "not configured")
            head = "Nothing yet" if (f is not None and getattr(f, "missing", False)) else "Not configured"
            return ('<div class="tile off"><div class="top"><span class="title">%s</span></div>'
                    '<div class="headline">%s</div>'
                    '<div class="sub">%s</div></div>' % (esc(title), esc(head), esc(note)))
        return ('<a class="tile" href="%s.html"><div class="top"><span class="title">%s</span>%s</div>'
                '<div class="headline">%s</div><div class="sub">%s</div>'
                '<div class="foot"><span class="dot %s"></span>%s</div></a>'
                % (esc(key), esc(title), extra, esc(headline), esc(sub),
                   esc(f.state), esc(f.age)))

    ident = models.get("identity")
    if ident:
        gaps = len(ident["gaps_failing"])
        crit = sum(1 for g in ident["gaps_failing"] if g.get("Severity") == "critical")
        extra = (badge("critical", "warn", "%d critical gap%s" % (crit, "" if crit == 1 else "s"))
                 if crit else (badge("warning", "warn", "%d gap%s" % (gaps, "" if gaps == 1 else "s"))
                               if gaps else badge("good", "check", "clean")))
        tiles.append(tile("identity", "Identity", ident["ca_enabled"],
                          "CA policies enforced of %d - %d members, %d guests"
                          % (len(ident["ca_policies"]), ident["members"], ident["guests"]),
                          feeds["tenant"], extra))
    else:
        tiles.append(tile("identity", "Identity", "", "", feeds.get("tenant")))

    sec = models.get("security")
    if sec:
        mfa = sec["mfa"]
        head = ("%s%%" % mfa["percent"]) if mfa and mfa.get("percent") is not None else "-"
        n = len(sec["admins_without_mfa"])
        extra = badge("critical", "warn", "%d admin%s" % (n, "" if n == 1 else "s")) if n else badge("good", "check", "admins ok")
        tiles.append(tile("security", "Security", head,
                          "MFA coverage - %d stale accounts, %d guests"
                          % (len(sec["stale_members"]), sec["guests_total"] or 0),
                          feeds["security"], extra))
    else:
        tiles.append(tile("security", "Security", "", "", feeds.get("security")))

    lic = models.get("licensing")
    if lic:
        n = len(lic["candidates"])
        extra = badge("warning", "warn", "%d to review" % n) if n else badge("good", "check", "clean")
        tiles.append(tile("licensing", "Licensing", lic["unassigned_total"],
                          "unassigned seats - %d licensed users" % (lic["licensed_users"] or 0),
                          feeds["licensing"], extra))
    else:
        tiles.append(tile("licensing", "Licensing", "", "", feeds.get("licensing")))

    fl = models.get("fleet")
    if fl:
        n = len(fl["attention"])
        extra = badge("warning", "warn", "%d need%s attention" % (n, "s" if n == 1 else "")) if n else badge("good", "check", "all clear")
        tiles.append(tile("fleet", "Print fleet", "%d/%d" % (fl["online"], fl["total"]),
                          "devices online - %s pages this week" % format(fl["week_pages"], ","),
                          feeds["fleet"], extra))
    else:
        tiles.append(tile("fleet", "Print fleet", "", "", feeds.get("fleet")))

    ch = models.get("changes")
    if ch:
        recent = ch["events"][:1]
        sub = ("most recent: %s" % recent[0]["ts"][:10]) if recent else "no changes recorded"
        tiles.append(tile("changes", "What changed", len(ch["events"]),
                          "changes across %d snapshots - %s" % (ch["snapshot_count"], sub),
                          feeds["history"]))
    else:
        tiles.append(tile("changes", "What changed", "", "", feeds.get("history")))

    # Anything that needs a human, gathered from every domain that loaded.
    # Deliberately narrow: things you would act on this week, not everything
    # every page flags. Informational findings stay on their own page.
    urgent = []

    def urge(rank, domain, what, detail):
        urgent.append((rank, domain, what, detail or ""))

    if ident:
        for g in ident["gaps_failing"]:
            sev = g.get("Severity")
            if sev in ("critical", "warning"):
                urge(0 if sev == "critical" else 2, "Identity",
                     "CA gap: %s" % g.get("Title"), g.get("Detail"))
    if sec:
        for a in sec["admins_without_mfa"][:5]:
            urge(0, "Security", "Admin without MFA: %s" % a.get("DisplayName"), a.get("Roles"))
    if fl:
        for d in fl["attention"]:
            if d["status"] in ("error", "offline"):
                urge(1, "Print fleet", "%s: %s" % (d["name"], d["status"]), d["detail"])
    if lic:
        # A disabled account still holding a paid seat is money leaking, and it
        # is the one licensing finding that needs no conversation first.
        for c in lic["disabled_holders"][:5]:
            urge(2, "Licensing",
                 "Disabled account still licensed: %s" % (c.get("DisplayName") or c.get("UserPrincipalName")),
                 c.get("Licenses") or c.get("Reason"))

    urgent.sort(key=lambda u: u[0])
    urgent_html = ""
    if urgent:
        shown = urgent[:12]
        rows = "".join(
            '<div class="chg"><span class="kind">%s</span><span>%s%s</span></div>'
            % (esc(dom), esc(what), (' <span class="cat">&mdash; %s</span>' % esc(detail)) if detail else "")
            for _rank, dom, what, detail in shown)
        note = ("Pulled from every domain below. %d item%s."
                % (len(urgent), "" if len(urgent) == 1 else "s"))
        if len(shown) < len(urgent):
            note = ("Pulled from every domain below. Top %d of %d - the rest are on their own pages."
                    % (len(shown), len(urgent)))
        urgent_html = ('<section><h2>Needs a human</h2>'
                       '<p class="note">%s</p>%s</section>' % (note, rows))

    stale = [f for f in feeds.values() if f.ok and f.state == "stale"]
    stale_html = ""
    if stale:
        stale_html = ('<section><h2>Stale data</h2><p class="note">These feeds are old enough '
                      'that the numbers above may not reflect reality. Re-run the tool that '
                      'produces each one.</p>%s</section>'
                      % "".join('<div class="chg"><span class="kind">%s</span>'
                                '<span>%s <span class="cat">&mdash; %s</span></span></div>'
                                % (esc(f.label), esc(f.age), esc(f.path)) for f in stale))

    body = ('<div class="tiles">%s</div>%s%s' % ("".join(tiles), urgent_html, stale_html))
    return shell("Overview", "index", available, body, generated,
                 subtitle="One card per domain. Open a card for that topic only.")


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

def build_identity(m, feed, available, generated):
    if not m:
        return shell("Identity", "identity", available,
                     _feed_empty("Identity", feed,
                                 "Run Export-EntraTenantDocs.ps1 (the Refresh shortcut does this for you)."), generated)
    cards = _cards([
        {"k": "Members", "v": m["members"], "d": "%s enabled" % m["enabled_members"]},
        {"k": "Guests", "v": m["guests"]},
        {"k": "CA enforced", "v": m["ca_enabled"], "d": "of %d policies" % len(m["ca_policies"])},
        {"k": "Report-only", "v": m["ca_report_only"]},
        {"k": "Groups", "v": (m["groups"] or {}).get("Total", 0),
         "d": "%d dynamic" % len(m["groups"].get("Dynamic") or [])},
        {"k": "App registrations", "v": len(m["applications"])},
    ])

    gaps_html = ""
    if m["gaps"]:
        rows = []
        for g in m["gaps"]:
            if g.get("Result") == "pass":
                b = badge("good", "check", "Pass")
            elif g.get("Result") == "unknown":
                b = muted_badge("Unknown")
            elif g.get("Severity") == "info":
                b = muted_badge("Gap")
            else:
                b = badge("critical" if g.get("Severity") == "critical" else "warning",
                          "warn", "Gap - %s" % g.get("Severity"))
            rows.append('<div class="chg"><span style="flex:none;width:132px">%s</span>'
                        '<span>%s%s</span></div>'
                        % (b, esc(g.get("Title")),
                           (' <span class="cat">&mdash; %s</span>' % esc(g.get("Detail")))
                           if g.get("Detail") else ""))
        gaps_html = ('<section><h2>Conditional Access gaps</h2>'
                     '<p class="note">Baseline hygiene checks, not a compliance audit.</p>%s</section>'
                     % "".join(rows))

    ca_rows = []
    for p in m["ca_policies"]:
        who = []
        for u in (p.get("IncludeUsers") or []):
            who.append("All users" if u == "All" else u)
        who += ["%s (group)" % g for g in (p.get("IncludeGroups") or [])]
        who += ["%s (role)" % r for r in (p.get("IncludeRoles") or [])]
        excl = len((p.get("ExcludeUsers") or []) + (p.get("ExcludeGroups") or []) +
                   (p.get("ExcludeRoles") or []))
        who_txt = esc(", ".join(who[:3]) or "(none)")
        if excl:
            who_txt += ' <span class="muted">&middot; excl. %d</span>' % excl
        ca_rows.append([
            esc(p.get("Name")), status_badge(p.get("State")), who_txt,
            esc(", ".join(p.get("IncludeApps") or [])),
            esc(", ".join(p.get("GrantControls") or [])) or '<span class="muted">(none)</span>',
        ])
    ca_html = ('<section><h2>Conditional Access</h2><p class="note">%d enabled, %d report-only, '
               '%d disabled.</p>%s</section>'
               % (m["ca_enabled"], m["ca_report_only"], m["ca_disabled"],
                  _table(["Policy", "State", "Who", "Apps", "Grant"], ca_rows)))

    roles_html = ('<section><h2>Directory roles</h2>'
                  '<p class="note">Permanent assignments only &mdash; PIM eligible assignments '
                  'are not included.</p>%s</section>'
                  % bar_rows([{"label": r["role"], "count": r["count"]} for r in m["roles"]]))

    g = m["groups"] or {}
    groups_html = ('<section><h2>Groups</h2>%s%s</section>' % (
        _cards([
            {"k": "Total", "v": g.get("Total", 0)},
            {"k": "Microsoft 365", "v": g.get("M365", 0)},
            {"k": "Security", "v": g.get("SecurityOnly", 0)},
            {"k": "Role-assignable", "v": len(g.get("RoleAssignable") or [])},
            {"k": "Dynamic", "v": len(g.get("Dynamic") or [])},
        ]),
        ('<details><summary>Dynamic membership rules</summary>%s</details>'
         % "".join('<p style="margin:8px 0 0;font-size:13px">%s <span class="muted">(%s)</span></p>'
                   '<pre>%s</pre>' % (esc(d.get("Name")), esc(d.get("State")), esc(d.get("Rule")))
                   for d in (g.get("Dynamic") or []))) if g.get("Dynamic") else ""))

    auth_rows = [[esc(a.get("Method")), status_badge(a.get("State")),
                  esc(", ".join(a.get("Targets") or []))] for a in m["auth_methods"]]
    auth_html = ('<section><h2>Authentication methods</h2>%s</section>'
                 % _table(["Method", "State", "Targets"], auth_rows,
                          "Not available in this snapshot."))

    set_rows = []
    for k, v in (m["user_settings"] or {}).items():
        shown = "Yes" if v is True else ("No" if v is False else str(v))
        set_rows.append([esc(k), esc(shown)])
    settings_html = ('<section><h2>User &amp; guest settings</h2>%s</section>'
                     % _table(["Setting", "Value"], set_rows,
                              "Not available in this snapshot."))

    intune_html = ""
    if m["intune"]:
        i = m["intune"]
        dev, comp = i.get("Devices") or {}, i.get("ComplianceSummary") or {}
        intune_html = ('<section><h2>Intune</h2>%s<p class="note">%d compliance policies, '
                       '%d configuration profiles.</p></section>'
                       % (_cards([
                           {"k": "Devices", "v": dev.get("Total", 0)},
                           {"k": "Windows", "v": dev.get("Windows", 0)},
                           {"k": "macOS", "v": dev.get("MacOS", 0)},
                           {"k": "iOS", "v": dev.get("IOS", 0)},
                           {"k": "Android", "v": dev.get("Android", 0)},
                           {"k": "Non-compliant", "v": comp.get("NonCompliant", 0)},
                       ]), len(i.get("CompliancePolicies") or []),
                          len(i.get("ConfigurationProfiles") or [])))

    body = (freshness_chip(feed.state, feed.age, "Snapshot") + cards + gaps_html + ca_html +
            roles_html + groups_html + intune_html + auth_html + settings_html)
    return shell("Identity", "identity", available, body, generated,
                 subtitle="%s &middot; who can get in, and under what conditions" % esc(m["tenant"]))


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #

def build_security(m, feed, available, generated):
    if not m:
        return shell("Security", "security", available,
                     _feed_empty("Security", feed,
                                 "Run Get-EntraSecuritySnapshot.ps1 (the Refresh shortcut does this for you)."), generated)
    mfa = m["mfa"] or {}
    cards = _cards([
        {"k": "MFA coverage", "v": ("%s%%" % mfa.get("percent")) if mfa.get("percent") is not None else "-",
         "d": "%s of %s enabled members" % (mfa.get("registered", "-"), mfa.get("total", "-"))},
        {"k": "Not registered", "v": mfa.get("not_registered", "-")},
        {"k": "Admins without MFA", "v": len(m["admins_without_mfa"])},
        {"k": "Stale accounts", "v": len(m["stale_members"]),
         "d": "over %s days" % m["stale_days"]},
        {"k": "Guests", "v": m["guests_total"] or 0,
         "d": "%d stale, %d pending" % (m["guests_stale"], m["guests_pending"])},
        {"k": "Legacy auth users", "v": len(m["legacy_users"]) if m["legacy_available"] else "-"},
    ])

    admin_rows = [[esc(a.get("DisplayName")), esc(a.get("UserPrincipalName")), esc(a.get("Roles"))]
                  for a in m["admins_without_mfa"]]
    admins_html = ('<section><h2>Admins without MFA registered</h2>'
                   '<p class="note">The list to work through first. Service accounts still count '
                   '&mdash; they just need a different fix.</p>%s</section>'
                   % _table(["Name", "UPN", "Roles"], admin_rows,
                            "Every role holder has MFA registered."))

    roles_html = ('<section><h2>Role assignments</h2>%s</section>'
                  % bar_rows([{"label": r.get("Role"), "count": r.get("Assignments")}
                              for r in m["role_summary"][:15]]))

    legacy_html = ""
    if m["legacy_available"]:
        rows = [[esc(s.get("ClientApp")), esc(s.get("SignIns")), esc(s.get("DistinctUsers"))]
                for s in m["legacy_summary"]]
        legacy_html = ('<section><h2>Legacy authentication</h2>'
                       '<p class="note">Sign-ins over protocols that cannot do modern auth. '
                       'Anything here is a path around your CA policies.</p>%s</section>'
                       % _table(["Client app", "#Sign-ins", "#Users"], rows,
                                "No legacy-auth sign-ins in the window."))
    else:
        legacy_html = ('<section><h2>Legacy authentication</h2>'
                       '<p class="note">Not available in this snapshot (needs Entra ID P1/P2 '
                       'sign-in logs).</p></section>')

    stale_rows = [[esc(s.get("DisplayName")), esc(s.get("UserPrincipalName")),
                   esc(str(s.get("LastSignIn") or "never")[:10])]
                  for s in m["stale_members"][:40]]
    more = ('<p class="note" style="margin-top:8px">Showing 40 of %d &mdash; the full list is in '
            'the snapshot CSV.</p>' % len(m["stale_members"])) if len(m["stale_members"]) > 40 else ""
    stale_html = ('<section><h2>Stale accounts</h2><p class="note">Enabled members with no sign-in '
                  'for over %s days. A quiet account is sometimes a service account doing its '
                  'job &mdash; check before disabling.</p>%s%s</section>'
                  % (m["stale_days"], _table(["Name", "UPN", "Last sign-in"], stale_rows,
                                             "No stale accounts."), more))

    body = (freshness_chip(feed.state, feed.age, "Snapshot") + cards + admins_html +
            legacy_html + roles_html + stale_html)
    return shell("Security", "security", available, body, generated,
                 subtitle="Posture: who can act, who is exposed, what is still reachable the old way")


# --------------------------------------------------------------------------- #
# Licensing
# --------------------------------------------------------------------------- #

def build_licensing(m, feed, available, generated):
    if not m:
        return shell("Licensing", "licensing", available,
                     _feed_empty("Licensing", feed,
                                 "Run Get-LicenseWasteReport.ps1 with -JsonPath "
                                 "(the Refresh shortcut does this for you)."),
                     generated)
    cards = _cards([
        {"k": "Unassigned seats", "v": m["unassigned_total"], "d": "across real SKUs"},
        {"k": "Licensed users", "v": m["licensed_users"] or "-"},
        {"k": "Disabled but licensed", "v": len(m["disabled_holders"])},
        {"k": "Stale but licensed", "v": len(m["stale_holders"]),
         "d": "over %s days" % m["stale_days"]},
        {"k": "SKUs tracked", "v": len(m["skus"])},
    ])

    sku_rows = []
    for s in m["skus"]:
        purchased = int(s.get("Purchased") or 0)
        assigned = int(s.get("Assigned") or 0)
        pct = round(100.0 * assigned / purchased) if purchased else None
        sku_rows.append([
            esc(s.get("Sku")), meter(pct),
            "%s / %s" % (esc(assigned), esc(purchased)),
            esc(s.get("Unassigned")),
        ])
    sku_html = ('<section><h2>Seats purchased vs assigned</h2>'
                '<p class="note">Sorted by how many seats are sitting unused.</p>%s</section>'
                % _table(["SKU", "Utilisation", "Assigned", "#Unassigned"], sku_rows))

    cons_html = ""
    if m["consumption_skus"]:
        rows = [[esc(s.get("Sku")), esc(s.get("Assigned"))] for s in m["consumption_skus"]]
        cons_html = ('<section><h2>Self-service &amp; consumption SKUs</h2>'
                     '<p class="note">Microsoft reports these with tens of thousands of nominal '
                     'seats, so they are excluded from the totals above. Shown here for '
                     'completeness &mdash; what matters is how many are in use.</p>%s</section>'
                     % _table(["SKU", "#In use"], rows))

    cand_rows = [[esc(c.get("Reason")), esc(c.get("DisplayName")),
                  esc(c.get("UserPrincipalName")), esc(c.get("Licenses"))]
                 for c in m["candidates"][:50]]
    cand_html = ('<section><h2>Reclaim candidates</h2>'
                 '<p class="note">Conversation starters, not verdicts &mdash; confirm with the '
                 'user\'s manager before reclaiming anything.</p>%s</section>'
                 % _table(["Reason", "Name", "UPN", "Licenses"], cand_rows,
                          "Nothing to reclaim."))

    body = freshness_chip(feed.state, feed.age, "Report") + cards + sku_html + cons_html + cand_html
    return shell("Licensing", "licensing", available, body, generated,
                 subtitle="What you are paying for, and what nobody is using")


# --------------------------------------------------------------------------- #
# Print fleet
# --------------------------------------------------------------------------- #

def build_fleet(m, feed, available, generated):
    if not m:
        return shell("Print fleet", "fleet", available,
                     _feed_empty("Print fleet", feed,
                                 "Printers are optional. To turn this on, put their IPs in "
                                 "print-fleet-dashboard's config.ini - the next Refresh picks "
                                 "them up automatically."), generated)
    cards = _cards([
        {"k": "Devices online", "v": "%d/%d" % (m["online"], m["total"])},
        {"k": "Need attention", "v": len(m["attention"])},
        {"k": "Low supplies", "v": m["low_supply_count"], "d": "under 20%"},
        {"k": "Pages this week", "v": format(m["week_pages"], ",")},
    ])

    att_rows = []
    for d in m["attention"]:
        att_rows.append([status_badge(d["status"]), esc(d["name"]),
                         esc(d["detail"] or ""), esc(d["model"] or "")])
    att_html = ('<section><h2>Needs attention</h2>%s</section>'
                % _table(["Status", "Device", "Detail", "Model"], att_rows,
                         "Every device is reporting OK."))

    dev_rows = []
    for d in m["devices"]:
        toners = []
        for s in d["supplies"]:
            if s["type"] not in ("toner", "ink", "ink-cartridge"):
                continue
            name = (s["description"] or "").replace(" Toner", "").replace(" Cartridge", "")
            toners.append('<div class="supply"><span class="nm" title="%s">%s</span>%s</div>'
                          % (esc(s["description"]), esc(name), meter(s["percent"])))
        spark = sparkline([v["pages"] for v in d["volumes"]]) if len(d["volumes"]) > 1 else ""
        dev_rows.append([
            esc(d["name"]) + ('<div class="muted" style="font-size:12px">%s</div>' % esc(d["ip"])),
            status_badge(d["status"]),
            '<div style="min-width:190px">%s</div>'
            % ("".join(toners) or '<span class="muted">&mdash;</span>'),
            spark or '<span class="muted">&mdash;</span>',
            esc(format(d["page_count"], ",") if d["page_count"] else "-"),
        ])
    dev_html = ('<section><h2>All devices</h2>'
                '<p class="note">Toner percentages come straight from the Printer MIB &mdash; '
                'device panels usually round these up to the nearest 10%%.</p>%s</section>'
                % _table(["Device", "Status", "Supplies", "14-day volume", "#Lifetime pages"],
                         dev_rows))

    body = freshness_chip(feed.state, feed.age, "Last poll") + cards + att_html + dev_html
    return shell("Print fleet", "fleet", available, body, generated,
                 subtitle="Which printers need a human today")


# --------------------------------------------------------------------------- #
# What changed
# --------------------------------------------------------------------------- #

KINDWORD = {"added": "Added", "removed": "Removed", "changed": "Changed"}


def build_changes(m, feed, available, generated):
    if not m:
        return shell("What changed", "changes", available,
                     _feed_empty("Change history", feed,
                                 "It appears after the tenant has been documented twice - "
                                 "there is nothing to compare until then."), generated)
    if not m["events"]:
        body = (freshness_chip(feed.state, feed.age, "History") +
                '<section><div class="empty">No configuration changes detected across %d '
                'snapshots.</div></section>' % m["snapshot_count"])
        return shell("What changed", "changes", available, body, generated,
                     subtitle="Computed by diffing archived snapshots")

    by_ts = {}
    for e in m["events"]:
        by_ts.setdefault(e["ts"], []).append(e)
    blocks = []
    for ts in sorted(by_ts, reverse=True):
        rows = "".join(
            '<div class="chg"><span class="kind">%s</span><span>%s%s '
            '<span class="cat">&middot; %s</span></span></div>'
            % (esc(KINDWORD.get(e["kind"], e["kind"])), esc(e["item"]),
               (" &mdash; %s" % esc(e["detail"])) if e["detail"] else "",
               esc(e["category"]))
            for e in sorted(by_ts[ts], key=lambda x: (x["category"], x["kind"], x["item"])))
        blocks.append('<div class="chg-ts">%s</div>%s'
                      % (esc(str(ts).replace("T", " ").replace("Z", " UTC")), rows))

    cats = {}
    for e in m["events"]:
        cats[e["category"]] = cats.get(e["category"], 0) + 1
    cat_html = ('<section><h2>By category</h2>%s</section>'
                % bar_rows([{"label": k, "count": v}
                            for k, v in sorted(cats.items(), key=lambda kv: -kv[1])]))

    body = (freshness_chip(feed.state, feed.age, "History") +
            _cards([
                {"k": "Changes", "v": len(m["events"])},
                {"k": "Snapshots", "v": m["snapshot_count"]},
                {"k": "Range", "v": str(m["first"] or "")[:10],
                 "d": "through %s" % str(m["last"] or "")[:10]},
            ]) + cat_html +
            '<section><h2>Timeline</h2><p class="note">Newest first. Only snapshots where '
            'something actually changed appear here.</p>%s</section>' % "".join(blocks))
    return shell("What changed", "changes", available, body, generated,
                 subtitle="Computed by diffing archived snapshots &mdash; the tenant's own history")
