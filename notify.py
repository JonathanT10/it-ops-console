"""Send the console's alerts to Teams and/or email - only when they are worth it.

    python notify.py --config alerts.ini --alerts output\\alerts.json --state output\\alerts-state.json
    python notify.py --config alerts.ini --test          # "alerts are connected" message
    python notify.py --config alerts.ini --alerts ... --state ... --dry-run   # show, send nothing

This is the ONE thing in this repository that talks to the network, and it
talks only to YOUR Teams webhook and YOUR mail relay. build.py (the renderer)
never sends anything; it writes alerts.json, and run-all.ps1 runs this script
afterwards as its own step.

What "worth it" means (alerts.ini [send] when):
  changes        a message only when an alert is new, has got worse, or has
                 cleared since the last message - the default, so nobody learns
                 to ignore a daily repeat
  every-refresh  a summary after every refresh
Either way, on the weekly digest day (alerts.ini [send] digest_day) one message
lists everything still open, or says "nothing open" - a heartbeat that also
proves the refresh is running.

The FIRST message is a starting point, not news. With no state file every open
alert is technically "new", so a console pointed at a busy tenant would open its
account in a channel with a wall of dozens of findings - the fastest way to
teach a team that this channel is noise. Instead the first message says how many
are open, shows the most serious few, gives a count per page, and says plainly
that this is where things stand rather than what just happened. Everything is
then marked as told, so the second message onwards is real change only.

State lives in alerts-state.json: which alerts people were already told about,
when the last message went, when the last digest went. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console import alerts as A  # noqa: E402

MAX_LINES_PER_SECTION = 25
# The first message is deliberately shorter than a normal one. Nobody reads a
# card with forty findings on it, and the point of that message is to be read.
BASELINE_MAX_LINES = 10
SEVERITY_TAG = {"critical": "[CRITICAL]", "warning": "[WARNING]", "info": "[info]"}


def _now():
    return datetime.now(timezone.utc)


def load_json(path, default):
    if not path or not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def clean(text):
    """Alert text is tenant data (display names, printer names). Adaptive Card
    text blocks render a little Markdown, so neutralise the two things a hostile
    name could do with it: make a link, or look like a tag."""
    t = str(text or "")
    return t.replace("](", "] (").replace("<", "‹").replace(">", "›")


# --------------------------------------------------------------------------- #
# Compose
# --------------------------------------------------------------------------- #

def _line(a):
    s = "%s %s" % (SEVERITY_TAG.get(a.get("severity"), "[info]"), clean(a.get("title")))
    if a.get("detail"):
        s += " - " + clean(a["detail"])
    return s


def _group(alerts):
    """Alerts grouped by tab, tabs in catalog order, severity order inside."""
    order = [t for t, _ in A.TABS]
    by_tab = {}
    for a in alerts:
        by_tab.setdefault(a.get("tab"), []).append(a)
    out = []
    for tab in order + [t for t in by_tab if t not in order]:
        if tab in by_tab:
            items = sorted(by_tab[tab], key=lambda a: (A.SEVERITY_RANK.get(a.get("severity"), 9), a.get("title", "")))
            out.append((A.TAB_LABEL.get(tab, tab), items))
    return out


def _section(heading, alerts, with_action=True):
    lines = []
    for tab_label, items in _group(alerts):
        lines.append("%s:" % tab_label)
        for a in items[:MAX_LINES_PER_SECTION]:
            lines.append("  " + _line(a))
            if with_action and a.get("action"):
                lines.append("     -> " + clean(a["action"]))
        if len(items) > MAX_LINES_PER_SECTION:
            lines.append("  (+%d more on that page)" % (len(items) - MAX_LINES_PER_SECTION))
    return heading, lines


def _by_severity(alerts):
    """'25 critical, 11 warning, 3 info' - in that order, skipping the empty."""
    n = {}
    for a in alerts:
        n[a.get("severity")] = n.get(a.get("severity"), 0) + 1
    return ", ".join("%d %s" % (n[s], s) for s in ("critical", "warning", "info") if n.get(s))


def _baseline_sections(all_alerts):
    """What the FIRST message shows: the worst few by name, then a count per
    page so nothing is hidden by the cap."""
    sections = []
    worst = [a for a in all_alerts if a.get("severity") == "critical"] or \
            [a for a in all_alerts if a.get("severity") == "warning"]

    # Take a turn from each page rather than filling up from the first one.
    # A tenant with 21 identity gaps and 4 admins without MFA would otherwise
    # spend the whole cap on identity and never mention the admins - and the
    # point of naming any of them is to show the SPREAD of what is wrong.
    grouped = [(label, list(items)) for label, items in _group(worst)]
    picked = {label: [] for label, _ in grouped}
    taken = 0
    while taken < BASELINE_MAX_LINES and any(items for _, items in grouped):
        for label, items in grouped:
            if taken >= BASELINE_MAX_LINES:
                break
            if items:
                picked[label].append(items.pop(0))
                taken += 1

    lines = []
    for label, _ in grouped:
        if picked[label]:
            lines.append("%s:" % label)
            for a in picked[label]:
                lines.append("  " + _line(a))
    left = len(worst) - taken
    if left > 0:
        lines.append("(+%d more like these on the Alerts page)" % left)
    if lines:
        sections.append(("Worth looking at first", lines))

    counts = []
    for tab_label, items in _group(all_alerts):
        counts.append("  %s: %s" % (tab_label, _by_severity(items)))
    if counts:
        sections.append(("Everything open, by page", counts))
    return sections


def compose(diff, all_alerts, cfg, doc, digest, now, baseline=False):
    """Title, one-line summary, and sections of plain lines. The same text goes
    to Teams (one text block per line) and email (joined with newlines)."""
    if baseline:
        n = len(all_alerts)
        bits = _by_severity(all_alerts)
        if n:
            title = "IT Ops Console: starting point - %d open%s" % (n, (" (%s)" % bits) if bits else "")
            if n == 1:
                first = ("This is the first message from this console, so it is where things"
                         " stand right now. The alert below is not something that just"
                         " happened - it is simply what is open.")
            else:
                first = ("This is the first message from this console, so it is where things"
                         " stand right now - not %d things that just happened. Most have been"
                         " true for a while." % n)
            why = [first,
                   "From now on you will only hear when something is new, gets worse, or clears."]
        else:
            title = "IT Ops Console: starting point - nothing open"
            why = ["Alerts are connected and nothing is firing.",
                   "From now on you will only hear when something is new, gets worse, or clears."]
        sections = [("", why)] + _baseline_sections(all_alerts)
        footer = []
        gen = (doc or {}).get("GeneratedUtc")
        if gen:
            footer.append("Refresh at %s (UTC)." % gen)
        link = cfg["send"].get("console_link") or ""
        if link:
            footer.append("Console: %s" % link)
        footer.append("What is watched, and how: the Alerts page in the console; settings in alerts.ini.")
        return {"title": title, "summary": "starting point", "sections": sections,
                "footer": footer, "link": link}

    counts = []
    if diff["new"]:
        counts.append("%d new" % len(diff["new"]))
    if diff["worse"]:
        counts.append("%d worse" % len(diff["worse"]))
    if diff["cleared"]:
        counts.append("%d cleared" % len(diff["cleared"]))
    open_count = len(all_alerts)
    if digest:
        title = "IT Ops Console weekly summary: %s" % (("%d open" % open_count) if open_count else "nothing open")
    elif counts:
        title = "IT Ops Console: " + ", ".join(counts)
    elif open_count:
        title = "IT Ops Console: %d alert%s open, nothing new" % (open_count, "" if open_count == 1 else "s")
    else:
        title = "IT Ops Console: all clear"

    sections = []
    if diff["new"]:
        sections.append(_section("New", diff["new"]))
    if diff["worse"]:
        sections.append(_section("Got worse", diff["worse"]))
    if diff["cleared"]:
        sections.append(_section("Cleared", [dict(c, severity="info") for c in diff["cleared"]], with_action=False))
    if digest or cfg["send"]["when"] == "every-refresh":
        still = [a for a in all_alerts if not a.get("transient")]
        if still:
            sections.append(_section("Still open" if (diff["new"] or diff["worse"]) else "Open", still))
        elif not (diff["new"] or diff["worse"]):
            sections.append(("Open", ["  Nothing - every rule that is on is quiet."]))

    footer = []
    gen = (doc or {}).get("GeneratedUtc")
    if gen:
        footer.append("Refresh at %s (UTC)." % gen)
    link = cfg["send"].get("console_link") or ""
    if link:
        footer.append("Console: %s" % link)
    footer.append("What is watched, and how: the Alerts page in the console; settings in alerts.ini.")
    summary = ", ".join(counts) if counts else ("weekly summary" if digest else "summary")
    return {"title": title, "summary": summary, "sections": sections, "footer": footer, "link": link}


def as_text(msg):
    out = [msg["title"], ""]
    for heading, lines in msg["sections"]:
        if heading:
            out.append(heading)
        out.extend(lines)
        out.append("")
    out.extend(msg["footer"])
    return "\n".join(out)


def as_card(msg):
    body = [
        {"type": "TextBlock", "text": clean(msg["title"]), "weight": "Bolder", "size": "Medium", "wrap": True},
    ]
    for heading, lines in msg["sections"]:
        if heading:
            body.append({"type": "TextBlock", "text": clean(heading), "weight": "Bolder", "wrap": True, "spacing": "Medium"})
        for l in lines:
            body.append({"type": "TextBlock", "text": l, "wrap": True, "spacing": "None"})
    for f in msg["footer"]:
        body.append({"type": "TextBlock", "text": clean(f), "isSubtle": True, "wrap": True, "spacing": "Small", "size": "Small"})
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }
    link = msg.get("link") or ""
    if link.lower().startswith(("http://", "https://")):
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open the console", "url": link}]
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


# --------------------------------------------------------------------------- #
# Send
# --------------------------------------------------------------------------- #

def send_teams(url, msg, timeout=30):
    data = json.dumps(as_card(msg)).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "it-ops-console/notify"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            if code >= 300:
                raise RuntimeError("Teams answered %s" % code)
    except urllib.error.HTTPError as e:
        raise RuntimeError("Teams did not accept the message (HTTP %s). Check the Workflows URL in alerts.ini." % e.code)
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise RuntimeError("Could not reach Teams (%s). Check the Workflows URL in alerts.ini and this computer's internet access." % getattr(e, "reason", e))


def send_email(email_cfg, msg, timeout=30):
    m = EmailMessage()
    m["Subject"] = msg["title"]
    m["From"] = email_cfg["from"]
    m["To"] = ", ".join(email_cfg["to"])
    m.set_content(as_text(msg))
    try:
        with smtplib.SMTP(email_cfg["smtp_server"], int(email_cfg.get("port") or 25), timeout=timeout) as s:
            if email_cfg.get("use_ssl"):
                s.starttls()
            s.send_message(m)
    except (smtplib.SMTPException, socket.timeout, OSError) as e:
        raise RuntimeError("The mail relay %s did not accept the message (%s). Check [email] in alerts.ini."
                           % (email_cfg["smtp_server"], e))


def deliver(cfg, msg, channels, env=None):
    """Try every configured channel; report each in plain words. Returns
    (any_sent, notes, failures)."""
    env = os.environ if env is None else env
    notes, failures, sent = [], [], False
    if channels["teams"]:
        url = cfg["teams"]["webhook"] or env.get("ITOPS_TEAMS_WEBHOOK", "")
        try:
            send_teams(url, msg)
            notes.append("Sent to Teams.")
            sent = True
        except RuntimeError as e:
            failures.append(str(e))
    if channels["email"]:
        try:
            send_email(cfg["email"], msg)
            notes.append("Emailed %s." % ", ".join(cfg["email"]["to"]))
            sent = True
        except RuntimeError as e:
            failures.append(str(e))
    return sent, notes, failures


# --------------------------------------------------------------------------- #
# Decide
# --------------------------------------------------------------------------- #

def is_first_message(state):
    """Has a message ever actually gone out from this state?

    Deliberately NOT "does the state file exist". A file can exist and have
    told nobody anything - runs before a channel was configured record what is
    firing so first_seen dates stay honest, and a channel that was set up and
    then removed leaves the same shape. In every one of those cases the next
    message is still somebody's first, and a wall of findings is still the
    wrong way to open.
    """
    return not (state.get("last_sent") or "")


def digest_due(cfg, state, now):
    day = (cfg["send"].get("digest_day") or "").lower()
    if not day:
        return False
    if now.strftime("%A").lower() != day:
        return False
    return (state.get("last_digest") or "") != now.strftime("%Y-%m-%d")


def decide(cfg, diff, state, now, baseline=False):
    """(send?, is_digest, reason in plain words)"""
    if baseline:
        # Always send, whatever [send] when says, and never as a digest: this
        # message IS the starting point the digest would otherwise repeat.
        return True, False, "this is the first message, so it says where things stand rather than what changed"
    digest = digest_due(cfg, state, now)
    changed = bool(diff["new"] or diff["worse"] or diff["cleared"])
    if cfg["send"]["when"] == "every-refresh":
        return True, digest, "a summary goes out after every refresh"
    if changed:
        return True, digest, "something is new, worse, or cleared"
    if digest:
        return True, True, "it is the weekly summary day"
    return False, False, "nothing is new, worse, or cleared since the last message"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="alerts.ini", help="alerts.ini (default: beside this script)")
    ap.add_argument("--alerts", default=None, help="alerts.json written by build.py")
    ap.add_argument("--state", default=None, help="alerts-state.json (created if missing)")
    ap.add_argument("--test", action="store_true", help="send an 'alerts are connected' message and exit")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent; send nothing, change nothing")
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(here, args.config)
    cfg = A.load_config(cfg_path)
    for p in cfg["problems"]:
        print("alerts.ini: " + p)
    channels = A.channels_configured(cfg)
    now = _now()

    if not channels["any"]:
        print("No channel is set up: paste a Teams Workflows URL under [teams] in %s, or fill in [email]." % cfg_path)
        return 2

    if args.test:
        msg = {
            "title": "IT Ops Console alerts are connected",
            "summary": "test",
            "sections": [("Test", ["  Sent from %s at %s (UTC). Real alerts will look like this, one line per finding."
                                    % (socket.gethostname(), now.strftime("%Y-%m-%d %H:%M"))])],
            "footer": ["What is watched, and how: the Alerts page in the console; settings in alerts.ini."],
            "link": cfg["send"].get("console_link") or "",
        }
        if args.dry_run:
            print(as_text(msg))
            return 0
        sent, notes, failures = deliver(cfg, msg, channels)
        for n in notes:
            print(n)
        for f in failures:
            print("PROBLEM: " + f)
        return 0 if sent and not failures else 1

    if not args.alerts:
        print("Give --alerts (the alerts.json that build.py writes).")
        return 1
    doc = load_json(args.alerts, None)
    if doc is None:
        print("alerts.json not found at %s - build the console first." % args.alerts)
        return 1
    alerts = doc.get("Alerts") or []
    state_path = args.state or os.path.join(os.path.dirname(os.path.abspath(args.alerts)), "alerts-state.json")
    state = load_json(state_path, None) or A.empty_state()

    baseline = is_first_message(state)
    diff = A.diff_state(alerts, state)
    send, digest, reason = decide(cfg, diff, state, now, baseline=baseline)
    msg = compose(diff, alerts, cfg, doc, digest, now, baseline=baseline)

    if args.dry_run:
        print("Would %s (%s)." % ("send" if send else "not send", reason))
        print("")
        print(as_text(msg))
        return 0

    if not send:
        print("No alert sent - %s." % reason)
        # Still remember what is firing, so first_seen dates stay honest.
        A.apply_state(state, alerts, diff, notified=False, now=now)
        save_json(state_path, state)
        return 0

    sent, notes, failures = deliver(cfg, msg, channels)
    for n in notes:
        print(n)
    for f in failures:
        print("PROBLEM: " + f)
    A.apply_state(state, alerts, diff, notified=sent, now=now)
    if sent:
        state["last_sent"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if digest or baseline:
            # A baseline stamps the digest day too. It already lists everything
            # open, which is exactly what the weekly summary would say - sending
            # both on day one is the repetition this whole design avoids.
            state["last_digest"] = now.strftime("%Y-%m-%d")
        hist = state.get("history") or []
        hist.insert(0, {"when": state["last_sent"], "title": msg["title"], "summary": msg["summary"],
                        "channels": [n.split(" ")[0].rstrip(".").lower() for n in notes],
                        # A baseline reports 0 new: nothing about it was news, and
                        # counting 39 "new" in the history would make the console's
                        # own record of alerting disagree with the message it sent.
                        "new": 0 if baseline else len(diff["new"]),
                        "worse": 0 if baseline else len(diff["worse"]),
                        "cleared": 0 if baseline else len(diff["cleared"]),
                        "baseline": bool(baseline),
                        "open": len(alerts)})
        state["history"] = hist[:20]
    save_json(state_path, state)
    if failures:
        return 1
    print("(%s)" % reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
