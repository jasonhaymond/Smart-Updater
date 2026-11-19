# Design

## Goals and non-goals

**Goals**

- Keep software current and security-patched unattended, on Windows and Linux, with one tool.
- Prefer *not updating* to *breaking something*, and always say which happened and why.
- When an update is blocked by another package, try to resolve it by updating that package too, as long as everything involved is allowed.
- Be standalone (standard library only) and leave an audit trail good enough to reconstruct any decision.
- Produce output a monitoring system can consume.

**Non-goals**

- Rebooting. Smart Updater reports that a reboot is needed; it never does one.
- Removing packages, changing held/pinned packages, or doing OS release upgrades.
- Being a vulnerability scanner. It acts on the update metadata and feeds it has; it does not inventory every CVE.

## Flow

```
            list updates ──► classify (security? version jump size?)
                                  │
                         per-package policy checks
                  (hold, rules, quarantine, jump ceiling)
                                  │ allowed
                                  ▼
                   ┌────────► dry run (simulate) ◄──────────────┐
                   │              │                             │
                   │     ┌────────┴──────────┐                  │
                   │  failed / would       clean               │
                   │  remove something       │                  │
                   │     │                   ▼                  │
                   │  named package    extra upgrades pulled in? │
                   │  has an update?    each must pass policy ───┘ (else: blocked, with reason)
                   │     │ yes
                   └─────┘  add it to the group, repeat (bounded by max_cascade_depth)

 plan mode: stop here and report.            apply mode: continue

   re-resolve against current state ─► snapshot old versions ─► apply (pinned to the simulated versions,
   --no-remove) ─► versions really changed? ─► health no worse than before? ─► yes: done
                                                         │ no
                                                         ▼
                                         roll back, verify the rollback, quarantine that version
```

## The cascade, concretely

From the real-apt integration test: `su-toy-core 2.0` declares `Breaks: su-toy-plugin (<< 2.0)`.

- If `su-toy-plugin 2.0` exists, the dry run already upgrades both; the extra upgrade is checked against policy and the pair becomes one group that is applied and, if needed, rolled back together.
- If it does not, the dry run says the plugin would be removed. Nothing in the group can fix that, so the update is **blocked as `would_remove`** and the plugin stays installed.

Failed dry runs work the same way: package names in the error text that have updates available are added to the group and the dry run repeats. Every step is appended to the decision's `chain`, so the report can say *"also upgrading X because the dry run failed naming it"*.

## Safety guarantees and where they live

| Guarantee | Mechanism | Tested by |
|---|---|---|
| Planning changes nothing | `Runner(read_only=True)` raises on any command marked `mutating` | `test_read_only_runner_blocks_mutating_commands` |
| No package is ever removed | Dry-run removals block the update; apt applies use `--no-remove` | `test_removal_*`, `test_apply_never_removes_*`, integration |
| What runs is what was simulated | Apply pins every package to its simulated version | `test_apply_never_removes_pins_versions_*` |
| Local config files survive | apt applies with `--force-confold` | `test_apply_never_removes_pins_versions_*` |
| State is re-checked before applying | The executor re-resolves each group right before applying | `test_state_change_between_plan_and_apply_is_caught` |
| Breakage is detected | Health snapshot before/after: custom checks, failed systemd units, stopped Windows services, package database problems | `test_health_regression_*`, integration |
| Breakage is undone | Rollback to recorded versions, then re-verified | `test_*rolled_back*`, integration |
| One bad update doesn't block the rest | Batches fall back to one-at-a-time; a failed version is quarantined; a run aborts after `max_failures` in a row | `test_batch_failure_splits_*`, `test_run_aborts_*` |
| A failed rollback is loud | Status `failed`, reason `rollback_failed`, severity critical | `test_failed_rollback_is_critical` |
| A silent monitor is not a healthy monitor | `check` returns UNKNOWN for stale reports | `test_check_output_and_exit_codes` |

Mutation testing was used to confirm these tests fail when the mechanism is removed.

## Backends

| Backend | Dry run | Security metadata | Rollback | Notes |
|---|---|---|---|---|
| apt | `apt-get -s install --only-upgrade pkg=ver` | Security pocket in `apt list --upgradable` | Downgrade to recorded version | Verified on real apt |
| dnf | `dnf upgrade --assumeno` | `dnf updateinfo list --security` | `dnf history undo`, else downgrade | Experimental |
| winget | none | none | `winget install --version old` | Experimental; one package at a time |
| brew (macOS) | `brew upgrade --dry-run` | none | none (Homebrew cannot install an older version); old keg kept, failure reported | Experimental. Runs as the owner of Homebrew, never as root |
| softwareupdate (macOS) | none | none | none | Experimental. Only restart-free updates; macOS releases are left to a person |
| pip | `pip install --dry-run --report -` | OSV.dev | `pip install pkg==old` | Opt-in per environment |

### Adding a backend

Subclass `Backend` in `src/smart_updater/backends/`, implement `available`, `list_updates`, `simulate`, `apply`, `installed_versions`, `rollback` (plus `held`, `problems`, `reboot_required` where the package manager supports them), and register it in `backends/__init__.py`. If it cannot dry-run, return `SimResult(..., analyzed=False)` so the report says dependencies were not checked. If it cannot roll back, `rollback()` must still check whether anything actually changed and return `ok=True` only when there is nothing to undo; otherwise return `ok=False` with the reason, which the executor reports as critical. Override `manual_reason(update)` for things only the backend knows (for example "this update needs a restart"); a user `allow` rule overrides it. Test the parsers with fixtures; `tests/fake_backend.py` shows the shape of a backend for planner and executor tests.

## Reports and escalation

Each run builds a list of **decisions** (one per package, with status, reason code, explanation, chain, suggested action) and a list of **attention items** derived from them plus system-level items (reboot required, backend unreachable, health check already failing).

Severity is `info` (a person decided or must decide, not urgent), `warning`, or `critical`. Only `warning` and `critical` change the overall status. Items that involve a security fix start as `warning` and become `critical` after `critical_after_days` (default 3), using a first-seen date remembered in `state.json`. A reboot that sits for `reboot_critical_days` (14) escalates the same way. A backend that fails three runs in a row becomes critical. Items that stop appearing are forgotten.

Attention item ids are stable (`<backend>:<package>:<reason_code>`), so a monitor can de-duplicate and track them across runs.

## Known weak spots

- **Rollback availability.** apt and dnf can only downgrade to a version that is still obtainable. Most distro repositories keep only the latest version in each update pocket. Mitigations are the dry run (it prevents most breakage), health verification (it catches the rest), and a loud report when rollback fails. A future option is to keep the previous `.deb`/`.rpm` files in a local cache before applying.
- **Health is only as good as your checks.** With no `[[health_checks]]`, only systemd/Windows service state and package database health are compared. Define checks for the things you care about.
- **Name matching in failed dry runs is textual.** A package mentioned in an error message that has an update gets pulled into the group. The next dry run and the policy checks still have to pass, so a wrong guess blocks the update rather than breaking anything, but it can produce a noisier chain.
- **winget** has no dry run and a fragile table output; see the README.
- **macOS backends have no rollback.** The mitigations are policy (smaller default jumps, no macOS releases, nothing that needs a restart), verification, and a critical report when something changed and could not be undone. They have never run on a real Mac.
- **Running as root and running Homebrew.** Homebrew refuses root, so the backend runs it with `sudo -n -u <owner>`; root never executes anything the owner could not. For the same reason PATH is not trusted when running as root.
