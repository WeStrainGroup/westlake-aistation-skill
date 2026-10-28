---
name: westlake-aistation
description: Use a configured Westlake AIStation account for debugging, resource checks, bounded compute and monitoring; prepare and diagnose native task-management jobs with explicit compatibility checks.
---

# Westlake AIStation

Use `scripts/wlai` relative to this skill directory. This community adapter uses AIStation/Kubernetes, not Slurm. Read [setup and command reference](references/usage.md) for configuration and [manual map](references/manual.md) for official requirements and tested boundaries.

Users supply their own account and trusted server certificate. Passwords use hidden input and optional system credential storage; never place them in specs, arguments, environment variables, logs or source control. HTTPS and container SSH identities must match trusted configuration. A changed identity requires independent verification.

## Choose the workflow

- Debugging and short validation: `job --script /absolute/main.py --minutes 30 --out job.json -- ...`, offline `plan --spec job.json`, then `plan --spec job.json --online`. Offline planning does not prove live availability. Use `run` or `submit --execute` only within the user's authorized inputs, resources and output scope. Present a concrete plan before substantial compute.
- Formal training belongs in task management. Stage code and data once in persistent storage, configure a training group, and generate with `job --purpose training --remote-script /personal/train.py ...`. Use `task-plan`, `task-status`, `task-logs`, `task-watch`, `task-reconcile` and `task-stop` for native-task records. Single-node multiple GPU requests are supported by the planner; the generated command is one Python process, so verify that the program itself starts all GPUs or supply an explicit launcher in `spec.argv`. Independent tasks are not multi-node DDP.
- **Native creation is experimental in this preview.** A live official-form request for one GPU was accepted but changed by the service to CPU/zero GPU. `task-submit --execute` therefore refuses new creation unless explicitly given `--experimental-native` for a bounded diagnostic. This switch is not production approval. Do not submit a production batch, retry guessed API fields, or use development environments to work around long-training limits. `task-guide` remains a manual form checklist; it does not prove that a GPU was allocated.

## Run, observe and verify

1. Discover `groups --purpose development|training` and current resources. Groups are checked against workload labels; visible GPU nodes alone do not authorize development allocation. CPU and GPU must fit together on one node, and account quota still applies.
2. Use verified master storage by default. SSD `ssdwork` requires explicit user intent and has separate group quota and charges. Verify the configured mount before creating the run directory. Never create a lookalike mount or change ACLs to bypass a failed check. `df` does not prove group quota.
3. Development `submit` runs a local controller; keep its machine powered and connected. Development has a four-hour platform lifetime, including preparation, transfers, computation, collection and cleanup. The client program cap is 13,800 seconds, with additional stage budgets. Native tasks, once accepted, belong to the platform and continue when the observer disconnects; group-specific runtime limits still apply.
4. Agents sharing a profile should share its account-scoped registry. Identical run IDs/specs reuse records; changed code or parameters require a new ID. An uncertain creation becomes `UNKNOWN`: reconcile before any new creation POST. Do not automatically adopt legacy unscoped state.
5. Confirm actual GPU count/model and persistent output evidence. Native guards run before scientific code and retain attempt receipts; platform success without a valid guard receipt is `COMPLETED_UNVERIFIED`. Exit zero and hardware verification do not validate scientific results. Observe logs/checkpoints/metrics and confirm resource release. `Deleted` is a released terminal state, not a reason to wait or repeatedly stop it.

## Updates shared by agents

After every GitHub release, locate the separate reviewed source checkout and run its absolute `scripts/install_local.py` path; the installed skill exposes a CLI wrapper, not the source checkout or installer. Install that same commit locally; update the discoverable skill and any pre-existing canonical CLI path together. Verify both `--version` outputs and `INSTALLATION.json` revision/content hash. Updating GitHub alone leaves other agents on old code.

Install immutable snapshots and switch wrappers; do not edit a version used by a running controller. Preserve private profiles, credential-store entries, certificates, data and legacy receipts. For the exact install/rollback commands, read [installation and updates](references/usage.md#installation-and-updates). Existing agents should reread the installed skill before the next operation after an upgrade.
