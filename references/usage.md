# Setup, jobs and recovery

When upgrading from v0.1, finish or stop active controllers with that version first and preserve their receipts. New preparation/collection budgets participate in the immutable plan fingerprint. Existing plans are not silently rewritten; create a new run ID for new work.

## Connection first, execution settings second

Default profile: `$XDG_CONFIG_HOME/westlake-aistation/profile.json`, or `~/.config/westlake-aistation/profile.json`. Global `--profile FILE` or `WLAI_PROFILE` selects another file. Paths to a CA/certificate resolve relative to that profile. The profile contains connection metadata, never passwords or tokens. State is isolated by portal/account under `$XDG_STATE_HOME`, or `~/.local/state`; use a local POSIX filesystem with locks and atomic rename.

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

## Formal training: export a web-submission guide

```bash
scripts/wlai job --purpose training --script ./train.py --minutes 30 --out training-job.json
scripts/wlai task-guide --spec training-job.json --out task-guide.md
```

These commands generate a spec and Markdown checklist only. They do not request resources or submit a native training task. Use the checklist in the platform's task-management form to review resource group, group-specific runtime, image, mount, absolute command, logs, checkpoint and result paths. Training limits vary; many groups allow five days. Do not assume that limit applies to every group or that a successful browser submission proves correct output. Native training GPU API compatibility remains unresolved, so `run/submit` must not silently turn a training plan into a development-environment run.

## Commands

| Command | Behavior |
| --- | --- |
| `init [--connection-only] [--server-cert FILE]` | Create personal configuration; no password is saved |
| `login [--remember]` | Verify hidden credentials; optionally use the system credential store |
| `configure` | Complete execution settings after connection/resource discovery |
| `doctor [--online]` | Check local setup; online mode reads resources without allocating |
| `resources --group all --format table` | Discover accessible group IDs, models and node capacity |
| `job --script FILE --minutes N --out FILE -- ...` | Generate a debugging spec from a script and arguments |
| `plan --spec FILE [--online]` | Offline plan by default; optional live resource query |
| `task-guide --spec FILE --out FILE` | Export formal-training web-form instructions without submitting |
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
