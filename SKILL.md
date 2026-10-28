---
name: westlake-aistation
description: Use a configured Westlake AIStation account to stage and submit native GPU training tasks, debug code, monitor execution, verify outputs and release resources.
---

# Westlake AIStation

Use `scripts/wlai` relative to the installed skill. This community adapter uses AIStation/Kubernetes, not Slurm. Read [usage](references/usage.md) for setup/updates and [manual map](references/manual.md) for platform rules.

Account setup uses direct input. Online commands default to hidden password input and process-memory reuse; do not read Keychain unless the user explicitly chooses `--use-keychain` or `credential_mode=system-store`. Never place passwords in files, environment variables, command arguments or logs. Users supply their own trusted server certificate and SSH fingerprint.

## Native production tasks

1. Stage code, data and dependencies once in verified personal persistent storage. Discover `groups --purpose training`; visible GPU nodes alone do not prove workload permission, quota or same-node capacity.
2. Generate `job --purpose training --remote-script /personal/train.py --minutes N --out training.json -- ...`. Review inputs, parameters, resources, runtime, checkpoint and persistent outputs before substantial compute. One node may request multiple GPUs; verify that the program uses them or provide an explicit launcher in `spec.argv`. Independent tasks are not DDP.
3. Run `task-stage --spec training.json --out training.staged.json --execute` to prepare SHA-256-checked startup files with a temporary 1 CPU / 0 GPU debugging environment and release it. This requires development group/SSH configuration; it does not run training. Inspect `task-plan --spec training.staged.json --online`.
4. Submit `task-submit --spec training.staged.json --execute`. No experimental switch is required. Native execution belongs to the platform and continues after the client disconnects; `task-watch` timeout only ends observation. Group lifetime policies still apply; do not assume all groups allow five days.
5. Use `task-status`, `task-logs`, `task-watch`, `task-reconcile` and `task-stop` with the recorded run ID. Verify hardware receipt, required scientific outputs/checkpoints and platform terminal release. Platform success without matching complete execution evidence is `COMPLETED_UNVERIFIED`; infrastructure success alone does not validate scientific conclusions.

The verified single-node GPU path passed real V100 CUDA computation, persistent result/receipt readback and checksums. Runtime still checks every allocation. Creation payload must **omit taskType**; copying the returned classification or a direct URL query into the request causes CPU data-processing fallback. The launcher avoids the platform's double-quote/newline rewriting. Default 1000-byte command budget is a client safeguard, not an official universal limit; long inline guard commands must use staged files instead.

`task-stop` routes queued/starting cancellation through the unfinished-task cancel API and Running through stop. It never uses finished-history deletion. A deleted task may still report Pending: require deleteFlag plus a complete no-active-pod view before claiming release. Errors trigger a state reread; rejected stops may be explicitly retried, accepted stops are only observed. An unconfirmed stop returns nonzero; `--timeout` bounds confirmation waiting. Preserve local request/attempt evidence.

Unknown creation outcomes require reconciliation before any new POST. Explicit platform parameter rejection is `REJECTED` and does not imply an allocated GPU. Identical run IDs/specs reuse records; changed code/parameters need new IDs. Observers do not launch replacement work. Do not adopt unscoped legacy state automatically.

## Development and storage

Development `run/submit` is for debugging and bounded validation, with a four-hour total lifetime including preparation, transfers, program, collection and cleanup. Its detached controller needs the local machine online and an explicitly selected system credential store; direct-input foreground execution remains available. Formal long training uses native task management.

Verify actual persistent mounts before creating run directories. Master is default; SSD requires explicit intent and has separate quota/charges. Do not create lookalike mount points, modify ACLs, silently migrate data or treat `df` as group quota proof. HTTPS/SSH identity changes require independent verification.

## Shared updates

After every GitHub release, run the installer from the separate clean source checkout, update the installed skill and every known canonical CLI entrypoint together, and compare `--version`, `INSTALLATION.json` revision/content hash. The installed wrapper directory is not the source checkout. Keep immutable versions for active controllers and backups for rollback; preserve private configuration, credentials, certificates, data and legacy receipts. Other agents must reread this skill after upgrade. See [installation and updates](references/usage.md#installation-and-updates).
