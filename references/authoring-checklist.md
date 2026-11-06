# Authoring & safety checklist

Use this checklist before changing `docker-cleanup`, before a release, and when reviewing a PR. It records the traps that were found while rebuilding this skill. **Do not delete items** — add new ones with a short “why” and a regression case.

---

## 0. Non-negotiable invariants

- [ ] `audit`, `plan`, `select`, `doctor` are read-only by default. The only optional side effect is `--peek`, which starts a short-lived `:ro` helper container and must be explicitly requested.
- [ ] No `docker system prune`, no `docker system prune -a --volumes`, no `docker volume prune -a`, no `docker rm -v`, no `docker rmi -f`, no `sudo docker ...`.
- [ ] No data volume is removed by a preset. `DECISION` items require `--allow-decision`, `approved: true`, and `confirm == name`.
- [ ] `PROTECTED` items never enter a plan. If one appears in a plan file, `apply` refuses.
- [ ] A preset is not consent. The agent must show the exact plan and get an explicit confirmation.
- [ ] Every removal is re-validated immediately before execution: context, daemon identity, object ID, state, and current usage.
- [ ] Tests never mutate the user's real Docker daemon. Use the fake daemon in a temp directory.
- [ ] No secrets: never print or persist `Config.Env` values, registry credentials, or tokens.
- [ ] Real reclaimed space is measured with `df` before/after, not copied from `docker system df` “Reclaimable”.
- [ ] Every failure is reported; one failed item does not abort the whole apply.

---

## 1. Docker CLI correctness traps

### Sizes and parsing

- [ ] Parse only the **leading** `<number><unit>` token. `docker ps --format json` returns `"114MB (virtual 1.2GB)"`; a loose “collect all digits” parser produces `1141.2` and loses the unit. Regression: stopped container with 114 MB writable layer must be `COST`, not `SAFE`.
- [ ] `docker system df` returns `Reclaimable` like `"10.79GB (37%)"`; never feed that whole string to a size parser.
- [ ] `docker system df -v --format '{{json .}}'` is **one JSON object** on modern Docker (`Images`, `Containers`, `Volumes`, `BuildCache`), not JSONL. Accept both shapes.
- [ ] `docker volume ls --format json` has no size. Size comes from `system df -v` (and may be missing/zero on some drivers); label it as an estimate.
- [ ] `docker buildx du` totals can double-count shared cache records. Report an estimate and measure the real delta with `df`.

### Time and age

- [ ] Container age uses `FinishedAt`, **not** `CreatedAt`. `docker container prune --filter until=7d` filters by creation time; a container created 30 days ago but stopped yesterday would be removed. Remove explicit IDs after filtering `FinishedAt` yourself.
- [ ] Image age uses `CreatedAt` (build date), not last-used. Docker does not store “last used”. Never claim an image is unused because it is old; use container references and registry evidence.
- [ ] Volume activity is approximate. `stat -c %Y /v` on the mount root is not enough; use the newest file mtime (bounded depth/timeout) plus container start/finish timestamps. Say “approx.”
- [ ] Build cache age uses `LastUsedAt`; apply prunes with the absolute `until=<cutoff_at>` stored in the plan, not a relative duration.
- [ ] Docker timestamps may contain `Z` and 9-digit nanoseconds. Python 3.9 `fromisoformat` cannot parse them directly: replace `Z` with `+00:00`, truncate the fraction to 6 digits.

### Volumes

- [ ] `Links: 0` does **not** mean empty. A volume left by a stopped project has zero links and full data. Never treat zero links as proof of emptiness.
- [ ] `docker rm -v` removes **anonymous** volumes, not named ones. Named volumes survive. Do not repeat the myth that `-v` deletes named volumes.
- [ ] `docker system prune --volumes` prunes **anonymous** volumes; named volumes survive. The command is still banned because of its blast radius (all stopped containers, all unused images, all build cache, all unused networks), not because it deletes named database volumes.
- [ ] `docker volume prune` without `-a` removes only anonymous volumes; `-a` also removes named volumes. Never use `-a`; remove explicit names one by one.
- [ ] Anonymous volumes can contain data and are hard to find later. `COST`/`DECISION`, never silently removed.
- [ ] A volume can be in use by a stopped container. It is still `PROTECTED` until that container is explicitly removed; even then the volume survives unless removed by name.

### Images

- [ ] `docker image prune -a` removes images not referenced by **any** container, including stopped containers. To remove an image used by a stopped container, remove the container first.
- [ ] `docker rmi` on a multi-tag image may only untag it. Verify the result; do not assume the image is gone.
- [ ] `docker rmi -f` can remove an image used by a running container. Forbidden.
- [ ] `RepoDigests` can exist for locally built images loaded with `buildx --load`. Its presence does not prove the image exists in a registry.
- [ ] Multi-arch images: `RepoDigests` may hold a platform manifest digest while `buildx imagetools inspect` returns the index digest. A mismatch is not always “different version”. Mark the comparison as uncertain instead of claiming “safe”.
- [ ] Registry check is network I/O. Timeout it, parallelize it, cache it, and provide `--no-registry`. Never print registry errors that may contain credentials.
- [ ] Base images (`php`, `nginx`, `postgres`, `node`, `alpine`, …) are `COST` with a “you may need this later” note. They are excluded from `Safe`/`Standard` by default.

### Networks

- [ ] `bridge`, `host`, `none` and `docker_gwbridge` are built-in/system networks and cannot be removed. Protect them.
- [ ] A network used by a stopped container is still in use. Remove the container first.
- [ ] Compose networks are recreated by `docker compose up -d`; still verify the compose file exists.
- [ ] Swarm `ingress` and service networks are protected. Detect active Swarm and warn. On an active swarm `apply` skips every tagged image (only dangling images are eligible), including images selected in a custom plan.

### Build cache

- [ ] `docker buildx prune` is not the same as `docker builder prune`. Prefer `buildx` when available; fall back to `builder` and document the difference.
- [ ] `InUse` cache records are not reclaimable; skip them and warn.
- [ ] Do not prune cache while a build is running. Check `buildx du` `InUse` records and buildkit containers, and warn.

### Context and daemon

- [ ] Always show `docker context show` and whether the endpoint is local or remote.
- [ ] Refuse to apply to a remote context unless `--allow-remote` is passed.
- [ ] Store a hash of the daemon ID in the plan; refuse if the daemon changed between plan and apply.
- [ ] `DOCKER_HOST` may override the context; treat it as part of the identity.
- [ ] Rootless Docker uses `$XDG_RUNTIME_DIR/docker.sock` and `~/.local/share/docker`; never `sudo`.
- [ ] Docker Desktop (macOS/Windows) runs a VM. Pruning inside the VM may not shrink the VM disk file; document the extra compaction step and do not promise host disk savings.

---

## 2. Classification rules

- [ ] Running/paused/restarting containers are `PROTECTED`.
- [ ] Stopped container with a compose project and an existing config file is `SAFE` (`docker compose up -d` recreates it), but only after the user chooses the age threshold.
- [ ] Stopped container with a missing compose file is `COST` (config not recreatable).
- [ ] Stopped container with only bind mounts is `SAFE` (data is on the host), but save a sanitized restore spec.
- [ ] Stopped container with named volumes is `SAFE` for `docker rm` without `-v`.
- [ ] Stopped container with anonymous volumes is `COST` (data survives only if `-v` is not used; the volume becomes orphaned).
- [ ] No mounts + writable layer > 1 MB is `COST`; < 1 MB is `SAFE`.
- [ ] Image used by a running container is `PROTECTED`; by a stopped container is `COST`.
- [ ] Dangling image is `SAFE`; base image is `COST`; local-only image is `DECISION` unless a compose/Dockerfile source exists.
- [ ] Registry-identical image is `SAFE`; registry-different is `COST`; unknown/offline is `COST`.
- [ ] Volume in use is `PROTECTED`; unused and confirmed empty is `SAFE`; data-like name/content is `DECISION`; cache-like name is `COST`; unknown content is `DECISION`.
- [ ] Network unused and non-builtin is `SAFE`; compose network with existing file is `SAFE`; built-in/in-use/swarm is `PROTECTED`.
- [ ] Build cache is always `SAFE` (cost: rebuild), subject to age and `InUse`.

---

## 3. Depth levels and user questions

- [ ] Always ask for the **age threshold**: 7 days (default), 30, 90, all, or a custom date. Never hardcode 7 or 30.
- [ ] Always ask for **volume preview consent** before running any helper container: all unused / selected / no preview. No consent = `content_known: false` = `DECISION`.
- [ ] `Safe`: build cache older than threshold, dangling images, unused networks older than threshold. No containers, no tagged images, no volumes.
- [ ] `Standard`: Safe + recreatable stopped containers, registry-identical images, empty volumes (only if peeked), unused compose networks, cache. Exclude base/local images and data volumes.
- [ ] `Deep`: Standard + all stopped containers, all re-pullable images (including base and registry-different), all empty/anonymous volumes, all cache/networks. Exclude local-only images and all non-empty/data volumes.
- [ ] `Custom`: nothing selected by default; the user selects categories/items.
- [ ] Presets may be overridden per category/item, but `DECISION` items are always a separate, explicit confirmation with the volume name typed.

---

## 4. Apply gates

- [ ] Require `--yes` in non-interactive mode. Without it, refuse with exit code 3.
- [ ] `--dry-run` performs read-only validation and prints commands; it must not call `rm`/`rmi`/`volume rm`/`network rm`/`prune`.
- [ ] Verify `schema_version`; reject unknown versions.
- [ ] Verify context and daemon ID hash.
- [ ] Verify object ID (not just name) and expected state. Containers and images must resolve by exact/short ID only; never accept a container name or an image tag as an object ID. Networks must match by exact ID; never fall back to the name, because compose reuses network names.
- [ ] Re-classify the live object and merge verdicts conservatively: `protected` wins, then `decision`, then the live `safe`/`cost`. A hand-edited plan must never downgrade a decision item.
- [ ] Re-check registry evidence for images planned as `registry_identical`. If the registry is unreachable or no longer matches, skip the image.
- [ ] If the live verdict is `cost` while the plan said `safe`, skip the item (`reclassified`); a safe approval is not a licence to remove a newly-costly object.
- [ ] Re-check container state: running/paused/restarting → skip.
- [ ] Re-check image usage by any container; if a stopped container in the plan was already removed, continue; otherwise skip.
- [ ] Re-check volume usage by any container; skip if in use.
- [ ] Re-peek approved empty volumes with the read-only helper; skip if they gained files.
- [ ] Re-check network usage and built-in status.
- [ ] `DECISION`: require `--allow-decision` + `approved: true` + `confirm == name`.
- [ ] `PROTECTED`: refuse, even if present in the plan.
- [ ] > 200 items: require `--force-count`.
- [ ] Order: containers → networks → images → volumes → build cache.
- [ ] Continue after a failed item; exit 4 if any failed.
- [ ] Save a JSON log + human Markdown log; include before/after `docker system df` and `df`.
- [ ] Save sanitized restore specs (no env values); include `docker compose` file/working dir and `docker pull repo@digest` hints.
- [ ] Idempotent: a second apply skips missing items without crashing.

---

## 5. Portability

- [ ] Python 3.9+, standard library only, no `pip install`.
- [ ] Resolve all paths relative to `__file__`; no hardcoded `~/.config/opencode`.
- [ ] macOS, Linux, Windows (Docker Desktop/WSL2). Use `os.path` / `pathlib`, UTF-8 with fallback.
- [ ] State dir: `$XDG_STATE_HOME` / `~/.local/state` (Linux), `~/Library/Application Support` (macOS), `%LOCALAPPDATA%` (Windows); allow `--state-dir`.
- [ ] Never use `shell=True` with user-controlled data; pass argv lists.
- [ ] Timeout every Docker command; a hung daemon must not hang the skill.
- [ ] Handle “docker not installed”, “daemon not running”, “permission denied” with actionable messages and exit code 2 — no stack trace, no `sudo`.

---

## 6. Privacy

- [ ] Never print `Config.Env` values. Restore specs store env **names** with `<redacted>`.
- [ ] Do not print registry credentials, tokens, or full remote endpoints. Show context name and “remote/local” only.
- [ ] Volume preview reads names/sizes/mtime only — no `cat`, no content extraction.
- [ ] Public fixtures use fake names and temp paths, never the author's real containers/paths.
- [ ] Logs may contain container names and host paths; tell the user where they are stored and make them easy to delete.

---

## 7. Testing

- [ ] Primary tests use a Docker CLI simulator with state in a temp directory; no real daemon.
- [ ] Test the full flow: `doctor → audit → plan → select → apply --dry-run → apply --yes → verify`.
- [ ] Assert `calls.log` contains no destructive command during `--dry-run`.
- [ ] Assert `PROTECTED` and unapproved `DECISION` items remain after apply.
- [ ] Assert approved `DECISION` items are removed only with the flag + exact `confirm`.
- [ ] Assert changed state between plan and apply causes a skip/refusal, not deletion.
- [ ] Assert idempotency and partial-failure exit code 4.
- [ ] Assert remote context warnings and refusal without `--allow-remote`.
- [ ] Assert >200 items refusal without `--force-count`.
- [ ] Validate `SKILL.md`: `name == folder`, description ≤1024 chars, body <500 lines, all referenced files exist.
- [ ] `python3 -m py_compile` on every Python file.
- [ ] Never run destructive integration tests against a user's daemon. If integration tests are added, require an explicit env flag and a disposable context/VM.

---

## 8. Report and UX

- [ ] Diagnosis before any question. The user must see the state before choosing a depth.
- [ ] Show relationships: container → image, container → volumes/binds/networks, compose project.
- [ ] Show timestamps and “how long ago”; use `FinishedAt` for stopped containers.
- [ ] Show full host paths for volumes and bind mounts.
- [ ] Show a depth preview table with item counts and estimated sizes.
- [ ] Show “what stays” next to “what would be removed”.
- [ ] Say clearly that `docker system df` “Reclaimable” is an estimate.
- [ ] Keep the report and all CLI output in English; keep reason codes in JSON stable.
- [ ] Escape `|` in Markdown tables; do not truncate safety-critical paths.
- [ ] End with the next step (choose depth / confirm plan), not a wall of raw JSON.

---

## 9. Release checklist

- [ ] `README.md` has install instructions for Claude Code, Codex/Cursor, OpenCode, and a generic client; requirements; safety guarantees; CLI examples; testing command.
- [ ] `README.md` and `.gitignore` are present in the skill folder.
- [ ] `SKILL.md` frontmatter: `name`, `description`, `compatibility`, `metadata`.
- [ ] No private data, no absolute user paths, no secrets in fixtures or docs.
- [ ] The development test suite (not shipped with the skill) is green.
- [ ] `audit --json` is valid JSON and includes `schema_version`.
- [ ] `plan` + `apply --dry-run` run on the fake daemon and leave state unchanged.
- [ ] Version bumped; changelog updated.

---

## 10. Regression matrix (must stay green)

| # | Case | Expected |
|---|---|---|
| 1 | Running container | PROTECTED, never removed |
| 2 | Stopped compose, config exists | SAFE, removable at chosen age |
| 3 | Stopped compose, config missing | COST |
| 4 | Stopped bind-only | SAFE |
| 5 | Stopped named volume | SAFE for `rm` without `-v` |
| 6 | Stopped anonymous volume | COST |
| 7 | Stopped writable 114 MB | COST (size parser regression) |
| 8 | Stopped no mounts, <1 MB | SAFE |
| 9 | Dangling image | SAFE |
| 10 | Image used by running container | PROTECTED |
| 11 | Image used by stopped container | COST, skip until container removed |
| 12 | Base image | COST, excluded from Safe/Standard |
| 13 | Local-only image | DECISION/COST |
| 14 | Registry-identical | SAFE |
| 15 | Registry-different | COST |
| 16 | Registry offline | COST |
| 17 | Empty volume after peek | SAFE |
| 18 | Data volume | DECISION, excluded from presets |
| 19 | Cache volume | COST |
| 20 | Anonymous unused volume | DECISION |
| 21 | Volume in use | PROTECTED |
| 22 | Built-in network | PROTECTED |
| 23 | Unused network | SAFE |
| 24 | Compose network, file exists | SAFE |
| 25 | Build cache 7/30/90/all | Correct buckets |
| 26 | Remote context | Warning + refusal without `--allow-remote` |
| 27 | Plan excludes protected | Always |
| 28 | `apply --dry-run` | Zero destructive calls |
| 29 | Container started after plan | Skip, no deletion |
| 30 | Volume became used after plan | Skip, no deletion |
| 31 | DECISION without flag | Refuse |
| 32 | DECISION without exact confirm | Refuse |
| 33 | DECISION with flag + confirm | Remove |
| 34 | Partial failure | Exit 4, log, continue |
| 35 | Second apply | Idempotent, skip missing |
| 36 | Context mismatch | Refuse |
| 37 | Daemon ID mismatch | Refuse |
| 38 | >200 items | Refuse without `--force-count` |
| 39 | `--json` | Valid JSON + schema_version |
| 40 | English-only output | Report and CLI text are English |

---

## 11. Known limitations (state them in the report)

- Docker does not expose “last used” for images; age is build/creation time.
- Volume activity is approximate unless the user consents to a `:ro` peek.
- `docker system df` “Reclaimable” overestimates shared layers and unused-but-important volumes.
- The skill cannot know whether data in a volume is still needed; it can only show evidence.
- Docker Desktop VM disk compaction is platform-specific and not guaranteed.
- Registry digest comparison can be ambiguous for multi-arch/platform-specific pulls.
- Kubernetes/Swarm workloads may use images outside the audited container list; the skill warns but does not manage them.

---

## 12. Traps found while building 1.0.0

These were discovered during the implementation and the fake-daemon tests. Keep them here so the next rewrite does not repeat them.

- [ ] **Directory mtime is not volume activity.** Creating files updates the volume root mtime to "now". Walking only the root directory makes an old volume look fresh (or a fresh one look old). Walk files with a bounded budget and use the root mtime only as a fallback for empty directories. Regression: a volume with a file from 20 days ago must not be filtered out by a 7-day age threshold.
- [ ] **Anonymous volumes have names.** Docker assigns a 64-hex name to anonymous volumes. Checking `mount.Name == ""` misses them. Detect the 64-hex shape. Regression: a stopped container with an anonymous volume must be `COST`, and the volume must be `DECISION`, not `PROTECTED`.
- [ ] **`system df -v` is one JSON object, not JSONL.** Accept an object, a list of objects and JSONL. The same helper must handle `docker inspect` arrays and `--format '{{json .}}'` output split into lines.
- [ ] **`docker ps -s` sizes are strings with a suffix.** Parse only the leading token; `114.00MB (virtual 162.00MB)` must be 114 MB. The old "collect all digits" parser produces 114.00162 and loses the unit.
- [ ] **Build cache cannot be removed per record.** Docker exposes a prune filter, not a per-ID delete. Model cache as one item per approved age filter; never pretend a per-record selection is precise.
- [ ] **Do not approve registry-unknown images in `deep`.** Offline or failed registry checks cannot distinguish a local-only image from a re-pullable one. `deep` must exclude `registry_unknown` and `registry_unverifiable`; only `custom` may expose them for manual confirmation.
- [ ] **Compose image references need repo normalization.** Compare `shop/api:1.0` with `shop/api`, not with the full tag. Otherwise a locally built image is classified as local-only instead of rebuildable.
- [ ] **`docker rmi <tag>` may only untag.** After a successful `rmi`, inspect the image ID again. Report "untagged only; image id still present" instead of claiming the image is gone.
- [ ] **`select --include ID=NAME` is the typed confirmation.** A plain ID sets `approved` but leaves `confirm` empty, so a decision volume is still skipped. Document the `=` syntax; do not silently fill `confirm`.
- [ ] **A decision item is skipped, not failed, when the gate is missing.** This lets the rest of the plan proceed. Report the skip reason clearly.
- [ ] **Re-check races in the removal call.** A container can start between validation and `docker rm`; Docker then returns "container is running". Treat that as a skip, not a partial failure. Same for volume "in use" and network "active endpoints".
- [ ] **`--dry-run` must print the commands.** A table of "would_remove" statuses is not enough; the user must see `docker rm`, `docker rmi`, `docker volume rm` and `docker buildx prune` before approving.
- [ ] **Scope the dry-run proof.** A `calls.log` assertion that scans the whole session can be polluted by an earlier `--peek` helper cleanup. Record the log length before the dry run and assert no destructive call was appended after that marker.
- [ ] **Do not write the fake state on read-only calls.** Parallel registry checks would race on `state.json`. Save only after a mutation.
- [ ] **Test the privacy boundary, not only the feature.** Assert that a fixture secret (an environment value and a database URL) never appears in audit JSON or apply logs. Env names may appear; values must not.
- [ ] **Count assertions, not test methods.** The acceptance gate is "at least 35 assertions"; a suite of 18 test methods can still contain 200+ assertions. Print the assertion count so the gate is visible.
- [ ] **Deep lists anonymous volumes but never approves them.** A preset may show a decision candidate so the user understands the orphan, but approval must still require `--allow-decision` and the exact name.
- [ ] **`plan` needs the peek evidence from `audit`.** A fresh audit inside `plan` does not pass `--peek`; merge the cached peek result by volume name and creation time, otherwise `standard` can never approve an empty volume.
- [ ] **A remote context must be refused before the audit, not only at apply.** Reading a production daemon is already a side effect the user may not want. Require `--allow-remote` for `audit`, `plan` and `apply`.
- [ ] **A network holds no data, but `docker network rm` refuses while any container is attached, even a stopped one.** Mark a network used only by stopped containers as `network_stopped_users`, and add it to a Standard/ Deep plan only when every attached container is also selected. Keep it `protected` if any attached container is running. Test the generated plan, not a hand-injected item.
- [ ] **Images used by stopped containers need a second pass.** Keep them out of the first plan: registry evidence was not collected while they were in use. After the containers are removed, re-run the audit/plan to classify the now unused images with real registry evidence.
- [ ] **Keep `Config.Env` out of every intermediate structure.** Extract names only at collection time; never store the values in the snapshot, the plan, the registry cache or the apply log.
- [ ] **Markdown tables must escape `|` in reason strings.** Compose project names and reasons can contain a pipe; an unescaped pipe breaks the table.
- [ ] **`--json` must be pure.** Progress, warnings and summaries go to stderr or the human report; stdout is JSON only.
- [ ] **Windows path hints are not shell commands.** `shlex.quote` produces POSIX quoting; restore hints are documentation, not copy-paste commands for `cmd.exe`. Say so when the host is Windows.

## 13. Extended regression matrix (1.0.0)

| # | Case | Expected |
|---|---|---|
| 41 | Volume with a file modified 20 d ago, threshold 7 d | Included (not filtered by root mtime) |
| 42 | Stopped container with a 64-hex anonymous volume | `COST`, volume `DECISION` |
| 43 | `--dry-run` output | Prints every removal command, executes none |
| 44 | Decision volume with plain `--include ID` | `approved: true`, `confirm: ""`, skipped |
| 45 | Decision volume with `--include ID=NAME` + `--allow-decision` | Removed |
| 46 | Image removed by tag but another tag remains | Report "untagged only", not fully gone |
| 47 | Offline registry + `deep` | No registry-unknown image approved |
| 48 | Container starts between plan and apply | Skip, exit 5, container intact |
| 49 | Built-in network injected into a plan | Not removed |
| 50 | Image used by a running container injected into a plan | Not removed |
| 51 | Audit JSON with fixture secrets | No env values present |
| 52 | Apply without `--yes`, non-interactive | Exit 3 before any call |
| 53 | Fake state read during parallel registry checks | No state file writes |
| 54 | `select` on an unknown item ID | Exit 2, plan unchanged |
| 55 | Plan with `items` not a list | Exit 2, readable error |
| 56 | `ps`/`images`/`network ls` short IDs vs full `inspect` | Both resolve; network stays protected |
| 57 | Image used through a non-primary tag | `used_by` populated, never `safe` |
| 58 | Compose container + its network in one plan | Container removed, then network removed |
| 59 | Container restarted/stopped after plan | `too_new_since_plan`/`state_changed`, skipped |
| 60 | Cache record younger than the plan cutoff | Survives an absolute `until=<cutoff_at>` prune |
| 61 | `audit --older-than 7` vs `all` | Preview changes; plan still requires `--older-than` |
| 62 | `--peek-volume NAME` | Exactly one helper run, for that volume only |
| 63 | State dir / logs on POSIX | Directory 0700, files 0600 |
| 64 | Remote `plan` without `--allow-remote` | Exit 3 |
| 65 | `deep` with a `node_modules` volume | Included as `cost`; `standard` excludes it |
| 66 | `docker rm` returns "container is paused" | `skipped`, not `failed`, exit 5 |
| 67 | `docker rmi tag` only untags | Reason "untagged only", reclaimed bytes 0 |
| 68 | Hand-edited plan relabels a decision volume `safe` | Live decision wins; skipped |
| 69 | Decision image + apply without `--allow-decision` | Skipped; not downgraded to cost |
| 70 | Network recreated with the same name and a new ID | Skipped; the new network remains |
| 71 | Network used only by stopped containers in the plan | Added to Standard/Deep; removed after containers |
| 72 | Apply with no containers, plan relabels a decision volume `safe` | Skipped; volume remains |
| 73 | Container name used as `object_id` | Skipped; never removed by name |
| 74 | Image recreated with the same tag and a new ID | Skipped; new image remains |
| 75 | Safe container's writable layer grows to 114 MB after plan | Skipped (`reclassified`) |
| 76 | Approved empty volume is re-peeked at apply | Removed only if still empty |
| 77 | Plan item `until` moved forward | Absolute `cutoff_at` wins; young cache survives |
| 78 | Container name used as `object_id` | Skipped; never removed by name |
| 79 | Volume `object_id` and display name differ | Removal uses the validated object ID |
| 80 | Protected volume after its stopped container is removed | Skipped with `protected_in_plan` |
| 81 | Active swarm, tagged image relabelled `dangling_image` | Live dangling flag wins; image stays |
| 82 | Volume peek `ls` fails | `peeked: false`, volume `decision`, never `safe` |
| 83 | Future `cutoff_at` in a plan | Rejected as `plan_invalid` |
