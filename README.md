# Smart Updater

A cross-platform updater for **Linux, Windows and macOS** that updates what is out of date or vulnerable **without breaking things**, and tells you plainly what it did, what it refused to do, and why.

Most updaters run "upgrade everything" and hope. Smart Updater treats every update as a small change that has to prove itself:

1. **Dry run first.** It asks the package manager what the update would do before doing it.
2. **Fix the cause, not the symptom.** If the dry run says "this would remove package B" or "this conflicts with B", and B has its own update, B is added to the same change and the dry run repeats. It keeps going until the change is clean or it hits a hard stop.
3. **Never remove anything.** An update that would remove a package is blocked and explained, never forced.
4. **Check afterwards.** Every change is followed by a health comparison (your own checks, failed services, broken package state). If anything got worse, that change is rolled back.
5. **Say why.** Every package ends up with a status, a reason code, a plain-English explanation, the dependency chain that was followed, and what a person should do next.

It is standalone: Python 3.9+, **no third-party dependencies**, one `.pyz` file if you want it.

> **Status: alpha.** See [What has and hasn't been tested](#what-has-and-hasnt-been-tested) before pointing it at a machine you care about. Start with `plan`, which changes nothing.

## Quick start

```bash
# from a clone of this repo
pip install .            # or: python -m smart_updater ...   or: python scripts/build_pyz.py

smart-updater doctor     # what was detected on this machine
smart-updater plan       # what would happen, and why. Changes nothing.
sudo smart-updater apply # apply the safe updates (Windows: run from an elevated prompt)
smart-updater check      # one-line monitoring status from the last run
smart-updater check --details   # ...plus what needs a person, why, and what to do
```

`plan` is read-only by construction: the command runner refuses to execute anything marked as mutating while planning.

## What a run tells you

```
STATUS: WARNING (exit code 1)
Updated 0 | would update 0 | blocked 1 | skipped 0 | rolled back 1 | failed 0 | security items open 0

NEEDS ATTENTION (1)
  [WARNING] su-toy-app 1.0 → 1.1: update broke something and was rolled back
      why:   Applied, but health got worse: health check 'web service' passed before and now fails. Rolled back to the previous versions.
      do:    Find the cause in /var/lib/smart-updater/runs/20261118T222754-8e9d/commands. This version is paused for 7 days so it does not fail again every run; clear the pause with: smart-updater unquarantine

FOR YOUR INFORMATION (1): not urgent, but a person decided or must decide
  [INFO] su-toy-core 1.0 → 2.0: update would remove packages
      why:   Applying this would remove su-toy-plugin and there is no newer version of it to move to. Smart Updater never removes packages by itself.
      do:    Review the removal and run the upgrade yourself if it is what you want, or hold this package.
```

That output is from a real run against apt (see the integration tests). Ask about any package later with `smart-updater explain <name>`.

### Why something was not updated

| Code | Meaning |
|---|---|
| `held` | The package manager has this package held or pinned. |
| `rule_hold` | A rule in your config says never to touch it. |
| `needs_human` | A rule says a person must approve it (built in for bootloaders, kernel-module packages, container runtimes, Python on Windows, macOS releases themselves, and anything Apple says needs a restart). |
| `bump_too_large` | The version jump is bigger than policy allows unattended. |
| `would_remove` | It would remove installed packages, which is never done automatically. |
| `dependency_blocked` | It needs another package to change and that change is not allowed. |
| `cascade_limit` | The dependency chain was longer than the configured limit. |
| `new_packages` | It would install new packages and policy forbids that. |
| `simulation_failed` | The dry run failed and nothing it names can be upgraded to fix it. |
| `apply_failed` / `version_mismatch` | The package manager errored, or said it worked but the version did not change. |
| `verify_failed` | It applied but health got worse, so it was rolled back. |
| `rollback_failed` | A rollback was needed and did not fully work. **Look at the machine now.** |
| `quarantined` | A previous attempt at this exact version failed; paused for a while (a newer version resets it). |
| `no_fix_available` | A known vulnerability exists and no fixed version is available. |
| `limit_reached`, `run_aborted`, `security_only` | Deferred or skipped by run limits or flags. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing needs attention |
| 1 | Something needs attention (warning) |
| 2 | Something urgent: a security fix has been stuck past the grace period, a rollback failed, or a backend keeps failing |
| 3 | Smart Updater itself could not run (bad config, no privileges, another run active). For `check`: unknown, e.g. the last report is stale |

## Configuration

Everything has a safe default; a config file is optional. See [`examples/config.example.toml`](examples/config.example.toml). Looked for in this order: `--config`, `$SMART_UPDATER_CONFIG`, `./smart-updater.toml` or `.json`, then `/etc/smart-updater/config.toml` (Linux), `/Library/Application Support/SmartUpdater/config.toml` (macOS) or `%ProgramData%\SmartUpdater\config.toml` (Windows), each also as `config.json`. TOML needs Python 3.11+ and JSON works everywhere (see [`examples/config.example.json`](examples/config.example.json)); on a Mac whose `python3` is older than 3.11, use JSON. Unknown keys are an error, so typos don't silently disable a safety setting.

The settings that matter most:

- **`max_bump`** per backend: the largest version jump applied unattended (`patch`, `minor`, `major`). Defaults: apt/dnf/winget `major` (distros vet versions per release, and apps are self-contained); pip `minor` (libraries break their dependents); Homebrew `minor` (it cannot roll back, so it is more careful than apt). Built-in rules also hold Python and the database servers to `patch` on Homebrew.
- **`[[rules]]`**: per-package `hold`, `manual` or `allow`, with an optional `max_bump` and a `reason` that is shown when it blocks something.
- **`[[health_checks]]`**: commands that must keep passing, for example `curl -f http://localhost/health`. A check already failing before an update is never blamed on it.
- **`batch_size`**: `1` (default) applies one change at a time, so any breakage is attributable. A larger value batches independent changes and automatically falls back to one at a time if a batch fails.

pip is **opt-in** and only touches environments you list. It never defaults to the system Python.

## Logs and state

Everything lives in one directory (`/var/lib/smart-updater`, `/Library/Application Support/SmartUpdater` on a Mac, `%ProgramData%\SmartUpdater`, or `~/.local/state/smart-updater` (`~/Library/Application Support/SmartUpdater` on a Mac) when unprivileged; override with `--state-dir` or `SMART_UPDATER_STATE_DIR`).

| Path | Contents |
|---|---|
| `latest.json`, `latest.txt` | The newest report. `latest.json` is the integration point for monitoring. |
| `smart-updater.log` | Rolling human-readable log of every run. |
| `runs/<id>/events.jsonl` | Every decision, simulation round, apply, verification and rollback, one JSON object per line. |
| `runs/<id>/commands/` | The complete output of every command that was run. |
| `runs/<id>/snapshots-*.json` | The versions recorded before each change, used by `smart-updater rollback <id>`. |
| `runs/<id>/report.{json,txt}` | The report for that run. |
| `state.json` | What it remembers between runs: when each problem was first seen, paused updates. |

Logs contain command lines and package names. They do not contain environment variables.

## Scheduling

- **Linux:** [`examples/smart-updater.service`](examples/smart-updater.service) and [`.timer`](examples/smart-updater.timer).
- **Windows:** [`examples/windows-task.ps1`](examples/windows-task.ps1). Run it under an administrator account rather than `SYSTEM`: winget behaves unreliably as `SYSTEM`.
- **macOS:** [`examples/com.smartupdater.apply.plist`](examples/com.smartupdater.apply.plist), a launchd daemon that runs daily as root. The install commands are in its header. It runs `/usr/bin/python3` on a file only root can change, with a `PATH` that leaves out Homebrew's folders, because the job runs as root.

Smart Updater **never reboots**. When a reboot is needed it says so, and the item escalates if it sits for 14 days.

## Monitoring (Looksee and others)

`smart-updater check` prints a Nagios-style line and exits 0/1/2/3, so anything that runs a command and reads an exit code can use it. A report older than 36 hours is `UNKNOWN`, so a monitor that silently stopped running never looks healthy. Security problems start as warnings and become critical after 3 days if nobody has dealt with them. `check --details` adds one block per item (what, why, what to do), sized to fit the 4 KB Looksee keeps from a script, and `push` (or `[looksee] push_url_file`) calls a Looksee Heartbeat check after each `apply`. See [`docs/LOOKSEE.md`](docs/LOOKSEE.md) for step-by-step setup on Linux, macOS and Windows and the full contract.

## What has and hasn't been tested

| Backend | Platform | State |
|---|---|---|
| apt | Debian, Ubuntu | **Tested against real apt/dpkg**: dry-run parsing, dependency cascade, "would remove" blocking, `--no-remove` applies, health-triggered rollback by downgrade ([`tests/integration`](tests/integration)). Run on Ubuntu 24.04. |
| pip | any | Parsers covered by fixtures; never run against a live package index. Needs pip 23+. |
| dnf | Fedora, RHEL family | **Experimental.** Parsers written from dnf's documented output and covered by fixtures only; never run on a real Fedora/RHEL host. |
| brew | macOS | **Experimental.** Output parsers and command construction are covered by fixtures only; never run on a real Mac. Runs Homebrew as the user who owns it (Homebrew refuses root). Uses `brew upgrade --dry-run`; no rollback (Homebrew cannot install an older version), so it keeps the previous version on disk and reports a failed update instead of hiding it. |
| softwareupdate | macOS | **Experimental.** Fixtures only; never run on a real Mac. Installs only small updates Apple says need no restart (Command Line Tools, Safari, ...); macOS releases and anything needing a restart are left to a person. No dry run, no rollback. |
| winget | Windows 10/11 | **Experimental.** Table parsing is covered by fixtures only; never run on a real Windows machine. winget has no dry run, so on Windows safety comes from policy, verification and rollback rather than a dependency simulation. |

Known limits:

- **Rollback is best effort.** It downgrades to the recorded version, which only works if that version is still available (cache or repository). A failed rollback is reported as critical, never hidden. On macOS (Homebrew and system updates) there is no automatic rollback at all; `smart-updater doctor` and the report's `backends[].rollback_support` say so.
- **macOS specifics.** Casks that update themselves (Chrome, Slack, ...) are skipped by Homebrew unless `greedy_casks = true`; some casks need an administrator's password and fail cleanly without one; a formula and a cask with the same name show as `name` and `cask:name`. Mac App Store apps (`mas`) and upgrades to a new macOS release are not handled.
- **winget output is a localised, width-formatted table.** English output is assumed; truncated or unrecognised rows are skipped and reported, not guessed.
- It does **not** upgrade the OS to a new release, apply Windows Update, or handle firmware. It does not replace a vulnerability scanner: `no_fix_available` only covers ecosystems with a vulnerability feed (pip via OSV.dev).
- Services restarted by package scripts are not tracked individually; the health comparison is what catches the fallout.
- Contacting OSV.dev sends package names and versions to a third party. Turn it off with `osv_enabled = false`.

## Development

```bash
pip install -e '.[dev]'
pytest                                   # fast unit tests, no system changes
sudo SMART_UPDATER_INTEGRATION=1 pytest tests/integration   # real apt; installs toy packages. Use a disposable VM.
```

How it works and how to add a backend: [`docs/DESIGN.md`](docs/DESIGN.md).

## License

MIT, see [`LICENSE`](LICENSE).
