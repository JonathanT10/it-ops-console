"""Test suite for notify.py:  python tests/test_notify.py

Runs notify.py against a fake Teams webhook (a local HTTP server that records
what was posted) and a fake mail relay (a local SMTP listener that records the
message), so every delivery decision can be checked without touching Teams or
a real relay: the FIRST message is a starting point rather than a wall of
findings, a repeat run stays quiet, a new or worse alert speaks, a cleared one
is reported once, the weekly digest fires on its day, every-refresh mode
summarises each time, a failed post leaves the alerts untold so they are
retried, --test and --dry-run behave, and hostile names cannot turn into links
in a card.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import notify  # noqa: E402
from console import alerts as A  # noqa: E402

FAILS = []


def check(label, cond):
    print("%s %s" % ("PASS" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class Webhook(http.server.BaseHTTPRequestHandler):
    posts = []
    status = 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        Webhook.posts.append(json.loads(body.decode("utf-8")))
        self.send_response(Webhook.status)
        self.end_headers()
        self.wfile.write(b"1")

    def log_message(self, *a):
        pass


class SmtpHandler(socketserver.StreamRequestHandler):
    messages = []

    def handle(self):
        def say(line):
            self.wfile.write((line + "\r\n").encode())
        say("220 fake.relay ESMTP")
        data_mode, data = False, []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if data_mode:
                if line == ".":
                    SmtpHandler.messages.append("\n".join(data))
                    data, data_mode = [], False
                    say("250 OK queued")
                else:
                    data.append(line[1:] if line.startswith("..") else line)
                continue
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                say("250-fake.relay"); say("250 8BITMIME")
            elif verb in ("MAIL", "RCPT"):
                say("250 OK")
            elif verb == "DATA":
                data_mode = True
                say("354 End data with <CR><LF>.<CR><LF>")
            elif verb == "QUIT":
                say("221 Bye")
                return
            else:
                say("250 OK")


def start_servers():
    web = http.server.HTTPServer(("127.0.0.1", 0), Webhook)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SmtpHandler)
    smtp.allow_reuse_address = True
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    return web, smtp


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def alert(key, sev="warning", tab="security", title=None, transient=False):
    return {"key": key, "tab": tab, "rule": key.split("/")[1], "severity": sev,
            "title": title or ("Alert " + key.split("/")[-1]), "detail": "some detail",
            "action": "Do the thing.", "transient": transient, "tab_label": A.TAB_LABEL.get(tab, tab),
            "rule_label": "x"}


def write_alerts(path, alerts):
    doc = {"GeneratedUtc": "2026-09-02T07:00:00Z", "Count": len(alerts), "Alerts": alerts,
           "Config": {}}
    with open(path, "w") as fh:
        json.dump(doc, fh)


def write_ini(path, webhook="", smtp=None, when="changes", digest_day="", link=""):
    lines = ["[send]", "when = %s" % when, "digest_day = %s" % digest_day, "console_link = %s" % link,
             "[teams]", "webhook = %s" % webhook]
    if smtp:
        lines += ["[email]", "smtp_server = 127.0.0.1", "port = %d" % smtp, "from = console@example.test",
                  "to = it@example.test; boss@example.test"]
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def run(args):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = notify.main(args)
    return code, buf.getvalue()


def card_text(post):
    blocks = post["attachments"][0]["content"]["body"]
    return "\n".join(b.get("text", "") for b in blocks)


def main():
    web, smtp = start_servers()
    url = "http://127.0.0.1:%d/hook" % web.server_address[1]
    smtp_port = smtp.server_address[1]
    tmp = tempfile.mkdtemp(prefix="notify-")
    ini = os.path.join(tmp, "alerts.ini")
    aj = os.path.join(tmp, "alerts.json")
    st = os.path.join(tmp, "alerts-state.json")
    base = ["--config", ini, "--alerts", aj, "--state", st]

    # -- 1. the FIRST message is a starting point, not a wall ------------- #
    # With no state every open alert is technically "new", so a console pointed
    # at a real tenant would introduce itself to a channel with dozens of
    # findings. That is how a team learns to ignore a channel on day one.
    write_ini(ini, webhook=url, link="https://intranet.example.test/console/")
    a1 = alert("security/admin_without_mfa/a@x", "critical", title="Admin without MFA: Ann <b>Admin</b>")
    a2 = alert("fleet/device_offline/10.0.0.1", "warning", tab="fleet", title="Printer offline: Warehouse")
    write_alerts(aj, [a1, a2])
    code, out = run(base)
    check("first run: exit 0, sent to Teams", code == 0 and "Sent to Teams." in out)
    check("first run: one post", len(Webhook.posts) == 1)
    txt = card_text(Webhook.posts[-1])
    check("first run: says starting point, and counts what is OPEN not what is new",
          "starting point - 2 open" in txt and "2 new" not in txt)
    check("first run: breaks the open count down by severity",
          "1 critical, 1 warning" in txt)
    check("first run: says in words that this is not 2 new problems",
          "not 2 things that just happened" in txt)
    check("first run: promises change-only from here",
          "only hear when something is new, gets worse, or clears" in txt)
    check("first run: names the most serious one", "[CRITICAL] Admin without MFA" in txt)
    check("first run: a count per page, so nothing is hidden",
          "Everything open, by page" in txt and "Print fleet: 1 info" not in txt
          and "Print fleet: 1 warning" in txt)
    check("first run: no per-line next steps in the starting point",
          "-> Do the thing." not in txt)
    check("first run: footer names the refresh time and the console", "Refresh at 2026-09-02T07:00:00Z" in txt and "intranet.example.test" in txt)
    card = Webhook.posts[-1]["attachments"][0]["content"]
    check("first run: open-console button when the link is a URL", card.get("actions", [{}])[0].get("url", "").startswith("https://intranet"))
    check("first run: angle brackets in names neutralised", "<b>" not in txt and "‹b›" in txt)
    state = json.load(open(st))
    check("first run: state marks both as told with first_seen",
          all(v["notified"] and v["first_seen"] for v in state["alerts"].values()) and state["last_sent"])
    check("first run: the history records 0 new, flagged as the baseline",
          state["history"][0]["new"] == 0 and state["history"][0]["open"] == 2
          and state["history"][0]["baseline"] is True)
    check("first run: it also counts as that day's digest, so day one is not said twice",
          state["last_digest"] == datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    # -- 2. same alerts again: quiet ------------------------------------- #
    code, out = run(base)
    check("repeat: nothing sent, exit 0", code == 0 and "No alert sent" in out and len(Webhook.posts) == 1)

    # -- 3. one new, one worse ------------------------------------------- #
    a3 = alert("licensing/disabled_account_licensed/z@x", "warning", tab="licensing")
    write_alerts(aj, [dict(a2, severity="critical"), a1, a3])
    code, out = run(base)
    txt = card_text(Webhook.posts[-1])
    check("change: new + worse sent", len(Webhook.posts) == 2 and "1 new, 1 worse" in txt)
    check("change: sections named", "New" in txt and "Got worse" in txt and "Still open" not in txt)

    # -- 4. one cleared ---------------------------------------------------- #
    write_alerts(aj, [dict(a2, severity="critical"), a3])
    code, out = run(base)
    txt = card_text(Webhook.posts[-1])
    check("cleared: reported once", len(Webhook.posts) == 3 and "1 cleared" in txt and "Cleared" in txt and "Admin without MFA" in txt)
    code, out = run(base)
    check("cleared: not repeated", len(Webhook.posts) == 3 and "No alert sent" in out)

    # -- 5. events are told once and never 'cleared' ---------------------- #
    ev = alert("changes/role_assignments/2026-09-01/Role assignments/added/Pat", "warning", tab="changes",
               title="Role assignments added: Pat", transient=True)
    write_alerts(aj, [dict(a2, severity="critical"), a3, ev])
    run(base)
    check("event: told as new", "1 new" in card_text(Webhook.posts[-1]) and "Pat" in card_text(Webhook.posts[-1]))
    write_alerts(aj, [dict(a2, severity="critical"), a3])
    code, out = run(base)
    check("event: its disappearance is not a 'cleared'", "No alert sent" in out and len(Webhook.posts) == 4)

    # -- 6. weekly digest on its day -------------------------------------- #
    today = datetime.now(timezone.utc).strftime("%A")
    write_ini(ini, webhook=url, digest_day=today)
    # The starting point in case 1 already listed everything open, which is
    # exactly what a digest says - so it stamped today as the digest day and a
    # digest must NOT also go out. Prove that before testing the digest itself.
    n_before = len(Webhook.posts)
    code, out = run(base)
    check("digest: suppressed on the day the starting point went out",
          len(Webhook.posts) == n_before and "No alert sent" in out)
    st_doc = json.load(open(st))
    st_doc["last_digest"] = "2000-01-01"
    with open(st, "w") as fh:
        json.dump(st_doc, fh)
    code, out = run(base)
    txt = card_text(Webhook.posts[-1])
    check("digest: sent on its day even with nothing new", len(Webhook.posts) == 5 and "weekly summary: 2 open" in txt and "Open" in txt)
    check("digest: lists what is still open", "Printer offline" in txt and "Alert z@x" in txt)
    code, out = run(base)
    check("digest: once per day", len(Webhook.posts) == 5 and "No alert sent" in out)
    state = json.load(open(st))
    check("digest: recorded", state["last_digest"] == datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    # -- 7. every-refresh mode -------------------------------------------- #
    write_ini(ini, webhook=url, when="every-refresh")
    run(base); run(base)
    check("every-refresh: a summary each time", len(Webhook.posts) == 7 and "2 alerts open, nothing new" in card_text(Webhook.posts[-1]))
    write_alerts(aj, [])
    run(base)
    check("every-refresh: everything cleared -> '2 cleared' and 'nothing open'",
          "2 cleared" in card_text(Webhook.posts[-1]) and "Nothing - every rule that is on is quiet." in card_text(Webhook.posts[-1]))
    run(base)
    check("every-refresh: quiet run says all clear", "IT Ops Console: all clear" in card_text(Webhook.posts[-1]))

    # -- 8. failure leaves alerts untold, so they are retried ------------- #
    write_ini(ini, webhook=url)
    Webhook.status = 500
    write_alerts(aj, [a1])
    code, out = run(base)
    check("failure: exit 1 with plain words", code == 1 and "did not accept the message (HTTP 500)" in out and "Workflows URL" in out)
    state = json.load(open(st))
    check("failure: alert recorded but NOT told", state["alerts"][a1["key"]]["notified"] is False)
    Webhook.status = 200
    code, out = run(base)
    check("failure: retried and told on the next run", code == 0 and "1 new" in card_text(Webhook.posts[-1]))

    # -- 9. --test and --dry-run ----------------------------------------- #
    n = len(Webhook.posts)
    code, out = run(["--config", ini, "--test"])
    check("--test: sends the connected message", code == 0 and len(Webhook.posts) == n + 1
          and "alerts are connected" in card_text(Webhook.posts[-1]))
    write_alerts(aj, [a1, a2])
    code, out = run(base + ["--dry-run"])
    check("--dry-run: shows, sends nothing, changes nothing",
          code == 0 and "Would send" in out and "Printer offline" in out and len(Webhook.posts) == n + 1
          and json.load(open(st))["alerts"].get(a2["key"]) is None)

    # -- 10. no channel ------------------------------------------------------ #
    write_ini(ini)
    code, out = run(base)
    check("no channel: exit 2 and says where to paste the URL", code == 2 and "paste a Teams Workflows URL" in out)

    # -- 11. email through the fake relay --------------------------------- #
    write_ini(ini, smtp=smtp_port)
    if os.path.exists(st):
        os.remove(st)
    write_alerts(aj, [a1])
    code, out = run(base)
    check("email: sent through the relay", code == 0 and "Emailed it@example.test, boss@example.test." in out and len(SmtpHandler.messages) == 1)
    mail = SmtpHandler.messages[-1]
    check("email: subject is the title, body has the lines",
          # The state file was deleted just above, so this is a first message
          # again - and the baseline has to read the same way over email.
          "Subject: IT Ops Console: starting point - 1 open" in mail
          and "[CRITICAL] Admin without MFA" in mail)
    check("email: a single alert is not described as '1 things'",
          "1 things that just happened" not in mail
          and "The alert below is not something that just happened" in mail)
    check("email: addressed to both", "To: it@example.test, boss@example.test" in mail)

    # -- 12. both channels, one failing: the other still counts ------------- #
    write_ini(ini, webhook=url, smtp=smtp_port)
    Webhook.status = 500
    write_alerts(aj, [a1, a2])
    code, out = run(base)
    check("both: Teams failed, email sent -> exit 1 but alerts told", code == 1 and "Emailed" in out
          and json.load(open(st))["alerts"][a2["key"]]["notified"] is True)
    Webhook.status = 200


    # -- 14. the starting point, in the shapes the sequence above cannot reach #
    def fresh(dirname):
        d = os.path.join(tmp, dirname)
        os.makedirs(d, exist_ok=True)
        return (os.path.join(d, "alerts.ini"), os.path.join(d, "alerts.json"),
                os.path.join(d, "alerts-state.json"))

    # (a) a busy tenant: the cap holds, and it spreads across pages instead of
    #     spending every line on whichever page happens to be worst.
    bi, ba, bs = fresh("busy")
    write_ini(bi, webhook=url)
    many = ([alert("identity/ca_gap/%d" % i, "critical", tab="identity",
                   title="CA gap %d" % i) for i in range(21)]
            + [alert("security/admin_without_mfa/%d" % i, "critical",
                     title="Admin without MFA %d" % i) for i in range(4)]
            + [alert("licensing/unused/%d" % i, "warning", tab="licensing",
                     title="Unused seats %d" % i) for i in range(11)]
            + [alert("fleet/supply/%d" % i, "info", tab="fleet",
                     title="Low toner %d" % i) for i in range(3)])
    write_alerts(ba, many)
    n_before = len(Webhook.posts)
    code, out = run(["--config", bi, "--alerts", ba, "--state", bs])
    txt = card_text(Webhook.posts[-1])
    check("busy: one message, not one per alert",
          code == 0 and len(Webhook.posts) == n_before + 1)
    check("busy: the title is the open count, not 39 new",
          "starting point - 39 open (25 critical, 11 warning, 3 info)" in txt
          and "39 new" not in txt)
    named = txt.count("[CRITICAL]")
    check("busy: the named list is capped", named == notify.BASELINE_MAX_LINES)
    check("busy: and it names BOTH bad pages, not just the worst one",
          "CA gap" in txt and "Admin without MFA" in txt)
    check("busy: it says how many it did not name", "(+15 more like these" in txt)
    check("busy: every page still gets a count, so nothing is hidden",
          "Identity: 21 critical" in txt and "Security: 4 critical" in txt
          and "Licensing: 11 warning" in txt and "Print fleet: 3 info" in txt)
    check("busy: the card stays a readable length",
          len(Webhook.posts[-1]["attachments"][0]["content"]["body"]) < 40)

    # (b) a state file that EXISTS but has never sent anything is still a first
    #     message - runs before a channel was configured leave exactly that.
    ei, ea, es = fresh("existing")
    write_ini(ei, webhook=url)
    write_alerts(ea, [a1, a2])
    with open(es, "w") as fh:
        json.dump({"alerts": {a1["key"]: {"severity": "critical", "tab": "security",
                                          "title": "x", "transient": False,
                                          "first_seen": "2026-09-01T00:00:00Z",
                                          "last_seen": "2026-09-01T00:00:00Z",
                                          "notified": False}},
                   "last_sent": None, "last_digest": None, "history": []}, fh)
    n_before = len(Webhook.posts)
    code, out = run(["--config", ei, "--alerts", ea, "--state", es])
    check("state file with nothing ever sent: still a starting point",
          "starting point - 2 open" in card_text(Webhook.posts[-1]))
    check("is_first_message() is about last_sent, not about the file existing",
          notify.is_first_message({"alerts": {"x": {}}, "last_sent": None}) is True
          and notify.is_first_message({"last_sent": "2026-09-09T00:00:00Z"}) is False)

    # (c) a starting point that FAILED to send is still a starting point next
    #     time - it must not become "2 new" just because it was attempted.
    fi, fa, fs = fresh("failed")
    write_ini(fi, webhook=url)
    write_alerts(fa, [a1, a2])
    Webhook.status = 500
    code, out = run(["--config", fi, "--alerts", fa, "--state", fs])
    check("failed starting point: exit 1 and nothing marked told",
          code == 1 and json.load(open(fs))["alerts"][a1["key"]]["notified"] is False
          and not json.load(open(fs))["last_sent"])
    Webhook.status = 200
    code, out = run(["--config", fi, "--alerts", fa, "--state", fs])
    check("failed starting point: the retry is STILL a starting point",
          "starting point - 2 open" in card_text(Webhook.posts[-1])
          and "2 new" not in card_text(Webhook.posts[-1]))

    # (d) and afterwards it behaves like any other console
    a_new = alert("security/legacy_auth/x@x", "critical", title="Legacy auth in use")
    write_alerts(fa, [a1, a2, a_new])
    code, out = run(["--config", fi, "--alerts", fa, "--state", fs])
    txt = card_text(Webhook.posts[-1])
    check("after a starting point: a real new alert is normal news, with its next step",
          "IT Ops Console: 1 new" in txt and "Legacy auth in use" in txt
          and "-> Do the thing." in txt)
    check("after a starting point: what it already told is not repeated",
          "Admin without MFA" not in txt and "starting point" not in txt)

    # (e) a quiet tenant gets told alerts are working, not silence
    qi, qa, qs = fresh("quiet")
    write_ini(qi, webhook=url)
    write_alerts(qa, [])
    code, out = run(["--config", qi, "--alerts", qa, "--state", qs])
    txt = card_text(Webhook.posts[-1])
    check("quiet tenant: the first message still goes, so the channel is proven",
          code == 0 and "starting point - nothing open" in txt
          and "Alerts are connected and nothing is firing." in txt)

    # -- 13. clean() ---------------------------------------------------------- #
    check("clean: markdown link syntax broken up", notify.clean("[x](http://evil)") == "[x] (http://evil)")

    web.shutdown(); smtp.shutdown()
    print("")
    if FAILS:
        print("RESULT: %d FAILURES" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
