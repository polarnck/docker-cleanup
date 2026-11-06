"""Verdicts, items, depth selection and audit payload."""

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
from .helpers import _dt_lt

def classify(snapshot):
    for c in snapshot["containers"]:
        state = (c["state"] or "").lower()
        is_running = state in ("running", "paused", "restarting")
        anon = [m for m in c["mounts"] if m["type"] == "volume" and
                (not m["name"] or is_anonymous_volume(m["name"]))]
        binds = [m for m in c["mounts"] if m["type"] == "bind"]
        named = [m for m in c["mounts"] if m["type"] == "volume" and m["name"]]
        if is_running:
            verdict, code = "protected", "running_service"
        elif state == "dead":
            verdict, code = "cost", "dead_container"
        elif anon:
            verdict, code = "cost", "anonymous_volume"
        elif c["compose_project"]:
            if c["compose_config_exists"]:
                verdict, code = "safe", "compose_recreatable"
            else:
                verdict, code = "cost", "compose_config_missing"
        elif binds and not named:
            verdict, code = "safe", "bind_only"
        elif named:
            verdict, code = "safe", "named_volume"
        elif c["writable_bytes"] > WRITABLE_LAYER_THRESHOLD:
            verdict, code = "cost", "writable_layer"
        else:
            verdict, code = "safe", "no_data"
        c["verdict"] = verdict
        c["reason_code"] = code
        if is_running:
            c["last_activity_at"] = None
            c["activity_source"] = "running"
        else:
            c["last_activity_at"] = (c["finished_at"] if parse_dt(c["finished_at"]) else
                                     c["started_at"] if parse_dt(c["started_at"]) else c["created_at"])
            c["activity_source"] = "finished_at"

    for img in snapshot["images"]:
        running = [u for u in img["used_by"] if _container_is_running(snapshot, u)]
        if running:
            verdict, code = "protected", "running_container"
        elif img["used_by"]:
            verdict, code = "cost", "stopped_container"
        elif img["dangling"]:
            verdict, code = "safe", "dangling_image"
        elif img["base"]:
            verdict, code = "cost", "base_image"
        else:
            reg = img.get("registry") if isinstance(img.get("registry"), dict) else {}
            local_compose = (img["repo"] in snapshot.get("compose_image_refs", set()) or
                             img["repo"] in snapshot.get("compose_image_repos", set()))
            if reg.get("exists") is False:
                if local_compose:
                    verdict, code = "cost", "local_build_recreatable"
                else:
                    verdict, code = "decision", "local_only_image"
            elif reg.get("checked") is False:
                verdict, code = "cost", "registry_unknown"
            elif reg.get("exists") is None:
                verdict, code = "cost", "registry_unknown"
            elif reg.get("matches_local"):
                verdict, code = "safe", "registry_identical"
            elif reg.get("ambiguous"):
                verdict, code = "cost", "registry_digest_ambiguous"
            elif not (reg.get("local_digests") or []):
                verdict, code = "cost", "registry_unverifiable"
            else:
                verdict, code = "cost", "registry_different"
        img["verdict"] = verdict
        img["reason_code"] = code
        img["last_activity_at"] = img["created_at"]
        img["activity_source"] = "created_at"

    for vol in snapshot["volumes"]:
        users = vol["used_by"]
        running_users = [u for u in users if _container_is_running(snapshot, u)]
        low = vol["name"].lower()
        content = vol.get("content") if isinstance(vol.get("content"), dict) else {}
        is_cache = any(h in low for h in CACHE_HINTS)
        is_data = any(h in low for h in DATA_HINTS) and not is_cache
        if running_users:
            verdict, code = "protected", "volume_in_use_running"
        elif users:
            verdict, code = "protected", "volume_in_use_stopped"
        elif content.get("peeked") and content.get("empty"):
            verdict, code = "safe", "volume_empty"
        elif is_data:
            verdict, code = "decision", "volume_data_name"
        elif is_cache:
            verdict, code = "cost", "volume_cache_name"
        elif vol["anonymous"]:
            verdict, code = "decision", "volume_anonymous"
        else:
            verdict, code = "decision", "volume_unknown"
        vol["verdict"] = verdict
        vol["reason_code"] = code

    for net in snapshot["networks"]:
        if net["builtin"]:
            verdict, code = "protected", "builtin_network"
        elif net["swarm"]:
            verdict, code = "protected", "swarm_network"
        elif net["used_by"]:
            running_users = [u for u in net["used_by"] if _container_is_running(snapshot, u)]
            if running_users:
                verdict, code = "protected", "network_in_use"
            else:
                # No data lives in a network.  It becomes removable after the
                # stopped containers attached to it are removed; depth_candidates
                # adds it only when every attached container is in the plan.
                verdict, code = "safe", "network_stopped_users"
        elif net["project"]:
            project_ok = None
            for proj in snapshot.get("compose_projects") or []:
                if proj["name"] == net["project"]:
                    project_ok = proj["config_exists"]
                    break
            if project_ok is False:
                verdict, code = "cost", "compose_network_missing"
            else:
                verdict, code = "safe", "compose_network"
        else:
            verdict, code = "safe", "unused_network"
        net["verdict"] = verdict
        net["reason_code"] = code
        net["last_activity_at"] = net["created_at"]
        net["activity_source"] = "created_at"


def _container_is_running(snapshot, name):
    for c in snapshot["containers"]:
        if c["name"] == name:
            return (c["state"] or "").lower() in ("running", "paused", "restarting")
    return False


def build_items(snapshot):
    items = []
    for c in snapshot["containers"]:
        items.append({
            "id": "container:%s" % short_id(c["container_id"], 16),
            "kind": "container",
            "name": c["name"],
            "object_id": c["container_id"],
            "verdict": c["verdict"],
            "risk": c["verdict"],
            "reason_code": c["reason_code"],
            "reason_params": {},
            "size_bytes": c["writable_bytes"],
            "created_at": c["created_at"],
            "last_activity_at": c["last_activity_at"],
            "activity_source": c["activity_source"],
            "evidence": ["docker ps -a -s", "docker inspect"],
            "details": {
                "state": c["state"], "status": c["status_text"],
                "image_ref": c["image_ref"], "image_id": c["image_id"],
                "exit_code": c["exit_code"], "started_at": c["started_at"],
                "finished_at": c["finished_at"], "restart_policy": c["restart_policy"],
                "ports": c["ports"], "health": c["health"],
                "compose_project": c["compose_project"],
                "compose_service": c["compose_service"],
                "compose_files": c["compose_files"],
                "compose_config_exists": c["compose_config_exists"],
                "mounts": c["mounts"], "networks": c["networks"],
                "writable_bytes": c["writable_bytes"],
                "env_names": c["env_names"],
            },
            "expected_state": c["state"],
            "restore": restore_spec_container(c),
            "remove": {"type": "container", "ref": c["container_id"]},
        })
    for img in snapshot["images"]:
        ref = img["tags"][0] if img["tags"] else img["image_id"]
        items.append({
            "id": "image:%s" % short_id(img["image_id"], 16),
            "kind": "image",
            "name": img["name"],
            "object_id": img["image_id"],
            "verdict": img["verdict"],
            "risk": img["verdict"],
            "reason_code": img["reason_code"],
            "reason_params": {},
            "size_bytes": img["size_bytes"],
            "created_at": img["created_at"],
            "last_activity_at": img["last_activity_at"],
            "activity_source": "created_at",
            "evidence": ["docker images", "docker image inspect", "buildx imagetools inspect"],
            "details": {
                "repo": img["repo"], "tag": img["tag"],
                "repo_digest": (img["repo_digests"] or [""])[0],
                "image_id": img["image_id"], "dangling": img["dangling"],
                "base": img["base"], "local_build": img["reason_code"] == "local_build_recreatable",
                "registry": (img.get("registry") if isinstance(img.get("registry"), dict) else {}),
                "used_by": img["used_by"], "tags": img["tags"],
            },
            "expected_state": "present",
            "restore": restore_spec_image(img),
            "remove": {"type": "image", "ref": ref},
        })
    for vol in snapshot["volumes"]:
        items.append({
            "id": "volume:%s" % vol["name"],
            "kind": "volume",
            "name": vol["name"],
            "object_id": vol["name"],
            "verdict": vol["verdict"],
            "risk": vol["verdict"],
            "reason_code": vol["reason_code"],
            "reason_params": {},
            "size_bytes": vol["size_bytes"],
            "created_at": vol["created_at"],
            "last_activity_at": vol["last_activity_at"],
            "activity_source": vol["activity_source"],
            "evidence": ["docker volume inspect", "docker system df -v"] +
                        (["read-only peek"] if (vol.get("content") or {}).get("peeked") else []),
            "details": {
                "name": vol["name"], "anonymous": vol["anonymous"],
                "mountpoint": vol["mountpoint"], "project": vol["project"],
                "compose_volume": vol["compose_volume"], "links": vol["links"],
                "created_at": vol["created_at"], "used_by": vol["used_by"],
                "content": (vol.get("content") if isinstance(vol.get("content"), dict)
                            else {"peeked": False}),
                "activity_source": vol["activity_source"],
                "activity_approx": vol.get("activity_approx", True),
            },
            "expected_state": "present",
            "restore": {"kind": "volume", "name": vol["name"],
                        "hint": "Volume data cannot be restored by Docker. Restore from your own backup if needed."},
            "remove": {"type": "volume", "ref": vol["name"]},
        })
    for net in snapshot["networks"]:
        items.append({
            "id": "network:%s" % short_id(net["network_id"], 16),
            "kind": "network",
            "name": net["name"],
            "object_id": net["network_id"],
            "verdict": net["verdict"],
            "risk": net["verdict"],
            "reason_code": net["reason_code"],
            "reason_params": {},
            "size_bytes": 0,
            "created_at": net["created_at"],
            "last_activity_at": net["created_at"],
            "activity_source": "created_at",
            "evidence": ["docker network ls", "docker network inspect"],
            "details": {
                "network_id": net["network_id"], "driver": net["driver"],
                "scope": net["scope"], "created_at": net["created_at"],
                "builtin": net["builtin"], "used_by": net["used_by"],
                "project": net["project"], "labels": net["labels"],
            },
            "expected_state": "present",
            "restore": {"kind": "network", "name": net["name"],
                        "hint": "Recreate with `docker network create %s` or `docker compose up -d`." % net["name"]},
            "remove": {"type": "network", "ref": net["network_id"]},
        })
    return items


def restore_spec_container(c):
    spec = {
        "kind": "container",
        "name": c["name"],
        "image": c["image_ref"],
        "restart_policy": c["restart_policy"],
        "ports": c["ports"],
        "mounts": [{"type": m["type"], "source": mount_source(m),
                    "destination": m["destination"], "rw": m["rw"]} for m in c["mounts"]],
        "env_names": c["env_names"],
        "env_values": "<redacted>",
        "networks": c["networks"],
        "compose_project": c["compose_project"],
        "compose_service": c["compose_service"],
        "compose_files": c["compose_files"],
    }
    if c["compose_project"] and c["compose_config_exists"]:
        files = " ".join("-f %s" % shlex.quote(f) for f in c["compose_files"])
        spec["hint"] = "docker compose %s up -d %s" % (files, c["compose_service"])
    else:
        parts = ["docker", "run", "-d", "--name", shlex.quote(c["name"])]
        if c["restart_policy"] and c["restart_policy"] != "no":
            parts += ["--restart", c["restart_policy"]]
        for m in c["mounts"]:
            src = mount_source(m)
            if src:
                parts += ["-v", "%s:%s%s" % (src, m["destination"], "" if m["rw"] else ":ro")]
        parts.append(c["image_ref"])
        spec["hint"] = " ".join(shlex.quote(p) if " " in p else p for p in parts)
    spec["note"] = ("Hint only: ports, env values and healthchecks may be missing. "
                    "Use the original compose file or your own run command.")
    return spec


def restore_spec_image(img):
    reg = img.get("registry") if isinstance(img.get("registry"), dict) else {}
    if img.get("reason_code") == "local_build_recreatable":
        return {"kind": "image", "name": img["name"], "repo_digests": img["repo_digests"],
                "registry_digest": reg.get("digest") or "",
                "hint": "Rebuild from the compose project or Dockerfile."}
    if reg.get("digest") and reg.get("exists"):
        hint = "docker pull %s@%s" % (img["repo"], reg["digest"])
    elif img["tags"]:
        hint = "docker pull %s" % img["tags"][0]
    else:
        hint = "No registry reference. Rebuild the image from its Dockerfile."
    return {"kind": "image", "name": img["name"], "repo_digests": img["repo_digests"],
            "registry_digest": reg.get("digest") or "", "hint": hint}


def _depth_match(item, depth):
    kind, code, risk = item["kind"], item["reason_code"], item["verdict"]
    if kind == "network" and code == "network_stopped_users":
        # Added by depth_candidates only after the attached stopped containers
        # are selected, so the plan is internally consistent.
        return False
    if depth == "safe":
        return ((kind == "image" and code == "dangling_image") or
                (kind == "network" and code in ("unused_network", "compose_network")))
    if depth == "standard":
        return (_depth_match(item, "safe") or
                (kind == "container" and code == "compose_recreatable") or
                (kind == "image" and code == "registry_identical") or
                (kind == "volume" and code == "volume_empty"))
    if depth == "deep":
        if kind == "container":
            return risk in ("safe", "cost")
        if kind == "image":
            return code in ("dangling_image", "registry_identical", "base_image",
                            "registry_different", "registry_digest_ambiguous",
                            "local_build_recreatable")
        if kind == "volume":
            return code in ("volume_empty", "volume_cache_name")
        if kind == "network":
            return risk == "safe"
    return False


def _old_enough(item, days):
    if days <= 0:
        return True
    ref = item.get("created_at") if item["kind"] == "image" else item.get("last_activity_at")
    if not ref:
        return False
    age = age_days(ref)
    return age is not None and age >= days


def cache_item(days, cache, approved, cutoff_at=None):
    if days <= 0:
        hours = 0
        until = None
        usable = cache["reclaimable_bytes"]
        label = "all"
    else:
        hours = int(days * 24)
        until = cutoff_at or iso_z(now_utc() - datetime.timedelta(days=days))
        cutoff = parse_dt(until)
        usable = sum(r["size"] for r in cache["records_detail"]
                     if not r["in_use"] and (parse_dt(r["last_used_at"]) is None or
                                             _dt_lt(parse_dt(r["last_used_at"]), cutoff)))
        label = "%dd" % days
    return {
        "id": "cache:%s" % label,
        "kind": "cache",
        "name": "build-cache older than %s" % (("%d days" % days) if days else "any age"),
        "object_id": "cache",
        "verdict": "safe",
        "risk": "safe",
        "reason_code": "build_cache",
        "reason_params": {"days": days},
        "size_bytes": usable,
        "created_at": cache.get("oldest_last_used_at"),
        "last_activity_at": cache.get("oldest_last_used_at"),
        "activity_source": "last_used_at",
        "evidence": ["docker system df -v", "docker buildx du"],
        "details": {"filter_hours": hours, "cutoff_at": until,
                    "records": cache["records"],
                    "in_use_records": cache["in_use_records"]},
        "restore": {"kind": "cache", "hint": "Rebuild recreates cache layers."},
        "remove": {"type": "cache", "filter_hours": hours, "until": until},
        "approved": approved,
        "selected": approved,
        "default_approved": approved,
        "confirm": "",
        "requires_decision": False,
        "command": "docker buildx prune --force%s" % (
            " --filter until=%s" % until if until else ""),
    }


def depth_candidates(snapshot, depth, days, flags=None, cutoff_at=None):
    flags = flags or {}
    items = build_items(snapshot)
    chosen = []
    for item in items:
        if item["verdict"] == "protected":
            continue
        if (snapshot.get("swarm_active") and depth != "custom" and
                item["kind"] == "image" and item["reason_code"] != "dangling_image"):
            # Swarm services can use images that are not referenced by any
            # container.  Presets must not touch tagged images on an active swarm.
            continue
        include = False
        approved = False
        if depth == "custom":
            include = True
        elif depth == "deep" and item["reason_code"] == "volume_anonymous":
            include, approved = True, False
        elif _depth_match(item, depth):
            include = True
            approved = True
        if flags.get("include_base_images") and item["reason_code"] == "base_image":
            include, approved = True, True
        if flags.get("include_local_images") and item["reason_code"] == "local_only_image":
            include, approved = True, False
        if flags.get("include_anonymous_volumes") and item["reason_code"] == "volume_anonymous":
            include, approved = True, False
        if flags.get("include_data_volumes") and item["reason_code"] in ("volume_data_name", "volume_unknown"):
            include, approved = True, False
        if not include:
            continue
        if not _old_enough(item, days):
            continue
        item["approved"] = approved
        item["selected"] = approved
        item["default_approved"] = approved
        item["confirm"] = ""
        item["requires_decision"] = item["verdict"] == "decision"
        item["command"] = command_for(item)
        item["action"] = item["command"]
        chosen.append(item)

    # A network holds no data.  If every container attached to it is a stopped
    # container already in this plan, the network becomes removable after them.
    # Standard/Deep add it automatically; Custom lists it unapproved in the
    # main loop and lets the user select it explicitly.
    if depth in ("standard", "deep"):
        chosen_ids = {i["id"] for i in chosen}
        chosen_containers = {i["name"] for i in chosen if i["kind"] == "container"}
        for item in items:
            if item["id"] in chosen_ids:
                continue
            if item["kind"] != "network" or item["reason_code"] != "network_stopped_users":
                continue
            users = set((item.get("details") or {}).get("used_by") or [])
            if not users or not users.issubset(chosen_containers):
                continue
            if not _old_enough(item, days):
                continue
            item["approved"] = True
            item["selected"] = True
            item["default_approved"] = True
            item["confirm"] = ""
            item["requires_decision"] = False
            item["depends_on"] = sorted(users)
            item["command"] = command_for(item)
            item["action"] = item["command"]
            chosen.append(item)

    if not snapshot.get("build_in_progress"):
        chosen.append(cache_item(days, snapshot["cache"], depth != "custom", cutoff_at))
        if depth == "custom":
            chosen[-1]["approved"] = False
            chosen[-1]["selected"] = False
            chosen[-1]["default_approved"] = False
        if chosen[-1]["size_bytes"] <= 0:
            chosen.pop()
    chosen.sort(key=lambda i: (KIND_ORDER.get(i["kind"], 9), -i["size_bytes"], i["name"]))
    return chosen


def command_for(item):
    kind = item["kind"]
    if kind == "container":
        return "docker rm %s" % shlex.quote(item["object_id"])
    if kind == "network":
        return "docker network rm %s" % shlex.quote(item["object_id"])
    if kind == "image":
        tags = (item.get("details") or {}).get("tags") or []
        ref = item["remove"]["ref"]
        if len(tags) <= 1:
            ref = item.get("object_id") or ref
        return "docker rmi %s" % shlex.quote(str(ref))
    if kind == "volume":
        return "docker volume rm %s" % shlex.quote(str(item.get("object_id") or item["name"]))
    if kind == "cache":
        until = item["remove"].get("until")
        if until:
            return "docker buildx prune --force --filter until=%s" % until
        hours = item["remove"].get("filter_hours") or 0
        return "docker buildx prune --force%s" % (" --filter until=%dh" % hours if hours else "")
    return ""


def compute_summary(snapshot, days=0):
    items = build_items(snapshot)
    reclaim = {"safe": 0, "cost": 0, "decision": 0, "protected": 0}
    for item in items:
        reclaim[item["verdict"]] = reclaim.get(item["verdict"], 0) + item["size_bytes"]
    reclaim["safe"] += snapshot["cache"]["reclaimable_bytes"]
    preview = {}
    for depth in ("safe", "standard", "deep"):
        cand = depth_candidates(snapshot, depth, days, {})
        approved = [i for i in cand if i["approved"]]
        preview[depth] = {"items": len(approved), "bytes": sum(i["size_bytes"] for i in approved)}
    return {
        "reclaimable": reclaim,
        "counts": {
            "containers": len(snapshot["containers"]),
            "images": len(snapshot["images"]),
            "volumes": len(snapshot["volumes"]),
            "networks": len(snapshot["networks"]),
            "cache_records": snapshot["cache"]["records"],
        },
        "depth_preview": preview,
        "age_filter_days": days,
    }


def audit_payload(snapshot, doctor, options):
    items = build_items(snapshot)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "options": options,
        "environment": doctor,
        "summary": compute_summary(snapshot, options.get("older_than_days") or 0),
        "containers": [i for i in items if i["kind"] == "container"],
        "images": [i for i in items if i["kind"] == "image"],
        "volumes": [i for i in items if i["kind"] == "volume"],
        "networks": [i for i in items if i["kind"] == "network"],
        "build_cache": {k: v for k, v in snapshot["cache"].items() if k != "records_detail"},
        "compose_projects": snapshot["compose_projects"],
        "bind_mounts": snapshot["bind_mounts"],
        "warnings": snapshot["warnings"],
    }
