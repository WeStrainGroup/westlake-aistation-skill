# Configuration, commands and recovery

## Configuration and authentication

The default profile is `$XDG_CONFIG_HOME/westlake-aistation/profile.json`, or `~/.config/westlake-aistation/profile.json`. Use global `--profile FILE` or `WLAI_PROFILE` to select another file. Paths to a CA/certificate are resolved relative to the profile file. The profile contains connection metadata, never a password or token. State defaults to an account-scoped directory under `$XDG_STATE_HOME`, or `~/.local/state`. Keep controller state on a local filesystem with POSIX lock and atomic rename support.

`init` prompts for nonsecret settings, or imports a completed template with `init --from-file FILE`; it refuses to replace an existing profile. `login` reads a password without echo. `login --remember` stores it only after successful verification in an available secure system credential store. No credential flags or password environment variables are supported. Background `submit` requires stored credentials; foreground `run` can prompt. Noninteractive commands fail instead of waiting for hidden input when a credential store is unavailable. Use the global `--non-interactive` flag in unattended processes.

Treat a trusted certificate/fingerprint as deployment configuration. Obtain it through an administrator or another trusted channel. Do not disable identity verification, accept arbitrary host keys, or copy another user's profile and credentials.

## Job schema

Required fields are `run_id` and a nonempty `argv` list beginning with an absolute executable path. Defaults include 12 CPU cores, one GPU, 60 program seconds, 150 readiness seconds and no explicit memory reservation (`memory_gb=0`). Provide explicit resources for real work. `max_seconds` must be at most 13,800 and program + readiness + cleanup allowance must fit the environment budget. The only supported backend is `environment`.

Optional `image`, `resource_group_id` and `card_type` override profile defaults for that run. Query live resources first and verify the image, software and GPU compatibility. Uploaded scripts may use `{run_dir}` in their destination and commands; path traversal outside the unique run directory is refused. Existing large inputs can be referenced at persistent remote paths. Use a new run ID for changed parameters or script content.

Job specs and program logs are persisted for reproducibility. Do not place API passwords, access tokens or other secrets in job arguments, scripts, filenames, output JSON or logs.

## Commands

| Command | Behavior |
| --- | --- |
| `doctor [--online]` | Check local configuration/dependencies; online mode reads live resources, never allocates |
| `resources --group development` | Read live single-node CPU/GPU availability |
| `plan --spec FILE` | Validate and record the immutable plan without allocation |
| `run --spec FILE --execute` | Foreground controller: allocate, upload, execute, collect and stop |
| `submit --spec FILE --execute` | Start a detached local controller with a secure credential backend |
| `runs` | Read local shared records without authentication |
| `status --run-id ID [--refresh]` | Read status; released runs use the local record unless refresh is requested |
| `logs --run-id ID --lines 100` | Read collected local logs or current remote logs |
| `metrics --run-id ID` | Read GPU utilization and memory while the environment is running |
| `watch --run-id ID --interval 3` | Observe without taking ownership of collection/cleanup |
| `collect --run-id ID` | Retry output collection from an existing environment |
| `stop --run-id ID` | Request cancellation and verify this run's resource release |
| `reconcile --run-id ID` | Resolve an uncertain allocation or runner state without duplicate execution |
| `env-create --run-id ID --execute` | Allocate a debugging environment; finish with explicit stop |
| `exec --run-id ID --timeout 60 -- COMMAND...` | Execute a bounded diagnostic command in an existing environment |
| `put/get --run-id ID --local PATH --remote PATH` | Transfer one file through an existing container |
| `native-status/native-logs --id ID` | Read existing native-task records; does not enable native submission |

Global options precede the subcommand, for example `scripts/wlai --profile /private/profile.json status --run-id ID`. A completed program is not equivalent to validated scientific outputs. Check result contents and hashes, then `resource_released=true` and the confirmed platform state. Exit code 2 represents a failed, uncertain or incomplete managed run; exit code 1 represents an operation error.

## Recovery

One controller owns each run. Other agents should observe through the shared profile/state; do not give each agent a separate state directory. Different portal/account combinations are isolated by default. `--state-dir` is an explicit override for testing or migration and does not authorize cross-account operations.

An allocation request timeout is ambiguous: the cluster may already have created it. Use `reconcile` to find the uniquely named environment and verify ownership, then resume the original plan. Never blindly repeat a creation POST. An ambiguous runner launch also needs inspection of the durable remote status; do not start a second copy merely because a local PID is missing.

If computation completed but downloading failed, inspect the stored compute result, retry `collect` while the environment is accessible, and `stop` after recovering required outputs. Do not call a collection failure scientific failure or claim data was downloaded without a checksum. Persistent files remain on the cluster after stopping, but container SSH is unavailable until an environment is available again. Inspect recorded errors before starting any new environment.

Short network interruptions are retried only within bounded deadlines. SFTP uses an inactivity timeout, reduced to the monitoring time remaining; it does not reset the run deadline. The remote watchdog starts with the child process, so a slow status write does not extend the child's runtime. Remote JSON status uses flush and atomic replacement, without mandatory storage `fsync`; this provides complete snapshots, not a power-loss durability guarantee. Shared-storage metadata operations can still be slow.

A client shutdown or sustained outage can prevent cleanup. On reconnection, inspect `runs`, `status --refresh`, `reconcile`, and `stop` as appropriate. Do not report resources released unless confirmed by the platform. The platform's development-environment lifetime is an independent upper bound and not a guarantee that every output has been safely recovered.

Native training GPU requests were observed to lose GPU allocation on one tested deployment; the root cause remains unresolved. This tool uses the validated development-environment route. Independent storage SFTP, multi-node training, automatic checkpoint segmentation, and cross-machine credential sharing are outside the current supported workflow.
