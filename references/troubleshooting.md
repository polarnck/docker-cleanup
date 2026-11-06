# Troubleshooting

Every error must be actionable and must not print a stack trace. Exit codes: `0` OK, `2` usage/environment, `3` safety refusal, `4` partial failure, `5` nothing to do.

## "docker: command not found"

The skill needs the Docker CLI, not only the daemon.

- Linux: install `docker-ce-cli` or the distribution package.
- macOS/Windows: install Docker Desktop and make sure `docker` is on `PATH`.
- The CLI is invoked by name; a test double can shadow it by putting a directory first on `PATH`.

## "permission denied" / "Got permission denied while trying to connect"

The daemon socket is usually owned by `root:docker` with mode `660`.

Options:

1. Add the user to the `docker` group and start a new login session: `sudo usermod -aG docker "$USER"`.
2. Use rootless Docker (socket under `$XDG_RUNTIME_DIR/docker.sock`, data under `~/.local/share/docker`).
3. On a system where the daemon is reachable only by root, run the audit as a user with access — **do not use `sudo docker`**. `sudo` changes the daemon root, the context and the file ownership, and this project forbids it.

The tool exits 2 with this explanation. It never escalates privileges.

## Remote context

`docker context show` may point at a staging or production host via `tcp://`, `ssh://` or `DOCKER_HOST`.

- `doctor` always shows the context and a `remote_context` warning.
- `audit`, `plan` and `apply` refuse without `--allow-remote`.
- `plan` is read-only but still contacts the remote daemon, so it is not exempt.
- Never run a cleanup on production because "it is only cache". Build cache and stopped containers can still be production recovery material.

If the user really wants a remote cleanup, use a dedicated context, verify the daemon ID hash in the plan, and keep `--allow-remote` explicit.

## Rootless Docker

Symptoms: socket under `$XDG_RUNTIME_DIR/docker.sock`, data under `~/.local/share/docker`, `docker info` works without sudo but `sudo docker` does not see the same daemon.

The audit detects `SecurityOptions` containing `rootless` and warns. Do not use sudo; it connects to a different daemon (or fails). Volume mountpoints are inside the user's home, so the bounded mtime walk usually works without root.

## Docker Desktop: `docker system df` drops but the host disk does not

Docker Desktop runs a VM. Images, containers and cache live in a virtual disk (`Docker.raw` on macOS, `ext4.vhdx` on Windows). Pruning inside the VM frees space **inside** the VM; the sparse host file may not shrink immediately.

- macOS: Docker Desktop -> Settings -> Resources -> Advanced -> "Disk image size" or Troubleshoot -> "Clean / Purge data".
- Windows: `Optimize-VHD` on the WSL2 virtual disk, or Docker Desktop's purge.
- WSL2: `wsl --shutdown` after pruning can release the VHDX; compaction is not guaranteed.

Do not promise host savings on Docker Desktop. Report the `docker system df` delta and the VM caveat separately.

## WSL2

- The daemon may be inside a separate WSL distribution; `docker context ls` shows which endpoint is used.
- Windows paths appear as `/mnt/c/...` inside WSL and `C:\...` in Docker Desktop labels. The audit uses `os.path` / `os.scandir` and full paths; do not truncate them.
- File mtimes on `/mnt/c` are slow and sometimes coarse. Volume activity is labelled approximate.
- `df` inside WSL reflects the WSL virtual disk, not the Windows host.

## Registry offline / air-gapped

`audit` performs `docker buildx imagetools inspect` for unused non-base images. It is network I/O with a timeout, parallel and cached for one hour in the state directory.

- Use `--no-registry` to skip it; every unchecked image becomes `cost` (`registry_unknown`), never `safe`.
- A registry error is sanitized (no tokens, no credentials) and truncated.
- If buildx is unavailable, the tool falls back to `docker manifest inspect`.
- An image that exists only locally becomes `decision` unless a compose/Dockerfile source proves it can be rebuilt.

## Build in progress

Symptoms: `buildx du` shows `InUse`, a `buildx_buildkit_*` container is running, or a build is writing cache.

- `doctor` and `audit` warn (`build_in_progress`).
- The plan never selects `InUse` cache records.
- Do not prune cache while a build runs; wait for it to finish.

## `docker system df` "Reclaimable" does not match reality

`Reclaimable` is an estimate. It counts shared layers once, ignores volumes that are unused but important, and may round. Real reclaimed space is measured with `df`/`shutil.disk_usage` before and after. The apply log records both numbers.

## `Links: 0` does not mean the volume is empty

A volume left by a stopped compose project has zero links and full data. The audit never treats zero links as proof. Only a read-only peek that lists no files, or a successful equivalent check, can produce `safe`. Otherwise the volume is `decision`.

## `docker rm -v` semantics

`-v` removes **anonymous** volumes attached to the container. Named volumes survive. The project never passes `-v`, because an anonymous volume can still contain data and becomes hard to find later. The correct workflow is: inspect the mounts, remove the container without `-v`, then decide about each volume by name.

## `docker volume prune -a` semantics

Without `-a`, `docker volume prune` removes only anonymous volumes. With `-a`, it also removes named volumes not used by any container. Both are banned here: the tool removes explicit names one by one.

## Multi-arch digest mismatch

`RepoDigests` may hold a platform manifest digest, while `buildx imagetools inspect` returns the index digest. A mismatch is not automatically a different version. The tool marks it `registry_digest_ambiguous` and keeps it `cost`.

## `docker rmi` only untagged the image

An image with multiple tags is not deleted until its last tag is removed. The plan untags a multi-tag image one tag at a time; single-tag and dangling images are removed by immutable ID. Apply reports the result and checks whether the image ID still exists. If another tag remains, the image stays.

## Windows paths and encoding

- Use `pathlib` / `os.path` and pass argv lists; never build a shell string.
- Console output is UTF-8 with `errors="replace"`; reports remain readable on `cp1252`.
- Compose `config_files` may contain commas in a path. The audit splits on commas because that is what Docker writes; verify unusual paths manually.
- Volume mountpoints use `C:\...`; the Markdown report keeps the full path.

## Timeouts and a hung daemon

Every Docker call has a timeout (60 s default, 120 s for `system df -v`, 300 s for removals, 25 s for registry checks). A timeout becomes a readable error, not a stack trace.

If the daemon hangs:

1. `docker version` and `docker info` to confirm it responds.
2. Check disk space and the daemon log.
3. Restart the daemon only with the user's permission.
4. Do not raise the timeout before diagnosing the cause.

## State directory and logs

Default:

- Linux: `$XDG_STATE_HOME/docker-cleanup` or `~/.local/state/docker-cleanup`
- macOS: `~/Library/Application Support/docker-cleanup`
- Windows: `%LOCALAPPDATA%\docker-cleanup`

Override with `--state-dir`. The directory contains:

- `registry-cache.json` (one hour TTL, references only);
- `last-audit.json` (peek evidence reused by `plan`);
- `apply-*.json` and `apply-*.md` (before/after, results, restore hints).

Logs may contain container names and host paths. They do not contain `Config.Env` values. Delete the directory to remove them.

## Exit code 5 is not an error

`5` means "nothing to do": no approved items, or every item was already gone. A second `apply` on the same plan returns 5. A partial failure returns 4 and the log lists which items failed.

## Selected-volume preview

When the user consents to inspect only some unused volumes, use the repeatable flag:

```bash
python3 docker_cleanup.py audit --peek-volume uploads --peek-volume empty_vol
```

Only those volumes are passed to the helper container. A used volume or an unknown name produces a warning and is not inspected. `--peek` without `--peek-volume` still means "all unused".

## State directory permissions

On POSIX the state directory is created `0700` and JSON/Markdown logs are `0600`. They may contain container names and host paths. Delete the directory to remove them. On Windows the mode bits are not meaningful; use the OS ACLs.

## Depth preview differs from the plan

`audit --older-than DAYS` (default 7) only controls the audit's depth preview. `plan` always requires an explicit `--older-than`, because the preview is not a commitment. If the preview and the plan differ, check the age you passed to each command.

## A decision volume is skipped even with `--allow-decision`

The gate needs all three:

1. `approved: true` and `selected: true` in the plan,
2. `confirm` exactly equal to the volume name,
3. `--allow-decision` on the command line.

Set the confirmation explicitly:

```bash
python3 docker_cleanup.py select --plan plan.json --include 'volume:abc123=my_volume'
```

Without the `=my_volume` part the confirm field stays empty and the item is skipped by design.
