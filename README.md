# it-ops-console

One page per topic, built from the output your other tools already produce.

Four separate tools means four separate outputs, and nobody opens four things.
This builds a small static site that puts them side by side: an overview with
the handful of numbers worth glancing at, then one page per domain that carries
the detail without burying it in the other three. It never collects anything
itself — it reads what the collectors wrote and renders it.

Everything it produces is plain HTML and CSS in a folder. No server, no
database, no JavaScript framework, no phoning home. Copy the folder to a file
share or point IIS at it and you have a console.

![Overview](docs/overview.png)

## What it reads

| Feed | Produced by | Feeds the page |
|---|---|---|
| `tenant.json` | [entra-tenant-docs](https://github.com/JonathanT10/entra-tenant-docs) `Export-EntraTenantDocs.ps1` | Identity |
| `run-summary.json` | same, same run | Identity (CA gap analysis) |
| `history/` | same, archived snapshots | What changed |
| `security-snapshot.json` | [entra-security-snapshot](https://github.com/JonathanT10/entra-security-snapshot) `-JsonPath` | Security |
| `licensing.json` | [m365-license-waste-report](https://github.com/JonathanT10/m365-license-waste-report) `-JsonPath` | Licensing |
| `fleet.db` | [print-fleet-dashboard](https://github.com/JonathanT10/print-fleet-dashboard) `collector.py` | Print fleet |

Every feed is optional. Configure two and you get two pages; the rest are
greyed out in the nav and labelled "not configured" rather than showing
zeroes that look like real numbers.

## The pages

**Overview** — one tile per domain with its headline number, a "needs a human"
panel that pulls the genuinely actionable items out of all four feeds into one
list, and a freshness dot per feed.

**Identity** — tenant and user counts, Conditional Access policies by state, the
CA gap analysis, directory roles by membership size, groups, authentication
methods, app registrations, and Intune compliance and configuration when the
export included it.

**Security** — MFA coverage, admins without MFA, stale
member and guest accounts, legacy authentication sign-ins, and whatever the
snapshot flagged as needing attention.

**Licensing** — SKUs sorted by unassigned seats, reclaim candidates split into
disabled accounts (reclaim today) and stale accounts (ask first), and
self-service consumption SKUs kept separate so they stop inflating the waste
number.

**Print fleet** — devices sorted worst-first, toner levels as severity-coloured
meters, a page-volume sparkline per device, and anything that has not reported
in 48 hours marked offline regardless of what its last snapshot said.

**What changed** — the console recomputes the diff from the archived snapshots
rather than trusting a pre-baked list, so it stays correct even when the export
that wrote `run-summary.json` is older than the history folder. Conditional
Access, role assignments, licence purchases, app registrations, and Intune
policies.

## Freshness is a first-class citizen

A console that quietly shows last month's numbers is worse than no console.
Every feed carries the timestamp its tool wrote, and every page says how old its
data is. Under 26 hours is fresh (a daily job that ran late still counts), up to
8 days is aging, beyond that is stale and says so loudly — on the overview, on
the page itself, and in the build output.

The builder also prints a feed table and a warning when anything has gone stale,
so a scheduled build tells you in its own log that a collector has stopped
running.

## The easy way — download one file, double-click it

Download [`Setup-IT-Ops-Console.cmd`](Setup-IT-Ops-Console.cmd) and
double-click it. That is the whole instruction: it fetches `setup.ps1` next to
itself and runs it with the right settings — no PowerShell knowledge, no
right-click menus, no execution policy. (Windows may show "Windows protected
your PC" for a downloaded file — click **More info → Run anyway**.)

Prefer to see what you are running first? Download [`setup.ps1`](setup.ps1)
itself and right-click → **Run with PowerShell**. Same result. It creates
`C:\IT-Ops`, downloads all five tools, installs the Graph modules, checks for
Python and offers to install it, writes a `sources.ini` where everything
already points at everything else, and puts two shortcuts on your desktop:

- **Refresh IT Ops Data** — runs every collector (you sign in when asked),
  then rebuilds the console. Double-click it on whatever rhythm suits you.
  A live progress page opens in your browser while it works: each step with
  a spinner and plain-English label, the collector's own activity streaming
  underneath, and stat chips appearing as data lands — then a plain-English
  finish with one button to open the console. (`-NoStatusPage` for
  scheduled runs.)
- **IT Ops Console** — opens the result in your browser.

![Refresh progress](docs/refresh-status.png)

Nothing stores a password: collection is read-only and you sign in
interactively each refresh. Works in Windows PowerShell 5.1 — the one
"right-click → Run with PowerShell" actually launches — as well as
PowerShell 7. Printers are optional; add their IPs to
`tools\print-fleet-dashboard\config.ini` whenever you get to it.

**If something goes wrong:** run [`check-setup.ps1`](check-setup.ps1)
(right-click → Run with PowerShell). It is read-only, says in plain words what
is missing and what to do about each thing, and writes `check-setup.log` for
whoever is helping you. Setup keeps its own `setup.log` the same way, and a
failed refresh ends with an "In plain words" section that translates the error
into an action.

## Setup by hand

Python 3.8 or newer. No third-party packages — standard library only.

```
git clone <this repo>
cd it-ops-console
python build.py --sample --out console-site
```

That builds the whole site from the bundled demo data, no tenant and no
printers required. Open `console-site/index.html` to see what you would get.

For your own data, copy the config and edit the paths:

```
cp sources.example.ini sources.ini
python build.py --config sources.ini --out C:\it-ops\console-site
```

Relative paths in `sources.ini` resolve against the config file's own folder,
not the current directory, so a scheduled task that starts in
`C:\Windows\System32` builds exactly the same site you get by hand. Backslash
separators in a config are normalized on Linux and macOS, so a `sources.ini`
written on Windows still loads there.

## Running everything in one go

`run-all.ps1` is the other half: it runs the collectors, then builds the
console.

```powershell
.\run-all.ps1 -ToolRoot C:\it-ops\tools `
              -OutputRoot C:\it-ops\output `
              -SitePath C:\inetpub\wwwroot\console
```

It signs into Graph once with the union of the three Entra tools' read-only
scopes, and each script reuses that context instead of prompting again. A
collector that fails does not stop the run — the failure is recorded, the
remaining tools still run, and the console is still built with that feed showing
its real age. The exit code is non-zero if anything failed, so a scheduled task
shows red instead of succeeding quietly.

```
Step                       Status   Seconds  Detail
-------------------------- -------- -------  ------------------------
entra-tenant-docs          ok          74.2
entra-security-snapshot    FAILED       3.1  Insufficient privileges to complete the operation.
m365-license-waste-report  ok          41.9
print-fleet-collector      skipped        0  no -FleetConfig/-FleetDb given
console build              ok           0.4
```

The printer collector normally runs on its own short timer, so `run-all.ps1`
leaves it alone unless you pass `-FleetConfig` and `-FleetDb`. The fleet
database is opened read-only by the console, so it is safe to build while the
collector is writing to it.

Scheduled nightly:

```powershell
$action  = New-ScheduledTaskAction -Execute 'pwsh.exe' `
    -Argument '-NoProfile -File C:\it-ops\tools\it-ops-console\run-all.ps1 -OutputRoot C:\it-ops\output -SitePath C:\inetpub\wwwroot\console'
$trigger = New-ScheduledTaskTrigger -Daily -At 4am
Register-ScheduledTask -TaskName 'IT ops console' -Action $action -Trigger $trigger
```

## Permissions

Read-only, all of it. Nothing in this repo or in `run-all.ps1` writes to Entra,
Intune, or a printer. The scopes are whatever the underlying tools need:
`Directory.Read.All`, `Policy.Read.All`, `RoleManagement.Read.Directory`,
`Application.Read.All`, `Organization.Read.All`, `User.Read.All`,
`AuditLog.Read.All`, and the three `DeviceManagement*.Read.All` scopes for the
Intune sections. Skip a tool and you can skip its scopes.

## Design notes

**The console is a renderer, nothing more.** It has no credentials, no network
calls, and no scheduler. That boundary is deliberate: collection is where the
permissions, the rate limits, and the failure modes live, and keeping it out of
the renderer means the console cannot be the reason a run failed. It also means
you can rebuild the site from yesterday's JSON to see exactly what it looked
like yesterday.

**A missing feed is a normal state, not an error.** No feed raises. A corrupt
JSON file, a deleted database, a tool nobody has run yet — each becomes a page
that says what is wrong and a nav entry that says "not configured". A console
that crashes because one of six inputs is bad is a console you stop trusting.

**Colour is never the only signal.** Status is an icon plus a label; the colour
reinforces it. Meters carry their number. The palette holds up in dark mode and
for colour-vision deficiency.

**Deterministic output.** Given the same feeds, the build produces the same HTML
apart from the generated-at footer and the relative ages ("3h ago"), which are
by definition read off the clock. Nothing else varies between runs, so diffing
last week's build against today's shows you real drift and not churn.

## Tests

```
python tests/test_console.py
```

55 checks covering freshness classification, timestamp parsing, feed
degradation (corrupt JSON, missing files, unconfigured sources), every model,
change detection, rendering with zero feeds configured, HTML escaping of
hostile input, and a full sample build.

## Licence

MIT.
