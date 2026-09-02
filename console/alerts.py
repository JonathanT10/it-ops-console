"""Alert rules: what the console tells people about, and the words it uses.

The framework in one paragraph. A RULE is one entry in CATALOG: which tab it
belongs to, a plain-English label, whether it is an on/off switch or a number
(a threshold), its default, its severity, and an evaluate function that looks
at the models the pages already use and returns zero or more ALERTS. An alert
is a small dict with a stable KEY (so the same finding on two days is the same
alert), a title, a detail, and the same next-step sentence the console shows.
People turn rules on and off, or change the number, in alerts.ini - one line
per rule, one section per tab. build.py runs the catalog on every build and
writes alerts.json (what is firing now); notify.py compares that with what was
already sent and decides whether anything is worth an interruption.

Adding a rule is one entry here plus one line in alerts.example.ini; a test
keeps the two in step. Nothing in this module sends anything anywhere.
"""

from __future__ import annotations

import configparser
import os
import re

from .actions import next_step
from .sources import parse_ts, utcnow

SEVERITIES = ("critical", "warning", "info")
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

TABS = [
    ("identity",  "Identity"),
    ("security",  "Security"),
    ("licensing", "Licensing"),
    ("fleet",     "Print fleet"),
    ("changes",   "What changed"),
    ("refresh",   "Refresh"),
]
TAB_LABEL = dict(TABS)

# The two channel/settings sections; everything else in alerts.ini is a tab.
SETTINGS_SECTIONS = ("send", "teams", "email")
SEND_KEYS = ("when", "digest_day", "console_link")
TEAMS_KEYS = ("webhook",)
EMAIL_KEYS = ("smtp_server", "port", "from", "to", "use_ssl")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class Rule:
    """One thing the console can tell you about."""

    def __init__(self, rid, tab, label, kind, default, severity, evaluate, unit="", help=""):
        self.id = rid            # the alerts.ini key, e.g. admin_without_mfa
        self.tab = tab           # alerts.ini section
        self.label = label       # plain English, shown on the Alerts page
        self.kind = kind         # "switch" (yes/no) or "number" (a threshold; 0 or blank = off)
        self.default = default   # True/False or a number
        self.severity = severity
        self.evaluate = evaluate # fn(ctx, value) -> list of alert dicts
        self.unit = unit         # "", "%", "days", "seats", "$"
        self.help = help         # one sentence for the ini comment and the page

    @property
    def key(self):
        return "%s.%s" % (self.tab, self.id)


def _alert(rule, key_suffix, title, detail="", action="", severity=None, transient=False):
    """Every alert has the same shape. `key` is what makes 'the same alert
    tomorrow' the same alert; `transient` marks an event (something that
    happened) as opposed to a state (something that is the case)."""
    return {
        "key": "%s/%s/%s" % (rule.tab, rule.id, key_suffix) if key_suffix else "%s/%s" % (rule.tab, rule.id),
        "tab": rule.tab,
        "rule": rule.id,
        "severity": severity or rule.severity,
        "title": str(title),
        "detail": str(detail or ""),
        "action": str(action or ""),
        "transient": bool(transient),
    }


def _days_until(ts_text, now):
    ts = parse_ts(ts_text)
    if ts is None:
        return None
    return (ts - now).total_seconds() / 86400.0


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

def _ca_gaps(ctx, severity):
    ident = ctx.models.get("identity")
    if not ident:
        return []
    return [g for g in ident["gaps_failing"] if g.get("Severity") == severity]


def ev_ca_gap_critical(ctx, rule, value):
    return [_alert(rule, g.get("Id"), "CA gap: %s" % g.get("Title"), g.get("Detail"),
                   next_step("ca-gap", g.get("Id"))) for g in _ca_gaps(ctx, "critical")]


def ev_ca_gap_warning(ctx, rule, value):
    return [_alert(rule, g.get("Id"), "CA gap: %s" % g.get("Title"), g.get("Detail"),
                   next_step("ca-gap", g.get("Id"))) for g in _ca_gaps(ctx, "warning")]


def _credentials(ctx):
    ident = ctx.models.get("identity")
    if not ident:
        return
    for app in ident["applications"]:
        for cred in app.get("Credentials") or []:
            yield app, cred


def ev_app_credential_expired(ctx, rule, value):
    out = []
    for app, cred in _credentials(ctx):
        left = _days_until(cred.get("ExpiresUtc"), ctx.now)
        if left is not None and left < 0:
            out.append(_alert(rule, "%s/%s" % (app.get("AppId"), cred.get("Name")),
                              "Expired app credential: %s" % app.get("Name"),
                              "%s '%s' expired %s" % (cred.get("Type"), cred.get("Name"),
                                                      str(cred.get("ExpiresUtc"))[:10]),
                              next_step("app-credential")))
    return out


def ev_app_credential_expiring(ctx, rule, value):
    out = []
    for app, cred in _credentials(ctx):
        left = _days_until(cred.get("ExpiresUtc"), ctx.now)
        if left is not None and 0 <= left <= value:
            out.append(_alert(rule, "%s/%s" % (app.get("AppId"), cred.get("Name")),
                              "App credential expires in %d day%s: %s"
                              % (int(left), "" if int(left) == 1 else "s", app.get("Name")),
                              "%s '%s' expires %s" % (cred.get("Type"), cred.get("Name"),
                                                      str(cred.get("ExpiresUtc"))[:10]),
                              next_step("app-credential")))
    return out


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #

def ev_admin_without_mfa(ctx, rule, value):
    sec = ctx.models.get("security")
    if not sec:
        return []
    return [_alert(rule, a.get("UserPrincipalName") or a.get("DisplayName"),
                   "Admin without MFA: %s" % (a.get("DisplayName") or a.get("UserPrincipalName")),
                   a.get("Roles"), next_step("admin-no-mfa")) for a in sec["admins_without_mfa"]]


def ev_mfa_coverage_below(ctx, rule, value):
    sec = ctx.models.get("security")
    mfa = (sec or {}).get("mfa")
    if not mfa or mfa.get("percent") is None:
        return []
    pct = float(mfa["percent"])
    if pct >= value:
        return []
    return [_alert(rule, "", "MFA coverage is %s%% (your line is %s%%)" % (_num(pct), _num(value)),
                   "%s of %s people have no MFA method registered"
                   % (mfa.get("not_registered"), mfa.get("total")),
                   next_step("mfa-coverage"))]


def ev_stale_accounts_above(ctx, rule, value):
    sec = ctx.models.get("security")
    if not sec:
        return []
    n = len(sec["stale_members"])
    if n <= value:
        return []
    return [_alert(rule, "", "%d stale accounts (your line is %d)" % (n, int(value)),
                   "no sign-in for %s days" % sec.get("stale_days"), next_step("stale-account"))]


def ev_legacy_auth_signins(ctx, rule, value):
    sec = ctx.models.get("security")
    if not sec or not sec["legacy_available"]:
        return []
    total = sum(int(s.get("SignIns") or 0) for s in sec["legacy_summary"])
    if total <= 0:
        return []
    top = sorted(sec["legacy_summary"], key=lambda s: -int(s.get("SignIns") or 0))[:3]
    return [_alert(rule, "", "Legacy authentication is still in use: %d sign-ins" % total,
                   ", ".join("%s (%s)" % (s.get("Protocol") or s.get("ClientApp") or "?", s.get("SignIns"))
                             for s in top),
                   next_step("legacy-auth"))]


# --------------------------------------------------------------------------- #
# Licensing
# --------------------------------------------------------------------------- #

def ev_disabled_account_licensed(ctx, rule, value):
    lic = ctx.models.get("licensing")
    if not lic:
        return []
    cur = ((lic.get("costing") or {}).get("Currency")) or "$"
    out = []
    for c in lic["disabled_holders"]:
        detail = c.get("Licenses") or c.get("Reason") or ""
        if c.get("MonthlyCost"):
            detail = "%s - %s%s/month" % (detail, cur, _num(c["MonthlyCost"]))
        out.append(_alert(rule, c.get("UserPrincipalName") or c.get("DisplayName"),
                          "Disabled account still licensed: %s"
                          % (c.get("DisplayName") or c.get("UserPrincipalName")),
                          detail, next_step("disabled-licensed")))
    return out


def ev_unused_seats_above(ctx, rule, value):
    lic = ctx.models.get("licensing")
    if not lic:
        return []
    n = int(lic["unassigned_total"] or 0)
    if n <= value:
        return []
    return [_alert(rule, "", "%d paid seats are unassigned (your line is %d)" % (n, int(value)),
                   "seats you pay for that nobody holds", next_step("unused-seats"))]


def ev_unused_monthly_cost_above(ctx, rule, value):
    lic = ctx.models.get("licensing")
    cost = (lic or {}).get("costing") or {}
    if not cost.get("HasPrices") or cost.get("UnusedSeatsMonthly") is None:
        return []
    amt = float(cost["UnusedSeatsMonthly"])
    if amt <= value:
        return []
    cur = cost.get("Currency") or "$"
    return [_alert(rule, "", "Unused seats are costing %s%s a month (your line is %s%s)"
                   % (cur, _num(amt), cur, _num(value)),
                   "%s%s a year at today's prices" % (cur, _num(cost.get("UnusedSeatsAnnual"))),
                   next_step("unused-seats"))]


# --------------------------------------------------------------------------- #
# Print fleet
# --------------------------------------------------------------------------- #

def _fleet_by_status(ctx, status):
    fl = ctx.models.get("fleet")
    if not fl:
        return []
    return [d for d in fl["devices"] if d["status"] == status]


def ev_device_offline(ctx, rule, value):
    return [_alert(rule, d["ip"] or d["name"], "Printer offline: %s" % d["name"],
                   "last seen %s" % (str(d.get("last_seen") or "never")[:16]),
                   next_step("fleet", "offline")) for d in _fleet_by_status(ctx, "offline")]


def ev_device_error(ctx, rule, value):
    return [_alert(rule, d["ip"] or d["name"], "Printer error: %s" % d["name"],
                   d.get("detail"), next_step("fleet", "error")) for d in _fleet_by_status(ctx, "error")]


def ev_supply_below_percent(ctx, rule, value):
    fl = ctx.models.get("fleet")
    if not fl:
        return []
    out = []
    for d in fl["devices"]:
        if d["status"] == "offline":
            continue
        for s in d["supplies"]:
            if s["percent"] is not None and s["percent"] < value:
                out.append(_alert(rule, "%s/%s" % (d["ip"] or d["name"], s.get("description")),
                                  "%s: %s at %d%%" % (d["name"], s.get("description") or "supply",
                                                      int(s["percent"])),
                                  "your line is %d%%" % int(value), next_step("fleet", "warning")))
    return out


def _discovery(ctx):
    f = ctx.feeds.get("fleet_discovery")
    return (f.data or {}) if (f is not None and f.ok) else None


def ev_new_printer_found(ctx, rule, value):
    """Told once per printer, so a scan that worked says so out loud."""
    d = _discovery(ctx)
    if not d:
        return []
    out = []
    for f in d.get("FoundThisScan") or []:
        ip = f.get("Ip") or ""
        out.append(_alert(rule, "%s/%s" % (d.get("LastScanUtc") or "", ip),
                          "New printer found: %s" % (f.get("Name") or ip),
                          "%s, looking in %s" % (ip, f.get("Range") or "a configured place"),
                          next_step("new-printer"), transient=True))
    return out


def ev_discovery_problem(ctx, rule, value):
    """A place to look that could not be scanned is worse than no place at
    all: it looks like it is working and finds nothing."""
    d = _discovery(ctx)
    if not d:
        return []
    return [_alert(rule, str(p)[:60], "Cannot look for printers there", str(p),
                   next_step("discovery-problem")) for p in (d.get("Problems") or [])]


# --------------------------------------------------------------------------- #
# What changed (events, not states: reported once, never "cleared")
# --------------------------------------------------------------------------- #

CHANGE_CATEGORIES = {
    "conditional_access": ("Conditional Access",),
    "role_assignments":   ("Role assignments",),
    "app_registrations":  ("App registrations",),
    "licenses":           ("Licenses",),
    "intune":             ("Intune compliance", "Intune configuration"),
}


def _recent_events(ctx, categories):
    ch = ctx.models.get("changes")
    if not ch:
        return []
    cutoff_days = ctx.options.get("changes.recent_days", 3)
    out = []
    for e in ch["events"]:
        if e.get("category") not in categories:
            continue
        ts = parse_ts(e.get("ts"))
        if ts is None or (ctx.now - ts).total_seconds() > cutoff_days * 86400:
            continue
        out.append(e)
    return out


def _change_alerts(ctx, rule, severity):
    out = []
    for e in _recent_events(ctx, CHANGE_CATEGORIES[rule.id]):
        out.append(_alert(rule, "%s/%s/%s/%s" % (str(e["ts"])[:19], e["category"], e["kind"], e["item"]),
                          "%s %s: %s" % (e["category"], e["kind"], e["item"]),
                          e.get("detail"), next_step("change", e["category"]),
                          severity=severity, transient=True))
    return out


def ev_changes_ca(ctx, rule, value):      return _change_alerts(ctx, rule, "warning")
def ev_changes_roles(ctx, rule, value):   return _change_alerts(ctx, rule, "warning")
def ev_changes_apps(ctx, rule, value):    return _change_alerts(ctx, rule, "info")
def ev_changes_lic(ctx, rule, value):     return _change_alerts(ctx, rule, "info")
def ev_changes_intune(ctx, rule, value):  return _change_alerts(ctx, rule, "info")


# --------------------------------------------------------------------------- #
# Refresh (the run itself)
# --------------------------------------------------------------------------- #

def _refresh_data(ctx):
    f = ctx.feeds.get("refresh_status")
    return (f.data or {}) if (f is not None and f.ok) else None


def ev_could_not_sign_in(ctx, rule, value):
    d = _refresh_data(ctx)
    if not d or not d.get("Scheduled"):
        return []
    signin = d.get("SignIn") or {}
    if signin.get("Ok") is False:
        return [_alert(rule, "", "The automatic refresh could not sign in to Microsoft 365",
                       signin.get("Detail"), next_step("refresh-signin"))]
    if signin.get("Dropped"):
        return [_alert(rule, "fell-back", "The automatic refresh had to fall back to another sign-in",
                       str(signin["Dropped"][0]), next_step("refresh-signin"), severity="warning")]
    return []


def ev_collector_failed(ctx, rule, value):
    d = _refresh_data(ctx)
    if not d:
        return []
    out = []
    for s in d.get("Steps") or []:
        if s.get("Status") in ("FAILED", "missing") and s.get("Step") not in ("sign-in", "console build"):
            out.append(_alert(rule, s.get("Step"), "%s did not complete" % s.get("Step"),
                              s.get("Detail"), next_step("collector-failed")))
    return out


def ev_certificate_expiring_days(ctx, rule, value):
    d = _refresh_data(ctx)
    cert = (d or {}).get("Certificate")
    if not cert or cert.get("DaysLeft") is None:
        return []
    left = int(cert["DaysLeft"])
    if left < 0:
        return [_alert(rule, "", "The automatic-refresh certificate expired on %s" % cert.get("Expires"),
                       "unattended refreshes cannot sign in until it is renewed",
                       next_step("certificate"), severity="critical")]
    if left <= value:
        return [_alert(rule, "", "The automatic-refresh certificate expires in %d day%s (%s)"
                       % (left, "" if left == 1 else "s", cert.get("Expires")),
                       "your line is %d days" % int(value), next_step("certificate"))]
    return []


# History folders are archives: their newest snapshot is naturally older than
# the live feed beside them, and the live feed already says when it is stale.
NOT_STALE_CHECKED = ("refresh_status", "history", "security_history", "licensing_history")


def ev_data_stale_days(ctx, rule, value):
    out = []
    for key, f in ctx.feeds.items():
        if key in NOT_STALE_CHECKED or f is None or not f.ok or f.ts is None:
            continue
        days = (ctx.now - f.ts).total_seconds() / 86400.0
        if days > value:
            out.append(_alert(rule, key, "%s data is %d days old" % (f.label, int(days)),
                              "your line is %d days" % int(value), next_step("stale-data")))
    return out


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #

CATALOG = [
    # identity
    Rule("ca_gap_critical", "identity", "A critical Conditional Access gap", "switch", True, "critical",
         ev_ca_gap_critical, help="A must-have sign-in protection is missing (MFA for everyone, legacy auth blocked, admins covered)."),
    Rule("ca_gap_warning", "identity", "A Conditional Access gap worth a look", "switch", True, "warning",
         ev_ca_gap_warning, help="Weaker gaps: report-only policies lingering, guests uncovered, unused locations."),
    Rule("app_credential_expired", "identity", "An app credential has expired", "switch", True, "critical",
         ev_app_credential_expired, help="A registered app's secret or certificate is past its date - whatever uses it has stopped working."),
    Rule("app_credential_expiring_days", "identity", "An app credential expires within", "number", 30, "warning",
         ev_app_credential_expiring, unit="days", help="Warn this many days ahead. 0 turns it off."),
    # security
    Rule("admin_without_mfa", "security", "An admin has no MFA", "switch", True, "critical",
         ev_admin_without_mfa, help="Any account holding an admin role with no MFA method registered."),
    Rule("mfa_coverage_below", "security", "MFA coverage falls below", "number", 90, "warning",
         ev_mfa_coverage_below, unit="%", help="Percent of enabled people with an MFA method. 0 turns it off."),
    Rule("stale_accounts_above", "security", "Stale accounts exceed", "number", 20, "warning",
         ev_stale_accounts_above, unit="accounts", help="Accounts with no sign-in for the stale period. 0 turns it off."),
    Rule("legacy_auth_signins", "security", "Legacy authentication is still being used", "switch", True, "warning",
         ev_legacy_auth_signins, help="Sign-ins over old protocols that cannot do MFA (needs the sign-in log to be available)."),
    # licensing
    Rule("disabled_account_licensed", "licensing", "A disabled account still holds a license", "switch", True, "warning",
         ev_disabled_account_licensed, help="Money leaking with no conversation needed."),
    Rule("unused_seats_above", "licensing", "Unassigned paid seats exceed", "number", 25, "warning",
         ev_unused_seats_above, unit="seats", help="Seats you pay for that nobody holds. 0 turns it off."),
    Rule("unused_monthly_cost_above", "licensing", "Unused seats cost more per month than", "number", 0, "warning",
         ev_unused_monthly_cost_above, unit="$", help="Needs prices.ini filled in. 0 turns it off."),
    # fleet
    Rule("device_offline", "fleet", "A printer is offline", "switch", True, "warning",
         ev_device_offline, help="It stopped answering for two days."),
    Rule("device_error", "fleet", "A printer reports an error", "switch", True, "critical",
         ev_device_error, help="Jam, door open, out of toner - whatever its panel says."),
    Rule("supply_below_percent", "fleet", "A toner or drum falls below", "number", 10, "info",
         ev_supply_below_percent, unit="%", help="Per supply, per printer. 0 turns it off."),
    Rule("new_printer_found", "fleet", "A new printer was found", "switch", True, "info",
         ev_new_printer_found, help="A scan of the places you named turned up a printer you had not listed. Said once."),
    Rule("discovery_problem", "fleet", "A place to look could not be scanned", "switch", True, "warning",
         ev_discovery_problem, help="A range with a typo, or one too big to scan, finds nothing while looking like it works."),
    # changes
    Rule("conditional_access", "changes", "A Conditional Access policy was added, removed or changed", "switch", True, "warning",
         ev_changes_ca, help="Reported once, when it appears in the change log."),
    Rule("role_assignments", "changes", "An admin role was granted or removed", "switch", True, "warning",
         ev_changes_roles, help="Reported once."),
    Rule("app_registrations", "changes", "An app registration was added or removed", "switch", False, "info",
         ev_changes_apps, help="Reported once."),
    Rule("licenses", "changes", "Purchased license counts changed", "switch", False, "info",
         ev_changes_lic, help="Reported once."),
    Rule("intune", "changes", "An Intune policy or profile changed", "switch", False, "info",
         ev_changes_intune, help="Reported once."),
    # refresh
    Rule("could_not_sign_in", "refresh", "The automatic refresh could not sign in", "switch", True, "critical",
         ev_could_not_sign_in, help="Or had to fall back to a different sign-in route."),
    Rule("collector_failed", "refresh", "A collector did not complete", "switch", True, "warning",
         ev_collector_failed, help="One of the tools failed, so its page shows older data."),
    Rule("certificate_expiring_days", "refresh", "The refresh certificate expires within", "number", 30, "warning",
         ev_certificate_expiring_days, unit="days", help="Unattended refresh only. Expired is always reported. 0 turns the warning off."),
    Rule("data_stale_days", "refresh", "Any page's data is older than", "number", 3, "warning",
         ev_data_stale_days, unit="days", help="Whatever the reason, the numbers are no longer current. 0 turns it off."),
]
RULES_BY_KEY = {r.key: r for r in CATALOG}
# Per-tab options that are not rules (still validated, still shown).
TAB_OPTIONS = {
    "changes.recent_days": (3, "Only changes newer than this many days can be reported (avoids a flood on the first run)."),
}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return ("%d" % int(round(f))) if abs(f - round(f)) < 1e-9 else ("%.1f" % f)


# --------------------------------------------------------------------------- #
# Configuration (alerts.ini)
# --------------------------------------------------------------------------- #

def _parse_bool(text):
    t = str(text).strip().lower()
    if t in ("yes", "true", "on", "1"):
        return True
    if t in ("no", "false", "off", "0", ""):
        return False
    return None


def default_config():
    return {
        "send": {"when": "changes", "digest_day": "monday", "console_link": ""},
        "teams": {"webhook": ""},
        "email": {"smtp_server": "", "port": 25, "from": "", "to": [], "use_ssl": False},
        "tabs": {tab: {"notify": True} for tab, _ in TABS},
        "rules": {r.key: r.default for r in CATALOG},
        "options": {k: v[0] for k, v in TAB_OPTIONS.items()},
        "problems": [],      # plain sentences about lines that could not be used
        "unreadable": False, # the file itself will not parse - not just one line
        "path": None,
        "exists": False,
    }


def load_config(path):
    """Read alerts.ini. Missing file = every default and nothing configured to
    send. Unknown keys and unusable values are collected as `problems` (and
    shown on the Alerts page) rather than raised - one typo must not stop the
    console from building."""
    cfg = default_config()
    cfg["path"] = path
    if not path or not os.path.exists(path):
        return cfg
    cfg["exists"] = True
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    cp.optionxform = str
    try:
        cp.read(path, encoding="utf-8-sig")
    except configparser.Error as e:
        # Keep the file's own path out of the sentence: the same fault must
        # read the same whether it is seen in alerts.ini or in a copy of it,
        # so "was this already broken?" has an honest answer.
        detail = re.sub(r"\s*While reading from '[^']*'", "", str(e)).strip()
        cfg["unreadable"] = True
        cfg["problems"].append("alerts.ini could not be read: %s" % detail)
        return cfg
    problems = cfg["problems"]

    for section in cp.sections():
        sec = section.strip().lower()
        if sec == "send":
            for k, v in cp.items(section):
                k = k.strip().lower(); v = v.strip()
                if k == "when":
                    if v.lower() in ("changes", "every-refresh"):
                        cfg["send"]["when"] = v.lower()
                    else:
                        problems.append("[send] when = '%s' is not 'changes' or 'every-refresh' - using 'changes'." % v)
                elif k == "digest_day":
                    if v == "" or v.lower() in WEEKDAYS:
                        cfg["send"]["digest_day"] = v.lower()
                    else:
                        problems.append("[send] digest_day = '%s' is not a weekday - no weekly digest." % v)
                        cfg["send"]["digest_day"] = ""
                elif k == "console_link":
                    cfg["send"]["console_link"] = v
                else:
                    problems.append("[send] '%s' is not a setting this console knows." % k)
        elif sec == "teams":
            for k, v in cp.items(section):
                k = k.strip().lower()
                if k == "webhook":
                    cfg["teams"]["webhook"] = v.strip()
                else:
                    problems.append("[teams] '%s' is not a setting this console knows." % k)
        elif sec == "email":
            for k, v in cp.items(section):
                k = k.strip().lower(); v = v.strip()
                if k == "smtp_server":
                    cfg["email"]["smtp_server"] = v
                elif k == "port":
                    try:
                        cfg["email"]["port"] = int(v)
                    except ValueError:
                        problems.append("[email] port = '%s' is not a number - using 25." % v)
                elif k == "from":
                    cfg["email"]["from"] = v
                elif k == "to":
                    cfg["email"]["to"] = [a.strip() for a in v.replace(",", ";").split(";") if a.strip()]
                elif k == "use_ssl":
                    b = _parse_bool(v)
                    if b is None:
                        problems.append("[email] use_ssl = '%s' is not yes or no - using no." % v)
                    else:
                        cfg["email"]["use_ssl"] = b
                else:
                    problems.append("[email] '%s' is not a setting this console knows." % k)
        elif sec in TAB_LABEL:
            for k, v in cp.items(section):
                k = k.strip().lower(); v = v.strip()
                if k == "notify":
                    b = _parse_bool(v)
                    if b is None:
                        problems.append("[%s] notify = '%s' is not yes or no - leaving it on." % (sec, v))
                    else:
                        cfg["tabs"][sec]["notify"] = b
                    continue
                opt = "%s.%s" % (sec, k)
                if opt in TAB_OPTIONS:
                    try:
                        cfg["options"][opt] = float(v) if v else TAB_OPTIONS[opt][0]
                    except ValueError:
                        problems.append("[%s] %s = '%s' is not a number - using %s." % (sec, k, v, TAB_OPTIONS[opt][0]))
                    continue
                rule = RULES_BY_KEY.get(opt)
                if rule is None:
                    problems.append("[%s] '%s' is not a rule this console knows - see alerts.example.ini for the list." % (sec, k))
                    continue
                if rule.kind == "switch":
                    b = _parse_bool(v)
                    if b is None:
                        problems.append("[%s] %s = '%s' is not yes or no - using the default (%s)."
                                        % (sec, k, v, "yes" if rule.default else "no"))
                    else:
                        cfg["rules"][rule.key] = b
                else:
                    if v == "":
                        cfg["rules"][rule.key] = 0
                        continue
                    try:
                        cfg["rules"][rule.key] = float(v)
                    except ValueError:
                        problems.append("[%s] %s = '%s' is not a number - using the default (%s)."
                                        % (sec, k, v, rule.default))
        else:
            problems.append("[%s] is not a section this console knows (tabs are: %s)."
                            % (section, ", ".join(t for t, _ in TABS)))
    return cfg


def channels_configured(cfg, env=None):
    """Which delivery routes alerts.ini (or the environment) actually names."""
    env = os.environ if env is None else env
    teams = bool(cfg["teams"]["webhook"] or env.get("ITOPS_TEAMS_WEBHOOK"))
    em = cfg["email"]
    email = bool(em["smtp_server"] and em["from"] and em["to"])
    return {"teams": teams, "email": email, "any": teams or email}


def rule_setting_text(rule, value):
    """How a rule's current setting reads on the Alerts page."""
    if rule.kind == "switch":
        return "on" if value else "off"
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0
    if v <= 0:
        return "off"
    unit = rule.unit
    if unit == "$":
        return "$%s" % _num(v)
    if unit == "%":
        return "%s%%" % _num(v)
    return "%s %s" % (_num(v), unit) if unit else _num(v)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #

class Context:
    def __init__(self, models, feeds, options=None, now=None):
        self.models = models or {}
        self.feeds = feeds or {}
        self.options = options or {}
        self.now = now or utcnow()


def evaluate(cfg, models, feeds, now=None):
    """Run every rule that is on. Returns alerts sorted by severity then tab,
    each carrying the tab's plain label for whoever renders it."""
    ctx = Context(models, feeds, cfg.get("options"), now)
    out = []
    for rule in CATALOG:
        if not cfg["tabs"].get(rule.tab, {}).get("notify", True):
            continue
        value = cfg["rules"].get(rule.key, rule.default)
        if rule.kind == "switch":
            if not value:
                continue
            found = rule.evaluate(ctx, rule, True)
        else:
            try:
                v = float(value)
            except (TypeError, ValueError):
                v = 0
            if v <= 0:
                continue
            found = rule.evaluate(ctx, rule, v)
        for a in found:
            a["tab_label"] = TAB_LABEL[rule.tab]
            a["rule_label"] = rule.label
            out.append(a)
    out.sort(key=lambda a: (SEVERITY_RANK.get(a["severity"], 9), a["tab"], a["title"]))
    return out


def alerts_document(alerts, cfg, generated=None):
    """The alerts.json build.py writes: what is firing now plus how alerts.ini
    was read, so notify.py (and a person reading the file) have the whole picture."""
    now = generated or utcnow()
    return {
        "GeneratedUtc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Count": len(alerts),
        "BySeverity": {s: sum(1 for a in alerts if a["severity"] == s) for s in SEVERITIES},
        "Alerts": alerts,
        "Config": {
            "Path": cfg.get("path"),
            "Exists": bool(cfg.get("exists")),
            "When": cfg["send"]["when"],
            "DigestDay": cfg["send"]["digest_day"],
            "ConsoleLink": cfg["send"]["console_link"],
            "Problems": list(cfg.get("problems") or []),
        },
    }


# --------------------------------------------------------------------------- #
# State: what was already told (shared by notify.py and the Alerts page)
# --------------------------------------------------------------------------- #

def empty_state():
    return {"alerts": {}, "last_sent": None, "last_digest": None, "history": []}


def diff_state(alerts, state):
    """Compare what is firing now with what people were already told.

    new      - firing, never told (or told and since cleared)
    worse    - firing, told, but the severity has risen
    cleared  - was told as a state, no longer firing (events never 'clear')
    still    - firing and already told, unchanged
    """
    known = state.get("alerts") or {}
    current = {a["key"]: a for a in alerts}
    new, worse, still = [], [], []
    for key, a in current.items():
        prev = known.get(key)
        if prev is None or not prev.get("notified"):
            new.append(a)
        elif SEVERITY_RANK.get(a["severity"], 9) < SEVERITY_RANK.get(prev.get("severity"), 9):
            worse.append(a)
        else:
            still.append(a)
    cleared = [dict(prev, key=key) for key, prev in known.items()
               if key not in current and not prev.get("transient") and prev.get("notified")]
    return {"new": new, "worse": worse, "cleared": cleared, "still": still}


def apply_state(state, alerts, diff, notified, now=None):
    """Bring the state up to date after a run. `notified` says whether a
    message actually went out - only then do new/worse become 'told'."""
    now = now or utcnow()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    known = dict(state.get("alerts") or {})
    for a in alerts:
        prev = known.get(a["key"], {})
        entry = {
            "severity": a["severity"],
            "tab": a["tab"],
            "title": a["title"],
            "transient": bool(a.get("transient")),
            "first_seen": prev.get("first_seen") or stamp,
            "last_seen": stamp,
            "notified": bool(prev.get("notified")) or notified,
        }
        known[a["key"]] = entry
    # Cleared states drop out; events that were told stay (so they are not
    # re-told) until they age out of the change window and stop being produced.
    # Anything no longer produced drops out: a cleared state, or an event that
    # has aged out of the change window and so can never be re-told anyway.
    current = {a["key"] for a in alerts}
    for key in list(known):
        if key not in current:
            del known[key]
    state["alerts"] = known
    return state


# --------------------------------------------------------------------------- #
# alerts.example.ini is generated from the catalog, so the file people copy
# and the rules the console runs can never drift apart (a test compares them).
# --------------------------------------------------------------------------- #

def _ini_value(rule):
    if rule.kind == "switch":
        return "yes" if rule.default else "no"
    return _num(rule.default)


def render_example_ini():
    lines = [
        "# alerts.ini - what the IT Ops Console flags, and where it tells you. Safe to edit.",
        "#",
        "# The rules below do two jobs: they decide what lands in \"Needs a human\" on",
        "# the overview, and what is worth a message. Turning one off takes it off both;",
        "# the domain page itself still shows everything.",
        "#",
        "# Every line is a yes/no or a number. Change one, save, and the next refresh",
        "# uses it. Blank or 0 on a number turns that rule off. Lines starting with",
        "# ; or # are comments. You can change all of this on the console's Alerts tab",
        "# instead of editing here - it shows how this file was read, including any",
        "# line it could not use.",
        "#",
        "# Alerts carry the same names the console shows (admin accounts, licence",
        "# holders, printer names). Send them to a channel whose members should see",
        "# that. Nothing in this file is a password; the Teams URL below lets anyone",
        "# who has it post into that channel, which is why this folder is locked to",
        "# you and Administrators. Prefer not to keep it in a file at all? Leave it",
        "# blank and set the ITOPS_TEAMS_WEBHOOK environment variable instead",
        "# (machine scope, if the refresh runs unattended).",
        "",
        "[send]",
        "; changes       = a message only when an alert appears, gets worse, or clears",
        "; every-refresh = a summary after every refresh, even when nothing changed",
        "when = changes",
        "; A weekly summary of everything still open, on this day (blank = never).",
        "digest_day = Monday",
        "; Optional: where people open the console - a URL or a \\\\server\\share path.",
        "; Shown at the bottom of every alert.",
        "console_link =",
        "",
        "[teams]",
        "; Your channel's Workflows URL. In Teams: channel > ... > Workflows >",
        "; \"Post to a channel when a webhook request is received\" > copy the URL.",
        "; Test it: python notify.py --test   (from the console folder)",
        "webhook =",
        "",
        "[email]",
        "; An internal mail relay that accepts mail from this computer without a",
        "; password. Blank = no email. (A relay that needs a login is not supported",
        "; here on purpose - there are no password fields anywhere in this suite.)",
        "smtp_server =",
        "port = 25",
        "from =",
        "; One or more addresses, separated by ;",
        "to =",
        "use_ssl = no",
    ]
    for tab, label in TABS:
        lines.append("")
        lines.append("[%s]" % tab)
        lines.append("; %s tab. notify = no silences every rule below without changing them." % label)
        lines.append("notify = yes")
        for rule in CATALOG:
            if rule.tab != tab:
                continue
            what = rule.label
            if rule.kind == "number" and rule.unit:
                what = "%s (%s)" % (what, rule.unit)
            lines.append("; %s%s" % (what, (" - " + rule.help) if rule.help else ""))
            lines.append("%s = %s" % (rule.id, _ini_value(rule)))
        for opt, (default, help_text) in TAB_OPTIONS.items():
            t, k = opt.split(".", 1)
            if t == tab:
                lines.append("; %s" % help_text)
                lines.append("%s = %s" % (k, _num(default)))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Editing alerts.ini from the console
#
# The Alerts page can produce a small settings block - rules and schedule only,
# never [teams] or [email], so no webhook or address is ever embedded in a page
# or put on the clipboard. apply-alerts.py MERGES that block into the real
# alerts.ini: your comments, your channel settings and anything this console
# does not recognise stay exactly as they were, and only the lines you changed
# move. The parse and the merge live here so both the CLI and the tests use the
# same code the console itself uses to read the file.
# --------------------------------------------------------------------------- #

CHANNEL_SECTIONS = ("teams", "email")
CASE_SENSITIVE_KEYS = {("send", "console_link")}
# Sections that belong to the PRINTER collector's config.ini rather than
# alerts.ini. The console's Print fleet tab produces these; apply-settings.py
# routes them by name, so one settings block can carry both kinds.
FLEET_SECTIONS = ("ranges", "discovery")
FLEET_DISCOVERY_KEYS = ("rescan_hours", "ignore")
# [ranges] rows are named by a person, so the section is REPLACED wholesale -
# that is what lets a row be deleted. Everything else merges key by key.
REPLACE_WHOLE = ("ranges",)
EDITABLE_SECTIONS = ("send",) + tuple(t for t, _ in TABS)


# What a "place to look for printers" may look like. The printer collector's
# iprange.py is the authority on what one MEANS (and on how big is too big);
# this is the SHAPE, checked here and - as the very same pattern - in the
# browser while you type, so one definition serves both.
PLACE_PATTERN = (r"^(\d{1,3}(?:\.\d{1,3}){3})"      # an address
                 r"(?:/(\d{1,2})|-(\d{1,3}(?:\.\d{1,3}){3}|\d{1,3}))?$")   # /prefix or -end


def looks_like_a_place(spec):
    """True if `spec` is shaped like an address, a range or a subnet."""
    m = re.match(PLACE_PATTERN, str(spec or "").strip())
    if not m:
        return False
    parts = [m.group(1)]
    if m.group(3) and "." in m.group(3):
        parts.append(m.group(3))
    for addr in parts:
        if any(int(o) > 255 for o in addr.split(".")):
            return False
    if m.group(2) is not None and int(m.group(2)) > 32:
        return False
    if m.group(3) is not None and "." not in m.group(3) and int(m.group(3)) > 255:
        return False
    return True


def _split_value(raw):
    """'yes ; because' -> ('yes', ' ; because'). Keeps a person's own note on
    the line when only the value changes."""
    for marker in (" ;", " #"):
        i = raw.find(marker)
        if i >= 0:
            return raw[:i].strip(), raw[i:]
    return raw.strip(), ""


def parse_settings_fragment(text):
    """Read a settings block the console produced.

    Returns (alerts, fleet, notes): two lists of (section, {key: value}) in the
    order they appeared - one for alerts.ini, one for the printer collector's
    config.ini - and plain sentences about anything skipped. Raises ValueError
    when the text holds nothing this console recognises, which is how "Apply
    Settings" tells settings apart from whatever else is on the clipboard.
    """
    settings, notes, order = {}, [], []
    section = None
    recognised = 0
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section in CHANNEL_SECTIONS:
                notes.append("Left your [%s] settings alone - the console never changes where alerts go." % section)
            elif section not in EDITABLE_SECTIONS and section not in FLEET_SECTIONS:
                notes.append("Ignored [%s]: not a section this console knows." % section)
            continue
        if section is None or section in CHANNEL_SECTIONS:
            continue
        if section not in EDITABLE_SECTIONS and section not in FLEET_SECTIONS:
            continue
        eq = line.find("=")
        if eq < 1:
            continue
        raw_key = line[:eq].strip()
        # A [ranges] key is a display name someone typed - "Front Office" must
        # stay "Front Office". Everywhere else keys are settings, and case is
        # noise.
        key = raw_key if section == "ranges" else raw_key.lower()
        value, _ = _split_value(line[eq + 1:])
        if section == "ranges":
            # A person names these rows, so any key is fine - the VALUE is what
            # has to make sense, and a typo here would otherwise scan nothing.
            if not looks_like_a_place(value):
                raise ValueError("'%s' (%s) is not an address, a range or a subnet. "
                                 "Try 10.0.10.0/24, 10.0.20.50-99, or 10.0.30.15."
                                 % (value, raw_key))
        elif section == "discovery":
            if key not in FLEET_DISCOVERY_KEYS:
                notes.append("Ignored [discovery] %s: not a setting this console knows." % key)
                continue
            if key == "ignore":
                for one in [p.strip() for p in value.replace(",", ";").split(";") if p.strip()]:
                    if not looks_like_a_place(one):
                        raise ValueError("'%s' in the ignore list is not an address." % one)
            elif key == "rescan_hours":
                try:
                    float(value or 0)
                except ValueError:
                    raise ValueError("'%s' is not a number of hours." % value)
        elif section == "send":
            if key not in SEND_KEYS:
                notes.append("Ignored [send] %s: not a setting this console knows." % key)
                continue
        else:
            known = (key == "notify" or ("%s.%s" % (section, key)) in RULES_BY_KEY
                     or ("%s.%s" % (section, key)) in TAB_OPTIONS)
            if not known:
                notes.append("Ignored [%s] %s: not a rule this console knows." % (section, key))
                continue
        if section not in settings:
            settings[section] = {}
            order.append(section)
        settings[section][key] = value
        recognised += 1
    if not recognised:
        raise ValueError("that does not look like settings from the console")
    alerts = [(s, settings[s]) for s in order if s in EDITABLE_SECTIONS]
    fleet = [(s, settings[s]) for s in order if s in FLEET_SECTIONS]
    return alerts, fleet, notes


def _ini_index(lines):
    """(section per line, last content line index per section)."""
    per_line, last = [], {}
    section = None
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            last.setdefault(section, i)
        per_line.append(section)
        if section is not None and line:
            last[section] = i
    return per_line, last


def merge_into_ini(target_text, settings):
    """Put `settings` into the text of an alerts.ini, changing nothing else.

    Returns (new_text, changes) where changes is a list of
    (section, key, old_value, new_value) for the lines that actually moved.
    """
    lines = str(target_text).splitlines()
    per_line, last = _ini_index(lines)
    changes, pending = [], []

    for section, keys in settings:
        for key, value in keys.items():
            hit = None
            for i, raw in enumerate(lines):
                if per_line[i] != section:
                    continue
                stripped = raw.strip()
                if not stripped or stripped[0] in ";#[":
                    continue
                eq = stripped.find("=")
                if eq < 1 or stripped[:eq].strip().lower() != key:
                    continue
                hit = i
                break
            if hit is None:
                pending.append((section, key, value))
                continue
            raw = lines[hit]
            eq = raw.find("=")
            old, trailer = _split_value(raw[eq + 1:])
            # "Monday" and "monday" are the same setting to load_config, so
            # they are not a change - rewriting them would churn a person's
            # file for nothing. A console link is a path: there, case counts.
            same = (old == value) if (section, key) in CASE_SENSITIVE_KEYS \
                else (old.strip().lower() == value.strip().lower())
            if same:
                continue
            lines[hit] = "%s= %s%s" % (raw[:eq], value, trailer)
            changes.append((section, key, old, value))

    # Keys the file did not have yet: add them under their section (or add the
    # section at the end), so a file written by an older version fills in.
    for section, key, value in pending:
        if section in last:
            at = last[section] + 1
            lines.insert(at, "%s = %s" % (key, value))
            per_line, last = _ini_index(lines)
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("[%s]" % section)
            lines.append("%s = %s" % (key, value))
            per_line, last = _ini_index(lines)
        changes.append((section, key, "", value))

    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text, changes


def replace_section_in_ini(target_text, section, pairs):
    """Rewrite one section's keys, keeping its explanatory comments.

    Used for [ranges], where the rows are named by a person: merging can add
    and change rows but never delete one, and deleting a place you no longer
    want scanned has to work. The comment block right under the section header
    is kept (that is where config.example.ini explains the section); notes
    tangled among the old rows go with them.
    """
    lines = str(target_text).splitlines()
    per_line, _last = _ini_index(lines)
    body = [i for i, sec in enumerate(per_line) if sec == section]
    new_rows = ["%s = %s" % (k, v) for k, v in pairs]

    if not body:
        out = list(lines)
        if out and out[-1].strip():
            out.append("")
        out.append("[%s]" % section)
        out.extend(new_rows)
        text = "\n".join(out)
        return (text if text.endswith("\n") else text + "\n"), [("", r) for r in new_rows]

    start, end = body[0], body[-1]
    # The blank line that separated this section from the next one belongs to
    # this section's span, so put back as many as were there - otherwise every
    # apply glues [ranges] onto the section below it.
    trailing = 0
    while end - trailing > start and not lines[end - trailing].strip():
        trailing += 1
    header, keep, old_rows = [], [], []
    seen_key = False
    for i in range(start, end + 1):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            header.append(lines[i])
            continue
        if not seen_key and (not stripped or stripped[0] in ";#"):
            keep.append(lines[i])
            continue
        if stripped and stripped[0] not in ";#" and "=" in stripped:
            seen_key = True
            old_rows.append(stripped)
    while keep and not keep[-1].strip():
        keep.pop()
    replacement = header + keep + new_rows + [""] * trailing
    lines[start:end + 1] = replacement
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    before = {r.split("=", 1)[0].strip(): _split_value(r.split("=", 1)[1])[0] for r in old_rows}
    after = dict(pairs)
    changes = []
    for k, v in after.items():
        if k not in before:
            changes.append(("added", "%s = %s" % (k, v)))
        elif before[k] != v:
            changes.append(("changed", "%s = %s (was %s)" % (k, v, before[k])))
    for k in before:
        if k not in after:
            changes.append(("removed", "%s = %s" % (k, before[k])))
    return text, changes


def describe_fleet_changes(section, changes):
    """Plain sentences for what changed in the printer collector's config."""
    out = []
    for kind, text in changes:
        if section == "ranges":
            out.append({"added": "Now looking in %s.", "removed": "No longer looking in %s.",
                        "changed": "Where to look changed: %s."}[kind] % text)
        else:
            out.append("%s: %s." % (section, text))
    return out


def describe_changes(changes):
    """Plain sentences for what a merge moved - the words "Apply Alert
    Settings" prints back to whoever clicked it."""
    out = []
    for section, key, old, new in changes:
        if section == "send":
            if key == "when":
                out.append("Messages: %s." % ("only when something changes" if new == "changes"
                                              else "a summary after every refresh"))
            elif key == "digest_day":
                out.append("Weekly summary: %s." % (new.capitalize() if new else "never"))
            elif key == "console_link":
                out.append("Console link: %s." % (new if new else "removed"))
            else:
                out.append("%s: %s." % (key, new))
            continue
        label = TAB_LABEL.get(section, section)
        if key == "notify":
            on = str(new).lower() in ("yes", "true", "on", "1")
            out.append("%s: %s." % (label, "alerts back on" if on else "all alerts silenced"))
            continue
        rule = RULES_BY_KEY.get("%s.%s" % (section, key))
        if rule is None:
            out.append("%s %s: %s." % (label, key, new))
            continue
        def _read(v):
            if rule.kind == "switch":
                return rule_setting_text(rule, str(v).lower() in ("yes", "true", "on", "1"))
            try:
                return rule_setting_text(rule, float(v or 0))
            except ValueError:
                return str(v)
        if old == "":
            out.append("%s - %s: %s (added)." % (label, rule.label, _read(new)))
        else:
            out.append("%s - %s: %s to %s." % (label, rule.label, _read(old), _read(new)))
    return out
