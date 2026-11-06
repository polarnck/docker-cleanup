# Depth levels, age thresholds and consent

This is the exact contract behind `plan --depth ... --older-than ...`.

## The three questions

The agent must ask all three before building a plan. The answers are not interchangeable: depth says **what categories**, age says **how old**, consent says **whether volume content may be inspected**.

### Q1 - Depth

| Depth | Containers | Images | Volumes | Networks | Build cache |
|---|---|---|---|---|---|
| `safe` | none | dangling only | none | unused, non-builtin | older than threshold |
| `standard` | stopped compose with an existing config file | registry-identical, unused | empty, confirmed by peek | unused compose and plain; networks attached only to planned stopped containers | older than threshold |
| `deep` | all stopped (safe and cost) | dangling, registry-identical, base, registry-different, ambiguous, rebuildable | empty + cache + anonymous (unapproved) | all unused | older than threshold |
| `custom` | user selection | user selection | user selection | user selection | user selection |

Opt-in flags extend a preset:

| Flag | Effect |
|---|---|
| `--include-base-images` | Adds base images (`nginx`, `postgres`, `node`, ...) as approved `cost` items |
| `--include-local-images` | Adds local-only images as unapproved `decision` items |
| `--include-anonymous-volumes` | Adds anonymous volumes as unapproved `decision` items to `safe`/`standard`; `deep` already lists them |
| `--include-data-volumes` | Adds data-like and unknown-content volumes as unapproved `decision` items |

`custom` already contains every non-protected candidate, all unapproved. `deep` lists anonymous volumes but never approves them.

### Q2 - Age threshold

`--older-than DAYS` accepts an integer, `0` or `all` (no filter). The agent asks the user; the default in conversation may be 7 days, but it is never applied silently.

| Object | Timestamp used | Why |
|---|---|---|
| Container | `FinishedAt` | `CreatedAt` would remove a container created long ago but stopped yesterday |
| Build cache | `LastUsedAt` | Real last use, per cache record |
| Network | `CreatedAt` | Docker exposes no last-use timestamp |
| Image | `CreatedAt` | Conservative floor only; registry evidence decides safety |
| Volume | Newest activity (container timestamps + bounded file mtime) | `stat` on the mount root alone misses nested writes; label as approximate |

An unknown timestamp fails the age test for presets. A volume with no known activity is not approved by a preset even if its name looks disposable.

Empty volumes approved by `standard`/`deep` are re-peeked at apply time with the same read-only helper. A volume that gained files after the plan is skipped.

The plan stores an **absolute** `cutoff_at` (`now - DAYS` at plan time). Apply re-checks the current activity against that timestamp. A container that restarted and stopped again after the plan is skipped even if it is still `exited`; cache is pruned with `--filter until=<cutoff_at>`, so a record that was too young at plan time cannot be removed later.

### Q3 - Volume preview consent

Without consent, `audit` does not start any helper container and every unused volume is `decision` (or `cost` for a cache-like name). With consent for all unused volumes:

```bash
python3 docker_cleanup.py audit --peek
```

With consent for selected volumes only:

```bash
python3 docker_cleanup.py audit --peek-volume uploads --peek-volume empty_vol
```

`--peek-volume` is repeatable, implies peeking, and never inspects a volume the user did not name.

The helper is:

```text
docker run --rm --network none --read-only --pull never \
  -v <volume>:/v:ro --entrypoint sh <alpine|busybox> -c '<ls/du/find/stat/printf/wc + text filters>'
```

Rules:

- Only `alpine:latest` or `busybox:latest`, and only if already local. No pull.
- Unique container name, `--rm`, timeout, no network, read-only root.
- No `cat`, `head` on file content, `strings`, `tar` or copy out.
- If the image is missing, preview is skipped with a warning; no fallback pull.
- If preview fails, the volume stays `decision`.

## Exact classification rules

### Containers

| Condition | Verdict | Reason code |
|---|---|---|
| running / paused / restarting | `protected` | `running_service` |
| dead | `cost` | `dead_container` |
| stopped with an anonymous volume | `cost` | `anonymous_volume` |
| stopped, compose project, all config files exist | `safe` | `compose_recreatable` |
| stopped, compose project, config file missing | `cost` | `compose_config_missing` |
| stopped, bind mounts only | `safe` | `bind_only` |
| stopped, named volumes | `safe` | `named_volume` |
| stopped, no mounts, writable layer > 1 MB | `cost` | `writable_layer` |
| stopped, no mounts, writable layer <= 1 MB | `safe` | `no_data` |

A container is removed with `docker rm <id>` (never `-f`, never `-v`). Named volumes therefore survive.

### Images

| Condition | Verdict | Reason code |
|---|---|---|
| used by a running container | `protected` | `running_container` |
| used by a stopped container | `cost` | `stopped_container` |
| dangling (`<none>:<none>`) | `safe` | `dangling_image` |
| base image | `cost` | `base_image` |
| registry digest matches local | `safe` | `registry_identical` |
| registry digest differs | `cost` | `registry_different` |
| registry not found, compose/build source exists | `cost` | `local_build_recreatable` |
| registry not found, no source | `decision` | `local_only_image` |
| registry offline / not checked | `cost` | `registry_unknown` |
| registry answered, local has no digest | `cost` | `registry_unverifiable` |
| multi-arch comparison ambiguous | `cost` | `registry_digest_ambiguous` |

With `--no-registry`, an image that exists only locally is `cost` (`registry_unknown`), not `decision`; `custom` can select it. Run the registry check before removing images you cannot re-pull.

Images used by stopped containers are not registry-checked until the container is handled. `deep` therefore does not approve `stopped_container` images; removing the container first is a separate plan run.

### Volumes

| Condition | Verdict | Reason code |
|---|---|---|
| used by a running container | `protected` | `volume_in_use_running` |
| used by any stopped container | `protected` | `volume_in_use_stopped` |
| unused, peek confirmed empty | `safe` | `volume_empty` |
| unused, data-like name (`db`, `postgres`, `uploads`, ...) | `decision` | `volume_data_name` |
| unused, cache-like name (`cache`, `node_modules`, `target`, ...) | `cost` | `volume_cache_name` |
| unused, anonymous 64-hex name | `decision` | `volume_anonymous` |
| unused, content unknown | `decision` | `volume_unknown` |

A volume used by a stopped container stays `protected` until that container is removed. Even then the volume survives `docker rm` and must be removed by name.

### Networks

| Condition | Verdict | Reason code |
|---|---|---|
| `bridge`, `host`, `none`, `docker_gwbridge` | `protected` | `builtin_network` |
| swarm scope / `ingress` | `protected` | `swarm_network` |
| used by a running container | `protected` | `network_in_use` |
| used only by stopped containers | `safe` | `network_stopped_users` |
| compose network, project config exists or unknown | `safe` | `compose_network` |
| compose network, known project config missing | `cost` | `compose_network_missing` |
| unused, non-builtin | `safe` | `unused_network` |

A network holds no data, but Docker refuses `network rm` while any container is attached, even a stopped one. A network with only stopped users is added to a Standard/Deep plan **only when every attached container is also in the plan**; the container is removed first. If any user is running, the network stays `protected`.

### Build cache

Always `safe` (the cost is rebuild time). `InUse` records are never selected. The age filter uses `LastUsedAt`; an unknown timestamp is treated as old for cache only, because cache is rebuildable.

## Worked examples

### Example 1 - safe, 7 days

User says: "Docker is huge, but I don't want to lose anything."

```bash
python3 docker_cleanup.py audit
python3 docker_cleanup.py plan --depth safe --older-than 7 --out /tmp/plan.json
```

Result: dangling images older than 7 days, unused networks older than 7 days, cache records unused for 7+ days. No containers, no tagged images, no volumes. Typical saving: tens of GB of build cache.

### Example 2 - standard, 30 days

User says: "I want to remove old project containers, but keep databases."

```bash
python3 docker_cleanup.py audit --peek      # after consent
python3 docker_cleanup.py plan --depth standard --older-than 30 --out /tmp/plan.json
```

Result: safe set + stopped compose containers older than 30 days with existing compose files + registry-identical unused images + empty volumes confirmed by peek
+ compose networks. Base images, local-only images and data volumes stay.

### Example 3 - deep, all ages

User says: "This is a disposable CI machine."

```bash
python3 docker_cleanup.py plan --depth deep --older-than all --out /tmp/plan.json
```

Result: every stopped container, every re-pullable image, all empty and cache volumes, all unused networks and all cache. Local-only images and data-like/anonymous volumes remain unapproved. A CI machine can add `--include-anonymous-volumes` and confirm each one, but the tool will not do it by default.

### Example 4 - custom, one volume

```bash
python3 docker_cleanup.py plan --depth custom --older-than 30 --out /tmp/plan.json
python3 docker_cleanup.py select --plan /tmp/plan.json \
  --include 'volume:3f2a9c1d=old_uploads'
python3 docker_cleanup.py apply --plan /tmp/plan.json --yes --allow-decision
```

The `=old_uploads` part is the typed confirmation. Without it the volume is skipped even with `--allow-decision`.

## What the presets deliberately leave behind

- Images used by stopped containers (the container must go first).
- Base images in `safe` and `standard` (deep or an explicit flag).
- Local-only images (decision).
- Every data-like or unknown-content volume, and every anonymous volume without an explicit confirmation.
- Rebuildable cache volumes are included in `deep` as `cost`; `standard` and `safe` leave them. They are never `safe` without a peek that proves emptiness.
- Networks in use by any container, including stopped ones.
- Build cache marked `InUse`.
- Anything on a remote context without `--allow-remote`.
