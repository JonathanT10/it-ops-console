"""The network-health database: what each switch said, and when.

Standard library only. The console opens this file read-only; only
network/collector.py writes to it.

    devices    one row per switch or access point, keyed by its address
    snapshots  one row per poll of one device - reachable or not
    ifaces     the interface table of a snapshot that got an answer

`ifaces.uplink` marks the ports network.ini lists under [uplinks], so the
console can tell "an uplink went down" from "a desk port is unplugged" without
reading the collector's config. It is the one column beyond the published
layout, and it is there for exactly that reason.

Nothing secret is ever written here: no user name, no password, no key.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY,
    ip          TEXT NOT NULL UNIQUE,
    name        TEXT,
    site        TEXT,
    first_seen  TEXT,
    last_seen   TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES devices(id),
    ts          TEXT NOT NULL,
    reachable   INTEGER NOT NULL,
    uptime_s    INTEGER,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_device_ts ON snapshots(device_id, ts);
CREATE TABLE IF NOT EXISTS ifaces (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    if_index    INTEGER NOT NULL,
    descr       TEXT,
    oper_status INTEGER,
    in_errors   INTEGER,
    out_errors  INTEGER,
    uplink      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ifaces_snapshot ON ifaces(snapshot_id);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_device(conn, ip, name, site, ts, answered):
    """The device row for this address, created on first sight.

    last_seen means 'last time it answered', so it only moves when it did -
    the same distinction the printer database draws."""
    row = conn.execute("SELECT id FROM devices WHERE ip = ?", (ip,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO devices (ip, name, site, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
            (ip, name, site, ts, ts if answered else None))
        return cur.lastrowid
    if answered:
        conn.execute("UPDATE devices SET name = ?, site = ?, last_seen = ? WHERE id = ?",
                     (name, site, ts, row["id"]))
    else:
        conn.execute("UPDATE devices SET name = ?, site = ? WHERE id = ?",
                     (name, site, row["id"]))
    return row["id"]


def insert_snapshot(conn, device_id, ts, reachable, uptime_s, detail, ifaces=()):
    cur = conn.execute(
        "INSERT INTO snapshots (device_id, ts, reachable, uptime_s, detail) VALUES (?, ?, ?, ?, ?)",
        (device_id, ts, 1 if reachable else 0, uptime_s, detail))
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO ifaces (snapshot_id, if_index, descr, oper_status, in_errors, out_errors, uplink)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(sid, i["if_index"], i.get("descr"), i.get("oper_status"), i.get("in_errors"),
          i.get("out_errors"), 1 if i.get("uplink") else 0) for i in ifaces])
    return sid
