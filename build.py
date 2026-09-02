"""Build the IT operations console from whatever your tools have already written.

    python build.py --config sources.ini --out console-site

The console never talks to Microsoft Graph, SNMP, or your printers. It reads
the JSON (and one SQLite file) that entra-tenant-docs, entra-security-snapshot,
m365-license-waste-report and print-fleet-dashboard produce, and renders a
small static site: one page per domain, plus an overview that carries only
headline numbers and how fresh each feed is.

Feeds you have not configured simply do not get a page. Feeds that have gone
stale say so on every page they touch - a console that quietly shows last
month's numbers is worse than no console.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

from console import alerts as alert_rules
from console import model, pages
from console import render
from console.render import write_page
from console.sources import base_dir, load_all


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="sources.ini",
                    help="feed locations (default: sources.ini)")
    ap.add_argument("--out", default="console-site",
                    help="output folder for the generated site")
    ap.add_argument("--sample", action="store_true",
                    help="build from the bundled sample data - no real feeds needed")
    ap.add_argument("--open-path", action="store_true",
                    help="print the absolute path of the built overview page")
    ap.add_argument("--alerts-out", default=None,
                    help="where to write alerts.json (default: base_path\\alerts.json; "
                         "not written for --sample)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    config = os.path.join(here, "sample", "sources.ini") if args.sample else args.config

    # Release bundles ship a VERSION file beside this script; the footer then
    # says which suite build produced the page. A git checkout has none.
    vpath = os.path.join(here, "VERSION")
    if os.path.exists(vpath):
        with open(vpath, "r", encoding="utf-8-sig") as fh:
            render.SUITE_VERSION = fh.readline().strip()

    cfg, feeds = load_all(config)

    models = {}
    models["identity"] = model.identity_model(feeds["tenant"], feeds.get("run_summary"))
    models["security"] = model.security_model(feeds["security"], models["identity"])
    models["licensing"] = model.licensing_model(feeds["licensing"])
    models["fleet"] = model.fleet_model(feeds["fleet"])
    # Where the printer collector looks, and what each place found.
    models["discovery"] = model.discovery_model(feeds.get("fleet_discovery"))
    models["changes"] = model.changes_model(feeds["history"], feeds.get("run_summary"))
    # Posture over time: archived snapshots (run-all writes them) + the current
    # one. Optional feeds - without them the pages simply have no trend section.
    models["trends"] = model.trends_model(feeds.get("security_history"), feeds["security"],
                                          feeds.get("licensing_history"), feeds["licensing"])
    # How this machine keeps itself fresh (run-all writes refresh-status.json).
    # One footer sentence on every page; a banner on the overview only when a
    # person must act. Older installs have no such file and get neither.
    models["refresh"] = model.refresh_model(feeds.get("refresh_status"))
    render.REFRESH_NOTE = models["refresh"]["note"] if models["refresh"] else ""

    # Alerts: run the rule catalog against alerts.ini (beside sources.ini) and
    # write alerts.json for notify.py. This build never sends anything - that
    # is notify.py's job, run by run-all as its own step afterwards.
    cfg_dir = os.path.dirname(os.path.abspath(config))
    base = base_dir(cfg, config)
    acfg = alert_rules.load_config(os.path.join(cfg_dir, "alerts.ini"))
    fired = alert_rules.evaluate(acfg, models, feeds)
    channels = alert_rules.channels_configured(acfg)
    alerts_out = args.alerts_out or (None if args.sample else os.path.join(base, "alerts.json"))
    if alerts_out:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(alerts_out)), exist_ok=True)
            with open(alerts_out, "w", encoding="utf-8") as fh:
                json.dump(alert_rules.alerts_document(fired, acfg), fh, indent=2)
        except OSError as e:
            print("WARNING: could not write %s (%s) - alerts will not be sent this run." % (alerts_out, e))
    state_path = os.path.join(os.path.dirname(os.path.abspath(alerts_out)) if alerts_out else base, "alerts-state.json")
    astate = None
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8-sig") as fh:
                astate = json.load(fh)
        except (OSError, ValueError):
            astate = None
    models["alerts"] = pages.alerts_page_model(fired, acfg, astate, channels)
    # The overview's "needs a human" list is these same alerts, so turning a
    # rule off on the Alerts tab takes it off the front page too.
    models["fired"] = fired

    available = {
        "index": True,
        "identity": models["identity"] is not None,
        "security": models["security"] is not None,
        "licensing": models["licensing"] is not None,
        "fleet": models["fleet"] is not None,
        "changes": models["changes"] is not None,
        "alerts": True,
    }
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    os.makedirs(args.out, exist_ok=True)
    written = [
        write_page(args.out, "index",
                         pages.build_overview(models, feeds, available, generated)),
        write_page(args.out, "identity",
                         pages.build_identity(models["identity"], feeds["tenant"],
                                              available, generated)),
        write_page(args.out, "security",
                         pages.build_security(models["security"], feeds["security"],
                                              available, generated,
                                              trend=models["trends"]["security"])),
        write_page(args.out, "licensing",
                         pages.build_licensing(models["licensing"], feeds["licensing"],
                                               available, generated,
                                               trend=models["trends"]["licensing"])),
        write_page(args.out, "fleet",
                         pages.build_fleet(models["fleet"], feeds["fleet"],
                                           available, generated,
                                           discovery=models["discovery"])),
        write_page(args.out, "changes",
                         pages.build_changes(models["changes"], feeds["history"],
                                             available, generated)),
        write_page(args.out, "alerts",
                         pages.build_alerts(models["alerts"], available, generated)),
    ]

    print("")
    print("Feeds:")
    for key, feed in feeds.items():
        if feed.ok:
            mark = {"fresh": "  ok  ", "aging": " aging", "stale": " STALE", "unknown": "  ?   "}
            print("  %s %-12s %s" % (mark.get(feed.state, "      "), key, feed.age))
        elif feed.error:
            print("  fail   %-12s %s" % (key, feed.error))
        elif feed.missing:
            print("  ----   %-12s nothing collected yet (%s)" % (key, feed.path))
        else:
            print("  ----   %-12s not configured" % key)

    stale = [f.key for f in feeds.values() if f.ok and f.state == "stale"]
    if stale:
        print("")
        print("  WARNING: stale feed(s): %s - re-run those tools before trusting the console."
              % ", ".join(stale))

    print("")
    print("Built %d pages in %s" % (len(written), os.path.abspath(args.out)))
    if args.open_path:
        print(os.path.abspath(os.path.join(args.out, "index.html")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
