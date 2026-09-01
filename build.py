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
import os
import shutil
import sys
from datetime import datetime, timezone

from console import model, pages
from console.render import write_page
from console.sources import load_all


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
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    config = os.path.join(here, "sample", "sources.ini") if args.sample else args.config

    cfg, feeds = load_all(config)

    models = {}
    models["identity"] = model.identity_model(feeds["tenant"], feeds.get("run_summary"))
    models["security"] = model.security_model(feeds["security"], models["identity"])
    models["licensing"] = model.licensing_model(feeds["licensing"])
    models["fleet"] = model.fleet_model(feeds["fleet"])
    models["changes"] = model.changes_model(feeds["history"], feeds.get("run_summary"))

    available = {
        "index": True,
        "identity": models["identity"] is not None,
        "security": models["security"] is not None,
        "licensing": models["licensing"] is not None,
        "fleet": models["fleet"] is not None,
        "changes": models["changes"] is not None,
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
                                              available, generated)),
        write_page(args.out, "licensing",
                         pages.build_licensing(models["licensing"], feeds["licensing"],
                                               available, generated)),
        write_page(args.out, "fleet",
                         pages.build_fleet(models["fleet"], feeds["fleet"],
                                           available, generated)),
        write_page(args.out, "changes",
                         pages.build_changes(models["changes"], feeds["history"],
                                             available, generated)),
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
