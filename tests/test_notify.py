"""Test suite for notify.py:  python tests/test_notify.py

Runs notify.py against a fake Teams webhook (a local HTTP server that records
what was posted) and a fake mail relay (a local SMTP listener that records the
message), so every delivery decision can be checked without touching Teams or
a real relay: first run tells everything, a repeat run stays quiet, a new or
worse alert speaks, a cleared one is reported once, the weekly digest fires on
its day, every-refresh mode summarises each time, a failed post leaves the
alerts untold so they are retried, --test and --dry-run behave, and hostile
names cannot turn into links in a card.
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

    # -- 1. first run: everything is new, one card ------------------------ #
    write_ini(ini, webhook=url, link="https://intranet.example.test/console/")
    a1 = alert("security/admin_without_mfa/a@x", "critical", title="Admin without MFA: Ann <b>Admin</b>")
    a2 = alert("fleet/device_offline/10.0.0.1", "warning", tab="fleet", title="Printer offline: Warehouse")
    write_alerts(aj, [a1, a2])
    code, out = run(base)
    check("first run: exit 0, sent to Teams", code == 0 and "Sent to Teams." in out)
    check("first run: one post", len(Webhook.posts) == 1)
    txt = card_text(Webhook.posts[-1])
    check("first run: title counts the new alerts", "IT Ops Console: 2 new" in txt)
    check("first run: grouped by tab with severity tags",
          "Security:" in txt and "Print fleet:" in txt and "[CRITICAL] Admin without MFA" in txt and "[WARNING] Printer offline" in txt)
    check("first run: next step under each line", "-> Do the thing." in txt)
    check("first run: footer names the refresh time and the console", "Refresh at 2026-09-02T07:00:00Z" in txt and "intranet.example.test" in txt)
    card = Webhook.posts[-1]["attachments"][0]["content"]
    check("first run: open-console button when the link is a URL", card.get("actions", [{}])[0].get("url", "").startswith("https://intranet"))
    check("first run: angle brackets in names neutralised", "<b>" not in txt and "‹b›" in txt)
    state = json.load(open(st))
    check("first run: state marks both as told with first_seen", all(v["notified"] and v["first_seen"] for v in state["alerts"].values())
          and state["last_sent"] and state["history"][0]["new"] == 2)

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
    check("email: subject is the title, body has the lines and the next step",
          "Subject: IT Ops Console: 1 new" in mail and "[CRITICAL] Admin without MFA" in mail and "-> Do the thing." in mail)
    check("email: addressed to both", "To: it@example.test, boss@example.test" in mail)

    # -- 12. both channels, one failing: the other still counts ------------- #
    write_ini(ini, webhook=url, smtp=smtp_port)
    Webhook.status = 500
    write_alerts(aj, [a1, a2])
    code, out = run(base)
    check("both: Teams failed, email sent -> exit 1 but alerts told", code == 1 and "Emailed" in out
          and json.load(open(st))["alerts"][a2["key"]]["notified"] is True)
    Webhook.status = 200

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
