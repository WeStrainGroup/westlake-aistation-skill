# Setup, jobs and recovery

## Installation and updates

Keep a source checkout separate from the discoverable skill directory. Install an immutable snapshot with the Python environment containing the requirements:

```bash
python3 -m venv scripts/.venv
scripts/.venv/bin/python -m pip install -r scripts/requirements.txt
scripts/.venv/bin/python scripts/install_local.py \
  --runtime-python "$PWD/scripts/.venv/bin/python"
```

The default destination is `$CODEX_HOME/skills/westlake-aistation` (or `~/.codex/skills/...`), with immutable source versions under `skill-versions`. Optional `--cli-path /absolute/old/wlai` replaces and backs up only that legacy entrypoint. `--profile /private/profile.json` and `--state-root /private/shared-state` configure default wrapper paths without reading or copying profiles, credentials or state; explicit environment values still take precedence. Default state remains account-scoped even with `WLAI_STATE_ROOT`.

After **every GitHub release**, update this local installation and all known canonical CLI entrypoints from the same reviewed commit:

```bash
wlai_source=/absolute/separate/westlake-aistation-skill
cd "$wlai_source"
git status --short
git fetch --tags
git checkout --detach REVIEWED_COMMIT
"$wlai_source/scripts/.venv/bin/python" "$wlai_source/scripts/install_local.py" \
  --runtime-python "$wlai_source/scripts/.venv/bin/python" \
  --cli-path /absolute/old/wlai --profile /private/profile.json \
  --state-root /private/shared-state
~/.codex/skills/westlake-aistation/scripts/wlai --version
/absolute/old/wlai --version
```

Run these update commands from the separate source checkout, never from the installed wrapper directory. The installer rejects a Git checkout with tracked changes or untracked files; resolve them without discarding user work. The content hash identifies the installed publishable file set, while the revision identifies the reviewed clean commit.

Read `~/.codex/skills/westlake-aistation/INSTALLATION.json` and confirm its `revision` matches the release commit and its `source_version` matches both entrypoints. Keep that installation result with the release verification. The installer is local: publishing on GitHub alone cannot update another machine. Other agents should reread the installed skill before their next operation; already loaded conversation instructions do not refresh themselves.

Snapshots and wrappers allow existing controllers to keep using their original code. Do not `git pull` inside a running controller's source directory. A legacy full-source skill directory needs its controllers finished/stopped before `--allow-legacy-migration`; wrapper-only installations can switch without modifying their private data. Previous wrappers and unknown files are backed up outside the discoverable skills directory. To roll back, rerun the installer with `--source-dir /absolute/previous/version` and the same installation options. Preserve profile/certificates/credential stores and run receipts; do not migrate unscoped legacy state automatically.

## Direct account and password input

```bash
scripts/wlai init --connection-only --server-cert /absolute/trusted-server.pem
scripts/wlai login
scripts/wlai resources --group all --format table
scripts/wlai configure --purpose development
scripts/wlai configure --purpose training
```

Initialization asks for your account. Online commands default to direct hidden password input and cache it only in the current process for token refresh. They do not read any OS credential store by default. Passwords are never written to profiles, environment variables, argv or logs. Native tasks continue on the platform without a client credential after submission.

System-store use is optional and explicit: `login --remember` saves after validation; later `--use-keychain` (before the subcommand), or `configure --credential-mode system-store`, enables reading it. `configure --credential-mode prompt` restores direct input. Detached development controllers require that explicit store choice because a background process cannot prompt; use foreground `run` for direct input. Global `--non-interactive` refuses missing credentials rather than reading a password from a pipe or environment.

Profiles contain trusted connection metadata only. Global `--profile` / `WLAI_PROFILE` selects a profile; default is under XDG config or ~/.config. Shared state is portal/account-scoped under `WLAI_STATE_ROOT`, otherwise XDG state. Agents should share that default, not invent independent registries. Preserve old unscoped state without silently adopting it.

## Native task management

Code, environments and large inputs must already exist in persistent storage. Use absolute interpreter/script paths and timely logs (`python -u`). Training groups use `usual,train`, development groups `develop,usual`. Same-node CPU/GPU capacity and account/storage quotas all matter.

```bash
scripts/wlai job --purpose training --remote-script /personal/train.py \
  --gpus 1 --cpus 12 --minutes 720 --out training.json -- --output '{run_dir}/results'
scripts/wlai task-stage --spec training.json --out training.staged.json
scripts/wlai task-stage --spec training.json --out training.staged.json --execute
scripts/wlai task-plan --spec training.staged.json --online
scripts/wlai task-submit --spec training.staged.json --execute
```

Without `--execute`, task-stage is a dry run and does not write the output spec or allocate resources. Execution uses a temporary 1 CPU / 0 GPU development environment to verify the mount and upload two small SHA-256-checked files, then confirms release. It requires development-group and SSH trust configuration. It does not upload research data or run training. The resulting spec uses short persistent paths and is ready for native submission.

Default `command_max_bytes=1000` is a conservative client budget, not a documented universal platform limit. Long commands are rejected locally before network access, with guidance to stage the files. The platform rejected approximately 5.8 KB inline commands on the tested deployment. Users with independently verified different limits may explicitly override the budget. A task-stage spec is normally preferable to inflating it.

The short launcher encodes its code so platform double-quote/newline rewriting cannot damage Python or shell syntax. It verifies complete guard/manifest bytes before compilation, then checks actual GPU count/model and persistent mount before scientific code. Keep staged files immutable while tasks are queued/running. Each attempt has its own receipt in `{run_dir}/attempts`; parameter/code changes require a new run ID.

**Do not include taskType in an ordinary creation payload.** Normal frontend creation omits it; it is returned as a task classification. Passing either string or integer 1 caused the service to save CPU data-processing tasks. Omitting it passed a real 1-V100 CUDA task. The earlier platform-GPU-failure diagnosis was incorrect.

One node may use multiple GPUs. The job generator starts one Python process; it does not build torchrun/DDP or rank variables. Confirm the program actually uses all requested cards, or review an explicit launcher in spec.argv. Independent tasks are not multi-node DDP. Batch dispatch is not implemented.

## Monitoring, results and recovery

```bash
scripts/wlai task-runs
scripts/wlai task-status --run-id RUN_ID
scripts/wlai task-logs --run-id RUN_ID
scripts/wlai task-watch --run-id RUN_ID --timeout 600
scripts/wlai task-reconcile --run-id RUN_ID
scripts/wlai task-stop --run-id RUN_ID
```

A local watch timeout does not cancel the platform task. Native outputs remain on persistent storage; automatic native downloads are not implemented. The platform group's own lifetime still applies, including guard/receipt allowances; verify it rather than assuming five days. Scientific checkpoint/resume is the program's responsibility.

`UNKNOWN` means creation may have happened: reconcile before another creation POST. Mutations use fresh verified TLS connections and are never automatically replayed. An explicit known parameter rejection is `REJECTED`, with no task created; it does not mean a GPU remains occupied. A changed spec needs a new run ID. `COMMAND_MISMATCH` or `RESOURCE_MISMATCH` stops only the newly submitted task; observers merely report remediation requirements.

Platform success without a matching terminal guard receipt is `COMPLETED_UNVERIFIED`. Multiple unbound attempts cannot establish a unique successful execution. Guard success is infrastructure evidence, not scientific validation. Check expected results/checkpoints and their hashes separately. Native `resource_released=true` is based on platform terminal status, not a physical account-wide usage measurement.

`task-guide --spec FILE --out FILE` still exports a manual form checklist only. Its direct science command lacks the native guard and hard timeout; prepare those separately for manual submission.

## Development and storage

```bash
scripts/wlai job --script ./main.py --minutes 30 --out debug.json
scripts/wlai plan --spec debug.json --online
scripts/wlai run --spec debug.json --execute
```

Development environments have a four-hour total lifetime. The 13,800-second program cap and stage budgets are client safeguards; preparation, transfer, execution, collection and cleanup must all fit. Development foreground/background controllers need the local machine available; native platform tasks do not. Use development only for debugging/bounded validation, not successive leases to replace a long native task.

Master storage is default. SSD ssdwork requires explicit intent and has separate charges and group quota. Runtime verifies actual mounts before writing; it does not create a lookalike mount or change ACLs. Filesystem df is not quota evidence. Large data should be staged before holding GPUs; standalone storage SFTP is not automated here.

Development outputs can be listed in downloads for SHA-256-checked collection; different existing files are not overwritten. If collection fails, preserve compute state and recover with collect while accessible, then confirm stop. Deleted is a released terminal state. Cleanup errors do not overwrite the primary failure.

HTTPS and SSH identities must match independently trusted configuration. A changed identity requires verification, not a bypass. Credentials, private profiles, certificates and run records do not belong in source control.
