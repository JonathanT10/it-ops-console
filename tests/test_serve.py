"""The local console server:  python tests/test_serve.py

The console is now served rather than opened as files, so its buttons can
actually start a refresh and apply your settings. That means a port on this
machine accepts POSTs - so what is checked here is not only "the buttons work"
but "nothing else can use them":

  - it listens on 127.0.0.1 only, never on the network;
  - a POST without the key is refused;
  - the key is handed to the console's own pages and nowhere else, and never
    travels in an address, where it would be bookmarked and kept in history;
  - double-clicking the icon twice opens the console you have, not a second;
  - no path trickery reaches a file outside the console folder;
  - a refresh actually starts the real run-all, and two at once is refused;
  - apply hands the block to the real apply-settings and reports what it said.

Nothing here signs in to anything: run-all and apply-settings are replaced with
stubs that record how they were called.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILS = []


def check(label, cond):
    print("%s %s" % ("PASS" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def post(url, data=None, key=None, timeout=30):
    req = urllib.request.Request(url, data=(data or b""), method="POST")
    if key:
        req.add_header("X-Console-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body}


def get(url, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    tmp = tempfile.mkdtemp(prefix="itops-serve-")
    console = os.path.join(tmp, "tools", "it-ops-console")
    site = os.path.join(tmp, "console-site")
    outroot = os.path.join(tmp, "output")
    os.makedirs(console); os.makedirs(site); os.makedirs(outroot)
    os.makedirs(os.path.join(tmp, "secret-place"))

    # the console's own files
    with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><head><title>console</title></head><body>hello</body></html>")
    with open(os.path.join(site, "progress.js"), "w", encoding="utf-8") as fh:
        fh.write("window.PROGRESS={};")
    # something that must never be served
    with open(os.path.join(tmp, "secret-place", "alerts.ini"), "w", encoding="utf-8") as fh:
        fh.write("webhook = https://example.invalid/SECRET")

    # stubs standing in for the real things
    with open(os.path.join(console, "run-all.ps1"), "w", encoding="utf-8") as fh:
        fh.write("param($ToolRoot,$OutputRoot,$SitePath,$Python,[switch]$NoStatusPage)\n"
                 "Add-Content -Path (Join-Path '%s' 'ran.txt') -Value \"refresh $SitePath\"\n"
                 "Start-Sleep -Seconds 3\n" % tmp.replace("\\", "/"))
    with open(os.path.join(console, "apply-settings.py"), "w", encoding="utf-8") as fh:
        fh.write("import sys\n"
                 "text = sys.stdin.read()\n"
                 "open(r'%s', 'w', encoding='utf-8').write(text)\n"
                 "print('applied %%d characters' %% len(text))\n"
                 "sys.exit(0 if '[send]' in text else 2)\n"
                 % os.path.join(tmp, "applied.txt"))
    # the live-progress template that lives with the tools
    with open(os.path.join(console, "refresh-status.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><head><title>Refreshing</title></head><body>TODAYS TEMPLATE</body></html>")
    # ...and an older copy already sitting in the built console
    with open(os.path.join(site, "status.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><head><title>Refreshing</title></head><body>LAST YEARS COPY</body></html>")
    import shutil
    shutil.copy(os.path.join(ROOT, "serve-console.py"), os.path.join(console, "serve-console.py"))

    proc = subprocess.Popen(
        [sys.executable, os.path.join(console, "serve-console.py"),
         "--site", site, "--tool-root", os.path.join(tmp, "tools"),
         "--output-root", outroot, "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = key = None
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "http://127.0.0.1:" in line:
                url = line.strip()
                break
        check("server: it says where it is listening", bool(url))
        if not url:
            print(proc.stdout.read()[:2000])
            return 1
        check("server: the address it prints carries no key", "k=" not in url)
        base = url.rstrip("/")
        port = int(base.rsplit(":", 1)[1])

        # The only way to have the key is to be a page this server sent.
        code, body = get(base + "/")
        m = re.search(r'window\.CONSOLE_KEY=("(?:[^"\\]|\\.)*")', body)
        key = json.loads(m.group(1)) if m else None
        check("server: a page it serves is given the key", bool(key))
        if not key:
            print(body[:800])
            return 1

        # ---- 1. only this computer ---- #
        s = socket.socket()
        s.settimeout(3)
        outside = None
        try:
            host = socket.gethostbyname(socket.gethostname())
            outside = s.connect_ex((host, port))
        except OSError:
            outside = 1
        finally:
            s.close()
        check("server: nothing on the network can reach it (127.0.0.1 only)",
              outside != 0 or socket.gethostbyname(socket.gethostname()).startswith("127."))

        # ---- 2. the console's files are served ---- #
        check("server: it serves the console", code == 200 and "hello" in body)
        code, body = get(base + "/progress.js")
        check("server: it serves the live progress file", code == 200 and "PROGRESS" in body)

        # ---- 3. nothing outside the console folder ---- #
        for trick in ("/../secret-place/alerts.ini",
                      "/..%2fsecret-place%2falerts.ini",
                      "/%2e%2e/secret-place/alerts.ini",
                      "/....//secret-place/alerts.ini"):
            code, body = get(base + trick)
            if "SECRET" in body:
                check("server: refuses %s" % trick, False)
            else:
                check("server: refuses %s" % trick, True)

        # ---- 4. a stranger cannot press the buttons ---- #
        code, obj = post(base + "/api/refresh")
        check("server: a POST with no key is refused", code == 403 and obj.get("ok") is False)
        code, obj = post(base + "/api/refresh", key="not-the-key")
        check("server: a POST with the wrong key is refused", code == 403)
        check("server: and nothing ran", not os.path.exists(os.path.join(tmp, "ran.txt")))
        code, obj = post(base + "/api/nonsense", key=key)
        check("server: there is no third thing it will run", code == 404)

        # ---- 5. Refresh ---- #
        code, obj = post(base + "/api/refresh", key=key)
        check("server: Refresh starts", code == 200 and obj.get("ok") is True)
        # The browser is sent to the live page right after this returns. The
        # last run's progress must not still be lying there saying "All done".
        check("server: starting a refresh takes the last run's progress away first",
              not os.path.exists(os.path.join(site, "progress.js")))
        code, body = get(base + "/status.html")
        check("server: and lands you on today's live page, not an older copy",
              "TODAYS TEMPLATE" in body)
        started = time.time() + 20
        while time.time() < started and not os.path.exists(os.path.join(tmp, "ran.txt")):
            time.sleep(0.2)
        ran = ""
        if os.path.exists(os.path.join(tmp, "ran.txt")):
            ran = open(os.path.join(tmp, "ran.txt"), encoding="utf-8").read()
        check("server: it really ran run-all, pointed at the console", "refresh" in ran and site.replace("\\", "/") in ran.replace("\\", "/"))
        code, obj = post(base + "/api/refresh", key=key)
        check("server: a second refresh while one is running is refused, kindly",
              code == 409 and "already running" in obj.get("message", ""))

        # ---- 6. Apply ---- #
        block = "# IT Ops Console settings\n[send]\nwhen = changes\n"
        code, obj = post(base + "/api/apply", data=block.encode("utf-8"), key=key)
        check("server: Apply hands the block over and says what came back",
              code == 200 and obj.get("ok") is True and "applied" in obj.get("message", ""))
        check("server: the block arrived intact",
              open(os.path.join(tmp, "applied.txt"), encoding="utf-8").read() == block)
        code, obj = post(base + "/api/apply", data=b"this is not settings", key=key)
        check("server: a block that is not settings comes back not-ok, with the reason",
              code == 200 and obj.get("ok") is False and obj.get("code") == 2)
        code, obj = post(base + "/api/apply", data=b"", key=key)
        check("server: an empty body is refused", code == 400)
        code, obj = post(base + "/api/apply", data=b"x" * (300 * 1024), key=key)
        check("server: an absurdly large body is refused", code == 400)

        # ---- 7. the icon, pressed twice ---- #
        check("server: it leaves a note saying where it is",
              os.path.isfile(os.path.join(outroot, "console-server.json")))
        second = subprocess.run(
            [sys.executable, os.path.join(console, "serve-console.py"),
             "--site", site, "--tool-root", os.path.join(tmp, "tools"),
             "--output-root", outroot, "--port", "0"],
            capture_output=True, text=True, timeout=60)
        check("server: pressing the icon again opens the console you already have",
              second.returncode == 0 and "already running" in second.stdout
              and (":%d/" % port) in second.stdout)
        check("server: and it did not start a second one",
              "Leave it open" not in second.stdout)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        # A window closed abruptly leaves its note behind. The note alone proves
        # nothing - nothing answers on that port any more - so the next launch
        # must start a real server rather than point at a ghost.
        ghost = subprocess.Popen(
            [sys.executable, os.path.join(console, "serve-console.py"),
             "--site", site, "--tool-root", os.path.join(tmp, "tools"),
             "--output-root", outroot, "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        said = None
        end = time.time() + 30
        while time.time() < end:
            line = ghost.stdout.readline()
            if not line:
                break
            if "http://127.0.0.1:" in line:
                said = line.strip()
                break
        check("server: a note left by a closed window does not stop the next one",
              bool(said) and "already running" not in (said or ""))
        ghost.terminate()
        try:
            ghost.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ghost.kill()
            ghost.wait(timeout=10)
        import shutil as sh
        sh.rmtree(tmp, ignore_errors=True)

    # ---- a refresh that never finishes is stopped, and the page says so ---- #
    # The per-step deadline used to be the guarantee, but it belonged to child
    # processes, and on the person route there are none any more - a collector
    # in a child had to sign in a second time, which is the failure this whole
    # change removes. So the guarantee lives here now: the server that started
    # the refresh is the thing still watching it.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "itops_serve_console", os.path.join(ROOT, "serve-console.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    shell = mod.find_powershell()
    check("deadline: there is a PowerShell to test the deadline with", bool(shell))
    if shell:
        d = tempfile.mkdtemp(prefix="itops-deadline-")
        cdir = os.path.join(d, "tools", "it-ops-console")
        sdir = os.path.join(d, "console-site")
        os.makedirs(cdir)
        os.makedirs(sdir)
        with open(os.path.join(cdir, "run-all.ps1"), "w", encoding="utf-8") as fh:
            fh.write("param($ToolRoot,$OutputRoot,$SitePath,$Python,[switch]$NoStatusPage)\n"
                     "Start-Sleep -Seconds 300\n")
        runner = mod.Runner(cdir, sdir, os.path.join(d, "tools"),
                            os.path.join(d, "output"), sys.executable, shell)
        runner.deadline_seconds = 4
        started, _ = runner.start_refresh()
        check("deadline: the refresh started", started)
        stuck = runner.proc
        # what a real run leaves lying there mid-flight, written after the
        # start because starting a refresh clears the last run's progress
        time.sleep(1)
        with open(os.path.join(sdir, "progress.js"), "w", encoding="utf-8") as fh:
            fh.write("window.PROGRESS = " + json.dumps({
                "done": False, "ok": True, "summary": [],
                "steps": [
                    {"key": "signin", "label": "Signing you in", "detail": "read-only",
                     "state": "ok", "seconds": 1.2, "now": None},
                    {"key": "security", "label": "Checking security posture",
                     "detail": "MFA coverage, admin accounts", "state": "running",
                     "seconds": None, "now": None}],
                "stats": [], "log": ["Collecting users (this can take a while)..."]}) + ";")
        limit = time.time() + 60
        while time.time() < limit and stuck.poll() is None:
            time.sleep(0.5)
        check("deadline: a refresh that overruns is stopped", stuck.poll() is not None)
        with open(os.path.join(sdir, "progress.js"), encoding="utf-8") as fh:
            raw = fh.read()
        payload = {}
        try:
            payload = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        except ValueError:
            pass
        words = " ".join(payload.get("summary") or [])
        check("deadline: the live page is told the run is over, not left spinning",
              payload.get("done") is True)
        check("deadline: and that it did not go well", payload.get("ok") is False)
        check("deadline: it says the refresh was stopped, and roughly when",
              "stopped after" in words and "had not finished" in words)
        check("deadline: and points at the usual cause",
              "sign-in window waiting behind another window" in words)
        onstep = [x for x in (payload.get("steps") or []) if x.get("key") == "security"]
        check("deadline: the step it was on is marked failed, not left running",
              bool(onstep) and onstep[0].get("state") == "failed")
        check("deadline: a step that had already finished is left alone",
              any(x.get("key") == "signin" and x.get("state") == "ok"
                  for x in (payload.get("steps") or [])))
        check("deadline: what the run had already said is kept",
              "Collecting users (this can take a while)..." in (payload.get("log") or []))

        # ...and a refresh that finishes in time is never touched
        with open(os.path.join(cdir, "run-all.ps1"), "w", encoding="utf-8") as fh:
            fh.write("param($ToolRoot,$OutputRoot,$SitePath,$Python,[switch]$NoStatusPage)\n"
                     "Start-Sleep -Seconds 1\n")
        runner.deadline_seconds = 30
        runner.start_refresh()
        quick = runner.proc
        limit = time.time() + 60
        while time.time() < limit and quick.poll() is None:
            time.sleep(0.5)
        time.sleep(1)
        with open(os.path.join(sdir, "progress.js"), "w", encoding="utf-8") as fh:
            fh.write("window.PROGRESS = " + json.dumps({"done": True, "ok": True,
                                                        "summary": ["All done."]}) + ";")
        time.sleep(8)
        with open(os.path.join(sdir, "progress.js"), encoding="utf-8") as fh:
            after = fh.read()
        check("deadline: a run that finished in time is never marked stopped",
              "All done." in after and "stopped after" not in after)

        # a progress file that is not there at all must not stop it saying so
        os.remove(os.path.join(sdir, "progress.js"))
        runner.mark_stopped("test words")
        with open(os.path.join(sdir, "progress.js"), encoding="utf-8") as fh:
            bare = fh.read()
        check("deadline: it can say so even with no progress file to build on",
              "test words" in bare and '"done": true' in bare)
        sh.rmtree(d, ignore_errors=True)

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
