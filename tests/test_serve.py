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

import contextlib
import io
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


def _cli_default(mod, flag):
    """The default argparse would apply for a flag, without running main."""
    import argparse
    seen = {}
    real = argparse.ArgumentParser.add_argument

    def spy(self, *a, **kw):
        for name in a:
            if name == flag:
                seen[flag] = kw.get("default")
        return real(self, *a, **kw)

    argparse.ArgumentParser.add_argument = spy
    quiet = io.StringIO()
    try:
        try:
            with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                mod.main(["--help"])       # prints the usage nobody here needs
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.add_argument = real
    return seen.get(flag)


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

    # ---- one address, the same one every time ---- #
    # It used to ask the operating system for any free port, so the console
    # lived somewhere different on every launch: nothing to bookmark, and the
    # address in your history was wrong by the next morning.
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import socket as _socket
    import threading as _threading

    check("port: there is a usual address, and it is a real fixed port",
          isinstance(mod.DEFAULT_PORT, int) and 1024 < mod.DEFAULT_PORT < 65535)
    check("port: and it is what you get without asking",
          mod.main.__doc__ is not mod.DEFAULT_PORT and
          _cli_default(mod, "--port") == mod.DEFAULT_PORT)

    def _free_port():
        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        p_ = s.getsockname()[1]
        s.close()
        return p_

    base = _free_port()
    srv, got = mod.bind_console(base, BaseHTTPRequestHandler)
    check("port: a free usual address is the one it takes", got == base)
    srv.server_close()

    # something else is sitting on it: move along rather than refuse to start
    holder = _socket.socket()
    holder.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", base))
    holder.listen(1)
    srv, got = mod.bind_console(base, BaseHTTPRequestHandler)
    check("port: taken usual address moves along by one, it does not fail",
          got == base + 1)
    srv.server_close()

    # the whole ladder is taken: still start, anywhere
    holders = [holder]
    for i in range(1, mod.PORT_LADDER):
        h = _socket.socket()
        h.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        try:
            h.bind(("127.0.0.1", base + i))
            h.listen(1)
            holders.append(h)
        except OSError:
            h.close()
    srv, got = mod.bind_console(base, BaseHTTPRequestHandler)
    check("port: with every usual address taken it still starts somewhere",
          got and got not in range(base, base + mod.PORT_LADDER))
    srv.server_close()
    for h in holders:
        h.close()

    # Windows lets a second server bind a port another is LISTENING on when
    # SO_REUSEADDR is set - two consoles, and the one-refresh-at-a-time lock
    # quietly meaningless. It has to be off there.
    check("port: address reuse is off on Windows, on everywhere else",
          mod.ConsoleServer.allow_reuse_address == (os.name != "nt"))

    # A console already running is found at the usual address even when the
    # note it leaves behind says nothing - that is the point of a usual address.
    class _NotUs(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    stranger_port = _free_port()
    stranger = HTTPServer(("127.0.0.1", stranger_port), _NotUs)
    _threading.Thread(target=stranger.serve_forever, daemon=True).start()
    check("port: something else answering on a port is not mistaken for the console",
          mod.already_serving(None, [stranger_port]) is None)
    stranger.shutdown()
    stranger.server_close()

    live_port = _free_port()
    d2 = tempfile.mkdtemp(prefix="itops-port-")
    s2 = os.path.join(d2, "console-site")
    o2 = os.path.join(d2, "output")
    os.makedirs(s2)
    os.makedirs(o2)          # so the note is written, and can then be taken away
    with open(os.path.join(s2, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><body>hi</body></html>")
    c2 = os.path.join(d2, "tools", "it-ops-console")
    os.makedirs(c2)
    shutil.copy(os.path.join(ROOT, "serve-console.py"), os.path.join(c2, "serve-console.py"))
    first = subprocess.Popen(
        [sys.executable, os.path.join(c2, "serve-console.py"), "--site", s2,
         "--tool-root", os.path.join(d2, "tools"), "--output-root", o2,
         "--port", str(live_port), "--print-url"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        limit = time.time() + 30
        while time.time() < limit and not mod.already_serving(None, [live_port]):
            time.sleep(0.3)
        check("port: a console at the usual address is found with no note to read",
              mod.already_serving(None, [live_port]) == "http://127.0.0.1:%d/" % live_port)
        # Take the note away. The usual address is now the ONLY way a second
        # launch can know the first is there - which is the point of having one.
        try:
            os.remove(os.path.join(o2, "console-server.json"))
        except OSError:
            pass
        try:
            second = subprocess.run(
                [sys.executable, os.path.join(c2, "serve-console.py"), "--site", s2,
                 "--tool-root", os.path.join(d2, "tools"), "--output-root", o2,
                 "--port", str(live_port)],
                capture_output=True, text=True, timeout=45)
            said = second.stdout
        except subprocess.TimeoutExpired:
            # It did not recognise the running console and started serving.
            said = ""
        check("port: with the note gone it still opens the console you have",
              "already running" in said and str(live_port) in said)
    finally:
        first.terminate()
        try:
            first.wait(timeout=10)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=10)
        sh.rmtree(d2, ignore_errors=True)

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
