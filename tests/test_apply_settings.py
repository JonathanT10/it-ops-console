"""Test suite for editing settings in the console:  python tests/test_apply_settings.py

Two pages hand you a block of settings - the Alerts tab (which rules count,
and when a message goes out) and the Print fleet tab (where to look for
printers) - and one block can carry both. Three halves, then.

The merge (no browser): the block goes into alerts.ini touching only the lines
it names - comments, the [teams] webhook, the [email] block and anything
unrecognised all survive - and a block that is not settings changes nothing.

The printer half (no browser): [ranges] is REPLACED as a whole, because that
is the only way deleting a place you no longer want scanned can work, while
[discovery] merges key by key; [snmp], [devices] and the section's own
explanatory comments must come through untouched either way. A range that is
not an address is refused before anything is written.

The round trip (needs Playwright; skipped with a note if it is not installed):
each page is opened in a real browser, its controls are read and changed, and
the text it hands you is applied for real. The important one is that an
untouched page round-trips to exactly the settings that were already in force,
because that is what stops the pages and the files from drifting apart.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from console import alerts as A  # noqa: E402
from console import pages  # noqa: E402

FAILS = []


def check(label, cond):
    print("%s %s" % ("PASS" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def apply_cli(settings_text, config_path, extra=(), fleet_path=None):
    args = [sys.executable, os.path.join(ROOT, "apply-settings.py"), "--config", config_path]
    if fleet_path:
        args += ["--fleet-config", fleet_path]
    p = subprocess.run(args + list(extra), input=settings_text, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


REAL_INI = """# My own notes at the top.
[send]
when = changes
digest_day = Monday
console_link =

[teams]
webhook = https://prod-00.westus.logic.azure.com/workflows/SECRET

[email]
smtp_server = relay.corp.local
from = console@corp.local
to = it@corp.local

[security]
notify = yes
; leave this one on, Burke asked
admin_without_mfa = yes
mfa_coverage_below = 90    ; we agreed 90 in the March review
stale_accounts_above = 20
legacy_auth_signins = yes
future_rule_from_a_newer_version = yes

[fleet]
notify = yes
device_offline = yes
"""


def test_parse():
    settings, fleet, notes = A.parse_settings_fragment(
        "[send]\nwhen = every-refresh\n[teams]\nwebhook = https://leak\n"
        "[security]\nnotify = no\nmfa_coverage_below = 95\nbogus = 1\n[kitchen]\nsink = yes\n")
    flat = dict(settings)
    check("parse: keeps the sections it knows", set(flat) == {"send", "security"})
    check("parse: an alerts-only block carries no printer settings", fleet == [])
    check("parse: reads the values", flat["send"]["when"] == "every-refresh"
          and flat["security"]["mfa_coverage_below"] == "95" and flat["security"]["notify"] == "no")
    check("parse: refuses to carry a webhook, and says so",
          "leak" not in json.dumps(settings) and any("[teams]" in n for n in notes))
    check("parse: unknown rule and unknown section reported, not fatal",
          any("bogus" in n for n in notes) and any("[kitchen]" in n for n in notes))
    for rubbish in ("", "hello there", "https://example.com/some/link", "[not a section"):
        try:
            A.parse_settings_fragment(rubbish)
            check("parse: rejects %r" % rubbish[:20], False)
        except ValueError:
            check("parse: rejects %r" % (rubbish[:20] or "(empty)"), True)


def test_parse_fleet():
    """The printer half of a settings block: which sections go where, and what
    a place is allowed to look like."""
    alerts, fleet, notes = A.parse_settings_fragment(
        "[ranges]\nFront Office = 10.0.10.0/24\nSpares = 10.0.20.50-99\n"
        "Boardroom = 10.0.30.15\nSpan = 10.0.40.1-10.0.40.9\n"
        "[discovery]\nrescan_hours = 6\nignore = 10.0.10.99; 10.0.10.100\nspeed = fast\n"
        "[send]\nwhen = changes\n")
    byname = dict(fleet)
    check("parse fleet: the two printer sections are routed apart from the alerts",
          [s for s, _ in fleet] == ["ranges", "discovery"] and [s for s, _ in alerts] == ["send"])
    check("parse fleet: a name someone typed keeps its capitals",
          "Front Office" in byname["ranges"] and "Boardroom" in byname["ranges"])
    check("parse fleet: every shape of place is accepted",
          byname["ranges"]["Spares"] == "10.0.20.50-99"
          and byname["ranges"]["Span"] == "10.0.40.1-10.0.40.9"
          and byname["ranges"]["Boardroom"] == "10.0.30.15")
    check("parse fleet: the discovery settings come through",
          byname["discovery"] == {"rescan_hours": "6", "ignore": "10.0.10.99; 10.0.10.100"})
    check("parse fleet: a discovery setting this console does not know is noted, not fatal",
          any("speed" in n for n in notes))

    # A place that is not a place has to stop the whole thing: a typo here
    # would otherwise scan nothing, silently, forever.
    bad = [("Office = 10.0.10.0/33", "prefix past 32"),
           ("Office = 10.0.10.300", "an octet past 255"),
           ("Office = 10.0.10.5:161", "a port"),
           ("Office = the third floor", "words"),
           ("Office = 10.0.10.0/24, 10.0.20.0/24", "two places on one line")]
    for line, why in bad:
        try:
            A.parse_settings_fragment("[ranges]\n%s\n" % line)
            check("parse fleet: refuses %s" % why, False)
        except ValueError as e:
            check("parse fleet: refuses %s, and says which row" % why,
                  "Office" in str(e) and "not an address" in str(e))
    for line, why in (("ignore = the printer in reception", "an ignore list that is not addresses"),
                      ("rescan_hours = whenever", "hours that are not a number")):
        try:
            A.parse_settings_fragment("[discovery]\n%s\n" % line)
            check("parse fleet: refuses %s" % why, False)
        except ValueError:
            check("parse fleet: refuses %s" % why, True)

    check("parse fleet: an empty [ranges] is a real answer - look nowhere",
          A.parse_settings_fragment("[ranges]\n[discovery]\nrescan_hours = 0\n")[1]
          == [("discovery", {"rescan_hours": "0"})])


def test_merge():
    settings, _fleet, _notes = A.parse_settings_fragment(
        "[send]\nwhen = every-refresh\n[security]\nmfa_coverage_below = 95\nstale_accounts_above = 20\n"
        "[licensing]\nunused_seats_above = 40\n")
    out, changes = A.merge_into_ini(REAL_INI, settings)
    check("merge: only what actually moved is reported",
          sorted((s, k) for s, k, _o, _n in changes)
          == [("licensing", "unused_seats_above"), ("security", "mfa_coverage_below"), ("send", "when")])
    check("merge: a value that was already right is not a change",
          all(k != "stale_accounts_above" for _s, k, _o, _n in changes))
    check("merge: the webhook is untouched", "webhook = https://prod-00.westus.logic.azure.com/workflows/SECRET" in out)
    check("merge: the email block is untouched",
          "smtp_server = relay.corp.local" in out and "to = it@corp.local" in out)
    check("merge: a person's comments survive",
          "# My own notes at the top." in out and "; leave this one on, Burke asked" in out)
    check("merge: an inline note on a changed line survives",
          "mfa_coverage_below = 95 ; we agreed 90 in the March review" in out)
    check("merge: a key from a newer version is left alone",
          "future_rule_from_a_newer_version = yes" in out)
    check("merge: a section the file did not have is added",
          "[licensing]" in out and "unused_seats_above = 40" in out)
    problems = _load(out)["problems"]
    check("merge: it reads back cleanly apart from the line that was already odd",
          len(problems) == 1 and "future_rule_from_a_newer_version" in problems[0])
    again, changes2 = A.merge_into_ini(out, settings)
    check("merge: applying the same settings twice changes nothing", changes2 == [] and again == out)

    # a key missing from a section that DOES exist lands inside that section
    settings2, _f2, _n2 = A.parse_settings_fragment("[fleet]\ndevice_error = no\n")
    out2, _ = A.merge_into_ini(out, settings2)
    cfg2 = _load(out2)
    check("merge: a missing key is added under its own section",
          cfg2["rules"]["fleet.device_error"] is False and cfg2["rules"]["fleet.device_offline"] is True)

    words = A.describe_changes(changes)
    check("merge: the change list reads as sentences",
          any("MFA coverage falls below: 90% to 95%" in w for w in words)
          and any("summary after every refresh" in w for w in words))


def _load(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
    fh.write(text)
    fh.close()
    try:
        return A.load_config(fh.name)
    finally:
        os.unlink(fh.name)


def test_cli(tmp):
    target = os.path.join(tmp, "alerts.ini")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(REAL_INI)
    frag = "[security]\nmfa_coverage_below = 95\n"

    code, out = apply_cli(frag, target, ["--dry-run"])
    check("cli: dry run says what would change and changes nothing",
          code == 0 and "Would change 1 setting" in out and "90% to 95%" in out
          and open(target, encoding="utf-8").read() == REAL_INI)

    code, out = apply_cli(frag, target)
    text = open(target, encoding="utf-8").read()
    check("cli: a line this version does not know is noted, not a blocker",
          "Note:" in out and "future_rule_from_a_newer_version" in out)
    check("cli: applies, backs up, and reports in plain words",
          code == 0 and "Changed 1 setting" in out and "mfa_coverage_below = 95" in text
          and os.path.exists(target + ".bak")
          and open(target + ".bak", encoding="utf-8").read() == REAL_INI)
    check("cli: tells you what to do next", 'Refresh IT Ops Data' in out)

    code, out = apply_cli(frag, target)
    check("cli: running it again is a no-op", code == 0 and "Nothing to change" in out)

    code, out = apply_cli("what I actually copied was this sentence", target)
    check("cli: something that is not settings changes nothing and explains",
          code == 2 and "does not look like settings from the console" in out
          and "Alerts or Print fleet tab" in out
          and open(target, encoding="utf-8").read() == text)

    before = open(target, encoding="utf-8").read()
    code, out = apply_cli("[send]\nwhen = sometimes\n", target)
    check("cli: a value the console could not read back is refused, file untouched",
          code == 1 and "would not read cleanly" in out
          and open(target, encoding="utf-8").read() == before)

    broken = os.path.join(tmp, "broken.ini")
    with open(broken, "w", encoding="utf-8") as fh:
        fh.write(REAL_INI + "\n[security]\nnotify = no\n")   # a second [security]
    code, out = apply_cli("[security]\nmfa_coverage_below = 95\n", broken)
    check("cli: a file that will not parse at all is refused with the line, nothing written",
          code == 1 and "cannot be read as it stands" in out and "already exists" in out
          and "Notepad" in out and "'" not in out.split("could not be read:")[1].split("]")[0]
          and open(broken, encoding="utf-8").read().endswith("notify = no\n"))

    # no alerts.ini yet: start from the example rather than writing a stub
    fresh = os.path.join(tmp, "fresh", "alerts.ini")
    os.makedirs(os.path.dirname(fresh))
    code, out = apply_cli("[security]\nadmin_without_mfa = no\n", fresh)
    cfg = A.load_config(fresh)
    check("cli: with no alerts.ini it starts from the example and keeps every other default",
          code == 0 and "started from the example" in out
          and cfg["rules"]["security.admin_without_mfa"] is False
          and cfg["rules"]["security.mfa_coverage_below"] == 90 and not cfg["problems"])


REAL_FLEET_INI = """; Copy to config.ini and point at your fleet.
[snmp]
community = public
timeout = 2
; Canon iR-ADV devices commonly answer SNMPv1 ONLY - uncomment for those:
; version = 1

[devices]
; Display Name = ip (optionally ip:port)
Front Office = 10.0.10.21
Warehouse    = 10.0.10.22

[ranges]
; WHERE TO LOOK for printers you have not listed above. One place per line.
; Leave this section empty and nothing is ever scanned.
Office = 10.0.10.0/24
Spares = 10.0.20.50-99   ; Zoie added this one

[discovery]
; How often to look again, in hours. 0 = only when asked (--discover).
rescan_hours = 24
; Addresses to leave alone even if a scan finds them.
ignore =
; max_addresses = 1024
"""


def test_fleet_merge():
    """[ranges] is replaced as a whole so a row can be DELETED; everything else
    in the file, comments included, has to survive that."""
    out, changes = A.replace_section_in_ini(
        REAL_FLEET_INI, "ranges",
        [("Office", "10.0.10.0/24"), ("Front Desk", "10.0.30.0/24")])
    kinds = dict((k, v) for k, v in changes)
    check("fleet merge: a row nobody kept is gone",
          "Spares" not in out and ("removed", "Spares = 10.0.20.50-99") in changes)
    check("fleet merge: a new row is added and reported",
          "Front Desk = 10.0.30.0/24" in out and kinds.get("added") == "Front Desk = 10.0.30.0/24")
    check("fleet merge: a row that did not move is not a change",
          all(not v.startswith("Office") for _k, v in changes))
    check("fleet merge: the section's own explanation is kept",
          "; WHERE TO LOOK for printers" in out and "; Leave this section empty" in out)
    check("fleet merge: [snmp] and [devices] are untouched",
          "community = public" in out and "Front Office = 10.0.10.21" in out
          and "; version = 1" in out)
    check("fleet merge: [discovery] is untouched",
          "rescan_hours = 24" in out and "; max_addresses = 1024" in out)
    again, changes2 = A.replace_section_in_ini(
        out, "ranges", [("Office", "10.0.10.0/24"), ("Front Desk", "10.0.30.0/24")])
    check("fleet merge: doing it twice changes nothing", changes2 == [] and again == out)
    check("fleet merge: the blank line before the next section survives",
          "10.0.30.0/24\n\n[discovery]" in out)

    changed, ch3 = A.replace_section_in_ini(out, "ranges", [("Office", "10.0.11.0/24"),
                                                            ("Front Desk", "10.0.30.0/24")])
    check("fleet merge: an edited row reports what it was",
          ("changed", "Office = 10.0.11.0/24 (was 10.0.10.0/24)") in ch3
          and "Office = 10.0.11.0/24" in changed)
    empty, ch4 = A.replace_section_in_ini(out, "ranges", [])
    check("fleet merge: emptying the section means look nowhere, and says so",
          "10.0.10.0/24" not in empty and len(ch4) == 2
          and all(k == "removed" for k, _v in ch4))

    added, ch5 = A.replace_section_in_ini("[snmp]\ncommunity = public\n", "ranges",
                                          [("Office", "10.0.10.0/24")])
    check("fleet merge: a file with no [ranges] yet gets one",
          "[ranges]" in added and "Office = 10.0.10.0/24" in added and len(ch5) == 1)

    words = A.describe_fleet_changes("ranges", changes)
    check("fleet merge: the change list reads as sentences",
          "Now looking in Front Desk = 10.0.30.0/24." in words
          and "No longer looking in Spares = 10.0.20.50-99." in words)

    # [discovery] merges key by key, and keeps a person's note on the line
    merged, dchanges = A.merge_into_ini(REAL_FLEET_INI, [("discovery", {"rescan_hours": "6"})])
    check("fleet merge: a discovery setting merges without disturbing the rest",
          "rescan_hours = 6" in merged and "; max_addresses = 1024" in merged
          and "ignore =" in merged and len(dchanges) == 1)


def _fleet_dir(tmp, with_config=True):
    d = os.path.join(tmp, "print-fleet-dashboard")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "config.example.ini"), "w", encoding="utf-8") as fh:
        fh.write(REAL_FLEET_INI)
    target = os.path.join(d, "config.ini")
    if with_config:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(REAL_FLEET_INI)
    return target


def test_fleet_cli(tmp):
    """One block, two files: each section has to reach the file that reads it,
    and a block carrying neither must not touch either."""
    alerts_ini = os.path.join(tmp, "alerts.ini")
    with open(alerts_ini, "w", encoding="utf-8") as fh:
        fh.write(REAL_INI)
    fleet_ini = _fleet_dir(tmp)

    # ranges only: the printer config moves, alerts.ini does not
    frag = "[ranges]\nOffice = 10.0.10.0/24\nFront Desk = 10.0.30.0/24\n"
    code, out = apply_cli(frag, alerts_ini, fleet_path=fleet_ini)
    text = open(fleet_ini, encoding="utf-8").read()
    check("fleet cli: a ranges-only block writes the printer config and reports in plain words",
          code == 0 and "Now looking in Front Desk = 10.0.30.0/24." in out
          and "No longer looking in Spares" in out)
    check("fleet cli: it says what to do next", 'Refresh IT Ops Data' in out)
    check("fleet cli: the printer config actually changed",
          "Front Desk = 10.0.30.0/24" in text and "Spares" not in text)
    check("fleet cli: the parts this must never touch survive",
          "community = public" in text and "Front Office = 10.0.10.21" in text
          and "; WHERE TO LOOK for printers" in text and "rescan_hours = 24" in text)
    check("fleet cli: the previous version is kept",
          open(fleet_ini + ".bak", encoding="utf-8").read() == REAL_FLEET_INI)
    check("fleet cli: alerts.ini was not touched",
          open(alerts_ini, encoding="utf-8").read() == REAL_INI
          and not os.path.exists(alerts_ini + ".bak"))

    code, out = apply_cli(frag, alerts_ini, fleet_path=fleet_ini)
    check("fleet cli: running the same block again is a no-op",
          code == 0 and "already says all of that" in out)

    # both kinds in one block
    both = ("[send]\nwhen = every-refresh\n"
            "[ranges]\nOffice = 10.0.10.0/24\nFront Desk = 10.0.30.0/24\n"
            "[discovery]\nrescan_hours = 12\n")
    code, out = apply_cli(both, alerts_ini, fleet_path=fleet_ini)
    fleet_text = open(fleet_ini, encoding="utf-8").read()
    check("fleet cli: one block reaches both files",
          code == 0 and "rescan_hours: 12 (was 24)." in out
          and "summary after every refresh" in out)
    check("fleet cli: both files really changed",
          "rescan_hours = 12" in fleet_text
          and "when = every-refresh" in open(alerts_ini, encoding="utf-8").read())

    # a place that is not a place: nothing at all is written
    before_fleet = open(fleet_ini, encoding="utf-8").read()
    before_alerts = open(alerts_ini, encoding="utf-8").read()
    code, out = apply_cli("[send]\nwhen = changes\n[ranges]\nOffice = the third floor\n",
                          alerts_ini, fleet_path=fleet_ini)
    check("fleet cli: a range that is not an address stops the whole block",
          code == 2 and "not an address" in out and "Office" in out)
    check("fleet cli: and neither file is touched",
          open(fleet_ini, encoding="utf-8").read() == before_fleet
          and open(alerts_ini, encoding="utf-8").read() == before_alerts)

    # a dry run says what would happen to both, and writes nothing
    code, out = apply_cli("[ranges]\nOffice = 10.0.99.0/24\n[send]\nwhen = changes\n",
                          alerts_ini, ["--dry-run"], fleet_path=fleet_ini)
    check("fleet cli: a dry run describes both halves and changes nothing",
          code == 0 and "Would change" in out and "Where to look changed" in out
          and open(fleet_ini, encoding="utf-8").read() == before_fleet
          and open(alerts_ini, encoding="utf-8").read() == before_alerts)

    # no config.ini yet: start from the example, keeping its comments
    fresh = _fleet_dir(os.path.join(tmp, "fresh"), with_config=False)
    code, out = apply_cli("[ranges]\nLobby = 10.0.50.0/24\n", alerts_ini, fleet_path=fresh)
    fresh_text = open(fresh, encoding="utf-8").read()
    check("fleet cli: with no printer config it starts from the example",
          code == 0 and "started from the example" in out
          and "Lobby = 10.0.50.0/24" in fresh_text and "community = public" in fresh_text)

    # a dry run must not create that file either
    fresh2 = _fleet_dir(os.path.join(tmp, "fresh2"), with_config=False)
    code, out = apply_cli("[ranges]\nLobby = 10.0.50.0/24\n", alerts_ini,
                          ["--dry-run"], fleet_path=fresh2)
    check("fleet cli: a dry run on a fresh install creates nothing",
          code == 0 and "would start one from the example" in out and not os.path.exists(fresh2))

    # the printer tool is not installed at all
    missing = os.path.join(tmp, "nowhere", "config.ini")
    code, out = apply_cli("[ranges]\nLobby = 10.0.50.0/24\n", alerts_ini, fleet_path=missing)
    check("fleet cli: no printer tool means a plain sentence, not a stack trace",
          code == 1 and "is it installed?" in out)

    # a printer config that will not parse is refused, not half-written
    broken_dir = os.path.join(tmp, "broken")
    broken = _fleet_dir(broken_dir)
    with open(broken, "a", encoding="utf-8") as fh:
        fh.write("\nthis line is not a setting at all\n")
    code, out = apply_cli("[ranges]\nLobby = 10.0.50.0/24\n", alerts_ini, fleet_path=broken)
    check("fleet cli: a printer config that will not parse is refused, file untouched",
          code == 1 and "would not read cleanly" in out
          and open(broken, encoding="utf-8").read().endswith("not a setting at all\n"))


# --------------------------------------------------------------------------- #
# The round trip: a real browser drives the page the console builds
# --------------------------------------------------------------------------- #

def _page_html(cfg, fired=()):
    am = pages.alerts_page_model(list(fired), cfg, None, A.channels_configured(cfg, env={}))
    return pages.build_alerts(am, {"index": True, "alerts": True}, "now")


def test_round_trip(tmp):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP round trip: Playwright is not installed (pip install playwright)")
        return

    defaults = A.load_config(os.path.join(tmp, "there-is-no-such-file.ini"))
    page_path = os.path.join(tmp, "alerts.html")
    with open(page_path, "w", encoding="utf-8") as fh:
        fh.write(_page_html(defaults))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(accept_downloads=True).new_page()
        page.goto("file://" + page_path)

        untouched = page.input_value("#settings-text")
        check("round trip: the page fills the box on load without being touched",
              untouched.strip().endswith("data_stale_days = 3"))

        # THE anti-drift check: an untouched page must reproduce exactly the
        # settings that were already in force.
        target = os.path.join(tmp, "rt.ini")
        shutil.copyfile(os.path.join(ROOT, "alerts.example.ini"), target)
        code, out = apply_cli(untouched, target)
        after = A.load_config(target)
        check("round trip: an untouched page changes nothing at all",
              code == 0 and "Nothing to change" in out
              and after["rules"] == defaults["rules"] and after["tabs"] == defaults["tabs"]
              and after["send"] == defaults["send"] and not after["problems"])

        # now change one of each kind of control
        page.uncheck('[data-sec="security"][data-key="admin_without_mfa"]')
        page.fill('[data-sec="security"][data-key="mfa_coverage_below"]', "95")
        page.fill('[data-sec="licensing"][data-key="unused_monthly_cost_above"]', "")
        page.uncheck('[data-sec="fleet"][data-key="notify"]')
        page.check('[data-sec="send"][data-key="when"][value="every-refresh"]')
        page.select_option('[data-sec="send"][data-key="digest_day"]', "friday")
        page.fill('[data-sec="send"][data-key="console_link"]', "\\\\fs01\\it\\console")
        edited = page.input_value("#settings-text")

        with page.expect_download() as dl:
            page.click("#save-btn")
        download = dl.value
        saved = download.path()
        check("round trip: Save hands you a file named for the icon that applies it",
              download.suggested_filename == "it-ops-settings.txt"
              and open(saved, encoding="utf-8").read() == edited)
        check("round trip: Save says what to do next",
              'double-click "Apply Settings"' in page.inner_text("#save-msg"))

        code, out = apply_cli(edited, target)
        cfg = A.load_config(target)
        check("round trip: exactly the six edits land",
              code == 0 and cfg["rules"]["security.admin_without_mfa"] is False
              and cfg["rules"]["security.mfa_coverage_below"] == 95
              and cfg["rules"]["licensing.unused_monthly_cost_above"] == 0
              and cfg["tabs"]["fleet"]["notify"] is False
              and cfg["send"]["when"] == "every-refresh" and cfg["send"]["digest_day"] == "friday"
              and cfg["send"]["console_link"] == "\\\\fs01\\it\\console")
        check("round trip: nothing else moved",
              cfg["rules"]["security.stale_accounts_above"] == 20
              and cfg["rules"]["fleet.device_offline"] is True
              and cfg["tabs"]["security"]["notify"] is True and not cfg["problems"])
        check("round trip: an emptied number reads as off",
              A.rule_setting_text(A.RULES_BY_KEY["licensing.unused_monthly_cost_above"],
                                  cfg["rules"]["licensing.unused_monthly_cost_above"]) == "off")

        # a page built from a config that HAS a webhook must not carry it
        with_hook = A.load_config(os.path.join(ROOT, "sample", "alerts.ini"))
        hook = with_hook["teams"]["webhook"]
        leak_path = os.path.join(tmp, "leak.html")
        with open(leak_path, "w", encoding="utf-8") as fh:
            fh.write(_page_html(with_hook))
        page.goto("file://" + leak_path)
        body = page.content()
        produced = page.input_value("#settings-text")
        check("round trip: the page of a configured console still holds no webhook",
              bool(hook) and hook not in body and hook not in produced
              and "[teams]" not in produced and "[email]" not in produced)
        check("round trip: it does show that a channel is connected",
              "connected - a Workflows URL is set" in body)
        browser.close()


def test_fleet_round_trip(tmp):
    """The Print fleet tab's "Where we look" editor, driven for real: type a
    place, delete one, and prove the block it hands you lands in a printer
    config.ini that still has everything else in it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP fleet round trip: Playwright is not installed (pip install playwright)")
        return

    from console import model
    from console.sources import Feed

    with open(os.path.join(ROOT, "sample", "feeds", "fleet-discovery.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    discovery = model.discovery_model(Feed("fleet_discovery", "Printer discovery", "x",
                                           data=data, ts=None))
    html = pages.build_fleet(None, Feed("fleet", "Print fleet", None),
                             {"index": True, "fleet": True}, "now", discovery=discovery)
    page_path = os.path.join(tmp, "fleet.html")
    with open(page_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    target = _fleet_dir(tmp)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(accept_downloads=True).new_page()
        page.goto("file://" + page_path)

        untouched = page.input_value("#settings-text")
        check("fleet round trip: the page fills the box from the file, untouched",
              "[ranges]" in untouched and "Office = 10.0.10.0/24" in untouched
              and "Warehouse = 10.0.20.50-99" in untouched and "rescan_hours = 24" in untouched)
        check("fleet round trip: the block carries nothing but the two sections it owns",
              "[snmp]" not in untouched and "[devices]" not in untouched
              and "public" not in untouched
              and sorted(l for l in untouched.splitlines() if l.startswith("["))
              == ["[discovery]", "[ranges]"])
        check("fleet round trip: and the page says so in as many words",
              "Your SNMP community and the printers you listed by hand are not in it" in html)

        rows = page.query_selector_all('[data-rowsec="ranges"] .row')
        check("fleet round trip: every place has a row, plus blanks to fill in",
              len(rows) == len(discovery["places"]) + 2)

        # A place the LAST scan could not do says so on its own row - and keeps
        # saying it while you look at the page, because that message is the
        # only thing explaining why it found nothing.
        check("fleet round trip: a place that could not be scanned says so on its row",
              "16,777,214 addresses" in rows[2].query_selector(".rowmsg").inner_text())
        rows[2].query_selector('[data-row="value"]').fill("10.0.0.0/24")
        check("fleet round trip: editing that place clears a message that no longer applies",
              rows[2].query_selector(".rowmsg").inner_text().strip() == "")
        rows[2].query_selector('[data-row="value"]').fill("10.0.0.0/8")
        check("fleet round trip: putting it back brings the reason back",
              "16,777,214 addresses" in rows[2].query_selector(".rowmsg").inner_text())

        # delete the oversized one, fix a name, add a new place
        rows[2].query_selector('[data-row="name"]').fill("")
        rows[2].query_selector('[data-row="value"]').fill("")
        rows[3].query_selector('[data-row="name"]').fill("Front Desk")
        rows[3].query_selector('[data-row="value"]').fill("10.0.30.0/24")
        page.fill('[data-sec="discovery"][data-key="rescan_hours"]', "6")
        page.fill('[data-sec="discovery"][data-key="ignore"]', "10.0.10.99; 10.0.10.100")
        edited = page.input_value("#settings-text")
        check("fleet round trip: the block says exactly what the rows say",
              "Front Desk = 10.0.30.0/24" in edited and "Old VLAN" not in edited
              and "rescan_hours = 6" in edited and "ignore = 10.0.10.99; 10.0.10.100" in edited)

        # a typo is caught in the browser, before anything is applied
        rows[3].query_selector('[data-row="value"]').fill("10.0.30.0/99")
        check("fleet round trip: a bad address is called out on its own row while you type",
              "not an address" in rows[3].query_selector(".rowmsg").inner_text())
        rows[3].query_selector('[data-row="value"]').fill("10.0.30.0/24")
        check("fleet round trip: fixing it clears the message",
              rows[3].query_selector(".rowmsg").inner_text().strip() == "")

        # "+ add a place" gives you an empty row, not a copy of the last one
        page.click('[data-addrow="ranges"]')
        fresh = page.query_selector_all('[data-rowsec="ranges"] .row')[-1]
        check("fleet round trip: add-a-place hands you an empty row",
              len(page.query_selector_all('[data-rowsec="ranges"] .row')) == len(rows) + 1
              and fresh.query_selector('[data-row="name"]').input_value() == ""
              and fresh.query_selector('[data-row="value"]').input_value() == "")

        with page.expect_download() as dl:
            page.click("#save-btn")
        check("fleet round trip: Save hands you the same file the icon looks for",
              dl.value.suggested_filename == "it-ops-settings.txt")

        edited = page.input_value("#settings-text")
        code, out = apply_cli(edited, os.path.join(tmp, "alerts-unused.ini"), fleet_path=target)
        text = open(target, encoding="utf-8").read()
        check("fleet round trip: what the page produced applies cleanly",
              code == 0 and "Now looking in Front Desk = 10.0.30.0/24." in out)
        check("fleet round trip: the file now says what the page said",
              "Front Desk = 10.0.30.0/24" in text and "Office = 10.0.10.0/24" in text
              and "Old VLAN" not in text and "rescan_hours = 6" in text
              and "ignore = 10.0.10.99; 10.0.10.100" in text)
        check("fleet round trip: everything the page never showed is still there",
              "community = public" in text and "Front Office = 10.0.10.21" in text
              and "; WHERE TO LOOK for printers" in text and "; version = 1" in text)
        check("fleet round trip: it reads back as a config file",
              _reads_as_ini(text))

        # THE anti-drift check: rebuild the page from the file we just wrote and
        # apply it untouched - nothing may move.
        cfg = _ranges_of(text)
        data2 = dict(data)
        data2["Ranges"] = [{"Name": n, "Spec": v, "Addresses": 0, "LastScanUtc": None,
                            "Found": 0, "Problem": None} for n, v in cfg]
        data2["RescanHours"] = 6
        data2["Ignored"] = ["10.0.10.99", "10.0.10.100"]
        d2 = model.discovery_model(Feed("fleet_discovery", "Printer discovery", "x",
                                        data=data2, ts=None))
        again_path = os.path.join(tmp, "fleet2.html")
        with open(again_path, "w", encoding="utf-8") as fh:
            fh.write(pages.build_fleet(None, Feed("fleet", "Print fleet", None),
                                       {"index": True, "fleet": True}, "now", discovery=d2))
        page.goto("file://" + again_path)
        code, out = apply_cli(page.input_value("#settings-text"),
                              os.path.join(tmp, "alerts-unused.ini"), fleet_path=target)
        check("fleet round trip: an untouched page changes nothing at all",
              code == 0 and "already says all of that" in out
              and open(target, encoding="utf-8").read() == text)
        browser.close()


def _reads_as_ini(text):
    import configparser
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    cp.optionxform = str
    try:
        cp.read_string(text)
        return True
    except configparser.Error:
        return False


def _ranges_of(text):
    import configparser
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    cp.optionxform = str
    cp.read_string(text)
    return list(cp["ranges"].items()) if cp.has_section("ranges") else []


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_parse()
        test_parse_fleet()
        test_merge()
        test_fleet_merge()
        test_cli(_mk(tmp, "cli"))
        test_fleet_cli(_mk(tmp, "fleet"))
        test_round_trip(_mk(tmp, "rt"))
        test_fleet_round_trip(_mk(tmp, "frt"))
    print("")
    if FAILS:
        print("RESULT: %d FAILURES" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("RESULT: ALL PASS")
    return 0


def _mk(base, name):
    path = os.path.join(base, name)
    os.makedirs(path, exist_ok=True)
    return path


if __name__ == "__main__":
    sys.exit(main())
