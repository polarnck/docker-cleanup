"""Constants, UI strings and verdict labels."""

import re

SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_ENV = 2
EXIT_REFUSED = 3
EXIT_PARTIAL = 4
EXIT_NOTHING = 5

MAX_ITEMS_WITHOUT_FORCE = 200
WRITABLE_LAYER_THRESHOLD = 1_000_000
KIND_ORDER = {"container": 0, "network": 1, "image": 2, "volume": 3, "cache": 4}
BUILTIN_NETWORKS = {"bridge", "host", "none", "docker_gwbridge"}
BASE_REPOS = {
    "php", "nginx", "httpd", "mariadb", "mysql", "postgres", "postgis", "mongo",
    "redis", "memcached", "node", "python", "ruby", "golang", "rust", "openjdk",
    "eclipse-temurin", "amazoncorretto", "alpine", "busybox", "debian", "ubuntu",
    "centos", "fedora", "composer", "maven", "gradle", "docker", "traefik",
    "caddy", "rabbitmq", "elasticsearch", "kibana", "minio", "adminer",
}
CACHE_HINTS = ("cargo-target", "cargo-registry", "node_modules", "build-cache",
               "cache", "target", "vendor", "go-pkg", "pip-cache", "npm-cache", "m2")
DATA_HINTS = ("db_data", "dbdata", "mysql", "maria", "postgres", "mongo", "redis",
              "data", "storage", "uploads", "files", "backup", "activity", "db")
HELPER_IMAGES = ("alpine:latest", "busybox:latest")


MESSAGES = {
    "audit_title": "Docker disk usage audit (read-only)",
    "plan_title": "Docker cleanup plan",
    "legend": "Legend",
    "environment": "Environment and warnings",
    "summary": "Summary",
    "depth_preview": "Depth preview",
    "depth_preview_at": "Depth preview at %d days",
    "containers": "Containers",
    "images": "Images",
    "volumes": "Volumes (with host paths)",
    "networks": "Networks",
    "build_cache": "Build cache",
    "compose": "Compose projects",
    "binds": "Bind mounts (data outside Docker)",
    "do_not_remove": "What NOT to remove",
    "unknowns": "What this audit does not know",
    "next_step": "Next step",
    "no_items": "Nothing in this section.",
    "verdict": "Verdict",
    "size": "Size",
    "reason": "Reason",
    "name": "Name",
    "kind": "Kind",
    "activity": "Activity",
    "created": "Created",
    "evidence": "Evidence",
    "state": "State",
    "project": "Project",
    "host_path": "Host path",
    "container_path": "Container path",
    "approved": "Approved",
    "decision": "Decision",
    "protected": "PROTECTED",
    "safe": "SAFE",
    "cost": "COST",
    "yes": "yes",
    "no": "no",
    "items": "items",
    "reclaimable": "Reclaimable (estimate)",
    "counts": "Object counts",
    "cache_buckets": "Cache buckets (bytes)",
    "context": "Context",
    "daemon": "Daemon",
    "storage": "Storage driver",
    "root_dir": "Docker root",
    "disk": "Disk",
    "swarm": "Swarm",
    "remote": "remote endpoint",
    "local": "local endpoint",
    "no_peek": "Volume content not inspected (no --peek or no consent).",
    "next_audit": "Review the report, then choose a depth and an age threshold: docker_cleanup.py plan --depth safe|standard|deep|custom --older-than DAYS",
    "registry_skipped": "Registry checks disabled (--no-registry).",
    "peek_no_helper": "No alpine/busybox image is local; volume preview skipped (no auto-pull).",
    "build_in_progress": "A build appears to be in progress; cache and buildkit containers are left alone.",
    "nothing_to_do": "Nothing to do.",
    "dry_run": "DRY RUN - no destructive command was executed.",
    "dry_run_title": "Dry run log",
    "would_remove": "Would remove",
    "running": "running",
    "plan_json_saved": "Plan JSON",
    "plan_md_saved": "Plan Markdown",
    "removed": "Removed",
    "skipped": "Skipped",
    "failed": "Failed",
    "reclaimed": "Reclaimed (sum of removed items)",
    "disk_before": "Disk free before",
    "disk_after": "Disk free after",
    "log_saved": "Log saved to",
    "plan_saved": "Plan saved to",
    "confirm_label": "confirm",
    "confirm_required": "Refusing to apply without --yes in a non-interactive session.",
    "remote_refused": "Remote Docker context requires --allow-remote.",
    "context_mismatch": "Plan context does not match the current Docker context.",
    "daemon_mismatch": "Daemon identity changed since the plan was created.",
    "schema_mismatch": "Unsupported plan schema version.",
    "count_refused": "More than 200 items need --force-count.",
    "reclassified": "Live state is less safe than the plan; skipped.",
    "protected_in_plan": "A PROTECTED item is present in the plan; refusing.",
    "decision_skipped": "Decision item not confirmed (needs --allow-decision, approved:true and confirm == name).",
    "missing": "already gone",
    "in_use": "in use",
    "state_changed": "state changed",
    "tag_changed": "tag changed",
    "dry_run_command": "Would run",
    "version": "Version",
    "os": "OS",
    "arch": "Architecture",
    "warnings": "Warnings",
    "none": "none",
    "approximate": "approx.",
    "unknown": "unknown",
    "selected": "Selected",
    "total": "Total",
    "commands_preview": "Commands preview",
    "restore_hint": "Restore hint",
    "depth": "Depth",
    "age": "Age threshold (days)",
    "all_ages": "all ages",
    "decision_items": "Decision items need a separate confirmation.",
    "apply_title": "Apply result",
    "est_note": "`docker system df` Reclaimable is an estimate; real reclaimed space is measured with `df` before/after.",
    "image_age_note": "`docker image inspect` has no last-used timestamp. Image age is the build date, not usage.",
    "volume_path_note": "`docker volume rm <name>` deletes the directory at the host path. `Links: 0` does not mean empty.",
    "cache_note": "Cache is rebuildable; the cost is build time.",
    "bind_note": "Data on bind mounts is outside Docker. `docker rm`, `rmi` and `prune` do not touch it.",
    "generated": "Generated",
    "unknown_image": "Whether you still need an image: the audit proves registry existence and digest match, not intent.",
    "unknown_volume": "Whether volume data is current: it shows file names, size and mtime, not business meaning.",
    "unknown_bind": "Whether a bind mount under `/tmp` matters: it disappears on host reboot anyway.",
    "unknown_stopped_image": "Images used by stopped containers are not registry-checked until the container is handled.",
}


REASONS = {
    "running_service": "service is running",
    "dead_container": "container is in dead state; inspect before removing",
    "compose_recreatable": "stopped compose container; config file exists, `docker compose up -d` recreates it",
    "compose_config_missing": "compose config file is missing; the container is not recreatable automatically",
    "anonymous_volume": "container uses anonymous volumes; removing it without -v orphans data",
    "bind_only": "data lives in host bind mounts; Docker does not delete it",
    "named_volume": "data lives in named volumes; `docker rm` without -v keeps them",
    "writable_layer": "no mounts and a writable layer above 1 MB would be lost",
    "no_data": "no mounts and a tiny writable layer",
    "running_container": "used by a running container",
    "stopped_container": "used by a stopped container",
    "dangling_image": "dangling image, no tag and no container references",
    "base_image": "base image you may need later; pull recreates it",
    "local_build_recreatable": "locally built image with a compose/Dockerfile source; rebuild recreates it",
    "local_only_image": "not in the registry and no build source found; pull cannot restore it",
    "registry_identical": "registry digest matches the local image; pull recreates it 1:1",
    "registry_different": "registry has a different digest; the local image may be newer",
    "registry_unknown": "registry could not be checked; assume the image may be local",
    "registry_unverifiable": "registry answered but the local image has no RepoDigest to compare",
    "registry_digest_ambiguous": "multi-arch digest comparison is ambiguous; treated as cost",
    "volume_in_use_running": "volume is used by a running container",
    "volume_in_use_stopped": "volume is used by a stopped container",
    "volume_empty": "unused volume confirmed empty by a read-only peek",
    "volume_data_name": "volume name suggests application data",
    "volume_cache_name": "volume name suggests a rebuildable cache",
    "volume_unknown": "unused volume with unknown content; never assume it is empty",
    "volume_anonymous": "anonymous volume; it may contain data and is hard to find later",
    "builtin_network": "built-in Docker network; cannot be removed",
    "swarm_network": "swarm network; protected",
    "network_in_use": "network is used by a running container",
    "network_stopped_users": "network is attached only to stopped containers; removable after they are removed",
    "compose_network": "compose network; config file exists, compose recreates it",
    "unused_network": "unused non-builtin network",
    "compose_network_missing": "compose network but the config file is missing",
    "build_cache": "build cache; rebuild recreates it",
    "too_recent": "younger than the chosen age threshold",
}


def _(key):
    return MESSAGES.get(key, key)


def reason_text(code):
    return REASONS.get(code, code)


STATUS_KEYS = {"removed": "removed", "would_remove": "would_remove",
               "skipped": "skipped", "failed": "failed"}


def status_text(status):
    return _(STATUS_KEYS.get(status, status))


SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kKmMgGtTpP]?i?[bB])?")
SIZE_MULT = {
    "b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "pb": 1000 ** 5, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3,
    "tib": 1024 ** 4, "pib": 1024 ** 5,
}
