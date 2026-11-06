# Docker cleanup report - template

Copy this skeleton into the conversation and fill it from `audit --json` / `audit` output. Use English consistently.

---

# Docker audit

> Read-only. Nothing has been removed.

## 1. Environment

| | |
|---|---|
| Context | `<name>` - local / remote |
| Version | client `<x>` / server `<y>` |
| Storage driver | `<overlayfs>` |
| Docker root | `<path>` |
| OS | `<os> / <arch>` |
| Swarm | `<inactive>` |
| Disk | `<free>` free of `<total>` (`<pct>%` used) |

**Warnings**

- `<remote_context>` - ...
- `<build_in_progress>` - ...
- `<rootless>` - ...

## 2. Summary

| Verdict | Meaning | Size |
|---|---|---|
| SAFE | no data loss, recreatable | `<n>` |
| COST | no data loss, rebuild or re-pull | `<n>` |
| DECISION | looks like user data | `<n>` |
| PROTECTED | running service or in-use data | `<n>` |

> `docker system df` "Reclaimable" is an estimate. Real savings are measured with `df` before and after.

### Depth preview

| Depth | Items | Size |
|---|---|---|
| safe | `<n>` | `<n>` |
| standard | `<n>` | `<n>` |
| deep | `<n>` | `<n>` |

## 3. Containers

| Name | State | Age (FinishedAt) | Image | Compose project | Mounts | Verdict | Reason |
|---|---|---|---|---|---|---|---|
| `<name>` | exited | 12 d | `<image>` | `<project>` | `<volumes/binds>` | SAFE | compose config exists |

## 4. Images

| Image | Size | Used by | Registry evidence | Verdict | Reason |
|---|---|---|---|---|---|
| `<repo:tag>` | `<n>` | `<containers>` | digest match / differs / not found / offline | `<verdict>` | `<reason>` |

> Docker has no last-used timestamp for images. Age is the build date.

## 5. Volumes

| Volume | Size | Host path | Project | Activity (approx.) | Used by | Verdict | Reason |
|---|---|---|---|---|---|---|---|
| `<name>` | `<n>` | `<full path>` | `<project>` | 5 d | `<container>` | PROTECTED | in use |

**How to read the host path:** `docker volume rm <name>` deletes the directory at that path.

## 6. Networks

| Network | Driver | Scope | Used by | Verdict | Reason |
|---|---|---|---|---|---|
| `<name>` | bridge | local | `<containers>` | `<verdict>` | `<reason>` |

## 7. Build cache

| Bucket | Size |
|---|---|
| older than 7 d | `<n>` |
| older than 30 d | `<n>` |
| older than 90 d | `<n>` |
| all reclaimable | `<n>` |

`<records>` records, `<in_use>` in use. Cost: rebuild time.

## 8. Compose projects

| Project | Services | Config files | Exists | Containers |
|---|---|---|---|---|
| `<name>` | `<services>` | `<paths>` | yes/no | `<names>` |

## 9. Bind mounts

| Container | Host path | Container path | Activity | Note |
|---|---|---|---|---|
| `<name>` | `<full path>` | `<path>` | 2 d | `<missing / under /tmp>` |

> Data on bind mounts is outside Docker. `docker rm`, `rmi` and `prune` do not touch it.

## 10. What NOT to remove

- **PROTECTED** `<name>` - `<reason>`
- **DECISION** `<name>` - `<reason>`

## 11. What this audit does not know

- Whether an image is still needed: it proves existence and digest match only.
- Whether volume data is current: it shows names, size and mtime only.
- Whether a bind mount matters: it shows the path and activity only.
- `<volume preview was not requested / registry was not checked>`

## 12. Next step

Choose one:

1. Depth: safe / standard / deep / custom.
2. Age: 7 / 30 / 90 days / all / custom.
3. Volume preview: yes, all unused / yes, selected / no.

Then the agent runs `plan`, shows the exact item table, and waits for an explicit confirmation before `apply`.
