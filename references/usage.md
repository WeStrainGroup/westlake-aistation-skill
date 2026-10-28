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

## Connection first, execution settings second

Default profile: `$XDG_CONFIG_HOME/westlake-aistation/profile.json`, or `~/.config/westlake-aistation/profile.json`. Global `--profile FILE` or `WLAI_PROFILE` selects another file. Paths to a CA/certificate resolve relative to that profile. The profile contains connection metadata, never passwords or tokens. State is isolated by portal/account under `$WLAI_STATE_ROOT` when set, otherwise `$XDG_STATE_HOME/westlake-aistation` or `~/.local/state/westlake-aistation`; use a local POSIX filesystem with locks and atomic rename.

```bash
scripts/wlai init --connection-only --server-cert /absolute/trusted-server.pem
scripts/wlai login --remember
scripts/wlai resources --group all --format table
scripts/wlai configure
scripts/wlai doctor
```

The connection-only profile permits login and resource discovery without already knowing group IDs or an image. `configure` completes execution settings. Full `init` combines the setup choices; `init --from-file FILE` imports a completed nonsecret template and refuses to replace an existing profile. `--server-cert` derives the HTTPS pin from a single server PEM supplied through a trusted channel; it does not retrieve and trust a network certificate automatically. Obtain the container SSH fingerprint independently as well.

`login` verifies a freshly hidden password; `--remember` optionally saves it in an available secure system credential store. Without storage, each independent online command can prompt; foreground `run` covers its whole controller session with that credential. Background `submit` requires a reusable system-store credential. No password flags or password environment variables are supported. Global `--non-interactive` fails instead of prompting when a stored credential is unavailable. Stored configuration, credentials and run records do not belong in source control.

## Storage selection

The manual's master mount is `/<account>` and SSD mount is `/ssdwork/<account>`. Full setup derives a personal run root from the selected mount. Master is the default. Choosing SSD must be deliberate: it has group quota and separate charges, with no free capacity. Custom storage requires an explicitly configured real mount; a directory merely named “persistent” does not establish persistence.

Runtime verifies mount evidence before starting work. On an existing debugging environment, `storage-check --run-id ID` reports the mount, a bounded small write, latency and `df` information without allocating another environment. A failed write can result from quota, permissions or storage service trouble; filesystem free space is not proof of remaining group quota. Do not automatically switch mounts, copy datasets, or broaden filesystem permissions to make a check pass.

Keep code, environments, checkpoints and results on verified mounts. `/tmp`, `/root` and other container-only locations are not substitutes for persistent outputs. For large data, prepare transfers before holding a GPU. The manual's browser upload limit does not impose a universal SFTP size cap; container transfer suitability depends on data volume, connection speed and remaining development lifetime. Independent storage SFTP is not automated by this client.

## Generate and inspect a debugging job

```bash
scripts/wlai job --script ./main.py --minutes 30 --out job.json -- --input /remote/input.dat
scripts/wlai plan --spec job.json
scripts/wlai plan --spec job.json --online
scripts/wlai run --spec job.json --execute
```

The generator writes a spec only. Offline `plan` validates local inputs, hashes and upload sizes and reports the complete stage budget; it neither queries live resources nor proves remote readiness. `--online` adds a resource query. Actual execution always rechecks availability and the configured storage mount. CPU/GPU requirements must fit together on one node; the user's total quota and actual memory needs remain relevant.

For foreground work use `run`; for a detached controller with secure stored credentials use `submit`. Neither allocates without `--execute`. Keep the controlling machine powered and connected for monitoring, collection and cleanup.

## Job fields

Job JSON uses a unique `run_id`, explicit resources and a nonempty `argv` list beginning with an absolute executable path. Use the personal Conda interpreter's absolute path where appropriate; `python -u` or explicit flushing helps timely logs. `memory_gb=0` means no explicit memory reservation, not zero memory use or a guarantee of unlimited usable memory.

The preparation allowance includes readiness and uploads. Default client budgets are 300 preparation seconds (including 150 readiness seconds), 120 collection seconds and 60 cleanup seconds. Together with program time, all stages must fit the configured development lifetime, never more than four hours. Program time also has a client cap of 13,800 seconds, so selecting that maximum may require shorter stage budgets than the defaults. These values are conservative client choices, not additional rules in the official manual. Execution uses one overall allocation deadline rather than restarting a fresh lifetime at each stage.

Optional `image`, `resource_group_id` and `card_type` override profile defaults for a specific run. Query current metadata and check compatibility; an old saved image name may no longer exist. Upload destinations and commands may use `{run_dir}`, which expands to the unique run directory on the configured mount. Uploaded content is hashed and cannot traverse outside that directory. Existing remote inputs can be referenced directly without repeated upload.

Requested downloads are explicit:

```json
{
  "downloads": [
    {"remote": "{run_dir}/result.json", "local": "/absolute/project/results/result.json"}
  ]
}
```

Hashes are verified during transfer; different existing local files are not replaced. Changing parameters or script contents requires a new run ID. Specs and program logs persist locally for reproducibility: never put API passwords or other secrets in script arguments, code, filenames or output logs.

## Native task management: plan and bounded diagnostics

Formal training belongs in task management after debugging. Code, inputs and environments must already be on the selected persistent mount; native tasks do not use a development container for uploads. Use absolute image/personal-environment interpreters and scripts. Configure training without requiring a development SSH fingerprint:

```bash
scripts/wlai groups --purpose training
scripts/wlai configure --purpose training
scripts/wlai job --purpose training --remote-script /personal/train.py \
  --minutes 1440 --out training-job.json -- --output '{run_dir}/results'
scripts/wlai task-plan --spec training-job.json
scripts/wlai task-plan --spec training-job.json --online
```

The job is a native spec, not a platform JSON import. `task-plan` does not submit; online planning checks training labels (`usual,train`) and same-node capacity. `groups --purpose development` uses `develop,usual`. Node visibility does not establish workload permission, current quota or a reservation. One node may request multiple GPUs. The generator starts one `python -u` process; it does not configure torchrun, a distributed backend or rank variables. Confirm that the program uses all requested GPUs, or explicitly review a launcher command in `spec.argv`. Independent tasks are not multi-node DDP. This preview has no batch dispatcher.

**New native submissions remain experimental.** A live request made by the official form was accepted but normalized to CPU/zero GPU. The root cause on this deployment is unresolved. Default `task-submit --spec FILE --execute` refuses creation. Only an authorized, bounded diagnostic may use `--experimental-native`; this opt-in is not evidence of production support. `--queue` additionally permits a compatible busy group to queue; it does not permit an impossible single-node request. Unknown creation outcomes must be reconciled, never blindly retried.

The default submitted command embeds a compressed standard-library startup guard. Some deployments reject even that command with `IRESOURCE_STRING_LENGTH_ILLEGAL`. In that case, stage the exact `scripts/native_runner.py` from the reviewed source checkout in verified personal persistent storage once, and add `guard_script` (its absolute remote path) to the spec. `guard_script_sha256` is fixed to the bundled source SHA-256; an explicitly different value is rejected. The short launcher reads that file and verifies its bytes before executing it. Use versioned/hash-named paths, preserve guards referenced by queued/running tasks, and never overwrite an active guard during upgrade. This staging is explicit; native submission does not allocate a debugging environment or upload files for you.

For long argument lists, also set an absolute personal `guard_manifest` path. The plan then reports `guard_mode=staged-files`, `guard_manifest_content` and `guard_manifest_sha256`. Before submitting, write the content as `json.dumps(content, sort_keys=True, separators=(',', ':')).encode('utf-8')` with **no trailing newline**, verify the reported SHA-256, and stage those bytes at `guard_manifest`. The short launcher verifies both complete files before loading the runner. Keep each manifest immutable for its queued/running task; after changing arguments use a new run ID and manifest path.

`task-plan` reports `guard_mode`, `guard_script_sha256` and `command_bytes`. Optional positive `command_max_bytes` enforces a locally configured deployment limit before any API call; the client does not guess a universal server limit.

The startup guard checks the real persistent mount and GPU count/model before starting user code, keeps separate attempt receipts in `{run_dir}/attempts`, and bounds the program with an independent timer. Project-specific environment variables belong in `spec.env`; the client does not set framework compatibility workarounds globally. The command/request snapshot is saved for reconciliation after a client upgrade. A submitted GPU resource mismatch stops only the newly created task; observers report evidence and do not silently submit replacement work.

A platform `Succeeded` without a matching successful terminal guard receipt becomes `COMPLETED_UNVERIFIED`. Multiple unbound attempts cannot establish a unique successful run. Guard success validates execution infrastructure, not scientific output; validate expected metrics/checkpoints separately. `resource_released` for native tasks records platform terminal status as its evidence, not an independent physical cluster-usage measurement.

`task-runs` is local; other commands use the recorded task identity and owner:

```bash
scripts/wlai task-status --run-id RUN_ID
scripts/wlai task-logs --run-id RUN_ID
scripts/wlai task-watch --run-id RUN_ID --timeout 600
scripts/wlai task-reconcile --run-id RUN_ID
scripts/wlai task-stop --run-id RUN_ID
```

Once accepted, the platform runs the task independently of the client machine. A local watch timeout does not cancel it. Outputs stay on persistent storage; native automatic downloads are not implemented. The guard's program budget does not override group policies: verify the group's actual lifetime, optionally set `group_max_seconds`, and include guard/receipt allowances. Do not assume every group allows five days. Checkpoint/resume belongs to the scientific program.

`task-guide --spec FILE --out FILE` still exports a manual web-form checklist only. Its direct science command does not include the native guard or hard timeout; prepare and review those separately before a manual submission. Manual submission also requires actual GPU and output verification. Do not replace long training with successive four-hour debugging environments to bypass the unsupported native interface.

## Commands

| Command | Behavior |
| --- | --- |
| `init [--connection-only] [--server-cert FILE]` | Create personal configuration; no password is saved |
| `login [--remember]` | Verify hidden credentials; optionally use the system credential store |
| `configure --purpose development|training` | Complete the selected workload settings; training needs no development SSH setup |
| `doctor [--online]` | Check local setup; online mode reads resources without allocating |
| `resources --group all --format table` | Discover accessible group IDs, models and node capacity |
| `job --script FILE --minutes N --out FILE -- ...` | Generate a debugging spec from a script and arguments |
| `plan --spec FILE [--online]` | Offline plan by default; optional live resource query |
| `groups --purpose development|training` | List workload-authorized resource groups |
| `task-plan --spec FILE [--online]` | Inspect a staged native task without submitting |
| `task-submit --spec FILE --execute --experimental-native` | Explicit bounded diagnostic; native compatibility remains unverified |
| `task-runs` / `task-status --run-id ID [--cached]` | Native registry or current task observation |
| `task-logs/task-watch --run-id ID` | Native logs/read-only monitoring; local timeout does not cancel |
| `task-stop/task-reconcile --run-id ID` | Stop this task or reconcile an uncertain creation |
| `task-guide --spec FILE --out FILE` | Export manual web-form instructions without submitting |
| `run/submit --spec FILE --execute` | Foreground/background development controller |
| `runs` | Local shared records without authentication |
| `status --run-id ID [--refresh]` | Read state; released runs use local records unless refresh is requested |
| `logs --run-id ID --lines 100` | Collected local logs or current remote logs |
| `metrics --run-id ID` | GPU utilization and memory while an environment runs |
| `watch --run-id ID --interval 3` | Observe without owning collection or cleanup |
| `storage-check --run-id ID` | Check an existing environment's configured mount and small-write behavior |
| `collect --run-id ID` | Recover outputs after confirmed computation completion |
| `stop --run-id ID` | Cancel/stop this run and verify resource release |
| `reconcile --run-id ID` | Resolve uncertain allocation or launch without duplicate execution |
| `env-create --run-id ID --execute` | Allocate a debugging environment; finish with explicit stop |
| `exec --run-id ID --timeout 60 -- COMMAND...` | Bounded diagnostics in an existing environment |
| `put/get --run-id ID --local PATH --remote PATH` | Transfer one file through an existing container |
| `native-status/native-logs --id ID` | Read existing native-task records; does not enable native submission |

Global options precede the subcommand. For example, `scripts/wlai --profile /private/profile.json status --run-id ID`. A zero program exit code does not validate scientific output. Confirm required outputs and hashes, then `resource_released=true` and the platform's final state. Failed, uncertain or incomplete managed runs and operation errors return nonzero exit status.

## Recovery

One controller owns a run. Agents sharing a profile should share the default account-scoped state, rather than each inventing a separate `--state-dir`. An explicit state override is for deliberate testing or migration and does not authorize cross-account operations.

If allocation times out, the cluster may already have created the environment. Use `reconcile` to identify its unique name and verify ownership; never blindly repeat a creation POST. An uncertain runner launch also needs inspection of the remote state before any further execution. Preserve the original ID and logs while reconciling; use a new ID only for a consciously new attempt.

If computation completed but downloading failed, inspect the stored compute result and retry `collect` while the environment is available, within the remaining lifetime. Do not collect a still-running program as complete. Stop after recovering outputs, or explicitly stop if further recovery is no longer intended. Confirm release; accepting a pause request is not proof. Once stopped, container SSH is unavailable even though persistent data remains on the mounted storage.

Collection failure can deliberately preserve an environment for recovery. Planned GPU-hours are not a billing cap: that environment can remain occupied until confirmed release or the platform lifetime cap. Inspect `resource_released` and use `stop` when recovery is no longer needed.

HTTPS retries apply only to safe reads. SSH/SFTP monitoring reconnects within the original deadline; inactivity timeouts do not create a new lease. The independent remote watchdog starts with the child process, so a slow status write does not extend program runtime. Remote JSON status uses flush and atomic replacement, not mandatory `fsync` or a power-loss durability guarantee.

A client shutdown or sustained outage can prevent immediate collection or cleanup. On reconnection, inspect `runs`, `status --refresh`, `reconcile` and `stop` as appropriate. A platform lifetime cap does not guarantee that every output has been recovered. The current public preview still requires live end-to-end GPU validation on the user's deployment; passing offline tests is a separate claim.
