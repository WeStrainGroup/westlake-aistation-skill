---
name: westlake-aistation
description: Use a configured Westlake AIStation account to inspect CPU/GPU availability, allocate development environments, submit bounded compute jobs, monitor logs, transfer results and release resources through the bundled CLI.
---

# Westlake AIStation

Use `scripts/wlai` relative to this skill directory. This is a community client for AIStation/Kubernetes development environments. It is not a Slurm client or an official university SDK.

For a new installation, follow [setup and command reference](references/usage.md). The user supplies their own portal, account, trusted certificate, SSH fingerprint, resource group, image and persistent directory. No account, password, cluster certificate or deployment profile is bundled. Passwords use hidden terminal input, with optional storage in the user's system credential store. Do not put credentials in job specs, command arguments, logs or source control.

## Run a job

1. Read current resources with `scripts/wlai resources --group development`. CPU and GPU requirements must fit together on one node. Check the project's software, inputs, parameters and persistent outputs.
2. Write a unique job spec and inspect `scripts/wlai plan --spec /absolute/job.json`. For substantial compute, present concrete resources, runtime, command, input and output paths before executing within the user's authorization.
3. Use `scripts/wlai submit --spec /absolute/job.json --execute` with an available system credential store, or foreground `run --spec ... --execute` with hidden password input. The background controller runs on the client machine, which must remain powered and connected to the cluster.
4. Observe with `status`, `logs`, `metrics` or read-only `watch --run-id ID`. Agents using the same profile share account-scoped atomic state. Same ID/spec is reused; changed parameters or scripts require a new ID.
5. Verify the program result, downloaded checksums, `resource_released=true` and final platform state. Exit code zero does not establish scientific correctness.

## Limits and recovery

- The supported execution backend is a development environment. Maximum program time is 13,800 seconds; startup and cleanup must also fit the configured platform limit, capped at four hours. Longer work needs an explicit checkpoint/restart design or an independently validated backend.
- Native training GPU submission is disabled because compatibility was not established on the tested deployment. Do not remove that guard or guess request fields to work around it.
- A creation timeout can leave `UNKNOWN` state. Use `reconcile` to identify the existing resource; never repeat an ambiguous allocation POST. An uncertain runner launch must also be reconciled before further execution.
- If collection fails, inspect the preserved compute state and use `collect`; confirm cleanup with `stop`. Read [recovery details](references/usage.md) before handling interrupted controllers. Never claim release from an accepted stop request alone.
- Keep the same state directory for agents using one profile. Different portal/account pairs have separate namespaces; do not copy another person's state or credentials.
- HTTPS certificates and SSH host keys must match the user's trusted configuration. If identity changes, verify through a trusted channel before updating. Restricted agent sandboxes may require scoped approval for network/system credential-store access; do not export passwords as a workaround.
- Container SSH/SFTP is supported. Independent storage-service access is deployment-specific and is not included.
