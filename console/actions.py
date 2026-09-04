"""The plain-English next step for each kind of finding.

Shared by the pages (the arrow line under a finding) and the alert rules (the
same sentence in a Teams or email alert), so the console and the message never
disagree about what to do.
"""

from __future__ import annotations

CA_GAP_ACTION = {
    "mfa-all-users":
        "Create a Conditional Access policy that requires MFA for all users on all cloud apps.",
    "block-legacy-auth":
        "Add a Conditional Access policy that blocks legacy clients "
        "(Exchange ActiveSync and 'Other clients').",
    "admin-mfa":
        "Require MFA for admin roles - a Conditional Access policy targeting directory roles, "
        "or confirm the all-users MFA policy covers them.",
    "baseline-exists":
        "Turn on at least one enforced Conditional Access policy, or enable Security Defaults.",
    "breakglass-exclusion":
        "Exclude your break-glass (emergency) accounts from every all-users block or MFA policy "
        "so you cannot lock yourself out.",
    "guest-protection":
        "Cover guests with MFA or a block: a Conditional Access policy targeting "
        "'Guests or external users'.",
    "risk-policies":
        "Optional, needs Entra ID P2: add sign-in-risk and user-risk Conditional Access policies.",
    "device-grants":
        "Optional: require a compliant or hybrid-joined device in a Conditional Access grant.",
    "report-only-lingering":
        "Decide on each report-only policy: switch it to On, or delete it.",
    "unused-locations":
        "Delete named locations no policy uses, or reference them in a policy.",
}


def next_step(kind, key=None):
    """One line: what a person does next about this kind of finding."""
    if kind == "ca-gap":
        return CA_GAP_ACTION.get(str(key or ""),
                                 "Review this Conditional Access gap in the Entra admin center.")
    if kind == "admin-no-mfa":
        return ("Have them register MFA (aka.ms/mfasetup) today, and enforce it for admin roles "
                "with Conditional Access so it cannot lapse.")
    if kind == "disabled-licensed":
        return ("Remove the license from the disabled account (M365 admin center > Users > "
                "Active users) so the seat can be reclaimed.")
    if kind == "stale-account":
        return ("Check with the person's manager; if they have left, disable the account and "
                "reclaim its licenses.")
    if kind == "legacy-auth":
        return ("Block it: a Conditional Access policy targeting legacy clients "
                "(Exchange ActiveSync and 'Other clients') closes this path.")
    if kind == "fleet":
        st = str(key or "")
        if st == "offline":
            return ("Check the printer is powered on and on the network; it is marked offline "
                    "when it stops answering SNMP.")
        if st == "warning":
            return "Restock what the detail names (toner or paper) before it runs out."
        return ("Walk to the printer - its panel will show what the detail here describes "
                "(jam, door open, out of toner).")
    # The kinds below are raised by the alert rules (console/alerts.py); the
    # pages that show the same finding use the same words.
    if kind == "app-credential":
        return ("Renew it before whatever uses it stops working: Entra admin center > App "
                "registrations > the app > Certificates & secrets - or delete it if nothing uses it.")
    if kind == "mfa-coverage":
        return ("Ask the people without a method to register at aka.ms/mfasetup, and enforce MFA "
                "with Conditional Access so registration cannot be skipped.")
    if kind == "unused-seats":
        return ("Reassign or reduce the seats at the next renewal (M365 admin center > Billing > "
                "Your products) - the Licensing page lists which SKUs.")
    if kind == "change":
        cat = str(key or "")
        if cat == "Conditional Access":
            return "If nobody on the team made this change, treat it as a possible compromise and check the audit log."
        if cat == "Role assignments":
            return "Confirm the grant was intended; admin roles should be few, named, and MFA-protected."
        return "Confirm it was intended; the What changed page has the before and after."
    if kind == "refresh-signin":
        return 'Double-click "Refresh IT Ops Data" on that computer to sign in; if it keeps happening, re-run setup and check the schedule choice.'
    if kind == "collector-failed":
        return ("The reason is on the refresh page under \"What the run said\", and in the run "
                "log in output\\logs. If it is not clear, run \"Refresh IT Ops Data\" by hand "
                "and read that step's red text.")
    if kind == "certificate":
        return "Re-run setup on that computer and pick unattended refresh again - it makes a new certificate to upload."
    if kind == "new-printer":
        return ("Check it is a printer you want tracked, then give it a proper name in the "
                "printer tool's config.ini - or add its address to the ignore list on the "
                "Print fleet tab if it should be left alone.")
    if kind == "discovery-problem":
        return ("Fix the place to look on the console's Print fleet tab - the message says "
                "what is wrong with it.")
    if kind == "stale-data":
        return 'Run "Refresh IT Ops Data", or check that the automatic refresh is still scheduled (check-setup.ps1).'
    return ""
