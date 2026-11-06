"""Read-only Docker snapshot collection and cache summaries."""

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from .constants import *
from .constants import _
from .helpers import *

def system_df_rows(docker):
    return json_lines(docker, ["system", "df", "--format", "{{json .}}"], timeout=120)


def system_df_v(docker):
    raw = docker.out(["system", "df", "-v", "--format", "{{json .}}"], timeout=120,
                     allow_failure=True)
    data = parse_json_any(raw)
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and ("Images" in row or "Volumes" in row):
                return row
        return {}
    return data if isinstance(data, dict) else {}


def buildx_du(docker):
    data = parse_json_any(docker.out(["buildx", "du", "--format", "json"], timeout=120,
                                     allow_failure=True))
    if isinstance(data, dict):
        records = data.get("records") or data.get("Records") or []
        if isinstance(records, list):
            return records
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def normalize_cache_records(dfv, buildx_records):
    rows = []
    if buildx_records:
        source = "buildx"
        for r in buildx_records:
            rows.append({
                "id": str(r.get("ID") or r.get("id") or ""),
                "size": parse_size(r.get("Size") or r.get("size") or 0),
                "last_used_at": r.get("LastUsedAt") or r.get("Last Used At") or r.get("lastUsedAt"),
                "in_use": as_bool(r.get("InUse") if "InUse" in r else r.get("In Use")),
                "shared": as_bool(r.get("Shared") or r.get("shared")),
                "cache_type": str(r.get("Type") or r.get("CacheType") or "regular"),
            })
    else:
        source = "system-df" if (dfv.get("BuildCache") or dfv.get("Build Cache")) else "none"
        for r in dfv.get("BuildCache") or dfv.get("Build Cache") or []:
            if not isinstance(r, dict):
                continue
            rows.append({
                "id": str(r.get("ID") or ""),
                "size": parse_size(r.get("Size") or 0),
                "last_used_at": r.get("Last Used At") or r.get("LastUsedAt"),
                "in_use": as_bool(r.get("In Use") if "In Use" in r else r.get("InUse")),
                "shared": as_bool(r.get("Shared")),
                "cache_type": str(r.get("Cache Type") or r.get("Type") or "regular"),
            })
    return rows, source


def cache_summary(records, now=None, source="none"):
    now = now or now_utc()
    total = sum(r["size"] for r in records)
    reclaimable = sum(r["size"] for r in records if not r["in_use"])
    buckets = {}
    for label, days in (("7d", 7), ("30d", 30), ("90d", 90)):
        total_bucket = 0
        for r in records:
            if r["in_use"]:
                continue
            age = age_days(r["last_used_at"], now)
            if age is None or age >= days:
                total_bucket += r["size"]
        buckets[label] = total_bucket
    buckets["all"] = reclaimable
    used = sorted([r["last_used_at"] for r in records if r["last_used_at"]])
    oldest = used[0] if used else None
    return {
        "total_bytes": total,
        "reclaimable_bytes": reclaimable,
        "records": len(records),
        "in_use_records": sum(1 for r in records if r["in_use"]),
        "oldest_last_used_at": oldest,
        "buckets": buckets,
        "builder": source,
        "buildx_available": source == "buildx",
        "records_detail": records,
    }


def image_is_base(repo):
    if not repo:
        return False
    r = repo.lower()
    if r in ("<none>", ""):
        return False
    last = r.rsplit("/", 1)[-1]
    return last in BASE_REPOS or r == "mcr.microsoft.com/mssql/server"


def volume_activity_from_fs(mountpoint, budget=2000):
    """Newest mtime below a volume mountpoint, bounded and read-only.

    Returns ``(timestamp, approximate)``.  Permission errors are normal when the
    daemon runs as root and the caller does not.
    """
    if not mountpoint or not os.path.isdir(mountpoint):
        return None, True
    newest = None
    root_mtime = None
    try:
        root_mtime = os.stat(mountpoint).st_mtime
    except OSError:
        pass
    seen = 0
    stack = [(mountpoint, 0)]
    try:
        while stack and seen < budget:
            current, depth = stack.pop()
            try:
                entries = os.scandir(current)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    seen += 1
                    if seen >= budget:
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False) and depth < 4:
                            stack.append((entry.path, depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            newest = max(newest or 0, entry.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        continue
    except OSError:
        return None, True
    if newest is None:
        newest = root_mtime
    if newest is None:
        return None, True
    return datetime.datetime.fromtimestamp(newest, tz=datetime.timezone.utc), True


def collect_snapshot(docker, want_registry=True, want_peek=False, registry_cache_path=None,
                     warnings=None, allow_remote=True):
    warnings = warnings if warnings is not None else []
    dfv = system_df_v(docker)
    if not dfv:
        warnings.append({"code": "system_df_unavailable", "severity": "medium",
                         "params": {}})
    ps_rows = json_lines(docker, ["ps", "-a", "-s", "--format", "{{json .}}"])
    container_ids = [r.get("ID") for r in ps_rows if r.get("ID")]
    c_inspect = inspect_many(docker, container_ids)

    img_rows = json_lines(docker, ["images", "--format", "{{json .}}"])
    img_ids = []
    for row in img_rows:
        iid = row.get("ID") or row.get("Id")
        if iid and iid not in img_ids:
            img_ids.append(iid)
    i_inspect = inspect_many(docker, img_ids, prefix=["image"])

    vol_rows = json_lines(docker, ["volume", "ls", "--format", "{{json .}}"])
    vol_names = [r.get("Name") for r in vol_rows if r.get("Name")]
    v_inspect = inspect_many(docker, vol_names, prefix=["volume"])

    net_rows = json_lines(docker, ["network", "ls", "--format", "{{json .}}"])
    net_ids = [r.get("ID") for r in net_rows if r.get("ID")]
    n_inspect = inspect_many(docker, net_ids, prefix=["network"])

    cache_records, cache_source = normalize_cache_records(dfv, buildx_du(docker))
    cache = cache_summary(cache_records, source=cache_source)

    # container index by id
    containers = []
    by_container_id = {}
    for row in ps_rows:
        cid = str(row.get("ID") or "")
        if not cid:
            continue
        obj = lookup_id(c_inspect, cid) or {}
        name = str(obj.get("Name") or row.get("Names") or "").lstrip("/")
        state = (obj.get("State") or {})
        status = str(state.get("Status") or row.get("State") or "")
        mounts = []
        for m in obj.get("Mounts") or []:
            mounts.append({
                "type": m.get("Type"),
                "name": m.get("Name") or "",
                "source": m.get("Source") or "",
                "destination": m.get("Destination") or "",
                "rw": bool(m.get("RW", True)),
            })
        labels = ((obj.get("Config") or {}).get("Labels") or {})
        env_names = []
        for item in (obj.get("Config") or {}).get("Env") or []:
            if isinstance(item, str):
                env_names.append(item.split("=", 1)[0])
        compose_project = labels.get("com.docker.compose.project") or ""
        compose_files = [f.strip() for f in str(labels.get("com.docker.compose.project.config_files") or "").split(",") if f.strip()]
        compose_workdir = labels.get("com.docker.compose.project.working_dir") or ""
        resolved_files = []
        for f in compose_files:
            if not os.path.isabs(f) and compose_workdir:
                f = os.path.normpath(os.path.join(compose_workdir, f))
            resolved_files.append(f)
        compose_exists = bool(resolved_files) and all(os.path.exists(f) for f in resolved_files)
        finished = state.get("FinishedAt") or ""
        started = state.get("StartedAt") or ""
        created = obj.get("Created") or row.get("CreatedAt") or ""
        full_container_id = str((obj or {}).get("Id") or cid)
        containers.append({
            "container_id": full_container_id,
            "image_id": str(obj.get("Image") or ""),
            "image_ref": str((obj.get("Config") or {}).get("Image") or row.get("Image") or ""),
            "name": name,
            "state": status,
            "status_text": str(row.get("Status") or ""),
            "created_at": created,
            "started_at": started,
            "finished_at": finished,
            "exit_code": state.get("ExitCode"),
            "restart_policy": str(((obj.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"),
            "ports": str(row.get("Ports") or ""),
            "health": str(state.get("Health", {}).get("Status") if isinstance(state.get("Health"), dict) else ""),
            "labels": labels,
            "compose_project": compose_project,
            "compose_service": labels.get("com.docker.compose.service") or "",
            "compose_files": resolved_files,
            "compose_workdir": compose_workdir,
            "compose_config_exists": compose_exists,
            "mounts": mounts,
            "networks": sorted(((obj.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
            "writable_bytes": parse_size(row.get("Size") or 0),
            "env_names": sorted(env_names),
        })
        by_container_id[cid] = containers[-1]
        by_container_id[full_container_id] = containers[-1]
        if full_container_id.startswith("sha256:"):
            by_container_id[full_container_id[7:]] = containers[-1]
            by_container_id[full_container_id[7:][:12]] = containers[-1]
        else:
            by_container_id[full_container_id[:12]] = containers[-1]

    # volume usage
    volume_users = {}
    for c in containers:
        for m in c["mounts"]:
            if m["type"] == "volume" and m["name"]:
                volume_users.setdefault(m["name"], []).append(c["name"])

    # image usage: inspect Image is a full id; list rows use short ids and a
    # container may reference any tag of the same image.
    image_users = {}
    tag_users = {}
    for c in containers:
        name = c["name"]
        iid = c["image_id"] or ""
        if iid:
            image_users.setdefault(iid, []).append(name)
            raw = iid[7:] if iid.startswith("sha256:") else iid
            image_users.setdefault(raw[:12], []).append(name)
        ref = c["image_ref"] or ""
        if ref:
            tag_users.setdefault(ref, []).append(name)

    def image_used_by(image_id, tags):
        users = []
        raw = image_id[7:] if image_id.startswith("sha256:") else image_id
        for key in (image_id, raw, raw[:12]):
            users.extend(image_users.get(key, []))
        for tag in tags:
            users.extend(tag_users.get(tag, []))
        seen = set()
        unique = []
        for name in users:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique

    # images grouped by image id (one logical image, many tags)
    images_by_id = {}
    for row in img_rows:
        iid = str(row.get("ID") or "")
        if not iid:
            continue
        entry = images_by_id.setdefault(iid, {
            "image_id": iid,
            "tags": [],
            "size": 0,
            "created_at": "",
            "repo": "",
            "tag": "",
        })
        repo = str(row.get("Repository") or "<none>")
        tag = str(row.get("Tag") or "<none>")
        if repo != "<none>" and tag != "<none>":
            entry["tags"].append("%s:%s" % (repo, tag))
            if not entry["repo"]:
                entry["repo"], entry["tag"] = repo, tag
        entry["size"] = max(entry["size"], parse_size(row.get("UniqueSize") or row.get("Size") or 0))
        entry["created_at"] = entry["created_at"] or str(row.get("CreatedAt") or "")

    # compose image references (evidence for local builds)
    compose_image_refs = set(c["image_ref"] for c in containers
                             if c["compose_project"] and c["compose_config_exists"])
    compose_image_repos = set()
    for ref in compose_image_refs:
        last = ref.rsplit("/", 1)[-1]
        compose_image_repos.add(ref.rsplit(":", 1)[0] if ":" in last else ref)

    images = []
    for iid, entry in images_by_id.items():
        obj = lookup_id(i_inspect, iid) or {}
        created = obj.get("Created") or entry["created_at"]
        repo_digests = obj.get("RepoDigests") or []
        tags = entry["tags"]
        dangling = not tags
        repo = entry["repo"] or "<none>"
        primary_tag = tags[0] if tags else ""
        users = image_used_by(iid, tags)
        full_image_id = str((obj or {}).get("Id") or iid)
        images.append({
            "image_id": full_image_id,
            "name": primary_tag or ("<none> (%s)" % short_id(full_image_id)),
            "tags": tags,
            "repo": repo,
            "tag": entry["tag"] or "<none>",
            "repo_digests": repo_digests,
            "dangling": dangling,
            "base": image_is_base(repo),
            "size_bytes": entry["size"],
            "created_at": created,
            "used_by": users,
        })

    volumes = []
    dfv_volumes = {}
    for row in dfv.get("Volumes") or []:
        if isinstance(row, dict) and row.get("Name"):
            dfv_volumes[str(row["Name"])] = row
    for row in vol_rows:
        name = str(row.get("Name") or "")
        if not name:
            continue
        obj = v_inspect.get(name) or {}
        dfrow = dfv_volumes.get(name, {})
        labels = obj.get("Labels") or {}
        mountpoint = str(obj.get("Mountpoint") or row.get("Mountpoint") or "")
        size = parse_size(dfrow.get("Size") or row.get("Size") or 0)
        fs_mtime, fs_approx = volume_activity_from_fs(mountpoint)
        users = volume_users.get(name, [])
        last_activity = None
        source = "unknown"
        if users:
            stamps = [max((c["started_at"] or "", c["finished_at"] or "")) for c in containers
                      if c["name"] in users]
            stamps = [s for s in stamps if s]
            if stamps:
                last_activity, source = max(stamps), "container_timestamp"
        if fs_mtime:
            previous = parse_dt(last_activity)
            if previous is None or fs_mtime > previous:
                last_activity, source = iso_z(fs_mtime), "file_mtime"
        volumes.append({
            "name": name,
            "anonymous": is_anonymous_volume(name),
            "mountpoint": mountpoint,
            "project": labels.get("com.docker.compose.project") or "",
            "compose_volume": labels.get("com.docker.compose.volume") or "",
            "links": int(dfrow.get("Links") or row.get("Links") or 0) if str(dfrow.get("Links") or row.get("Links") or "0").isdigit() else 0,
            "created_at": obj.get("CreatedAt") or row.get("CreatedAt") or "",
            "used_by": users,
            "size_bytes": size,
            "last_activity_at": last_activity,
            "activity_source": source,
            "activity_approx": fs_approx or source == "unknown",
            "driver": obj.get("Driver") or row.get("Driver") or "local",
        })

    # network usage from inspect Containers map
    networks = []
    for row in net_rows:
        nid = str(row.get("ID") or "")
        obj = lookup_id(n_inspect, nid) or {}
        name = str(obj.get("Name") or row.get("Name") or "")
        labels = obj.get("Labels") or {}
        attached = obj.get("Containers") or {}
        used_by = []
        for cid, meta in attached.items():
            c = by_container_id.get(str(cid)) or by_container_id.get(str(cid)[:12])
            used_by.append(c["name"] if c else short_id(cid))
        builtin = name in BUILTIN_NETWORKS
        full_network_id = str((obj or {}).get("Id") or nid)
        networks.append({
            "network_id": full_network_id,
            "name": name,
            "driver": str(obj.get("Driver") or row.get("Driver") or ""),
            "scope": str(obj.get("Scope") or row.get("Scope") or "local"),
            "created_at": obj.get("Created") or row.get("CreatedAt") or "",
            "builtin": builtin,
            "swarm": str(obj.get("Scope") or "").lower() == "swarm" or name == "ingress",
            "used_by": used_by,
            "project": labels.get("com.docker.compose.project") or "",
            "labels": labels,
        })

    # compose projects
    projects = {}
    for c in containers:
        if not c["compose_project"]:
            continue
        proj = projects.setdefault(c["compose_project"], {
            "name": c["compose_project"],
            "services": [],
            "config_files": c["compose_files"],
            "working_dir": c["compose_workdir"],
            "config_exists": c["compose_config_exists"],
            "containers": [],
        })
        proj["containers"].append(c["name"])
        if c["compose_service"] and c["compose_service"] not in proj["services"]:
            proj["services"].append(c["compose_service"])
        if not proj["config_files"] and c["compose_files"]:
            proj["config_files"] = c["compose_files"]
        proj["config_exists"] = proj["config_exists"] and c["compose_config_exists"]

    # bind mounts
    binds = []
    for c in containers:
        for m in c["mounts"]:
            if m["type"] != "bind":
                continue
            src = m["source"] or ""
            exists = bool(src) and os.path.exists(src)
            note = []
            if not exists:
                note.append("path does not exist")
            if src == "/tmp" or src.startswith("/tmp/") or src == "/var/tmp" or src.startswith("/var/tmp/"):
                note.append("under /tmp; lost on host reboot")
            try:
                mtime = iso_z(datetime.datetime.fromtimestamp(os.stat(src).st_mtime,
                                                              tz=datetime.timezone.utc)) if exists else None
            except OSError:
                mtime = None
            binds.append({
                "container": c["name"],
                "container_state": c["state"],
                "source": src,
                "destination": m["destination"],
                "rw": m["rw"],
                "exists": exists,
                "last_activity_at": mtime,
                "note": ", ".join(note),
            })

    return {
        "dfv": dfv,
        "containers": containers,
        "images": images,
        "volumes": volumes,
        "networks": networks,
        "cache": cache,
        "compose_projects": list(projects.values()),
        "bind_mounts": binds,
        "warnings": warnings,
        "compose_image_refs": compose_image_refs,
        "compose_image_repos": compose_image_repos,
    }


def df_totals(docker):
    rows = system_df_rows(docker)
    total = 0
    reclaimable = 0
    for row in rows:
        total += parse_size(row.get("Size") or 0)
        reclaimable += parse_size(row.get("Reclaimable") or 0)
    return {"total": total, "reclaimable": reclaimable, "rows": rows}
