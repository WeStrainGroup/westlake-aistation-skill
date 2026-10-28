---
name: westlake-aistation
description: Use a configured Westlake AIStation account for development-environment debugging, bounded validation jobs, resource and storage checks, monitoring and result recovery; prepare a web-submission guide for formal training.
---

# Westlake AIStation

Use `scripts/wlai` relative to this skill directory. This is a community adapter for AIStation/Kubernetes, not an official SDK or Slurm client. The [manual map](references/manual.md) distinguishes official requirements, client safeguards and deployment observations.

For new users, follow [setup and command reference](references/usage.md). `init --connection-only --server-cert /trusted/server.pem` configures only the connection; the PEM must come from a trusted source. Then authenticate, inspect `resources --group all --format table`, and use `configure` to complete resource group, image, GPU model, SSH fingerprint and storage. Full `init` is also available. Users supply their own account; passwords use hidden input and optional system credential storage. Never put credentials in specs, command arguments, logs or source control.

## Choose the workflow

- Development/debugging or a short validation: generate with `job --script /absolute/main.py --minutes 30 --out job.json -- ...`; inspect offline `plan --spec job.json`, then `plan --spec job.json --online`. Offline plans do not establish live availability. Execute only the user's authorized inputs, parameters, resources and outputs, presenting a concrete plan before substantial compute.
- Formal training: use `job --purpose training ...` then `task-guide --spec training-job.json --out task-guide.md`. This exports a web-form guide only. The manual expects debugged code to run through task management; native GPU API submission remains disabled. Do not route long training through development environments or guess API fields.

## Run, observe and verify

1. Choose master or, with explicit user intent, `ssdwork`; SSD is charged and group-quota limited. Do not silently migrate data to it. Runtime must verify the configured mount before executing. `storage-check --run-id ID` checks an existing environment only; `df` does not prove group quota is available.
2. Use `run --spec job.json --execute` for foreground control, or `submit --spec job.json --execute` with a system credential store for a detached local controller. The client machine must remain powered and connected. Execution rechecks CPU/GPU availability together on one node; a plan is not a reservation.
3. Other agents sharing the profile can inspect `runs`, `status`, `logs`, `metrics` or read-only `watch`. Same ID/spec is reused; changed code or parameters need a new ID.
4. Check scientific outputs, downloaded hashes, `resource_released=true` and confirmed platform state. Exit code zero is not scientific validation.

## Limits and recovery

- Development environments have a four-hour platform limit. Preparation, transfer, program, collection and cleanup must all fit. The 13,800-second program cap and stage allowances are client safeguards, not additional manual rules. Formal-task limits vary by resource group and require checkpoint planning.
- Store results and environments on verified persistent mounts, using absolute executable paths and timely logs (`python -u` or explicit flushing). Avoid using a GPU environment for lengthy bulk transfers. See the manual map for standalone SFTP caveats.
- An uncertain creation or launch becomes `UNKNOWN`; use `reconcile`, never blindly repeat it. If collection fails, inspect the preserved compute state and recover with `collect` while the environment is accessible. Finish with verified `stop`. Read [recovery details](references/usage.md) before taking over an interrupted controller.
- HTTPS and SSH identities must match trusted configuration. A changed certificate/key requires independent verification; do not disable identity checks or export passwords to bypass a restricted sandbox. Keep account-scoped controller state on a local POSIX filesystem.
- The public preview has not yet passed a complete live GPU workflow on the tested deployment. Preserve that distinction from passing offline tests when reporting readiness.
