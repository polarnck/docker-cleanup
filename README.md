# Docker Cleanup

A diagnosis-first Agent Skill for reclaiming Docker disk space without losing data. It looks at containers, images, volumes, networks and build cache, explains what is safe to remove, asks how deep you want to go, and removes only what you approve.

Vibecoded and expanded version of my local skill. Point your agent at it and it
will handle the rest. Remember to always check the output and don't execute anything if you don't understand it - you may lose data.

Built by [@polarnck](https://x.com/polarnck) 

## What it does

- Shows what is using space and how old it is.
- Links containers to images, volumes, networks and compose projects.
- Explains why something is safe, costs time, needs a decision, or is off-limits.
- Asks for depth, age and permission to inspect volumes.
- Builds an exact plan, re-checks every object, then removes it.
- Measures disk before and after and keeps a log of what happened.

## How it works

1. **Diagnose.** It reads Docker state without changing anything: containers, images, volumes, networks, build cache, compose projects and bind mounts.
2. **Explain.** Every object gets a verdict: safe, costs time, needs a decision, or is protected. The report shows the evidence and the host path.
3. **Ask.** It asks for depth, age and permission to inspect volume contents. Nothing is removed yet.
4. **Plan.** It turns your answers into an exact list of objects and commands. You can deselect items or add more.
5. **Apply.** It re-checks the Docker context, daemon, object ID, state and usage immediately before each removal. Anything that changed is skipped.
6. **Verify.** It measures disk before and after, writes a log, and shows restore hints.

A preset is not consent: choosing safe, standard or deep only builds a plan.

## Quick start

Copy this folder to your client's skills directory. Keep the directory name `docker-cleanup`.

Claude Code:

```bash
mkdir -p ~/.claude/skills
cp -r docker-cleanup ~/.claude/skills/
```

Codex / Cursor:

```bash
mkdir -p ~/.agents/skills
cp -r docker-cleanup ~/.agents/skills/
```

OpenCode:

```bash
mkdir -p ~/.config/opencode/skills
cp -r docker-cleanup ~/.config/opencode/skills/
```

For any other client, put the folder where it discovers skills, or point it at `SKILL.md`. No `pip install`, no virtualenv.

Then ask:

```text
Docker is taking 80 GB. What can I safely remove?
```

```text
Clean Docker, but keep the database volumes.
```

## What the agent asks

1. **Depth** — safe, standard, deep, or custom.
2. **Age** — 7, 30, 90 days, all, or a custom threshold.
3. **Volume preview** — all unused, selected volumes, or no preview.

A preview is a short-lived read-only container. Without consent, volume contents stay unknown and volumes are treated as a decision, not as empty.

## Good to know

- Nothing is removed before you approve the exact plan.
- Data volumes are never removed by a preset.
- Running containers, in-use volumes and built-in networks are protected.
- Images used by stopped containers are left for a second pass.
- The tool never uses `docker system prune`, `docker rm -v`, `docker rmi -f`, `docker volume prune -a` or `sudo`.
- Remote Docker contexts need `--allow-remote`.
- `docker system df` "Reclaimable" is an estimate. The report measures `df` before and after.
- Environment values are never printed or stored; logs contain object names and host paths.

## Requirements

- Docker CLI 20.10+ with a reachable daemon.
- Python 3.9+ (standard library only).
- Linux, macOS, or Windows with Docker Desktop / WSL2.

## Layout

| Path | What |
|---|---|
| `SKILL.md` | Agent instructions and the full flow |
| `scripts/docker_cleanup.py` | Thin CLI entry point |
| `scripts/docker_cleanup_lib/` | Implementation modules; see `scripts/docker_cleanup_lib/README.md` for the module map and data flow |
| `references/` | Safety model, depth rules, troubleshooting, authoring checklist |
| `assets/report-template.md` | Report skeleton |

The full CLI reference is in `SKILL.md`.

## Example session

This is a simulated session with fictional objects. No real Docker daemon is involved.

### 1. Audit

```text
You: Docker is taking too much space. What can I safely remove?
```

The agent runs `doctor` and `audit`. It shows a read-only report:

| Verdict | Size |
|---|---|
| **SAFE** | 20.80 GB |
| **COST** | 10.80 GB |
| **DECISION** | 77.00 GB |
| **PROTECTED** | 12.50 GB |

#### Depth preview at 30 days

| Depth | items | Size |
|---|---|---|
| safe | 3 | 19.60 GB |
| standard | 7 | 20.80 GB |
| deep | 12 | 24.90 GB |

#### Containers

| Name | image | Size | State | Activity | Verdict | Reason |
|---|---|---|---|---|---|---|
| `shop-api` | `shop/api:1.0` | 0 B | running | running | PROTECTED | service is running |
| `shop-worker` | `shop/worker:2.0` | 5 MB | exited | 12 d | SAFE | compose config exists |
| `legacy` | `shop/legacy-runner:1.4` | 2 MB | exited | 40 d | SAFE | compose config exists |
| `old-job` | `shop/job:3.0` | 114 MB | exited | 64 d | COST | writable layer above 1 MB |
| `tmp-anon` | `shop/api:1.0` | 1 MB | exited | 31 d | COST | anonymous volume |

#### Images

| Name | Size | used by | Verdict | Reason | Evidence |
|---|---|---|---|---|---|
| `shop/api:1.0` | 480 MB | `shop-api`, `tmp-anon` | PROTECTED | used by a running container | - |
| `shop/legacy:2.0` | 1.2 GB | - | SAFE | registry digest matches | digest match |
| `<none>` | 780 MB | - | SAFE | dangling image | - |
| `alpine:latest` | 7 MB | - | COST | base image you may need later | - |
| `shop/old:1.0` | 300 MB | - | COST | registry digest differs | digest differs |

#### Volumes

| Name | Size | Host path | Project | used by | Activity | Verdict | Reason |
|---|---|---|---|---|---|---|---|
| `shop_db` | 12.4 GB | `/var/lib/docker/volumes/shop_db/_data` | shop | `shop-api` | 8 h (approx.) | PROTECTED | volume is used by a running container |
| `shop_uploads` | 38.2 GB | `/var/lib/docker/volumes/shop_uploads/_data` | - | - | 5 d (approx.) | DECISION | volume name suggests application data |
| `node_modules` | 4.1 GB | `/var/lib/docker/volumes/node_modules/_data` | - | - | 20 d (approx.) | COST | volume name suggests a rebuildable cache |
| `empty_vol` | 0 B | `/var/lib/docker/volumes/empty_vol/_data` | - | - | unknown | DECISION | unused volume with unknown content |

#### Networks

| Name | driver | scope | Verdict | Reason | used by |
|---|---|---|---|---|---|
| `shop_default` | bridge | local | PROTECTED | network is used by a running container | `shop-api` |
| `legacy_default` | bridge | local | SAFE | network is attached only to stopped containers | `legacy` |
| `stale_net` | bridge | local | SAFE | unused non-builtin network | - |

#### Build cache

| bucket | Size |
|---|---|
| older than 7 d | 22.10 GB |
| older than 30 d | 18.80 GB |
| older than 90 d | 9.40 GB |
| all reclaimable | 29.30 GB |

> 269 / 0. Cache is rebuildable; the cost is build time.

### 2. Questions

```text
┌─ Cleanup depth ──────────────────────────────────────┐
│ ○ Safe      cache, dangling images, unused networks  │
│ ● Standard  + recreatable containers, images         │
│ ○ Deep      + all stopped containers, all cache      │
│ ○ Custom    choose items                             │
└──────────────────────────────────────────────────────┘

┌─ Age threshold ──────────────────────────────────────┐
│ ○ 7 days    ● 30 days    ○ 90 days    ○ all    ○ custom │
└──────────────────────────────────────────────────────┘

┌─ Volume preview ─────────────────────────────────────┐
│ ● No, classify without touching volumes              │
│ ○ Yes, all unused volumes                            │
│ ○ Yes, selected volumes                              │
└──────────────────────────────────────────────────────┘
```

The user selects **Standard / 30 days / No preview**.

### 3. Plan

```text
## Docker cleanup plan
Depth: standard · Age: 30 days · Context: default

| id | Kind | Name | Size | Verdict | Reason | Approved | confirm | Host path |
|---|---|---|---|---|---|---|---|---|
| container:8f2a1c9d4b7e | container | shop-worker | 5 MB | safe | compose config exists | yes | - | - |
| container:3c1b5e8a2d6f | container | legacy | 2 MB | safe | compose config exists | yes | - | - |
| network:71d0a4c2b9e1 | network | legacy_default | 0 B | safe | network attached only to stopped containers | yes | - | - |
| network:9a44f1d7c3b2 | network | stale_net | 0 B | safe | unused non-builtin network | yes | - | - |
| image:ab12cd34ef56 | image | shop/legacy:2.0 | 1.2 GB | safe | registry digest matches | yes | - | - |
| image:cd34ef56ab12 | image | <none> | 780 MB | safe | dangling image | yes | - | - |
| cache:30d | cache | build-cache older than 30 days | 18.8 GB | safe | build cache | yes | - | - |

Total: 7 items, ~20.8 GB estimated.
```

**What stays**

- 11 running containers, including `shop-api`
- 5 data volumes, including `shop_db` and `shop_uploads`
- base images (`alpine`, `nginx`, `postgres`)
- images used by stopped containers (second pass)
- all uninspected volume contents

**Commands**

```bash
docker rm 8f2a1c9d4b7e
docker rm 3c1b5e8a2d6f
docker network rm 71d0a4c2b9e1
docker network rm 9a44f1d7c3b2
docker rmi shop/legacy:2.0
docker rmi cd34ef56ab12
docker buildx prune --force --filter until=2026-10-07T12:00:00Z
```

### 4. Apply

```text
You: apply
```

```text
## Apply result

| id | name | status | size | reason |
|---|---|---|---|---|
| container:8f2a1c9d4b7e | shop-worker | Removed | 5 MB | |
| container:3c1b5e8a2d6f | legacy | Removed | 2 MB | |
| network:71d0a4c2b9e1 | legacy_default | Removed | 0 B | |
| network:9a44f1d7c3b2 | stale_net | Skipped | 0 B | network is used by a container |
| image:ab12cd34ef56 | shop/legacy:2.0 | Removed | 1.2 GB | |
| image:cd34ef56ab12 | <none> | Removed | 780 MB | |
| cache:30d | build-cache older than 30 days | Removed | 18.8 GB | |

The `legacy_default` network was removed after its stopped container.

- Removed: 6
- Skipped: 1
- Failed: 0
- Reclaimed (sum of removed items): 20.80 GB
- Disk: 118.20 GB -> 138.60 GB
- Log saved to: ~/.local/state/docker-cleanup/apply-20261106-121500.md
```

**Restore hints from the log**

```text
- shop-worker: docker compose -f /srv/shop/docker-compose.yml up -d worker
- legacy: docker compose -f /srv/legacy/docker-compose.yml up -d web
- shop/legacy:2.0: docker pull shop/legacy@sha256:ab12cd34ef56...
```

Images used by stopped containers are intentionally left for a second pass. After the containers are gone, run `audit` and `plan` again to classify them with real registry evidence.
