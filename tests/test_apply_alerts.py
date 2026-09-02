"""Test suite for editing alerts from the console:  python tests/test_apply_alerts.py

Two halves.

The merge (no browser): the settings block the Alerts page produces goes into
alerts.ini touching only the lines it names - comments, the [teams] webhook,
the [email] block and anything unrecognised all survive - and a block that is
not settings changes nothing at all.

The round trip (needs Playwright; skipped with a note if it is not installed):
the page is opened in a real browser, its controls are read and changed, and
the text it hands you is applied for real. The important one is that an
untouched page round-trips to exactly the settings that were already in force,
because that is what stops the page and the rules from drifting apart.
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


def apply_cli(settings_text, config_path, extra=()):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "apply-alerts.py"),
                        "--config", config_path] + list(extra),
                       input=settings_text, capture_output=True, text=True)
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
    settings, notes = A.parse_settings_fragment(
        "[send]\nwhen = every-refresh\n[teams]\nwebhook = https://leak\n"
        "[security]\nnotify = no\nmfa_coverage_below = 95\nbogus = 1\n[kitchen]\nsink = yes\n")
    flat = dict(settings)
    check("parse: keeps the sections it knows", set(flat) == {"send", "security"})
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


def test_merge():
    settings, _ = A.parse_settings_fragment(
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
    settings2, _ = A.parse_settings_fragment("[fleet]\ndevice_error = no\n")
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
          code == 2 and "does not look like alert settings" in out
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
              download.suggested_filename == "alert-settings.txt"
              and open(saved, encoding="utf-8").read() == edited)
        check("round trip: Save says what to do next",
              'double-click "Apply Alert Settings"' in page.inner_text("#save-msg"))

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


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_parse()
        test_merge()
        test_cli(_mk(tmp, "cli"))
        test_round_trip(_mk(tmp, "rt"))
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
