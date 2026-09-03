"""The console's buttons, driven in a real browser:  python tests/test_buttons.py

Needs Playwright; skipped with a note if it is not installed.

Two claims, and they are the whole point of serving the console:

  1. SERVED - "Refresh now" and "Apply settings" are really there and really
     do it. Apply reaches apply-settings and reports back in the page; Refresh
     starts run-all and sends you to the live status page.
  2. AS A FILE - the same pages still work the old way. A page opened from
     disk cannot start anything, so the Refresh bar must not appear at all and
     Save must go back to copy-and-download. A button that does nothing is
     worse than no button.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAILS = []


def check(label, cond):
    print("%s %s" % ("PASS" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP buttons: Playwright is not installed (pip install playwright)")
        return 0

    tmp = tempfile.mkdtemp(prefix="itops-buttons-")
    console = os.path.join(tmp, "tools", "it-ops-console")
    site = os.path.join(tmp, "console-site")
    os.makedirs(console)
    os.makedirs(os.path.join(tmp, "output"))

    # a real console, built from the bundled sample
    rc = subprocess.run([sys.executable, os.path.join(ROOT, "build.py"), "--sample", "--out", site],
                        capture_output=True, text=True)
    check("buttons: the sample console builds", rc.returncode == 0)

    shutil.copy(os.path.join(ROOT, "serve-console.py"), console)
    with open(os.path.join(console, "run-all.ps1"), "w", encoding="utf-8") as fh:
        fh.write("param($ToolRoot,$OutputRoot,$SitePath,$Python,[switch]$NoStatusPage)\n"
                 "Add-Content -Path (Join-Path '%s' 'ran.txt') -Value 'refreshed'\n" % tmp.replace("\\", "/"))
    with open(os.path.join(console, "apply-settings.py"), "w", encoding="utf-8") as fh:
        fh.write("import sys\ntext = sys.stdin.read()\n"
                 "open(r'%s','w',encoding='utf-8').write(text)\n"
                 "print('Changed 1 setting:')\nprint('  Messages: only when something changes.')\n"
                 % os.path.join(tmp, "applied.txt"))
    # status.html is what Refresh sends you to
    with open(os.path.join(site, "status.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><head><title>Refreshing</title></head><body>live activity</body></html>")

    proc = subprocess.Popen(
        [sys.executable, os.path.join(console, "serve-console.py"),
         "--site", site, "--tool-root", os.path.join(tmp, "tools"),
         "--output-root", os.path.join(tmp, "output"), "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "http://127.0.0.1:" in line:
                url = line.strip()
                break
        if not url:
            check("buttons: the server started", False)
            return 1

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context().new_page()

            # ---- 1. served ---- #
            page.goto(url)
            check("served: the Refresh bar is visible", page.is_visible("#refresh-btn"))
            check("served: the page was given a key", bool(page.evaluate("window.CONSOLE_KEY")))
            check("served: the file note is nowhere to be seen",
                  page.is_hidden("#filenote") and "looking at these pages as files"
                  not in page.inner_text("body"))

            page.goto(url.rstrip("/") + "/alerts.html")
            check("served: the settings button says Apply", page.inner_text("#save-btn").strip() == "Apply settings")
            page.click("#save-btn")
            page.wait_for_function("document.getElementById('save-msg').textContent.indexOf('Applied') >= 0", timeout=20000)
            check("served: it says it applied, and what changed",
                  "Applied" in page.inner_text("#save-msg") and "Messages" in page.inner_text("#save-msg"))
            applied = open(os.path.join(tmp, "applied.txt"), encoding="utf-8").read()
            check("served: the settings really reached apply-settings", "[send]" in applied)
            check("served: and it tells you the next step",
                  "Refresh now" in page.inner_text("#save-msg"))

            page.click("#refresh-btn")
            page.wait_for_url("**/status.html*", timeout=20000)
            check("served: Refresh starts and takes you to the live page", "status.html" in page.url)
            waited = time.time() + 20
            while time.time() < waited and not os.path.exists(os.path.join(tmp, "ran.txt")):
                time.sleep(0.2)
            check("served: run-all really ran", os.path.exists(os.path.join(tmp, "ran.txt")))

            # ---- 2. opened as a plain file ---- #
            page.goto("file://" + os.path.join(site, "index.html"))
            check("as a file: no Refresh button is shown at all",
                  page.is_hidden("#refresh-btn") or page.locator("#refresh-btn").count() == 0)
            # The whole point: a file-opened page must SAY so, standing, before
            # anyone clicks anything. Absence of a button is not a message.
            check("as a file: the note is visible without touching anything",
                  page.is_visible("#filenote"))
            note = page.inner_text("#filenote")
            check("as a file: it says nothing on the page can change anything",
                  "nothing on them can change anything" in note)
            check("as a file: it names the icon to use instead", "IT Ops Console" in note)
            check("as a file: and how to tell you got the real one",
                  "127.0.0.1" in note and "file://" in note)
            page.goto("file://" + os.path.join(site, "alerts.html"))
            check("as a file: the button still reads Apply settings",
                  page.inner_text("#save-btn").strip() == "Apply settings")
            page.click("#save-btn")
            said = page.inner_text("#save-msg")
            check("as a file: it says it cannot change anything, and where to go",
                  "cannot change anything" in said and 'IT Ops Console' in said)
            check("as a file: it does not hand you a file to carry to an icon",
                  "Downloads" not in said and "Ctrl+C" not in said)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

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
