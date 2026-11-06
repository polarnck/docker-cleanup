---
name: docker-cleanup
description: Audit Docker disk usage and safely reclaim space. Always diagnoses first (read-only), shows relationships and evidence, asks for depth, age threshold and volume-preview consent, builds an exact plan, then removes only approved objects with re-validation. Use when the user says "clean Docker", "Docker is taking too much space", "docker system df", "remove unused images/containers/volumes", "prune build cache". Never deletes before explicit approval; presets never remove data volumes.
compatibility: Requires Docker CLI 20.10+ and Python 3.9+. Linux, macOS, or Windows with Docker Desktop. No network access required except optional registry digest checks.
metadata:
  version: "1.0.2"
---

# Docker cleanup

A diagnosis-first workflow for reclaiming Docker disk space without losing data. The CLI is `scripts/docker_cleanup.py` (Python 3.9+, standard library only).

## Core contract

1. **Diagnosis before any question.** Run `audit` first and show the report. The user must see the state before choosing what to remove.
2. **A preset is not consent.** Choosing `safe`, `standard` or `deep` only builds a plan. Nothing is removed until the user confirms that exact plan.
3. **Evidence, not guesses.** Every verdict carries a reason code and evidence. No evidence means `decision`, never `safe`.
4. **Data volumes are never removed by a preset.** They need a separate decision.
5. **Re-validate immediately before every removal.** Context, daemon identity, object ID, state and current usage are checked again in `apply`.
6. **Report facts you do not know.** Registry offline, unknown volume content and approximate activity are stated, not hidden.

If the user asks only "how much space does Docker use?", run `doctor` + `audit` and stop. Do not propose a plan unless they ask.

## Workflow

### 0. Check the environment

```bash
python3 scripts/docker_cleanup.py doctor
```

Show the context name, local/remote, Docker version, storage driver, root dir, swarm state, disk free and warnings. If the context is remote, stop and ask for explicit permission before even auditing: production is not a cleanup target by default.

### 1. Audit (read-only)

```bash
python3 scripts/docker_cleanup.py audit
```

For machine-readable output:

```bash
python3 scripts/docker_cleanup.py audit --json
```

`audit` is read-only. The only optional side effect is `--peek` / `--peek-volume`, which starts a short-lived `:ro` helper container to list file names and sizes in unused volumes. **Never pass them without asking the user first.** Use `--peek-volume NAME` (repeatable) when consent covers only selected volumes.

Audit with registry checks disabled (offline or fast):

```bash
python3 scripts/docker_cleanup.py audit --no-registry
```

### 2. Ask exactly three questions

Ask them in one batch when the question tool is available. Each question needs context, options and consequences.

**Q1 - depth** (`safe` / `standard` / `deep` / `custom`)

| Depth | What it may remove | What it never removes |
|---|---|---|
| `safe` | build cache older than the threshold, dangling images, unused non-builtin networks | containers, tagged images, volumes |
| `standard` | safe + stopped compose containers whose config file exists, registry-identical unused images, empty volumes confirmed by peek, compose networks | base images, local-only images, data volumes |
| `deep` | standard + every stopped container, all re-pullable images (base and registry-different), empty volumes, rebuildable cache volumes (`node_modules`, `target`, `cargo-target`), all unused networks and cache; anonymous volumes appear as unapproved decision items | local-only images, data volumes, any anonymous volume without confirmation |
| `custom` | only items the user selects; nothing is approved by default | anything protected |

Anonymous volumes appear in `deep` as unapproved `decision` items; use `--include-anonymous-volumes` to add them to `safe`/`standard`. Data-like volumes are always opt-in with `--include-data-volumes`. Both stay `decision` items and need the decision gate in `apply`.

**Q2 - age threshold** (`7` / `30` / `90` / `all` / custom)

Never hardcode 7 or 30. The threshold applies to:

- containers: `FinishedAt` (not `CreatedAt`),
- build cache: `LastUsedAt`,
- networks: `CreatedAt`,
- images: `CreatedAt` as a conservative floor; registry evidence decides,
- volumes: newest activity (container timestamps + bounded file mtime).

Warn the user that Docker has no "last used" timestamp for images. Age is build date, not usage.

**Q3 - volume preview consent**

- "Yes, inspect all unused volumes" -> run `audit --peek`.
- "Yes, but only selected volumes" -> run `audit --peek-volume NAME` for each volume the user named. Only those volumes are inspected.
- "No" -> classify without preview. Every unused volume becomes `decision` or `cost`, never `safe`.

The helper container uses `--rm --network none --read-only`, a `:ro` mount, and only read-only list/size/stat utilities (`ls`, `du`, `find`, `stat`, `printf`, `wc`, plus text filters such as `head`, `cut` and `sort`). It never reads file content and never pulls an image automatically (`--pull never`). If no `alpine`/`busybox` image is local, preview is skipped.

If the user already answered, do not ask again. If they change the depth or the age threshold, build a new plan; do not edit the old one by hand.

### 3. Build the exact plan

```bash
python3 scripts/docker_cleanup.py plan --depth standard --older-than 30 \
  --out /tmp/docker-cleanup-plan.json
```

`plan` runs a fresh audit and writes:

- `plan.json` - machine-readable items, expected IDs/states, commands, best-effort restore hints (not a full container manifest);
- `plan.md` - the human table to show the user.

Show the plan as a table: action, object, size, verdict, reason, approved. Show what stays next to what would be removed. State the total estimate and that `docker system df` "Reclaimable" is an estimate.

The plan includes only candidates for the chosen depth and age. `protected` items never enter the plan.

### 4. Get explicit confirmation

The user can accept the preset, deselect items, or add items:

```bash
python3 scripts/docker_cleanup.py select --plan plan.json --exclude network:abc123
python3 scripts/docker_cleanup.py select --plan plan.json --include image:def456
python3 scripts/docker_cleanup.py select --plan plan.json --none
python3 scripts/docker_cleanup.py select --plan plan.json --all-default
```

For a decision item the user must type the exact name. Pass it after `=`:

```bash
python3 scripts/docker_cleanup.py select --plan plan.json \
  --include 'volume:3f2a...=old_uploads'
```

Do not invent a confirmation. If the user says "yes, but I'm not sure about the database volume", leave that volume unapproved.

### 5. Apply with re-validation

```bash
python3 scripts/docker_cleanup.py apply --plan plan.json --dry-run
python3 scripts/docker_cleanup.py apply --plan plan.json --yes
```

- `--dry-run` performs the same validation and prints the commands, but executes no destructive command.
- `apply` without `--yes` refuses in a non-interactive session (exit 3).
- Remote contexts require `--allow-remote` for `audit`, `plan` and `apply`.
- More than 200 items require `--force-count`.
- Decision items require `--allow-decision` **and** `approved: true` **and** `confirm == name`. Missing one of them skips the item.

`apply` re-checks the plan schema, Docker context, daemon ID hash, object ID, state and current usage. A container that started after the plan is skipped. A volume that became used is skipped. An approved empty volume is re-peeked with the same read-only helper, so a volume that gained files after the plan is skipped. A network is removed only if it is free after the containers earlier in the plan; a built-in network is refused. An image used by a container outside the plan is skipped. An image planned as `registry_identical` is checked against the registry again; if the registry is unreachable or no longer matches, the image is skipped unless the decision gate is satisfied. A live object that is less safe than the plan (`safe` becoming `cost`) is skipped, never removed.

Order: containers -> networks -> images -> volumes -> build cache. One failed item does not stop the rest; the exit code is 4 if anything failed.

Exit codes: `0` OK, `2` usage/environment, `3` safety refusal, `4` partial failure, `5` nothing to do.

### 6. Verify and report

`apply` prints and stores:

- removed / skipped / failed counts,
- estimated reclaimed bytes (sum of removed items),
- disk free before/after,
- the exact commands,
- restore hints,
- a JSON + Markdown log in the state directory (`$XDG_STATE_HOME/docker-cleanup`, `~/Library/Application Support/docker-cleanup` or `%LOCALAPPDATA%\docker-cleanup`).

Tell the user where the log is and that it may contain container names and host paths. A second `apply` on the same plan is idempotent: missing objects are skipped, not treated as errors.

Images used by stopped containers are intentionally left for a **second pass**: the first plan removes the containers, then a fresh audit/plan can classify the now-unused images with real registry evidence. Networks are different: a network attached only to stopped containers is added to Standard/Deep together with those containers, because the network holds no data and is recreated by compose.

## How to present the report

Follow `assets/report-template.md`. At minimum:

1. Environment and warnings (context, local/remote, disk free).
2. Summary: safe / cost / decision / protected bytes.
3. Depth preview: items and bytes for safe, standard, deep.
4. Containers: state, age from `FinishedAt`, compose project, image, mounts, verdict.
5. Images: size, tags, registry evidence, used-by.
6. Volumes: **full host path**, project, activity (approx.), verdict, users.
7. Networks: built-in, in use, compose project.
8. Build cache: 7/30/90/all buckets.
9. Compose projects and bind mounts.
10. What NOT to remove and why.
11. What the audit does not know.
12. Next step: choose depth / confirm plan.

Show relationships explicitly: container -> image, container -> volumes/binds, container -> networks, compose project -> containers. A volume row without a host path is not acceptable.

## What this skill never does

- `docker system prune`, `docker system prune -a --volumes`
- `docker volume prune -a`
- `docker rm -v`
- `docker rmi -f`
- `sudo docker ...`
- remove a data volume without `--allow-decision`, `approved: true` and the exact typed name
- run `--peek` or `--peek-volume` without consent
- touch a remote context without `--allow-remote` (including `plan`)
- remove tagged images while a swarm is active (only dangling images are eligible)
- claim that `Links: 0` means an empty volume
- claim that `docker rm -v` removes named volumes (it removes anonymous ones)
- promise host disk savings on Docker Desktop before VM disk compaction

## Decision-item rule

A volume with application data, an anonymous volume, or an image that exists only locally is `decision`. To remove a volume:

1. The user sees the volume name, host path, size, files (if peeked) and users.
2. The user types the exact volume name.
3. The plan item has `approved: true` and `confirm` equal to that name.
4. `apply` is called with `--allow-decision`.

For a decision image, the typed confirmation is its tag (for example `shop/localbuild:dev`) and the same four steps apply.

If any step is missing, the item stays. Do not use `--allow-decision` as a blanket "yes".

## Troubleshooting and references

- `references/safety-model.md` - where data lives, verdicts, dangerous commands, edge cases (compose orphans, swarm, Docker Desktop, rootless, multi-arch).
- `references/depth-levels.md` - exact rules per depth, age semantics, examples.
- `references/troubleshooting.md` - permission denied, remote context, rootless, Docker Desktop VM disk, WSL2, registry offline, `df` discrepancy, `Links: 0`, `rm -v`, multi-arch digests, Windows paths.
- `references/authoring-checklist.md` - invariants, traps and the regression matrix.
- `assets/report-template.md` - report skeleton.

## Privacy

- Never print or persist `Config.Env` values. Restore specs store env **names** with `<redacted>`.
- Never print registry credentials or full remote endpoints.
- `--peek` reads names, sizes and mtimes only. No `cat`, no content extraction.
- Logs contain container names and host paths; say where they are and how to delete them.
- Public examples use fake names and temp paths.

## Testing

The test suite is maintained in the development repository and is **not shipped** with the published skill. The published package contains only the CLI and its documentation. Tests run against a Docker CLI simulator with state in a temporary directory; they never touch a real daemon and never run a global prune.

## If no question tool is available

Print the three questions as numbered options and wait for the user's reply. Do not assume a default. Example:

```text
1) Depth:   [1] safe  [2] standard  [3] deep  [4] custom
2) Age:     [1] 7 days  [2] 30 days  [3] 90 days  [4] all  [5] custom
3) Preview: [1] yes, all unused  [2] yes, selected  [3] no
Reply with three choices, for example: 2 2 3
```

Only after the answer may you run `plan` and later `apply`.

## CLI reference

```bash
python3 docker_cleanup.py doctor [--json]
python3 docker_cleanup.py audit [--json] [--no-registry] \
  [--peek] [--peek-volume NAME] [--older-than DAYS] [--allow-remote] [--state-dir PATH]
python3 docker_cleanup.py plan --depth safe|standard|deep|custom --older-than DAYS \
  [--include-base-images] [--include-local-images] [--include-anonymous-volumes] \
  [--include-data-volumes] [--out FILE] [--allow-remote] [--state-dir PATH] \
  [--no-registry]
python3 docker_cleanup.py select --plan FILE [--include ID[,ID]] [--exclude ID[,ID]] [--all-default] [--none]
python3 docker_cleanup.py apply --plan FILE [--dry-run] [--yes] [--allow-decision] \
  [--allow-remote] [--force-count] [--state-dir PATH]
```

`--older-than 0` or `all` means no age filter. `plan` requires an explicit `--older-than`; `audit --older-than` only changes the depth preview (default 7 days). `--json` prints pure JSON to stdout; human reports are Markdown. All output is in English.
