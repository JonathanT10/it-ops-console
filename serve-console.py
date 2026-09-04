"""Serve the console, and let its buttons actually do something.

    python serve-console.py --site ..\..\console-site --open

Until now the console was a folder of files you opened directly, and a page
opened that way can do exactly nothing on your computer - it cannot start a
refresh, and it cannot save your settings. That is why applying a setting used
to be: copy some text, find an icon on your desktop, double-click it, hope you
copied the right thing. And why there was a status page that only worked if you
opened the copy of it that happened to have data beside it.

So the console is served instead. One address, from this computer, for this
computer: 127.0.0.1 only - nothing else on the network can reach it - and every
button carries a key made fresh each time this starts. A page that does not
have the key cannot make anything happen, which matters because any program on
this machine can talk to a port on it.

It does four things and nothing else:
    GET  /...            the console's own files
    GET  /api/ping       "yes, the console is already running here"
    POST /api/refresh    start a refresh (the same run-all the icon ran)
    POST /api/apply      apply a settings block (the same apply-settings)
There is no way to ask it to run anything else.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 256 * 1024          # a settings block is a few hundred bytes
# How long a refresh may run before this stops it.
#
# Each collector used to carry its own deadline because each ran in a child
# process that could be killed. On the person route they now run inside the
# refresh itself - which is what stopped them signing in a second time and
# failing - so the guarantee moves up here, to the one thing that is still
# watching. It is deliberately generous: a real refresh of a 200-user tenant
# takes minutes, not tens of minutes, so anything past this is stuck.
RUN_DEADLINE_MINUTES = 45
STOPPED_WORDS = ('The refresh was stopped after {0} minutes because it had not finished. '
                 'The usual cause is a Microsoft sign-in window waiting behind another '
                 'window - bring it to the front and finish it, then start the refresh again.')
TEXT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


def find_powershell():
    """Where PowerShell actually is. Windows always has powershell.exe on the
    path; elsewhere it may be pwsh, or installed somewhere off it. Returning
    None is a real answer - the refresh then says so instead of failing with a
    file-not-found nobody can act on."""
    import shutil
    for name in (("powershell.exe", "pwsh") if os.name == "nt" else ("pwsh", "powershell")):
        found = shutil.which(name)
        if found:
            return found
    for guess in ("/opt/pwsh/pwsh", "/usr/bin/pwsh", "/usr/local/bin/pwsh"):
        if os.path.isfile(guess) and os.access(guess, os.X_OK):
            return guess
    return None


def stamp_path(output_root):
    """Where this records that it is running. Beside the collected data, which
    is per-install and already exists; if it does not, we simply go without -
    the worst that costs is a second console window."""
    return os.path.join(output_root, "console-server.json") if os.path.isdir(output_root) else None


def already_serving(stamp):
    """The address of a console already running here, or None.

    Double-clicking the icon twice should open the console you have, not start
    a second one: two servers means two refreshes can run at once, which is the
    exact thing the one-at-a-time lock exists to prevent. A stale note from a
    window that has since been closed answers nothing, so it is ignored."""
    if not stamp or not os.path.isfile(stamp):
        return None
    try:
        with open(stamp, "r", encoding="utf-8") as fh:
            port = int(json.load(fh).get("port") or 0)
    except (OSError, ValueError, TypeError):
        return None
    if not port:
        return None
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=2) as r:
            if json.loads(r.read().decode("utf-8", "replace")).get("itops") is True:
                return "http://127.0.0.1:%d/" % port
    except Exception:
        return None
    return None


class Runner:
    """One refresh at a time, and a record of how the last one went."""

    def __init__(self, console_dir, site, tool_root, output_root, python, powershell=None):
        self.console_dir = console_dir
        self.site = site
        self.tool_root = tool_root
        self.output_root = output_root
        self.python = python
        self.powershell = powershell or find_powershell()
        self.proc = None
        self.started = None
        self.lock = threading.Lock()
        self.deadline_seconds = RUN_DEADLINE_MINUTES * 60

    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def stop_tree(self, proc=None):
        """Stop the refresh and anything it started. Killing the PowerShell
        alone is not enough on Windows - a collector may have started its own
        child - so taskkill /T is asked first and .kill() is the backstop."""
        proc = proc or self.proc
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            proc.kill()
        except OSError:
            pass

    def mark_stopped(self, words):
        """Say so on the live page. Killing the refresh leaves progress.js
        frozen mid-run, and a page that spins for ever is exactly the thing
        this is meant to prevent - so the last run's own progress is reopened,
        marked finished-and-not-ok, and given the sentence in plain words."""
        path = os.path.join(self.site, "progress.js")
        payload = None
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                payload = json.loads(raw[start:end + 1])
        except (OSError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            payload = {"steps": [], "stats": [], "log": []}
        payload["done"] = True
        payload["ok"] = False
        payload["summary"] = [words]
        for step in payload.get("steps") or []:
            if isinstance(step, dict) and step.get("state") == "running":
                step["state"] = "failed"
                step["detail"] = words
        log = payload.get("log")
        payload["log"] = (log if isinstance(log, list) else []) + [words]
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("window.PROGRESS = " + json.dumps(payload) + ";")
        except OSError:
            pass

    def _watch(self, proc, deadline):
        """One thread per refresh, and it does nothing until the deadline. It
        is a thread rather than a check inside the API because a stuck run has
        nobody clicking anything - the whole point is that it ends without
        being asked."""
        while proc.poll() is None and time.time() < deadline:
            time.sleep(5)
        if proc.poll() is not None:
            return
        words = STOPPED_WORDS.format(max(1, int(round(self.deadline_seconds / 60.0))))
        self.stop_tree(proc)
        self.mark_stopped(words)

    def clear_stale_progress(self):
        """The Refresh button sends the browser to the live page a moment after
        this returns, and run-all needs a second or two to write its first
        progress. Without this the page finds the LAST run's progress lying
        there and announces "All done" for a refresh that has not started yet.
        Take it away first: the page then waits, and fills in when it lands.

        The status page itself is refreshed from the tools folder at the same
        time, so a console built by an older version still lands on today's."""
        try:
            os.remove(os.path.join(self.site, "progress.js"))
        except OSError:
            pass
        tpl = os.path.join(self.console_dir, "refresh-status.html")
        if os.path.isfile(tpl):
            try:
                import shutil
                shutil.copyfile(tpl, os.path.join(self.site, "status.html"))
            except OSError:
                pass

    def start_refresh(self):
        with self.lock:
            if self.busy():
                return False, "A refresh is already running."
            if not self.powershell:
                return False, ("PowerShell could not be found, so a refresh cannot be started "
                               "from here. Double-click \"Refresh IT Ops Data\" instead.")
            cmd = [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   os.path.join(self.console_dir, "run-all.ps1"),
                   "-ToolRoot", self.tool_root, "-OutputRoot", self.output_root,
                   "-SitePath", self.site, "-NoStatusPage"]
            if self.python:
                cmd += ["-Python", self.python]
            self.clear_stale_progress()
            try:
                self.proc = subprocess.Popen(cmd, cwd=self.console_dir,
                                             stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
            except OSError as e:
                return False, "Could not start the refresh (%s)." % e
            self.started = time.time()
            threading.Thread(target=self._watch,
                             args=(self.proc, self.started + self.deadline_seconds),
                             daemon=True).start()
            return True, "Refresh started."

    def apply_settings(self, text):
        """Hand the block to apply-settings.py exactly as the icon did."""
        exe = sys.executable or "python"
        cmd = [exe, os.path.join(self.console_dir, "apply-settings.py")]
        try:
            p = subprocess.run(cmd, cwd=self.console_dir, input=text,
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, "Could not apply the settings (%s)." % e
        return p.returncode, (p.stdout or "") + (p.stderr or "")


def make_handler(root, key, runner):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ITOpsConsole"

        def log_message(self, fmt, *a):
            pass        # the console window is for the person, not for a log

        # ---- helpers ---- #
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            # Nothing here should ever be reachable from a page on the internet.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, code, obj):
            self._send(code, json.dumps(obj))

        def _authorised(self):
            """The key rides in a header the page sets from the copy this
            server put in it. Never in the address - an address is typed,
            bookmarked, and kept in history, and this key should not be.
            Anything without that header is a stranger."""
            return self.headers.get("X-Console-Key") == key

        def _safe_path(self, url_path):
            """A path under the site folder, or None. Anything that climbs out
            - .., a symlink, an absolute path - is not a file we will serve."""
            rel = urlparse(url_path).path.lstrip("/")
            if rel in ("", "index.htm"):
                rel = "index.html"
            full = os.path.realpath(os.path.join(root, rel))
            base = os.path.realpath(root)
            if full != base and not full.startswith(base + os.sep):
                return None
            return full

        # ---- GET: the console's own files ---- #
        def do_GET(self):
            # Asked by a second copy of the launcher, before it decides whether
            # to start a server or just open the one you already have. It says
            # nothing a program on this computer could not already tell by
            # connecting, so it needs no key.
            if urlparse(self.path).path == "/api/ping":
                self._json(200, {"itops": True})
                return
            path = self._safe_path(self.path)
            if not path or not os.path.isfile(path):
                self._send(404, "Not found.", "text/plain; charset=utf-8")
                return
            ext = os.path.splitext(path)[1].lower()
            ctype = TEXT_TYPES.get(ext, "application/octet-stream")
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                self._send(404, "Not found.", "text/plain; charset=utf-8")
                return
            # The page needs the key to press its own buttons. It is only ever
            # given to a page served from here, which is only ever this console.
            if ext == ".html":
                raw = raw.replace(b"</head>",
                                  b'<script>window.CONSOLE_KEY=' +
                                  json.dumps(key).encode("ascii") +
                                  b";</script></head>", 1)
            self._send(200, raw, ctype)

        # ---- POST: the two things a button may ask for ---- #
        def do_POST(self):
            if not self._authorised():
                self._json(403, {"ok": False, "message": "This page is not allowed to do that."})
                return
            route = urlparse(self.path).path
            if route == "/api/refresh":
                ok, message = runner.start_refresh()
                self._json(200 if ok else 409, {"ok": ok, "message": message})
                return
            if route == "/api/apply":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > MAX_BODY:
                    self._json(400, {"ok": False, "message": "That is not a settings block."})
                    return
                text = self.rfile.read(n).decode("utf-8", "replace")
                code, out = runner.apply_settings(text)
                self._json(200, {"ok": code == 0, "code": code, "message": out.strip()})
                return
            self._json(404, {"ok": False, "message": "No such thing."})

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default=None, help="the built console folder to serve")
    ap.add_argument("--tool-root", default=None)
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--python", default=None)
    ap.add_argument("--powershell", default=None, help="PowerShell to run the refresh with")
    ap.add_argument("--port", type=int, default=0, help="0 = pick a free one")
    ap.add_argument("--run-deadline-minutes", type=int, default=RUN_DEADLINE_MINUTES,
                    help="stop a refresh that has not finished by then (minimum 5)")
    ap.add_argument("--open", action="store_true", help="open a browser at it")
    ap.add_argument("--print-url", action="store_true", help="print the address and keep serving")
    args = ap.parse_args(argv)

    site = os.path.abspath(args.site or os.path.join(os.path.dirname(HERE), "..", "console-site"))
    if not os.path.isdir(site):
        print("There is no console to serve at %s." % site)
        print('Run "Refresh IT Ops Data" once first - that is what builds it.')
        return 2

    tool_root = os.path.abspath(args.tool_root or os.path.dirname(HERE))
    output_root = os.path.abspath(args.output_root or os.path.join(os.path.dirname(tool_root), "output"))
    stamp = stamp_path(output_root)
    running = already_serving(stamp)
    if running:
        print("")
        print("Your console is already running at %s" % running)
        print("Opening that one rather than starting a second.")
        if args.open:
            try:
                webbrowser.open(running)
            except Exception:
                print("(could not open your browser - copy the address above)")
        return 0

    key = secrets.token_urlsafe(24)
    runner = Runner(HERE, site, tool_root, output_root, args.python, args.powershell)
    runner.deadline_seconds = max(5, args.run_deadline_minutes) * 60

    # 127.0.0.1, never 0.0.0.0: this is for the person sitting here.
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(site, key, runner))
    port = httpd.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    if stamp:
        try:
            with open(stamp, "w", encoding="utf-8") as fh:
                json.dump({"port": port, "pid": os.getpid()}, fh)
        except OSError:
            stamp = None

    print("")
    print("=== IT Ops Console ====================================================")
    print("")
    print("  Your console is open at:")
    print("    %s" % url)
    print("")
    print("  This window is what serves it. Leave it open while you use the")
    print("  console; close it when you are done. Nothing else on your network")
    print("  can reach it.")
    print("")
    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            print("  (could not open your browser - copy the address above)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("")
        print("Console closed.")
    finally:
        if stamp:
            try:
                os.remove(stamp)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
