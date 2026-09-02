"""The six pages. Each renders only its own domain - that's the whole point."""

from __future__ import annotations

from . import alerts as alert_rules
from .actions import CA_GAP_ACTION, next_step  # noqa: F401 - re-exported for callers and tests
from .render import (ICONS, bar_rows, badge, delta_badge, esc, fmt_metric, freshness_chip,
                     meter, money, muted_badge, shell, sparkline, status_badge, trend_card)


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
# Plain-English next steps. A finding is only useful if the reader knows what
# to DO about it, so every finding carries one line saying exactly that -
# wherever it appears. Conditional Access gaps are keyed by the gap analysis's
# own check ids (entra-tenant-docs, Get-CaGapAnalysis).
# --------------------------------------------------------------------------- #

def _act(text):
    """The arrow line under a finding. Empty when there is nothing to say."""
    return ('<span class="act">%s</span>' % esc(text)) if text else ""


def _trend_section(title, block, keys=None):
    """Posture over time as small multiples. With fewer than two days of data
    it says so calmly rather than drawing a one-point chart; with none at all
    it renders nothing (the feed is simply not configured)."""
    if not block:
        return ""
    if block["points"] < 2:
        return ('<section><h2>%s</h2><p class="note">Trends appear after the second '
                'refresh &mdash; one snapshot so far (%s). Each refresh adds a day.</p></section>'
                % (esc(title), esc(block["last"] or "")))
    metrics = block["metrics"]
    if keys:
        metrics = [m for m in metrics if m["key"] in keys]
    metrics = [m for m in metrics if len(m["values"]) >= 1]
    if not metrics:
        return ""
    note = ("%d days of snapshots, %s &rarr; %s. Each card is the value now and how it "
            "moved since the previous day; green means moving the right way."
            % (block["points"], esc(block["first"]), esc(block["last"])))
    return ('<section><h2>%s</h2><p class="note">%s</p><div class="trend-grid">%s</div></section>'
            % (esc(title), note, "".join(trend_card(m) for m in metrics)))


def _tile_trend(metric):
    """Sparkline + delta badge for an overview tile; empty without two points."""
    if not metric or len(metric.get("values") or []) < 2:
        return ""
    return ('<div class="spark">%s%s</div>'
            % (sparkline(metric["values"], width=160, height=26), delta_badge(metric)))


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

def _refresh_banners(refresh):
    """Only when a person must act (see model.refresh_model). A refresh that
    simply worked adds nothing here - the footer note carries the schedule."""
    if not refresh or not refresh.get("banners"):
        return ""
    out = []
    for b in refresh["banners"]:
        why = ('<span class="why">%s</span>' % esc(b["detail"])) if b.get("detail") else ""
        out.append('<div class="banner %s" role="status">%s<div>%s%s</div></div>'
                   % (esc(b.get("tone") or "warning"), ICONS["warn"], esc(b["text"]), why))
    return "".join(out)


def build_overview(models, feeds, available, generated):
    tiles = []

    def tile(key, title, headline, sub, feed, extra="", trend=""):
        # `key` is the PAGE name; `feed` is the loaded feed that backs it -
        # they are deliberately different (identity <- tenant, changes <- history).
        # `trend` is an optional sparkline+delta row: direction at a glance.
        f = feed
        if f is None or not f.ok:
            note = (f.status_note if f else "not configured")
            head = "Nothing yet" if (f is not None and getattr(f, "missing", False)) else "Not configured"
            return ('<div class="tile off"><div class="top"><span class="title">%s</span></div>'
                    '<div class="headline">%s</div>'
                    '<div class="sub">%s</div></div>' % (esc(title), esc(head), esc(note)))
        return ('<a class="tile" href="%s.html"><div class="top"><span class="title">%s</span>%s</div>'
                '<div class="headline">%s</div><div class="sub">%s</div>%s'
                '<div class="foot"><span class="dot %s"></span>%s</div></a>'
                % (esc(key), esc(title), extra, esc(headline), esc(sub), trend,
                   esc(f.state), esc(f.age)))

    trends = models.get("trends") or {}
    sec_trend = trends.get("security") or {}
    lic_trend = trends.get("licensing") or {}

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
        # Direction at a glance: the MFA coverage trend under the headline.
        tiles.append(tile("security", "Security", head,
                          "MFA coverage - %d stale accounts, %d guests"
                          % (len(sec["stale_members"]), sec["guests_total"] or 0),
                          feeds["security"], extra,
                          _tile_trend((sec_trend.get("by_key") or {}).get("mfa_percent"))))
    else:
        tiles.append(tile("security", "Security", "", "", feeds.get("security")))

    lic = models.get("licensing")
    if lic:
        n = len(lic["candidates"])
        extra = badge("warning", "warn", "%d to review" % n) if n else badge("good", "check", "clean")
        lcost = lic.get("costing") or {}
        lkeys = lic_trend.get("by_key") or {}
        if lcost.get("HasPrices"):
            # Money leads when we have it: the annual unused figure, then the
            # reclaimable figure as the subline - and the $/month trend beneath.
            headline = money(lcost.get("UnusedSeatsAnnual"), lcost.get("Currency") or "$")
            sub = ("unused seats / year - %s/yr reclaimable now"
                   % money(lcost.get("ReclaimableAnnual"), lcost.get("Currency") or "$"))
            tiles.append(tile("licensing", "Licensing", headline, sub, feeds["licensing"], extra,
                              _tile_trend(lkeys.get("unused_monthly") or lkeys.get("unassigned"))))
        else:
            tiles.append(tile("licensing", "Licensing", lic["unassigned_total"],
                              "unassigned seats - %d licensed users" % (lic["licensed_users"] or 0),
                              feeds["licensing"], extra, _tile_trend(lkeys.get("unassigned"))))
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

    def urge(rank, domain, what, detail, action):
        urgent.append((rank, domain, what, detail or "", action or ""))

    if ident:
        for g in ident["gaps_failing"]:
            sev = g.get("Severity")
            if sev in ("critical", "warning"):
                urge(0 if sev == "critical" else 2, "Identity",
                     "CA gap: %s" % g.get("Title"), g.get("Detail"),
                     next_step("ca-gap", g.get("Id")))
    if sec:
        for a in sec["admins_without_mfa"][:5]:
            urge(0, "Security", "Admin without MFA: %s" % a.get("DisplayName"), a.get("Roles"),
                 next_step("admin-no-mfa"))
    if fl:
        for d in fl["attention"]:
            if d["status"] in ("error", "offline"):
                urge(1, "Print fleet", "%s: %s" % (d["name"], d["status"]), d["detail"],
                     next_step("fleet", d["status"]))
    if lic:
        # A disabled account still holding a paid seat is money leaking, and it
        # is the one licensing finding that needs no conversation first.
        for c in lic["disabled_holders"][:5]:
            urge(2, "Licensing",
                 "Disabled account still licensed: %s" % (c.get("DisplayName") or c.get("UserPrincipalName")),
                 c.get("Licenses") or c.get("Reason"),
                 next_step("disabled-licensed"))

    urgent.sort(key=lambda u: u[0])
    urgent_html = ""
    if urgent:
        shown = urgent[:12]
        rows = "".join(
            '<div class="chg"><span class="kind">%s</span><span>%s%s%s</span></div>'
            % (esc(dom), esc(what),
               (' <span class="cat">&mdash; %s</span>' % esc(detail)) if detail else "",
               _act(action))
            for _rank, dom, what, detail, action in shown)
        note = ("Pulled from every domain below. %d item%s."
                % (len(urgent), "" if len(urgent) == 1 else "s"))
        if len(shown) < len(urgent):
            note = ("Pulled from every domain below. Top %d of %d - the rest are on their own pages."
                    % (len(shown), len(urgent)))
        urgent_html = ('<section><h2>Needs a human</h2>'
                       '<p class="note">%s</p>%s</section>' % (note, rows))

    # refresh_status is about the refresh itself, not a domain: when it is old,
    # every real feed is old too and already listed - naming it again is noise.
    stale = [f for f in feeds.values() if f.ok and f.state == "stale" and f.key != "refresh_status"]
    stale_html = ""
    if stale:
        stale_html = ('<section><h2>Stale data</h2><p class="note">These feeds are old enough '
                      'that the numbers above may not reflect reality. Re-run the tool that '
                      'produces each one.</p>%s</section>'
                      % "".join('<div class="chg"><span class="kind">%s</span>'
                                '<span>%s <span class="cat">&mdash; %s</span></span></div>'
                                % (esc(f.label), esc(f.age), esc(f.path)) for f in stale))

    body = ('%s<div class="tiles">%s</div>%s%s'
            % (_refresh_banners(models.get("refresh")), "".join(tiles), urgent_html, stale_html))
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
            # Only a FAILING gap gets a next step - a passing check has nothing to do.
            act = _act(next_step("ca-gap", g.get("Id"))) if g.get("Result") == "fail" else ""
            rows.append('<div class="chg"><span style="flex:none;width:132px">%s</span>'
                        '<span>%s%s%s</span></div>'
                        % (b, esc(g.get("Title")),
                           (' <span class="cat">&mdash; %s</span>' % esc(g.get("Detail")))
                           if g.get("Detail") else "", act))
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

def build_security(m, feed, available, generated, trend=None):
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
                   '&mdash; they just need a different fix. %s</p>%s</section>'
                   % (esc(next_step("admin-no-mfa")),
                      _table(["Name", "UPN", "Roles"], admin_rows,
                             "Every role holder has MFA registered.")))

    roles_html = ('<section><h2>Role assignments</h2>%s</section>'
                  % bar_rows([{"label": r.get("Role"), "count": r.get("Assignments")}
                              for r in m["role_summary"][:15]]))

    legacy_html = ""
    if m["legacy_available"]:
        rows = [[esc(s.get("ClientApp")), esc(s.get("SignIns")), esc(s.get("DistinctUsers"))]
                for s in m["legacy_summary"]]
        legacy_html = ('<section><h2>Legacy authentication</h2>'
                       '<p class="note">Sign-ins over protocols that cannot do modern auth. '
                       'Anything here is a path around your CA policies. %s</p>%s</section>'
                       % (esc(next_step("legacy-auth")),
                          _table(["Client app", "#Sign-ins", "#Users"], rows,
                                 "No legacy-auth sign-ins in the window.")))
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
                  'job &mdash; check before disabling. %s</p>%s%s</section>'
                  % (m["stale_days"], esc(next_step("stale-account")),
                     _table(["Name", "UPN", "Last sign-in"], stale_rows, "No stale accounts."),
                     more))

    # Posture over time sits right under the cards: the same numbers, as a story.
    trend_html = _trend_section("Posture over time", trend)

    body = (freshness_chip(feed.state, feed.age, "Snapshot") + cards + trend_html + admins_html +
            legacy_html + roles_html + stale_html)
    return shell("Security", "security", available, body, generated,
                 subtitle="Posture: who can act, who is exposed, what is still reachable the old way")


# --------------------------------------------------------------------------- #
# Licensing
# --------------------------------------------------------------------------- #

def build_licensing(m, feed, available, generated, trend=None):
    if not m:
        return shell("Licensing", "licensing", available,
                     _feed_empty("Licensing", feed,
                                 "Run Get-LicenseWasteReport.ps1 with -JsonPath "
                                 "(the Refresh shortcut does this for you)."),
                     generated)
    # Costing rides in only when a price list was used. `priced` means at least
    # one SKU actually has a number; a price file that exists but is still blank
    # gets a gentle nudge instead of a dollar figure.
    cost = m.get("costing") or {}
    cur = cost.get("Currency") or "$"
    priced = bool(cost.get("HasPrices"))

    card_items = [
        {"k": "Unassigned seats", "v": m["unassigned_total"], "d": "across real SKUs"},
        {"k": "Licensed users", "v": m["licensed_users"] or "-"},
        {"k": "Disabled but licensed", "v": len(m["disabled_holders"])},
        {"k": "Stale but licensed", "v": len(m["stale_holders"]),
         "d": "over %s days" % m["stale_days"]},
        {"k": "SKUs tracked", "v": len(m["skus"])},
    ]
    if priced:
        # Lead with the money - it is the number a budget owner reads first.
        card_items[:0] = [
            {"k": "Unused seats / year", "v": money(cost.get("UnusedSeatsAnnual"), cur),
             "d": "%s / month" % money(cost.get("UnusedSeatsMonthly"), cur)},
            {"k": "Reclaimable now / year", "v": money(cost.get("ReclaimableAnnual"), cur),
             "d": "disabled & stale accounts"},
        ]
    cards = _cards(card_items)

    money_note = ""
    if priced and cost.get("UnpricedSkuCount"):
        money_note = ('<p class="note">%d SKU(s) have no price yet, so they show seats only. '
                      'Add them to prices.ini to cost them: %s</p>'
                      % (cost.get("UnpricedSkuCount"),
                         esc(", ".join(cost.get("UnpricedSkus") or []))))
    elif (m.get("costing") is not None) and not priced:
        money_note = ('<p class="note">Your SKUs are listed in the license tool\'s '
                      '<code>prices.ini</code> &mdash; type a per-seat price next to each and '
                      'run Refresh again to see the dollar waste here.</p>')
    else:
        # First run: no price list was used yet. Refresh has just written a
        # starter prices.ini with these SKUs, so point the user at it.
        money_note = ('<p class="note">Want these seats in dollars? Add per-seat prices to the '
                      'license tool\'s <code>prices.ini</code> (Refresh has listed your SKUs '
                      'there for you) and run Refresh again.</p>')

    sku_rows = []
    for s in m["skus"]:
        purchased = int(s.get("Purchased") or 0)
        assigned = int(s.get("Assigned") or 0)
        pct = round(100.0 * assigned / purchased) if purchased else None
        row = [
            esc(s.get("Sku")), meter(pct),
            "%s / %s" % (esc(assigned), esc(purchased)),
            esc(s.get("Unassigned")),
        ]
        if priced:
            uc = s.get("UnusedMonthlyCost")
            row.append(money(uc, cur) if uc is not None else '<span class="muted">&mdash;</span>')
        sku_rows.append(row)
    sku_cols = ["SKU", "Utilisation", "Assigned", "#Unassigned"]
    if priced:
        sku_cols.append("#$/mo unused")
    sku_html = ('<section><h2>Seats purchased vs assigned</h2>'
                '<p class="note">Sorted by how many seats are sitting unused.</p>%s%s</section>'
                % (_table(sku_cols, sku_rows), money_note))

    cons_html = ""
    if m["consumption_skus"]:
        rows = [[esc(s.get("Sku")), esc(s.get("Assigned"))] for s in m["consumption_skus"]]
        cons_html = ('<section><h2>Self-service &amp; consumption SKUs</h2>'
                     '<p class="note">Microsoft reports these with tens of thousands of nominal '
                     'seats, so they are excluded from the totals above. Shown here for '
                     'completeness &mdash; what matters is how many are in use.</p>%s</section>'
                     % _table(["SKU", "#In use"], rows))

    cand_cols = ["Reason", "Name", "UPN", "Licenses"]
    if priced:
        cand_cols.append("#$/mo")
    cand_rows = []
    for c in m["candidates"][:50]:
        row = [esc(c.get("Reason")), esc(c.get("DisplayName")),
               esc(c.get("UserPrincipalName")), esc(c.get("Licenses"))]
        if priced:
            mc = c.get("MonthlyCost")
            row.append(money(mc, cur) if mc is not None else '<span class="muted">&mdash;</span>')
        cand_rows.append(row)
    cand_html = ('<section><h2>Reclaim candidates</h2>'
                 '<p class="note">Conversation starters, not verdicts &mdash; confirm with the '
                 'user\'s manager before reclaiming anything. Disabled accounts are the '
                 'exception: %s</p>%s</section>'
                 % (esc(next_step("disabled-licensed")),
                    _table(cand_cols, cand_rows, "Nothing to reclaim.")))

    # Waste over time: the dollar lines lead when the SKUs are priced; the seat
    # and headcount lines are always there. Metrics with no data simply skip.
    trend_html = _trend_section("Waste over time", trend)

    body = (freshness_chip(feed.state, feed.age, "Report") + cards + trend_html + sku_html +
            cons_html + cand_html)
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
                         esc(d["detail"] or ""), esc(d["model"] or ""),
                         esc(next_step("fleet", d["status"]))])
    att_html = ('<section><h2>Needs attention</h2>%s</section>'
                % _table(["Status", "Device", "Detail", "Model", "What to do"], att_rows,
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


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #

ALERTS_JS = r"""
/* The console is a renderer: this is the one page with any script, and all it
   does is turn the controls above into TEXT for you to copy. It talks to
   nothing, stores nothing, and never sees where your alerts go. */
(function () {
  var out = document.getElementById('settings-text');
  var btn = document.getElementById('save-btn');
  var msg = document.getElementById('save-msg');
  if (!out || !btn) { return; }
  function valueOf(el) {
    var kind = el.getAttribute('data-kind');
    if (kind === 'switch') { return el.checked ? 'yes' : 'no'; }
    if (kind === 'radio') { return el.checked ? el.value : null; }
    var v = (el.value || '').trim();
    if (kind === 'number' && v === '') { return '0'; }
    return v;
  }
  function build() {
    var els = document.querySelectorAll('[data-sec]');
    var order = [], bySection = {};
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var v = valueOf(el);
      if (v === null) { continue; }
      var sec = el.getAttribute('data-sec');
      if (!bySection[sec]) { bySection[sec] = []; order.push(sec); }
      bySection[sec].push(el.getAttribute('data-key') + ' = ' + v);
    }
    var lines = ['# IT Ops Console alert settings, made in the console.',
                 '# Double-click "Apply Alert Settings" on your desktop to use them.',
                 '# Your Teams and email settings, and any notes in alerts.ini, are kept.',
                 ''];
    for (var j = 0; j < order.length; j++) {
      lines.push('[' + order[j] + ']');
      lines = lines.concat(bySection[order[j]]);
      lines.push('');
    }
    out.value = lines.join('\n');
    if (msg) { msg.textContent = ''; }
  }
  document.addEventListener('change', build);
  document.addEventListener('input', build);
  btn.addEventListener('click', function () {
    var copied = false;
    try { out.focus(); out.select(); copied = document.execCommand('copy'); } catch (e) { copied = false; }
    var saved = false;
    try {
      var blob = new Blob([out.value], { type: 'text/plain' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'alert-settings.txt';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      saved = true;
    } catch (e) { saved = false; }
    var where = saved ? ' A copy is in your Downloads folder.' : '';
    msg.textContent = copied
      ? 'Copied. Now double-click "Apply Alert Settings" on your desktop.' + where
      : 'Select the text below and press Ctrl+C, then double-click "Apply Alert Settings" on your desktop.' + where;
  });
  build();
})();
"""


SEV_BADGE = {
    "critical": lambda: badge("critical", "warn", "critical"),
    "warning":  lambda: badge("warning", "warn", "warning"),
    "info":     lambda: muted_badge("info", "dash"),
}


def alerts_page_model(fired, cfg, state, channels):
    """Everything the Alerts page needs: where alerts go, what is firing, every
    rule with its current setting, how alerts.ini was read, what was last sent."""
    when = cfg["send"]["when"]
    day = cfg["send"]["digest_day"]
    em = cfg["email"]
    rules = []
    for tab, label in alert_rules.TABS:
        rows = []
        for r in alert_rules.CATALOG:
            if r.tab != tab:
                continue
            val = cfg["rules"].get(r.key, r.default)
            rows.append({"id": r.id, "label": r.label, "help": r.help, "severity": r.severity,
                         "kind": r.kind, "unit": r.unit, "value": val,
                         "setting": alert_rules.rule_setting_text(r, val),
                         "on": bool(val) if r.kind == "switch" else (float(val or 0) > 0)})
        rules.append({"tab": tab, "label": label, "notify": cfg["tabs"].get(tab, {}).get("notify", True), "rows": rows})
    hist = list((state or {}).get("history") or [])
    return {
        "teams": channels["teams"],
        "email": channels["email"],
        "email_to": list(em["to"]),
        "email_relay": em["smtp_server"],
        "any_channel": channels["any"],
        "when": when,
        "when_text": ("only when an alert appears, gets worse, or clears" if when == "changes"
                      else "a summary after every refresh"),
        "digest_day": day,
        "digest_text": ("every %s" % day.capitalize()) if day else "never",
        "console_link": cfg["send"]["console_link"],
        "config_path": cfg.get("path") or "alerts.ini",
        "config_exists": bool(cfg.get("exists")),
        "problems": list(cfg.get("problems") or []),
        "fired": fired,
        "by_tab": _group_alerts(fired),
        "rules": rules,
        "last_sent": (state or {}).get("last_sent"),
        "history": hist[:6],
    }


def _numtxt(v):
    """A threshold as a person would type it: 90, not 90.0."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return "%d" % int(f) if abs(f - round(f)) < 1e-9 else ("%g" % f)


def _group_alerts(fired):
    out = []
    for tab, label in alert_rules.TABS:
        items = [a for a in fired if a.get("tab") == tab]
        if items:
            out.append((label, items))
    return out


def build_alerts(am, available, generated):
    if not am:
        return shell("Alerts", "alerts", available, _empty("Alerts are not available in this build."), generated)

    # -- where alerts go -------------------------------------------------- #
    kv = []
    kv.append(("Teams", "connected - a Workflows URL is set" if am["teams"] else "not set up"))
    if am["email"]:
        kv.append(("Email", "to %s via %s" % (esc(", ".join(am["email_to"])), esc(am["email_relay"]))))
    else:
        kv.append(("Email", "not set up"))
    kv.append(("Last message", esc(am["last_sent"][:16].replace("T", " ") + " UTC") if am["last_sent"] else "none yet"))
    where = '<div class="kv">%s</div>' % "".join(
        '<span class="k">%s</span><span>%s</span>' % (esc(k), v if k in ("Email", "Last message") else esc(v))
        for k, v in kv)
    setup_note = ""
    if not am["any_channel"]:
        setup_note = ('<div class="banner warning" role="status">%s<div>Alerts are worked out on every refresh '
                      'but not sent anywhere yet. To receive them, open <code>%s</code> and paste your Teams '
                      'channel\'s Workflows URL under <code>[teams]</code>, or fill in <code>[email]</code>; then '
                      'run <code>python notify.py --test</code> from the console folder to see one arrive.</div></div>'
                      % (ICONS["warn"], esc(am["config_path"])))
    where_html = ('<section><h2>Where alerts go</h2><p class="note">The refresh sends a message %s. '
                  'Which channel is set in alerts.ini; everything else below you can change here.</p>%s%s</section>'
                  % (esc(am["when_text"]), setup_note, where))

    # -- firing now ------------------------------------------------------- #
    n = len(am["fired"])
    if n:
        rows = []
        for label, items in am["by_tab"]:
            for a in items:
                rows.append('<div class="chg"><span class="kind">%s</span><span>%s %s%s%s</span></div>'
                            % (esc(label), SEV_BADGE.get(a.get("severity"), SEV_BADGE["info"])(), esc(a.get("title")),
                               (' <span class="cat">&mdash; %s</span>' % esc(a["detail"])) if a.get("detail") else "",
                               _act(a.get("action"))))
        firing = ('<section><h2>Firing now</h2><p class="note">%d alert%s from the rules that are on. '
                  'Whether a message goes out depends on what changed since the last one.</p>%s</section>'
                  % (n, "" if n == 1 else "s", "".join(rows)))
    else:
        firing = ('<section><h2>Firing now</h2><p class="note">Nothing - every rule that is on is quiet.'
                  '</p></section>')

    # -- what is watched: the editor ------------------------------------- #
    # These controls only ever produce TEXT, in the box at the bottom of the
    # section: the page cannot write to your install by itself, and it is
    # deliberately never given your Teams webhook or mail settings, because
    # console-site is a folder people copy onto shares.
    days = [("", "never")] + [(d, d.capitalize()) for d in alert_rules.WEEKDAYS]
    day_options = "".join('<option value="%s"%s>%s</option>'
                          % (esc(v), " selected" if v == am["digest_day"] else "", esc(t))
                          for v, t in days)
    send_ctl = (
        '<div class="kv sendctl">'
        '<span class="k">Send a message</span><span>'
        '<label class="opt"><input type="radio" name="when" data-sec="send" data-key="when" '
        'data-kind="radio" value="changes"%s> only when something is new, worse or cleared</label>'
        '<label class="opt"><input type="radio" name="when" data-sec="send" data-key="when" '
        'data-kind="radio" value="every-refresh"%s> after every refresh, even when nothing changed</label>'
        '</span>'
        '<span class="k">Weekly summary</span><span><select data-sec="send" data-key="digest_day" '
        'data-kind="select">%s</select> <span class="help">everything still open, on this day</span></span>'
        '<span class="k">Console link</span><span><input type="text" class="wide" data-sec="send" '
        'data-key="console_link" data-kind="text" value="%s" placeholder="https://... or \\\\server\\share"> '
        '<span class="help">shown at the bottom of every message</span></span>'
        '</div>'
        % (" checked" if am["when"] != "every-refresh" else "",
           " checked" if am["when"] == "every-refresh" else "",
           day_options, esc(am["console_link"])))

    tables = []
    for t in am["rules"]:
        trs = []
        for r in t["rows"]:
            rid = "r-%s-%s" % (t["tab"], r["id"])
            if r["kind"] == "switch":
                ctl = ('<input type="checkbox" id="%s" data-sec="%s" data-key="%s" data-kind="switch"%s>'
                       % (esc(rid), esc(t["tab"]), esc(r["id"]), " checked" if r["value"] else ""))
                unit = ""
            else:
                ctl = ('<input type="number" class="num" id="%s" data-sec="%s" data-key="%s" '
                       'data-kind="number" min="0" step="1" value="%s">'
                       % (esc(rid), esc(t["tab"]), esc(r["id"]), esc(_numtxt(r["value"]))))
                unit = '<span class="unit">%s</span>' % esc(r["unit"] if r["unit"] != "$" else "$ / month")
            trs.append('<tr><td class="ctl">%s%s</td><td><label for="%s">%s</label>'
                       '<span class="help">%s</span></td><td>%s</td></tr>'
                       % (ctl, unit, esc(rid), esc(r["label"]), esc(r["help"]),
                          SEV_BADGE.get(r["severity"], SEV_BADGE["info"])()))
        tables.append('<h3><label class="tabtoggle"><input type="checkbox" data-sec="%s" data-key="notify" '
                      'data-kind="switch"%s> %s</label></h3>'
                      '<div class="scroll"><table class="rules"><thead><tr><th>Setting</th><th>Rule</th>'
                      '<th>Severity</th></tr></thead><tbody>%s</tbody></table></div>'
                      % (esc(t["tab"]), " checked" if t["notify"] else "", esc(t["label"]), "".join(trs)))

    savebox = (
        '<div class="savebox">'
        '<button type="button" id="save-btn" class="btn">Save settings</button>'
        '<span id="save-msg" class="savemsg"></span>'
        '<p class="note">This is exactly what gets applied. Where alerts go is not in it and is never '
        'changed from this page. Reload the page to go back to the saved settings.</p>'
        '<textarea id="settings-text" rows="10" readonly spellcheck="false"></textarea>'
        '</div>'
        '<noscript><p class="note">This page needs JavaScript to build the settings for you. '
        'Without it, edit alerts.ini directly - every rule above is one line in that file.</p></noscript>')

    watched = ('<section><h2>Change what you are told about</h2>'
               '<p class="note">Tick, untick, or change a number - 0 means off; the box beside a tab '
               'name silences that whole tab at once. Then click <b>Save settings</b> and double-click '
               '<b>Apply Alert Settings</b> on your desktop. Nothing here changes until you do both.</p>'
               '%s%s%s</section>'
               % (send_ctl, "".join(tables), savebox))

    # -- how the file was read ------------------------------------------- #
    if not am["config_exists"]:
        read = ('<section><h2>alerts.ini</h2><p class="note">No alerts.ini yet at %s - every rule is at its '
                'default. Copy alerts.example.ini to alerts.ini to change any of them.</p></section>' % esc(am["config_path"]))
    elif am["problems"]:
        read = ('<section><h2>alerts.ini</h2><p class="note">%d line%s could not be used as written; the rest '
                'were. Each is using its default instead:</p>%s</section>'
                % (len(am["problems"]), "" if len(am["problems"]) == 1 else "s",
                   "".join('<div class="chg"><span class="kind">line</span><span>%s</span></div>' % esc(p) for p in am["problems"])))
    else:
        read = ('<section><h2>alerts.ini</h2><p class="note">Every line in %s was understood.</p></section>'
                % esc(am["config_path"]))

    # -- recent messages -------------------------------------------------- #
    hist_html = ""
    if am["history"]:
        hist_html = ('<section><h2>Recent messages</h2>%s</section>' % "".join(
            '<div class="chg"><span class="kind">%s</span><span>%s <span class="cat">&mdash; %s</span></span></div>'
            % (esc(str(h.get("when", ""))[:10]), esc(h.get("title", "")),
               esc(", ".join(h.get("channels") or []) or "sent")) for h in am["history"]))

    body = where_html + firing + watched + read + hist_html + ("<script>%s</script>" % ALERTS_JS)
    return shell("Alerts", "alerts", available, body, generated,
                 subtitle="What this console tells you about, where, and what is firing right now.")
