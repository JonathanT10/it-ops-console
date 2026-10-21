"""Network health: ask each switch and access point how it is, over SNMPv3.

    python network/collector.py --config network.ini --db C:\\IT-Ops\\output\\network.db
                                --secret C:\\IT-Ops\\output\\network-credential.bin

For every device listed in network.ini it does one GET (sysUpTime, sysName) and
walks four columns of the interface table (ifDescr, ifOperStatus, ifInErrors,
ifOutErrors), using SNMPv3 with SHA authentication and AES-128 privacy - the
only combination the UniFi gear here is set up for. Each poll lands in a small
SQLite database that the console opens read-only.

The credential is the one secret this suite stores. set-network-credential.ps1
writes it, encrypted by Windows (DPAPI, machine scope), into the output folder
that setup locks to the installer, Administrators and SYSTEM. This program only
ever DECRYPTS it, holds it in memory for the run, and never prints, logs,
stores or puts it in an error message.

Exit codes - each one says in plain words what happened:
    0  every device was asked (some may be unreachable - that is a finding)
    1  something went wrong that is not one of the below; the text says what
    3  the SNMP library is missing        - nothing contacted, nothing written
    4  the credential file is missing or cannot be read, or a site has no
       credential in it                   - nothing contacted, nothing written
    5  a device answered but refused the credential - nothing recorded for it

Requires: pip install "pysnmp>=7.1" "cryptography>=43" (for the whole computer -
see README). pysnmp does its AES privacy through cryptography but does not
install it, so without it every device fails with "Ciphering services not
available" - that is a missing library, never a device that is down.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netdb  # noqa: E402

SNMP_MISSING_EXIT = 3
CREDENTIAL_EXIT = 4
REFUSED_EXIT = 5

# TEST-ONLY. With --test-mode, the credential JSON is read from this variable
# instead of the DPAPI file, so the suite can be tested off Windows. Without
# --test-mode it is ignored, so a stray variable can never quietly replace the
# real, encrypted credential.
TEST_SECRET_ENV = "ITOPS_NETWORK_SECRET_PLAINTEXT_FOR_TESTS"

OID_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYSNAME = "1.3.6.1.2.1.1.5.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"
OID_IF_IN_ERR = "1.3.6.1.2.1.2.2.1.14"
OID_IF_OUT_ERR = "1.3.6.1.2.1.2.2.1.20"

# What pysnmp reports when the device ANSWERED but would not accept who we
# said we were. That is not "unreachable": the switch is up, the credential is
# wrong, and marking it down would send somebody to check a cable.
REFUSED_INDICATIONS = {
    "UnknownUserName", "WrongDigest", "DecryptionError", "UnsupportedSecurityLevel",
    "UnsupportedAuthProtocol", "UnsupportedPrivProtocol", "AuthenticationFailure",
    "AuthenticationError", "UnknownSecurityName",
}


class Unreachable(Exception):
    """No usable answer came back."""


class Refused(Exception):
    """The device answered and refused the credential."""


class CipherUnavailable(ImportError):
    """pysnmp could not encrypt the request on THIS computer (its AES needs the
    cryptography package). Nothing was sent, so nothing is known about the
    device. An ImportError, so it is reported exactly like a missing library."""


class Credential:
    """A user name and password, held for one run.

    repr/str never show the password, so an accidental print or a traceback
    that formats this object cannot leak it."""
    __slots__ = ("user", "_password")

    def __init__(self, user, password):
        self.user = user
        self._password = password

    @property
    def password(self):
        return self._password

    def __repr__(self):
        return "<credential for %s>" % self.user

    __str__ = __repr__


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# SNMP (pysnmp >= 7, asyncio hlapi)
# --------------------------------------------------------------------------- #

def import_snmp():
    """THE one place pysnmp is imported, so the pre-flight and the real work ask
    exactly the same question (the printer collector learned this the hard
    way: a check that can disagree with the work is worse than no check)."""
    from pysnmp.hlapi.v3arch.asyncio import (
        SnmpEngine, UsmUserData, usmHMACSHAAuthProtocol, usmAesCfb128Protocol,
        UdpTransportTarget, ContextData, ObjectType, ObjectIdentity, get_cmd, walk_cmd,
    )
    # pysnmp's AES privacy is built on this, and pysnmp does not install it.
    # Asking here means a computer without it stops before any device is
    # contacted, instead of filing every device as unreachable.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms  # noqa: F401
    return SimpleNamespace(
        SnmpEngine=SnmpEngine, UsmUserData=UsmUserData,
        usmHMACSHAAuthProtocol=usmHMACSHAAuthProtocol,
        usmAesCfb128Protocol=usmAesCfb128Protocol,
        UdpTransportTarget=UdpTransportTarget, ContextData=ContextData,
        ObjectType=ObjectType, ObjectIdentity=ObjectIdentity,
        get_cmd=get_cmd, walk_cmd=walk_cmd)


def snmp_missing_words(error):
    return "\n".join([
        "",
        "  The network check needs two pieces of Python, pysnmp and cryptography",
        "  (pysnmp encrypts with it), and the account that ran this cannot see",
        "  one of them (%s)." % type(error).__name__,
        "",
        "  NOTHING WAS CHANGED. No switch was contacted, nothing was written",
        "  down, and nothing has been marked down - the Network page still shows",
        "  the last real reading, marked as not updated by this run.",
        "",
        "  Install it for the WHOLE COMPUTER. In a PowerShell window opened with",
        "  'Run as administrator', on this computer:",
        "",
        '      & "%s" -m pip install "pysnmp>=7.1" "cryptography>=43"' % sys.executable,
        "",
    ])


def require_snmp():
    try:
        import_snmp()
    except ImportError as e:
        print("[x] The network devices could not be checked.")
        print(snmp_missing_words(e))
        return False
    return True


def _indication(err_ind):
    name = type(err_ind).__name__
    if name == "EncryptionError":
        # pysnmp could not encrypt the request here - nothing left this computer.
        return CipherUnavailable("pysnmp could not encrypt (%s)" % err_ind)
    if name in REFUSED_INDICATIONS:
        return Refused(name)
    # errind messages are fixed library text ("No SNMP response received
    # before timeout") - they never carry the credential.
    return Unreachable(str(err_ind))


async def snmp_poll(host, port, cred, timeout, retries, context=""):
    """Return {'uptime_s', 'sysname', 'ifaces': [...]}. Raises Unreachable,
    Refused, or ImportError."""
    snmp = import_snmp()
    ObjectType, ObjectIdentity = snmp.ObjectType, snmp.ObjectIdentity
    engine = snmp.SnmpEngine()
    try:
        # UniFi uses one password for both authentication and privacy.
        auth = snmp.UsmUserData(cred.user, cred.password, cred.password,
                                authProtocol=snmp.usmHMACSHAAuthProtocol,
                                privProtocol=snmp.usmAesCfb128Protocol)
        transport = await snmp.UdpTransportTarget.create((host, port), timeout=timeout,
                                                         retries=retries)
        ctx = snmp.ContextData(contextName=context)
        err_ind, err_stat, _, var_binds = await snmp.get_cmd(
            engine, auth, transport, ctx,
            ObjectType(ObjectIdentity(OID_UPTIME)), ObjectType(ObjectIdentity(OID_SYSNAME)))
        if err_ind:
            raise _indication(err_ind)
        if err_stat:
            raise Unreachable(err_stat.prettyPrint())
        uptime_s = sysname = None
        for oid, val in var_binds:
            if val is None or val.__class__.__name__ in ("NoSuchObject", "NoSuchInstance",
                                                         "EndOfMibView"):
                continue
            if str(oid) == OID_UPTIME:
                uptime_s = int(val) // 100
            elif str(oid) == OID_SYSNAME:
                sysname = str(val).strip("\x00 ") or None

        async def walk(base):
            out = {}
            async for w_ind, w_stat, _, binds in snmp.walk_cmd(
                    engine, auth, transport, ctx, ObjectType(ObjectIdentity(base))):
                if w_ind:
                    raise _indication(w_ind)
                if w_stat:
                    break
                for oid, val in binds:
                    s = str(oid)
                    if not s.startswith(base + "."):
                        return out
                    out[int(s[len(base) + 1:])] = val
            return out

        descr = await walk(OID_IF_DESCR)
        oper = await walk(OID_IF_OPER)
        in_err = await walk(OID_IF_IN_ERR)
        out_err = await walk(OID_IF_OUT_ERR)
        ifaces = []
        for idx in sorted(set(descr) | set(oper)):
            ifaces.append({
                "if_index": idx,
                "descr": str(descr[idx]).strip("\x00 ") if idx in descr else None,
                "oper_status": int(oper[idx]) if idx in oper else None,
                "in_errors": int(in_err[idx]) if idx in in_err else None,
                "out_errors": int(out_err[idx]) if idx in out_err else None,
            })
        return {"uptime_s": uptime_s, "sysname": sysname, "ifaces": ifaces}
    finally:
        engine.close_dispatcher()


async def poll_one(device, cred, cfg):
    """One device's whole exchange, with a hard wall-clock ceiling, so one
    switch that answers some requests and stalls on others cannot hold up the
    run."""
    host, _, port = device["ip"].partition(":")
    return await asyncio.wait_for(
        snmp_poll(host.strip(), int(port) if port else 161, cred, cfg["timeout"],
                  cfg["retries"], cfg["context"]),
        timeout=cfg["device_timeout"])


async def poll_many(devices, creds, cfg):
    """Every device, a few at a time, results in the order given."""
    gate = asyncio.Semaphore(max(1, int(cfg["poll_at_once"])))

    async def one(device):
        async with gate:
            try:
                return device, await poll_one(device, creds[device["credential"]], cfg), None
            except Exception as e:  # noqa: BLE001 - sorted by kind in main()
                return device, None, e

    return await asyncio.gather(*(one(d) for d in devices))


# --------------------------------------------------------------------------- #
# network.ini
# --------------------------------------------------------------------------- #

def read_config(path):
    """-> (settings, devices, uplinks). devices: [{name, ip, site, credential}]."""
    # "=" only: an address may carry ":port", and section names use "site:".
    cp = configparser.ConfigParser(delimiters=("=",), inline_comment_prefixes=(";", "#"))
    cp.optionxform = str
    if not cp.read(path, encoding="utf-8-sig"):
        raise FileNotFoundError(path)
    s = cp["snmp"] if cp.has_section("snmp") else {}
    cfg = {
        "timeout": float(s.get("timeout", 2)),
        "retries": int(s.get("retries", 1)),
        "device_timeout": float(s.get("device_timeout", 20)),
        "poll_at_once": int(s.get("poll_at_once", 8)),
        "context": str(s.get("context", "")).strip(),
    }
    devices = []
    for sec in cp.sections():
        if not sec.lower().startswith("site:"):
            continue
        site = sec.split(":", 1)[1].strip()
        # Lower case on both sides: set-network-credential.ps1 saves names that way.
        cred = (cp.get(sec, "credential", fallback="").strip() or site).lower()
        for key, value in cp.items(sec):
            if key == "credential" or not str(value).strip():
                continue
            devices.append({"name": key.strip(), "ip": str(value).strip(), "site": site,
                            "credential": cred})
    uplinks = {}
    if cp.has_section("uplinks"):
        for ip, ports in cp.items("uplinks"):
            nums = set()
            for p in str(ports).replace(";", ",").split(","):
                p = p.strip()
                if p.isdigit():
                    nums.add(int(p))
            uplinks[ip.strip()] = nums
    return cfg, devices, uplinks


# --------------------------------------------------------------------------- #
# The credential
# --------------------------------------------------------------------------- #

def _dpapi_unprotect(blob):
    """Windows DPAPI CryptUnprotectData. Works for the machine-scope blobs
    set-network-credential.ps1 writes. Raises OSError on failure."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    src = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(dst))
    if not ok:
        raise OSError("CryptUnprotectData failed (Windows error %d)" % ctypes.GetLastError())
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


class CredentialProblem(Exception):
    """Plain words for a person. Never contains the secret."""


def load_credentials(secret_path, test_mode):
    """-> {credential name: Credential}. Raises CredentialProblem."""
    if test_mode and os.environ.get(TEST_SECRET_ENV):
        raw = os.environ[TEST_SECRET_ENV].encode("utf-8")
    else:
        if not secret_path or not os.path.exists(secret_path):
            raise CredentialProblem(
                "There is no saved network credential on this computer (looked for %s)."
                % (secret_path or "network-credential.bin"))
        if os.name != "nt":
            raise CredentialProblem(
                "The network credential is encrypted by Windows and can only be read on "
                "the Windows computer that saved it.")
        try:
            with open(secret_path, "rb") as f:
                raw = _dpapi_unprotect(f.read())
        except OSError:
            raise CredentialProblem(
                "The saved network credential could not be decrypted. It only opens on "
                "the computer that saved it - it may have been copied from another "
                "machine, or damaged.") from None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # Deliberately no detail: a parse message can quote the text it choked on.
        raise CredentialProblem("The saved network credential is not in the expected form.") from None
    finally:
        raw = None
    if not isinstance(doc, dict):
        raise CredentialProblem("The saved network credential is not in the expected form.")
    creds = {}
    for name, entry in doc.items():
        if (isinstance(entry, dict) and str(entry.get("user") or "").strip()
                and str(entry.get("password") or "")):
            creds[str(name).strip().lower()] = Credential(str(entry["user"]).strip(), str(entry["password"]))
    return creds


def credential_words(problem):
    return "\n".join([
        "",
        "[x] The network devices could not be checked.",
        "",
        "  %s" % problem,
        "",
        "  NOTHING WAS CHANGED. No switch was contacted, nothing was written",
        "  down, and nothing has been marked down.",
        "",
        "  Fix: on this computer, in a PowerShell window opened with 'Run as",
        "  administrator', run set-network-credential.ps1 from the console folder",
        "  and enter the SNMPv3 user and password from the LastPass item for that",
        "  site. It saves them encrypted; nothing is typed into any file.",
        "",
    ])


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="Network health over SNMPv3.")
    ap.add_argument("--config", default="network.ini")
    ap.add_argument("--db", default="network.db")
    ap.add_argument("--secret", default="",
                    help="network-credential.bin written by set-network-credential.ps1")
    ap.add_argument("--test-mode", action="store_true",
                    help="TEST ONLY: read the credential JSON from %s" % TEST_SECRET_ENV)
    args = ap.parse_args(argv)

    try:
        cfg, devices, uplinks = read_config(args.config)
    except FileNotFoundError:
        print("[x] Config not found: %s (copy network.example.ini to network.ini)" % args.config)
        return 1
    except (configparser.Error, ValueError) as e:
        print("[x] network.ini could not be read: %s" % e)
        return 1
    if not devices:
        print("Nothing to do: network.ini lists no devices under a [site:...] section.")
        return 0

    # The library first, then the credential - both BEFORE the database is
    # opened, so a run that cannot check anything leaves no trace that looks
    # like a reading.
    if not require_snmp():
        return SNMP_MISSING_EXIT
    if os.environ.get(TEST_SECRET_ENV) and not args.test_mode:
        print("[!] %s is set but ignored - it is honoured only with --test-mode." % TEST_SECRET_ENV)
    try:
        creds = load_credentials(args.secret, args.test_mode)
        missing = sorted({d["credential"] for d in devices} - set(creds))
        if missing:
            raise CredentialProblem(
                "The saved network credential has nothing for site(s): %s."
                % ", ".join(missing))
    except CredentialProblem as p:
        print(credential_words(p))
        return CREDENTIAL_EXIT

    ts = utcnow_iso()
    conn = netdb.connect(args.db)
    up = down = refused = broke = 0
    library_gone = False
    try:
        results = asyncio.run(poll_many(devices, creds, cfg))
        for device, got, error in results:
            label = "%s (%s)" % (device["name"], device["ip"])
            if error is None:
                ports = uplinks.get(device["ip"], set())
                for i in got["ifaces"]:
                    i["uplink"] = i["if_index"] in ports
                did = netdb.upsert_device(conn, device["ip"], device["name"], device["site"], ts, True)
                netdb.insert_snapshot(conn, did, ts, True, got["uptime_s"], None, got["ifaces"])
                down_ports = [i for i in got["ifaces"] if i["uplink"] and i["oper_status"] != 1]
                print("[+] %s: up, %d interfaces%s" % (
                    label, len(got["ifaces"]),
                    ", UPLINK DOWN: %s" % ", ".join(str(i["if_index"]) for i in down_ports)
                    if down_ports else ""))
                up += 1
            elif isinstance(error, ImportError):
                print("[x] %s: could not be checked - the SNMP library failed on this "
                      "computer (%s). Nothing recorded; NOT marked down." % (label, type(error).__name__))
                library_gone = True
                broke += 1
            elif isinstance(error, Refused):
                print("[x] %s: answered but refused the %s credential (%s). Nothing recorded; "
                      "NOT marked down." % (label, device["credential"], error))
                refused += 1
            elif isinstance(error, (Unreachable, asyncio.TimeoutError, TimeoutError, OSError)):
                if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
                    detail = "No SNMP response (gave up after %gs)" % cfg["device_timeout"]
                elif isinstance(error, Unreachable):
                    said = str(error)
                    detail = ("No SNMP response (timed out)" if "before timeout" in said
                              else "No SNMP response (%s)" % said)
                else:
                    detail = "No SNMP response (%s)" % type(error).__name__
                did = netdb.upsert_device(conn, device["ip"], device["name"], device["site"], ts, False)
                netdb.insert_snapshot(conn, did, ts, False, None, detail)
                print("[!] %s: unreachable - %s" % (label, detail))
                down += 1
            else:
                # Not a network answer at all - a fault on THIS side. Saying the
                # switch is down would be a guess, so say nothing about it.
                print("[x] %s: could not be checked (%s). Nothing recorded; NOT marked down."
                      % (label, type(error).__name__))
                broke += 1
            conn.commit()
    finally:
        conn.commit()
        conn.close()
        creds = None

    print("Done: %d answered, %d unreachable, %d refused the credential, %d not checked."
          % (up, down, refused, broke))
    if library_gone:
        print(snmp_missing_words(next(e for _, _, e in results if isinstance(e, ImportError))))
        return SNMP_MISSING_EXIT
    if refused:
        print("  A device that refuses the credential was NOT marked down. Re-enter that "
              "site's credential with set-network-credential.ps1 (Run as administrator).")
        return REFUSED_EXIT
    if broke:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
