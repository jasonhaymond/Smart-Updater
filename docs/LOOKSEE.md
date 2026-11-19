# Using Smart Updater with Looksee

[Looksee](https://github.com/jasonhaymond/Looksee) is a self-hosted monitor. Smart Updater is standalone and never talks to it unless you ask it to. There are two ways to connect them. Use the first on any machine that runs a Looksee agent; the second is for machines without one, or as a second safety net.

| | **A. Custom script check** (recommended) | **B. Heartbeat push** |
|---|---|---|
| Needs a Looksee agent on the machine | yes | no |
| Needs the machine to reach Looksee | no (the agent already does) | yes |
| Shows what needs attention, why and what to do | yes, on the check's page | one line |
| Notices the updater silently stopped | yes (report older than 36 h becomes UNKNOWN) | yes (no ping, so Looksee marks it down) |

Both can run together.

> **How this was built, and what has not been run.** Everything here was written against Looksee's source (`agent/internal/checks/script.go`, `engine/src/routes/public.ts`, its docs) and tested against copies of the parsing rules and a stub HTTP server. It has **not** been run against a live Looksee install. The first time you set it up, click **Run now** on the new check (or wait one interval) and compare with the sample below.

## What Looksee does with a script (and what that means for the output)

Taken from the agent's code and docs:

- The **exit code** is the status: `0` up, `1` warn, `2` down, anything else unknown. `smart-updater check` already uses exactly these (OK, WARNING, CRITICAL, UNKNOWN).
- The **first line** is the message, and the **first** `label=value` after a `|` is the check's value (that is what graphs and thresholds use). `smart-updater check` therefore puts `attention=<warnings + criticals>` first.
- The first **4096 characters** of output are sent to the engine and shown on the check's page under *Latest details*. `check --details` fills that with one block per item that needs a person and stops before the limit, with a "+N more" line if needed.
- Scripts must sit directly in the agent's `script_dir` and have a plain file name; Looksee never sends commands, it only picks which approved script runs.

## A. Custom script check

Install Smart Updater first (see the README), schedule `apply`, and confirm `smart-updater check` prints a status line on that machine.

### 1. Put the wrapper where the agent can run it

The agent runs the wrapper, and on macOS and Windows it runs as root or SYSTEM, so the wrapper and the Smart Updater executable must be in places only an administrator can change.

**Linux** (the agent runs as the unprivileged `looksee-agent` user, and reports are world-readable, so no extra permissions are needed):

```bash
sudo install -m 755 dist/smart-updater.pyz /usr/local/bin/smart-updater      # python scripts/build_pyz.py makes it
sudo install -d -m 755 -o root -g root /etc/looksee-agent/scripts
sudo install -m 755 integrations/looksee/smart-updater-check.sh /etc/looksee-agent/scripts/
# in looksee-agent.yaml:  script_dir: /etc/looksee-agent/scripts      then restart the agent
```

**macOS** (the agent runs as root). Keep both files out of Homebrew's folders, which the user who installed Homebrew can often write to (on an Intel Mac that includes `/usr/local/bin` and `/usr/local/etc`):

```bash
sudo install -d -m 755 -o root -g wheel /usr/local/libexec/smart-updater
sudo install -m 755 -o root -g wheel dist/smart-updater.pyz /usr/local/libexec/smart-updater/
sudo install -d -m 755 -o root -g wheel "/Library/Application Support/LookseeAgent/scripts"
sudo install -m 755 -o root -g wheel integrations/looksee/smart-updater-check.sh "/Library/Application Support/LookseeAgent/scripts/"
# in looksee-agent.yaml:  script_dir: /Library/Application Support/LookseeAgent/scripts     then restart the agent
```

**Windows** (the agent runs as SYSTEM). Install Python 3.9+ for all users, and put the file under `Program Files`, not `ProgramData` (ordinary users can add files to `ProgramData`):

```powershell
New-Item -ItemType Directory "$env:ProgramFiles\SmartUpdater" -Force
Copy-Item dist\smart-updater.pyz "$env:ProgramFiles\SmartUpdater\"
Copy-Item integrations\looksee\smart-updater-check.ps1 C:\ProgramData\LookseeAgent\scripts\
# script_dir is C:\ProgramData\LookseeAgent\scripts in the agent's config; restart the agent
```

If `smart-updater` lives somewhere else, edit the list at the top of the wrapper. (The shell version also honours `SMART_UPDATER_BIN` in the agent's environment.)

### 2. Add the check in Looksee

On the endpoint: **+ Add check → Custom script**.

| Setting | Value |
|---|---|
| Script file name | `smart-updater-check.sh` (Linux, macOS) or `smart-updater-check.ps1` (Windows). **Browse** lists it once the agent has found it. |
| Arguments | blank, or `--max-age-hours 60` if you only run the updater weekly |
| Expected output | None, so the exit code decides |
| Check every (seconds) | 1800 to 3600. The report only changes when the updater runs, but a short interval means a stale report is noticed promptly |
| Alert rule, *Failures in a row* | 1 or 2 |

Optional: a threshold on the value ("warn above 0") adds nothing, since the exit code already maps the same state. A graph of the value shows how many things have needed a person over time.

### 3. What you will see

The check's message is the first line, and *Latest details* holds the rest:

```
SMART_UPDATER CRITICAL - 2 need attention (1 security): openssl 3.0.13 → 3.0.15: held back, security fix waiting 4 days; su-toy-app 1.0 → 1.1: update broke something and was rolled back | attention=2 attention_critical=1 attention_warning=1 attention_info=1 security_open=1 updated=3 failed=0 rolled_back=1 age_hours=0.0
[CRITICAL] [SECURITY] openssl 3.0.13 -> 3.0.15: held back, security fix waiting 4 days (open 4d)
  why:  openssl is held/pinned in the package manager, so it was left alone on purpose. This is a security update and has been waiting 4 days, past the 3 day limit.
  next: Release the hold when you are ready to update it: apt-mark unhold openssl
  id:   apt:openssl:held
[WARNING] su-toy-app 1.0 -> 1.1: update broke something and was rolled back
  why:  Applied, but health got worse: health check 'web service' passed before and now fails. Rolled back to the previous versions.
  next: Find the cause in /var/lib/smart-updater/runs/20261118T222754-8e9d/commands. This version is paused for 7 days so it does not fail again every run; clear the pause with: smart-updater unquarantine
  id:   apt:su-toy-app:verify_failed
1 informational item(s) (held or deliberately skipped) are in the full report
```

(Arrows in the detail lines are plain `->` on purpose, so a legacy code page cannot garble them.)

| State | Looksee | Meaning |
|---|---|---|
| OK | up | Nothing needs a person |
| WARNING | warn | Something needs a person: a blocked update, a rolled-back update, a pending reboot, a security fix inside its 3 day grace period |
| CRITICAL | down | A security fix stuck past 3 days, a rollback that did not fully work, a package manager that failed 3 runs in a row, or a reboot pending for 14 days |
| UNKNOWN | unknown | No report, an unreadable one, or the last run is older than 36 hours: the updater has stopped running |

### Where to read reports

`check` and `report` look in the system location first (`/var/lib/smart-updater`, `/Library/Application Support/SmartUpdater`, `%ProgramData%\SmartUpdater`) and then the per-user one, and use the first that has a report. A monitoring agent running as a different user than the updater needs no setup. Use `--state-dir` or `SMART_UPDATER_STATE_DIR` only if you moved it.

## B. Heartbeat push (no agent needed)

Smart Updater can call a Looksee **Heartbeat** check's URL after every `apply` run. Looksee records the status, a message and a value, and marks the check down if the calls stop.

1. In Looksee add a **Heartbeat** check. Set *Expect a ping every* to how often `apply` runs (86400 for daily) and a *Grace period* that covers a slow run (e.g. 3600).
2. Open the check and copy its URL (`https://<looksee>/api/hb/<token>`). **The URL is the only credential**: anyone who has it can report results. Regenerate it in Looksee if it leaks.
3. Store the URL where only root can read it and point the config at it. It is never put on a command line, in the config file itself, or in a log.

```bash
sudo install -m 600 /dev/null /etc/smart-updater/looksee-url
sudoedit /etc/smart-updater/looksee-url           # paste the URL
```

```toml
# /etc/smart-updater/config.toml  (or config.json)
[looksee]
push_url_file = "/etc/smart-updater/looksee-url"
timeout = 10
```

Or set `SMART_UPDATER_LOOKSEE_URL` in the scheduled job's environment instead of using a file. Then:

```bash
smart-updater push        # tell Looksee about the latest report now (a good first test)
```

After that every `apply` run pushes by itself. Details:

- **Mapping:** OK to `up`, WARNING to `warn`, CRITICAL to `down`. A missing or stale report is sent as `warn`, never `up`. The message is the status line (without the performance data, at most 900 characters) and the value is the number of warnings plus criticals.
- **Plan-only runs do not push**, so a stopped `apply` schedule cannot look alive because someone ran `plan`.
- **A failed push never affects an update run.** It is logged as a warning (without the URL) and Looksee's own missing-ping alert covers it.
- Prefer `https://`. A plain `http://` URL works, but the token crosses the network in clear text.
- It sends a JSON `POST`, which Looksee's `/api/hb/<token>` accepts (query-string values win over body values; the body here carries `status`, `msg` and `value`).

## The report file, for anything else

`latest.json` (schema `smart-updater/report/v1`) is replaced atomically at the end of every run. Anything that can read a file can use it.

| Field | Use |
|---|---|
| `status` | `ok` / `warning` / `critical` |
| `finished` | When it last ran. Treat old as unknown, as `check` does |
| `attention[]` | What needs a person, most severe first. `info` items do not change `status` |
| `attention[].id` | Stable across runs (`<backend>:<package>:<reason_code>`): de-duplicate, acknowledge, or track an item over time |
| `attention[].severity`, `.title`, `.reason`, `.suggested_action` | What to show a person |
| `attention[].security`, `.advisories` | Security fixes and any advisory ids known |
| `attention[].first_seen`, `.age_days` | How long it has been open |
| `reboot_required` | A reboot is pending |
| `backends[]` | What was checked, including `rollback_support` (`none` for Homebrew and macOS system updates) |
| `decisions[]` | Everything considered, with the dependency `chain` that was followed |

## Alongside Looksee's own software checks

Looksee already has *Upgradable packages*, *Installed software changed* and *Vulnerable or unsupported software* checks. They tell you what is out of date. Smart Updater is the part that acts on it and explains why something was **not** updated, so the two fit together: Looksee shows the problem, Smart Updater's check shows what is being done about it. Looksee also has a *Scheduled task / timer* check that can watch the Smart Updater timer itself.
