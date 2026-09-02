"""Put the alert settings you made in the console into alerts.ini.

    python apply-alerts.py --settings alert-settings.txt
    python apply-alerts.py < alert-settings.txt
    python apply-alerts.py --settings ... --dry-run     # say what would change

The Alerts page in the console produces a small block of settings - the rules
and the schedule, never where alerts go. This merges that block into your
alerts.ini: your comments, your [teams] and [email] settings, and anything this
console does not recognise are all left exactly as they were, and only the
lines you changed move. The old file is kept as alerts.ini.bak.

Nothing is written unless the result reads back cleanly, so a mangled paste
leaves your settings alone. Normally you do not run this by hand: double-click
"Apply Alert Settings" on your desktop and it does this with whatever you just
copied.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console import alerts as A  # noqa: E402


def read_text(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", default=None,
                    help="the settings block from the console (default: read it from the pipe)")
    ap.add_argument("--config", default=None,
                    help="the alerts.ini to change (default: the one beside this script)")
    ap.add_argument("--dry-run", action="store_true", help="say what would change; change nothing")
    args = ap.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    target = args.config or os.path.join(here, "alerts.ini")

    if args.settings:
        if not os.path.exists(args.settings):
            print("Could not find %s." % args.settings)
            return 2
        text = read_text(args.settings)
    else:
        text = sys.stdin.read()

    try:
        settings, notes = A.parse_settings_fragment(text)
    except ValueError:
        print("That does not look like alert settings.")
        print('Open the console, go to the Alerts tab, change what you want and click "Save settings" -')
        print("then run this again (or double-click \"Apply Alert Settings\" on your desktop).")
        return 2

    if not os.path.exists(target):
        example = os.path.join(here, "alerts.example.ini")
        if os.path.exists(example):
            shutil.copyfile(example, target)
            print("There was no alerts.ini yet, so one was started from the example.")
        else:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("")
    before = read_text(target)
    was = A.load_config(target)
    if was["unreadable"]:
        print("Nothing was changed - %s cannot be read as it stands:" % target)
        for p in was["problems"]:
            print("  " + p)
        print("Open it in Notepad, fix that line, and try again.")
        return 1

    merged, changes = A.merge_into_ini(before, settings)

    # Never write something the console cannot read back. Only NEW trouble
    # counts: a file that already had a line this version does not recognise
    # (an older setting, a typo someone left) must still be editable.
    tmp = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False, encoding="utf-8")
    try:
        tmp.write(merged)
        tmp.close()
        checked = A.load_config(tmp.name)
    finally:
        os.unlink(tmp.name)
    introduced = [p for p in checked["problems"] if p not in was["problems"]]
    if checked["unreadable"] or introduced:
        print("Nothing was changed - the result would not read cleanly:")
        for p in (introduced or checked["problems"]):
            print("  " + p)
        print("Your alerts.ini is exactly as it was.")
        return 1
    for p in was["problems"]:
        print("Note: %s" % p)

    for n in notes:
        print(n)
    if not changes:
        print("Nothing to change - alerts.ini already says all of that.")
        return 0

    lines = A.describe_changes(changes)
    if args.dry_run:
        print("Would change %d setting%s in %s:" % (len(changes), "" if len(changes) == 1 else "s", target))
        for l in lines:
            print("  " + l)
        return 0

    backup = target + ".bak"
    try:
        shutil.copyfile(target, backup)
    except OSError as e:
        print("Could not make a backup (%s) - nothing was changed." % e)
        return 1
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(merged)

    print("Changed %d setting%s:" % (len(changes), "" if len(changes) == 1 else "s"))
    for l in lines:
        print("  " + l)
    print("")
    print("Saved to %s (the version before this is %s)." % (target, os.path.basename(backup)))
    print('Double-click "Refresh IT Ops Data" when you want the console to use them.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
