"""Test suite: python tests/test_network.py

The network-health collector (network/collector.py). What is easy to get
quietly wrong, and what each group pins down:

  * pysnmp is imported in exactly one place, so the pre-flight and the poll
    can never disagree about whether the library works.
  * A run that can check NOTHING (library missing: exit 3; credential missing,
    unreadable, or without a site: exit 4) writes nothing - no database, no
    row, nothing marked down.
  * "Did not answer" (recorded, reachable = 0) is kept apart from "answered
    and refused the credential" and "could not be checked" (both recorded as
    NOTHING, because neither says anything about the switch).
  * One device that stalls cannot hold the run past its deadline, and only
    poll_at_once devices are asked at the same time.
  * The password appears nowhere: not on screen, not in the database, not in
    an error.
  * A real SNMPv3 SHA/AES-128 poll against a local simulator (snmpsim), when
    snmpsim is installed. Without it those checks print SKIP UNVERIFIED-live
    and the suite says so - they never count as a pass.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE_DIR)
NET_DIR = os.path.join(ROOT_DIR, "network")
COLLECTOR = os.path.join(NET_DIR, "collector.py")
sys.path.insert(0, NET_DIR)

import collector  # noqa: E402

FAILS = []
SKIPS = []

# A test credential for the local simulator only - not a real one anywhere.
PASSWORD = "Sim-Only-Pa55-7319"
USER = "simuser"
ENV = collector.TEST_SECRET_ENV


def check(label, cond):
    print("%s %s" % ("PASS" if cond else "FAIL", label))
    if not cond:
        FAILS.append(label)


def skip(label, why):
    print("SKIP UNVERIFIED-live %s (%s)" % (label, why))
    SKIPS.append(label)


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def ini(devices, device_timeout=5, poll_at_once=8, context="", uplinks="", timeout=1, retries=0):
    lines = ["[snmp]", "timeout = %s" % timeout, "retries = %s" % retries,
             "device_timeout = %s" % device_timeout,
             "poll_at_once = %s" % poll_at_once, "context = %s" % context, "",
             "[site:dfw]", "credential = dfw"]
    lines += ["%s = %s" % (n, a) for n, a in devices]
    if uplinks:
        lines += ["", "[uplinks]", uplinks]
    return "\n".join(lines) + "\n"


def run(args, env_extra=None, pythonpath=None, drop_env=True):
    env = dict(os.environ)
    if drop_env:
        env.pop(ENV, None)
    env.update(env_extra or {})
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    p = subprocess.run([sys.executable, COLLECTOR] + args, capture_output=True, text=True,
                       env=env, timeout=120)
    return p.returncode, p.stdout + p.stderr


def rows(db):
    conn = sqlite3.connect(db)
    try:
        return {t: conn.execute("SELECT * FROM %s" % t).fetchall()
                for t in ("devices", "snapshots", "ifaces")}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 1. One import site
# --------------------------------------------------------------------------- #

def test_single_import_site():
    with open(COLLECTOR, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    sites = []
    for fn in ast.walk(tree):
        for node in ast.walk(fn) if isinstance(fn, ast.FunctionDef) else []:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pysnmp"):
                sites.append(fn.name)
            if isinstance(node, ast.Import) and any(a.name.startswith("pysnmp") for a in node.names):
                sites.append(fn.name)
    every = [n for n in ast.walk(tree)
             if (isinstance(n, ast.ImportFrom) and (n.module or "").startswith("pysnmp"))
             or (isinstance(n, ast.Import) and any(a.name.startswith("pysnmp") for a in n.names))]
    check("import: pysnmp is imported exactly once in the collector", len(every) == 1)
    check("import: and that once is inside import_snmp()", sites == ["import_snmp"])
    names = {a.name for a in every[0].names} if every else set()
    check("import: the one import brings the v3 pieces (user, SHA, AES-128)",
          {"UsmUserData", "usmHMACSHAAuthProtocol", "usmAesCfb128Protocol"} <= names)
    with open(os.path.join(NET_DIR, "netdb.py"), encoding="utf-8") as fh:
        check("import: the database module never touches pysnmp", "pysnmp" not in fh.read())


# --------------------------------------------------------------------------- #
# 2. Nothing can be checked -> nothing is written
# --------------------------------------------------------------------------- #

def test_exit_3_writes_nothing(tmp):
    d = os.path.join(tmp, "e3")
    fake = os.path.join(d, "fake")
    os.makedirs(os.path.join(fake, "pysnmp"))
    write(os.path.join(fake, "pysnmp", "__init__.py"), "raise ImportError('no pysnmp here')\n")
    cfg, db = os.path.join(d, "network.ini"), os.path.join(d, "network.db")
    write(cfg, ini([("Core", "192.0.2.2")]))
    rc, out = run(["--config", cfg, "--db", db, "--test-mode"],
                  env_extra={ENV: json.dumps({"dfw": {"user": USER, "password": PASSWORD}})},
                  pythonpath=fake)
    check("exit 3: a missing SNMP library exits 3", rc == 3)
    check("exit 3: no database is created", not os.path.exists(db))
    check("exit 3: it says nothing was changed or marked down",
          "NOTHING WAS CHANGED" in out and "nothing has been marked down" in out)
    check("exit 3: and never calls a switch unreachable", "unreachable" not in out.lower())
    check("exit 3: the library is asked BEFORE the credential (no credential words)",
          "credential" not in out.lower())


def test_exit_3_without_cryptography(tmp):
    """20 Oct 2026, first live run: pysnmp was installed, cryptography was not.
    pysnmp does its AES through cryptography but does not install it, so every
    poll failed with 'Ciphering services not available' and every switch was
    filed as unreachable. That is a missing library, and must read as one."""
    d = os.path.join(tmp, "e3c")
    fake = os.path.join(d, "fake")
    os.makedirs(os.path.join(fake, "cryptography"))
    write(os.path.join(fake, "cryptography", "__init__.py"),
          "raise ImportError('no cryptography here')\n")
    cfg, db = os.path.join(d, "network.ini"), os.path.join(d, "network.db")
    write(cfg, ini([("Core", "192.0.2.2")]))
    rc, out = run(["--config", cfg, "--db", db, "--test-mode"],
                  env_extra={ENV: json.dumps({"dfw": {"user": USER, "password": PASSWORD}})},
                  pythonpath=fake)
    check("cryptography: missing it exits 3, like a missing SNMP library", rc == 3)
    check("cryptography: ... with no database created", not os.path.exists(db))
    check("cryptography: ... never calling a switch unreachable", "unreachable" not in out.lower())
    check("cryptography: ... and the fix names it", '"cryptography>=43"' in out)


def test_exit_4_writes_nothing(tmp):
    d = os.path.join(tmp, "e4")
    os.makedirs(d)
    cfg, db = os.path.join(d, "network.ini"), os.path.join(d, "network.db")
    write(cfg, ini([("Core", "192.0.2.2")]))
    secret = os.path.join(d, "network-credential.bin")

    rc, out = run(["--config", cfg, "--db", db, "--secret", secret])
    check("exit 4: no credential file exits 4", rc == 4)
    check("exit 4: and writes no database", not os.path.exists(db))
    check("exit 4: it says how to fix it, on one line, from LastPass",
          "set-network-credential.ps1" in out and "LastPass" in out)
    check("exit 4: and that nothing was marked down", "nothing has been marked down" in out)

    rc, out = run(["--config", cfg, "--db", db, "--secret", secret],
                  env_extra={ENV: json.dumps({"dfw": {"user": USER, "password": PASSWORD}})})
    check("exit 4: the test variable is IGNORED without --test-mode", rc == 4)
    check("exit 4: ... and says it was ignored", "ignored" in out and ENV in out)
    check("exit 4: ... and still writes nothing", not os.path.exists(db))

    rc, out = run(["--config", cfg, "--db", db, "--test-mode"],
                  env_extra={ENV: json.dumps({"hou": {"user": USER, "password": PASSWORD}})})
    check("exit 4: a site with no credential exits 4", rc == 4 and "dfw" in out)
    check("exit 4: ... writing nothing", not os.path.exists(db))

    broken = '{"dfw": {"user": "%s", "password": "%s"' % (USER, PASSWORD)  # unterminated
    rc, out = run(["--config", cfg, "--db", db, "--test-mode"], env_extra={ENV: broken})
    check("exit 4: a credential that will not parse exits 4", rc == 4)
    check("exit 4: ... without quoting what it choked on", PASSWORD not in out and USER not in out)

    write(secret, "not a DPAPI blob")
    rc, out = run(["--config", cfg, "--db", db, "--secret", secret])
    check("exit 4: a credential file that cannot be opened here exits 4", rc == 4)
    check("exit 4: ... writing nothing", not os.path.exists(db))


# --------------------------------------------------------------------------- #
# 3. Unreachable vs could-not-check (in process, scripted answers)
# --------------------------------------------------------------------------- #

def _scripted(answers):
    """Replace the SNMP exchange with scripted outcomes keyed by host."""
    async def fake(host, port, cred, timeout, retries, context=""):
        what = answers[host]
        if isinstance(what, float):
            await asyncio.sleep(what)
            return {"uptime_s": 1, "sysname": host, "ifaces": []}
        if isinstance(what, BaseException):
            raise what
        return what
    return fake


def _main_with(tmp, name, devices, answers, **kw):
    d = os.path.join(tmp, name)
    os.makedirs(d)
    cfg, db = os.path.join(d, "network.ini"), os.path.join(d, "network.db")
    write(cfg, ini(devices, **kw))
    real = collector.snmp_poll
    collector.snmp_poll = _scripted(answers)
    os.environ[ENV] = json.dumps({"dfw": {"user": USER, "password": PASSWORD}})
    try:
        t = time.monotonic()
        rc = collector.main(["--config", cfg, "--db", db, "--test-mode"])
        took = time.monotonic() - t
    finally:
        collector.snmp_poll = real
        os.environ.pop(ENV, None)
    return rc, db, took


def test_unreachable_vs_not_checked(tmp):
    up = {"uptime_s": 500, "sysname": "core", "ifaces": [
        {"if_index": 1, "descr": "Port 1", "oper_status": 1, "in_errors": 0, "out_errors": 0},
        {"if_index": 49, "descr": "SFP+ 1", "oper_status": 2, "in_errors": 0, "out_errors": 0}]}
    devices = [("Up", "192.0.2.1"), ("Silent", "192.0.2.2"), ("Timeout", "192.0.2.3"),
               ("Refuses", "192.0.2.4"), ("Ours", "192.0.2.5")]
    answers = {"192.0.2.1": up,
               "192.0.2.2": collector.Unreachable("No SNMP response received before timeout"),
               "192.0.2.3": TimeoutError(),
               "192.0.2.4": collector.Refused("WrongDigest"),
               "192.0.2.5": ValueError("a fault on this side")}
    rc, db, _ = _main_with(tmp, "uvn", devices, answers, uplinks="192.0.2.1 = 49")
    r = rows(db)
    ips = {row[1]: row[0] for row in r["devices"]}
    snaps = {row[1]: row for row in r["snapshots"]}
    check("sorting: a device that answered is recorded up", snaps[ips["192.0.2.1"]][3] == 1)
    check("sorting: no answer is recorded as unreachable",
          snaps[ips["192.0.2.2"]][3] == 0 and snaps[ips["192.0.2.2"]][5] == "No SNMP response (timed out)")
    check("sorting: a deadline hit is recorded as unreachable too", snaps[ips["192.0.2.3"]][3] == 0)
    check("sorting: answered-but-refused is NOT recorded (not marked down)", "192.0.2.4" not in ips)
    check("sorting: a fault on this side is NOT recorded (not marked down)", "192.0.2.5" not in ips)
    check("sorting: a refusal makes the run exit 5", rc == collector.REFUSED_EXIT)
    upl = [row for row in r["ifaces"] if row[1] == 49]
    check("sorting: the configured uplink is flagged in the database",
          len(upl) == 1 and upl[0][6] == 1 and [row[6] for row in r["ifaces"] if row[1] == 1] == [0])

    rc, db, _ = _main_with(tmp, "fault", [("Ours", "192.0.2.5")],
                           {"192.0.2.5": ValueError("x")})
    check("sorting: only a local fault -> exit 1, nothing recorded",
          rc == 1 and rows(db)["devices"] == [])
    rc, db, _ = _main_with(tmp, "lib", [("A", "192.0.2.1"), ("B", "192.0.2.6")],
                           {"192.0.2.1": up, "192.0.2.6": ImportError("gone mid-run")})
    check("sorting: the library failing part-way -> exit 3, that device not recorded",
          rc == 3 and [row[1] for row in rows(db)["devices"]] == ["192.0.2.1"])

    class EncryptionError(Exception):
        """Stands in for pysnmp's errind.EncryptionError (matched by name)."""
    gone = collector._indication(EncryptionError("Ciphering services not available"))
    check("sorting: pysnmp unable to encrypt is a library fault, not a down device",
          isinstance(gone, ImportError) and not isinstance(gone, collector.Unreachable))
    rc, db, _ = _main_with(tmp, "cipher", [("A", "192.0.2.1"), ("B", "192.0.2.7")],
                           {"192.0.2.1": up, "192.0.2.7": gone})
    check("sorting: ... so that device exits 3 and is not recorded (not marked down)",
          rc == 3 and [row[1] for row in rows(db)["devices"]] == ["192.0.2.1"])


def test_deadline_and_pool(tmp):
    rc, db, took = _main_with(tmp, "deadline", [("Stuck", "192.0.2.9")], {"192.0.2.9": 30.0},
                              device_timeout=0.5)
    snap = rows(db)["snapshots"]
    check("deadline: a device that stalls is given up on at its deadline", took < 5)
    check("deadline: and recorded as unreachable, saying why",
          len(snap) == 1 and snap[0][3] == 0 and "gave up after 0.5s" in snap[0][5])
    check("deadline: a stalled device is a finding, not a failure", rc == 0)

    live = {"now": 0, "most": 0}
    real = collector.snmp_poll

    async def counted(host, port, cred, timeout, retries, context=""):
        live["now"] += 1
        live["most"] = max(live["most"], live["now"])
        await asyncio.sleep(0.2)
        live["now"] -= 1
        return {"uptime_s": 1, "sysname": host, "ifaces": []}

    cfg = {"timeout": 1, "retries": 0, "device_timeout": 5, "poll_at_once": 2, "context": ""}
    devs = [{"name": str(i), "ip": "192.0.2.%d" % i, "site": "dfw", "credential": "dfw"}
            for i in range(1, 7)]
    collector.snmp_poll = counted
    try:
        out = asyncio.run(collector.poll_many(devs, {"dfw": collector.Credential(USER, PASSWORD)}, cfg))
    finally:
        collector.snmp_poll = real
    check("pool: never more than poll_at_once devices at the same time", live["most"] == 2)
    check("pool: every device answered, in the order given",
          [o[0]["ip"] for o in out] == [d["ip"] for d in devs] and all(o[2] is None for o in out))


# --------------------------------------------------------------------------- #
# 4. The password appears nowhere
# --------------------------------------------------------------------------- #

def test_password_hygiene():
    c = collector.Credential(USER, PASSWORD)
    check("secret: the credential object never shows its password",
          PASSWORD not in repr(c) and PASSWORD not in str(c) and PASSWORD not in "%s %r" % (c, c))
    try:
        raise RuntimeError("failed for %r" % c)
    except RuntimeError as e:
        check("secret: an error that formats it does not leak it", PASSWORD not in str(e))
    with open(COLLECTOR, encoding="utf-8") as fh:
        src = fh.read()
    check("secret: the collector never prints an exception's own text for an unknown failure",
          'type(error).__name__' in src and "% (label, error)" not in src)


# --------------------------------------------------------------------------- #
# 5. A real SNMPv3 SHA / AES-128 poll against snmpsim
# --------------------------------------------------------------------------- #

SNMPREC = """1.3.6.1.2.1.1.3.0|67|8640000
1.3.6.1.2.1.1.5.0|4|sim-switch-01
1.3.6.1.2.1.2.2.1.2.1|4|Port 1
1.3.6.1.2.1.2.2.1.2.2|4|Port 2
1.3.6.1.2.1.2.2.1.2.49|4|SFP+ 1
1.3.6.1.2.1.2.2.1.8.1|2|1
1.3.6.1.2.1.2.2.1.8.2|2|2
1.3.6.1.2.1.2.2.1.8.49|2|2
1.3.6.1.2.1.2.2.1.14.1|65|4
1.3.6.1.2.1.2.2.1.14.2|65|0
1.3.6.1.2.1.2.2.1.14.49|65|17
1.3.6.1.2.1.2.2.1.20.1|65|1
1.3.6.1.2.1.2.2.1.20.2|65|0
1.3.6.1.2.1.2.2.1.20.49|65|0
"""


def _free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_snmpsim():
    """(process, port, workdir) or (None, reason, None)."""
    exe = None
    for cand in ("snmpsim-command-responder",):
        for p in os.environ.get("PATH", "").split(os.pathsep):
            if os.path.exists(os.path.join(p, cand)):
                exe = os.path.join(p, cand)
    if not exe:
        return None, "snmpsim is not installed", None
    # snmpsim refuses to run as root without dropping to an unprivileged
    # user, which then needs a folder it can reach - so a world-traversable
    # temp folder, not this suite's own.
    work = tempfile.mkdtemp(prefix="itops-snmpsim-")
    os.chmod(work, 0o755)
    data, cache = os.path.join(work, "data"), os.path.join(work, "cache")
    os.makedirs(data)
    os.makedirs(cache)
    os.chmod(data, 0o755)
    os.chmod(cache, 0o777)
    write(os.path.join(data, "sim.snmprec"), SNMPREC)
    os.chmod(os.path.join(data, "sim.snmprec"), 0o644)
    port = _free_udp_port()
    args = [exe, "--data-dir=%s" % data, "--cache-dir=%s" % cache,
            "--agent-udpv4-endpoint=127.0.0.1:%d" % port, "--v3-user=%s" % USER,
            "--v3-auth-key=%s" % PASSWORD, "--v3-auth-proto=SHA",
            "--v3-priv-key=%s" % PASSWORD, "--v3-priv-proto=AES"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args += ["--process-user=nobody", "--process-group=nogroup"]
    log = open(os.path.join(work, "sim.log"), "w")
    proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        time.sleep(0.25)
        if proc.poll() is not None:
            return None, "snmpsim exited (see %s)" % os.path.join(work, "sim.log"), None
        with open(os.path.join(work, "sim.log"), encoding="utf-8", errors="replace") as fh:
            if "Listening at" in fh.read():
                time.sleep(0.5)
                return proc, port, work
    proc.kill()
    return None, "snmpsim did not start", None


def test_live_snmpv3(tmp):
    proc, port, work = _start_snmpsim()
    labels = ["live: a real SNMPv3 SHA/AES-128 poll records the switch",
              "live: wrong password is refused, not marked down",
              "live: the password appears nowhere"]
    if proc is None:
        for lab in labels:
            skip(lab, port)
        return
    try:
        d = os.path.join(tmp, "live")
        os.makedirs(d)
        cfg, db = os.path.join(d, "network.ini"), os.path.join(d, "network.db")
        dead = _free_udp_port()
        # snmpsim serves each data file under its own SNMPv3 context name;
        # real UniFi gear uses none, which is network.ini's default.
        write(cfg, ini([("Sim Switch", "127.0.0.1:%d" % port), ("Nobody", "127.0.0.1:%d" % dead)],
                       context="sim", uplinks="127.0.0.1:%d = 49" % port,
                       timeout=3, retries=2, device_timeout=30))
        good = {ENV: json.dumps({"dfw": {"user": USER, "password": PASSWORD}})}
        rc, out = run(["--config", cfg, "--db", db, "--test-mode"], env_extra=good)
        print("    collector said: " + " / ".join(out.strip().splitlines()))
        r = rows(db)
        dev = {row[1]: row for row in r["devices"]}
        sim = dev.get("127.0.0.1:%d" % port)
        snaps = {row[1]: row for row in r["snapshots"]}
        ok = (rc == 0 and sim is not None and snaps[sim[0]][3] == 1 and snaps[sim[0]][4] == 86400
              and len(r["ifaces"]) == 3)
        check(labels[0], ok)
        up49 = [row for row in r["ifaces"] if row[1] == 49]
        check("live: the interface table came back - descr, status, both error columns",
              ("SFP+ 1", 2, 17, 0, 1) == tuple(up49[0][2:7]) if up49 else False)
        check("live: the silent address next to it is recorded unreachable",
              snaps[dev["127.0.0.1:%d" % dead][0]][3] == 0)
        check("live: and the run says the uplink is down", "UPLINK DOWN: 49" in out)

        bad = {ENV: json.dumps({"dfw": {"user": USER, "password": "Wrong-Pa55-0000"}})}
        db2 = os.path.join(d, "bad.db")
        write(cfg, ini([("Sim Switch", "127.0.0.1:%d" % port)], context="sim",
                       timeout=3, retries=2, device_timeout=30))
        rc2, out2 = run(["--config", cfg, "--db", db2, "--test-mode"], env_extra=bad)
        check(labels[1], rc2 == 5 and rows(db2)["snapshots"] == [] and "NOT marked down" in out2)

        with open(db, "rb") as fh:
            blob = fh.read()
        with open(db2, "rb") as fh:
            blob += fh.read()
        check(labels[2], PASSWORD not in out and PASSWORD not in out2
              and PASSWORD.encode() not in blob and b"Wrong-Pa55" not in blob
              and "Wrong-Pa55" not in out2)
    finally:
        proc.kill()
        proc.wait()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_single_import_site()
        test_exit_3_writes_nothing(tmp)
        test_exit_3_without_cryptography(tmp)
        test_exit_4_writes_nothing(tmp)
        test_unreachable_vs_not_checked(tmp)
        test_deadline_and_pool(tmp)
        test_password_hygiene()
        test_live_snmpv3(tmp)
    print("")
    if SKIPS:
        print("NOTE: %d live check(s) skipped - UNVERIFIED-live, not passed." % len(SKIPS))
    if FAILS:
        print("RESULT: %d FAILURES" % len(FAILS))
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
