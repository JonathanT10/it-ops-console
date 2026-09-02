"""Put the settings you made in the console into the files that use them.

    python apply-settings.py --settings settings.txt
    python apply-settings.py < settings.txt
    python apply-settings.py --settings ... --dry-run    # say what would change

Two pages in the console hand you a block of settings: the Alerts tab (which
rules count, and when a message goes out) and the Print fleet tab (where to
look for printers). A block can carry either or both, and this routes each
section to the file that reads it - alerts.ini beside this script, and the
printer collector's config.ini next door.

Both are merged, not overwritten: your comments, your [teams] and [email]
settings, your [snmp] and [devices] lines, and anything this console does not
recognise are left exactly as they were. The one exception is [ranges], which
is rewritten as a whole - that is what lets you delete a place you no longer
want scanned - though its explanatory comments are kept. Each file's previous
version is kept as <name>.bak, and nothing is written unless the result reads
back cleanly, so a mangled paste leaves everything alone.

Normally you do not run this by hand: double-click "Apply Settings" on your
desktop and it does this with whatever you just copied.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
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
    ap.add_argument("--fleet-config", default=None,
                    help="the printer collector's config.ini (default: the one in "
                         "..\\print-fleet-dashboard)")
    ap.add_argument("--dry-run", action="store_true", help="say what would change; change nothing")
    args = ap.parse_args(argv)

    if args.settings:
        if not os.path.exists(args.settings):
            print("Could not find %s." % args.settings)
            return 2
        text = read_text(args.settings)
    else:
        text = sys.stdin.read()

    try:
        alert_settings, fleet_settings, notes = A.parse_settings_fragment(text)
    except ValueError as e:
        message = str(e)
        if "does not look like settings" in message:
            print("That does not look like settings from the console.")
            print('Open the console, change what you want on the Alerts or Print fleet tab, click')
            print('"Save settings", then double-click "Apply Settings" on your desktop.')
        else:
            print("Nothing was changed - " + message)
        return 2
    for n in notes:
        print(n)

    rc = 0
    if fleet_settings:
        rc |= apply_fleet(args, fleet_settings)
    if alert_settings:
        if fleet_settings:
            print("")
        rc |= apply_alerts(args, alert_settings)
    return rc


def apply_alerts(args, settings):
    here = os.path.dirname(os.path.abspath(__file__))
    target = args.config or os.path.join(here, "alerts.ini")

    example = os.path.join(here, "alerts.example.ini")
    # With no alerts.ini yet, start from the example so every setting you did
    # not touch keeps its documented default. A dry run READS the example; it
    # must not create anything, because "change nothing" has to mean nothing.
    source = target
    if not os.path.exists(target) and os.path.exists(example):
        source = example
        if args.dry_run:
            print("There is no alerts.ini yet - this would start one from the example.")
        else:
            shutil.copyfile(example, target)
            print("There was no alerts.ini yet, so one was started from the example.")
            source = target
    if not os.path.exists(target) and not args.dry_run:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("")
        source = target
    before = read_text(source) if os.path.exists(source) else ""
    was = A.load_config(source)
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


def apply_fleet(args, settings):
    """Put [ranges] / [discovery] into the printer collector's config.ini."""
    here = os.path.dirname(os.path.abspath(__file__))
    target = args.fleet_config or os.path.join(
        os.path.dirname(here), "print-fleet-dashboard", "config.ini")
    example = os.path.join(os.path.dirname(target), "config.example.ini")

    source = target
    if not os.path.exists(target):
        if not os.path.exists(example):
            print("Could not find the printer tool at %s - is it installed?"
                  % os.path.dirname(target))
            return 1
        source = example
        if args.dry_run:
            print("There is no printer config.ini yet - this would start one from the example.")
        else:
            shutil.copyfile(example, target)
            print("There was no printer config.ini yet, so one was started from the example.")
            source = target

    before = read_text(source)
    merged, changes, described = before, [], []
    for section, pairs in settings:
        if section in A.REPLACE_WHOLE:
            # Rows a person names: replaced as a whole, so deleting one works.
            merged, section_changes = A.replace_section_in_ini(
                merged, section, list(pairs.items()))
            described.extend(A.describe_fleet_changes(section, section_changes))
            changes.extend(section_changes)
        else:
            merged, section_changes = A.merge_into_ini(merged, [(section, pairs)])
            for _sec, key, old, new in section_changes:
                described.append("%s: %s (was %s)." % (key, new, old or "not set"))
            changes.extend(section_changes)

    # It has to still parse, or the collector will not read a word of it.
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    cp.optionxform = str
    try:
        cp.read_string(merged)
    except configparser.Error as e:
        print("Nothing was changed in the printer config - the result would not read cleanly:")
        print("  %s" % re.sub(r"\s*While reading from '[^']*'", "", str(e)).strip())
        return 1

    if not changes:
        print("Nothing to change - the printer config already says all of that.")
        return 0
    if args.dry_run:
        print("Would change %d thing%s in %s:"
              % (len(changes), "" if len(changes) == 1 else "s", target))
        for line in described:
            print("  " + line)
        return 0

    try:
        shutil.copyfile(target, target + ".bak")
    except OSError as e:
        print("Could not make a backup of the printer config (%s) - nothing was changed." % e)
        return 1
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(merged)
    print("Printer settings: changed %d thing%s."
          % (len(changes), "" if len(changes) == 1 else "s"))
    for line in described:
        print("  " + line)
    print("  Saved to %s (the version before this is config.ini.bak)." % target)
    print('  The next "Refresh IT Ops Data" looks in any new place.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
