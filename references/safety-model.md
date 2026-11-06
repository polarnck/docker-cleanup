# Safety model

This document explains **where Docker stores data**, which operations can destroy it, and what evidence the skill needs before calling anything safe.

## 1. Where the data lives

| Location | What it is | `docker rm` (no `-v`) | `docker rm -v` | `docker volume rm` | Verdict |
|---|---|---|---|---|---|
| Bind mount (`-v /host/path:/data`) | A normal host directory | Not touched | Not touched | Not applicable | `safe` for the container; the host path is the user's responsibility |
| Named volume (`-v name:/data`) | Docker-managed directory under `/var/lib/docker/volumes` | Survives | Survives | Deleted | `safe` for the container, `protected` for the volume while in use |
| Anonymous volume (64-hex name) | Docker-managed, no stable name | Survives, becomes orphaned | Deleted | Deleted by name | `cost` for the container, `decision` for the volume |
| Container writable layer | Files created after start, not on a mount | Deleted | Deleted | Not applicable | `cost` if > 1 MB, otherwise `safe` |
| Build cache | Intermediate build layers | Not touched | Not touched | Not touched | `safe` (cost: rebuild time) |
| Image layers | Read-only layers referenced by images | Not touched | Not touched | Not touched | Depends on registry/build evidence |

The writable layer is the most commonly forgotten location: a database running without a volume writes into the container layer, and `docker rm` destroys it. The `writable_layer` verdict is applied when a stopped container has no mounts. When it has a bind or named-volume mount, that mount is treated as the data channel and the container is `safe` to remove, but anything written only to the container layer is still lost; the report prints the writable size so the user can see it.

## 2. Verdicts

| Verdict | Meaning | May a preset remove it? |
|---|---|---|
| `safe` | No data loss and no service breakage; recreatable from a registry, compose file, host bind or rebuild | Yes, after the plan is confirmed |
| `cost` | No data loss, but time or bandwidth is lost (re-pull, rebuild, writable layer) | Yes, consciously; `standard` includes only registry-identical images |
| `decision` | Looks like user data, or the content is unknown | **No.** Needs `--allow-decision`, `approved: true` and `confirm == name` |
| `protected` | A running service, a volume in use, a built-in network, a swarm resource | **No.** It never enters a plan; `apply` refuses if one is injected |

## 3. Evidence the audit uses

- `docker ps -a -s` for container state, image, mounts and writable size.
- `docker inspect` for `FinishedAt`, restart policy, compose labels and mounts.
- `docker system df -v` for image/volume sizes and volume `Links`.
- `docker volume inspect` for the **host mountpoint** and creation time.
- `docker network inspect` for attached containers, driver and scope.
- `docker buildx du` / `docker system df -v` for cache records and `LastUsedAt`.
- `docker buildx imagetools inspect` for the registry digest.
- Compose labels for project, service, config files and working directory.
- A bounded, read-only file walk for volume activity (newest mtime, approximate).

If an evidence source is unavailable, the verdict moves **towards** `decision` or `cost`, never towards `safe`.

## 4. Dangerous commands and what they really do

### `docker system prune -a --volumes`

Banned. It removes all stopped containers, all unused images (not only dangling), all unused networks, all build cache and all **anonymous** volumes. Named volumes survive, but the blast radius is still enormous. The myth that it deletes named database volumes is false; the command is banned because it can remove objects the user forgot about, not because of named volumes.

### `docker volume prune -a`

Banned. Without `-a` it removes only anonymous volumes. With `-a` it also removes named volumes that are not used by a container. `Links: 0` does not mean empty.

### `docker rm -v`

Banned. `-v` removes **anonymous** volumes attached to the container. Named volumes survive. Even so, an anonymous volume can contain data (databases created by an image without a `VOLUME` declaration are rare, but application state in `/tmp` is common), so the project never uses `-v`.

### `docker rmi -f`

Banned. Force-removing an image used by a running container can leave the container in a broken state. The skill removes images by tag or ID without `-f` and re-checks usage first.

### `docker container prune --filter until=7d`

Not used. The filter is relative to **CreatedAt**, not `FinishedAt`. A container created 30 days ago but stopped yesterday would be removed. The skill filters by `FinishedAt` itself and removes explicit IDs.

### `docker image prune -a --filter until=7d`

Not used. The filter is relative to the image **creation** date, not last use. Docker does not store "last used" for images. Registry evidence decides.

### `docker buildx prune`

Used with the absolute `--filter until=<cutoff_at>` stored in the approved plan; a relative duration is never used at apply time. `docker builder prune` is a fallback when buildx is unavailable. Cache totals can double-count shared records; the report labels them as estimates and measures the real delta with `df`.

## 5. Edge cases

### Compose orphans

A stopped container with `com.docker.compose.project` and an existing `config_files` path is `safe`: `docker compose up -d` recreates it. If the file is missing, the container is `cost` — the configuration is not recreatable automatically. Compose networks follow the same rule when a container provides the evidence; a project-only network with no container is treated as `safe` because compose recreates networks by project name, but the report shows the project so the user can verify.

A network holds no data, but Docker refuses `network rm` while any container is attached, including a stopped one. A network attached only to stopped containers is `safe` / `network_stopped_users`; Standard and Deep add it to the plan only when every attached container is also in the plan, and the container is removed first. If any attached container is running, the network stays `protected`. Images are different: an image used by a stopped container is left for a second pass so its registry evidence can be checked once it is unused.

### Swarm

`docker service ls` images are not visible as ordinary containers. On an active swarm manager, removing images or pruning networks can break services. The audit detects `Swarm.LocalNodeState=active` and warns. On an active swarm, presets exclude **all tagged images** (only dangling images remain eligible), and `apply` skips tagged images even in a custom plan; a custom plan may include them for inspection, but the image is not removed while the swarm is active. Swarm- scoped networks (`ingress`) are protected.

### Docker Desktop (macOS/Windows)

Docker runs inside a VM. Pruning inside the VM reduces `docker system df`, but the VM disk file (`Docker.raw`, `ext4.vhdx`) may not shrink. Do not promise host disk savings. The user may need "Clean / Purge data" in Docker Desktop or a manual compaction step.

### Rootless Docker

The socket is under `$XDG_RUNTIME_DIR/docker.sock` and data under `~/.local/share/docker`. Never use `sudo`; it points at the rootful daemon or fails. Permission errors are reported with an explanation, exit code 2.

### Multi-arch images

`RepoDigests` may contain a platform manifest digest while `buildx imagetools inspect` returns the index digest. A mismatch is not always a different version. The skill marks such comparisons as ambiguous and treats them as `cost`, never `safe`.

### Locally built images

`RepoDigests` can exist for images loaded with `buildx --load` even when the registry has never seen them. Presence of a digest does not prove registry existence. The audit asks the registry; "not found" plus a compose/Dockerfile source becomes `cost` (rebuild), otherwise `decision`.

### Images with multiple tags

`docker rmi <id>` fails with "image is referenced in multiple repositories". Single-tag and dangling images are removed by immutable ID; a multi-tag image is untagged one tag at a time. If another tag remains, apply reports "untagged only" and counts **zero** reclaimed bytes.

### Remote context

`audit`, `plan` and `apply` all require `--allow-remote` for a remote context. `plan` is read-only but still talks to production, so it is not exempt.

### Running containers with `restart: always`

A container can start between the plan and the apply. The apply re-checks the state immediately before `docker rm` and skips anything that is running, paused or restarting.

### Build in progress

`buildx du` marks records `InUse`. The audit warns when a buildkit container or in-use cache is present; the plan skips in-use records and the apply's cache prune keeps them.

### Volumes and `Links: 0`

A volume left by a stopped compose project has zero links and full data. Zero links is not evidence of emptiness. Only a read-only peek that returns no files, or a successful empty check by another trusted source, can produce `safe`.

### Bind mounts

Data on a bind mount is outside Docker. `docker rm`, `rmi` and `prune` do not touch it. The audit still lists every bind mount with its full host path, because the user may be about to delete the host directory manually. Paths under `/tmp` are flagged as lost on host reboot.
