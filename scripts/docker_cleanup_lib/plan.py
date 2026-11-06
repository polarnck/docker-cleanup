"""Plan construction, loading and summary refresh."""

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
from .classify import *
from .classify import _old_enough
from .docker_cli import DockerError

def merge_peek_cache(snapshot, cached):
    if not cached or not isinstance(cached, dict):
        return
    options = cached.get("options")
    if not isinstance(options, dict) or not options.get("peek"):
        return
    peeks = {}
    cached_volumes = cached.get("volumes")
    if not isinstance(cached_volumes, list):
        return
    for v in cached_volumes:
        if not isinstance(v, dict):
            continue
        content = (v.get("details") or {}).get("content") or {}
        if not isinstance(content, dict):
            continue
        if content.get("peeked") and v.get("name"):
            peeks[str(v["name"])] = (v.get("created_at"), content, v.get("size_bytes"))
    for vol in snapshot["volumes"]:
        hit = peeks.get(vol["name"])
        if not hit:
            continue
        created, content, size = hit
        if created and vol.get("created_at") and created != vol["created_at"]:
            continue
        vol["content"] = content
        if size:
            vol["size_bytes"] = max(vol["size_bytes"], size)
        if content.get("newest_mtime"):
            new = parse_dt(content["newest_mtime"])
            current = parse_dt(vol.get("last_activity_at"))
            if new is not None and (current is None or _dt_lt(current, new)):
                vol["last_activity_at"] = content["newest_mtime"]
                vol["activity_source"] = "file_mtime"


def skipped_summary(snapshot, days, chosen_ids):
    buckets = {}
    for item in build_items(snapshot):
        if item["id"] in chosen_ids:
            continue
        if item["verdict"] == "protected":
            code = "protected"
        elif not _old_enough(item, days):
            code = "too_recent"
        else:
            code = "not_selected_for_depth"
        entry = buckets.setdefault(code, {"reason_code": code, "count": 0, "bytes": 0})
        entry["count"] += 1
        entry["bytes"] += item["size_bytes"]
    return [buckets[k] for k in sorted(buckets)]


def build_plan(snapshot, doctor, depth, days, flags, options):
    cutoff_at = iso_z(now_utc() - datetime.timedelta(days=days)) if days > 0 else None
    items = depth_candidates(snapshot, depth, days, flags, cutoff_at)
    by_kind = {}
    for i in items:
        entry = by_kind.setdefault(i["kind"], {"items": 0, "bytes": 0, "approved": 0})
        entry["items"] += 1
        entry["bytes"] += i["size_bytes"]
        if i["approved"]:
            entry["approved"] += 1
    approved = [i for i in items if i["approved"] and i["selected"]]
    plan = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": uuid.uuid4().hex,
        "created_at": iso_now(),
        "context": doctor["context"]["name"],
        "context_is_remote": doctor["context"]["remote"],
        "daemon_id_hash": doctor["daemon"]["id_hash"],
        "depth": depth,
        "older_than_days": days,
        "cutoff_at": cutoff_at,
        "flags": flags,
        "options": options,
        "items": items,
        "summary": {
            "items": len(items),
            "bytes": sum(i["size_bytes"] for i in items),
            "approved": len(approved),
            "approved_bytes": sum(i["size_bytes"] for i in approved),
            "by_kind": by_kind,
        },
        "skipped": skipped_summary(snapshot, days, set(i["id"] for i in items)),
        "warnings": list(snapshot.get("warnings") or []),
        "commands": [i["command"] for i in approved],
    }
    if depth in ("standard", "deep") and not options.get("peek"):
        plan["warnings"].append({"code": "no_volume_peek", "severity": "info", "params": {}})
    return plan


def load_plan(path):
    if not os.path.exists(path):
        raise DockerError("plan_missing", "Plan file not found: %s" % path)
    try:
        plan = read_json(path)
    except OSError as exc:
        raise DockerError("plan_missing", "Cannot read plan file: %s" % exc)
    except ValueError as exc:
        raise DockerError("plan_invalid", "Plan file is not valid JSON: %s" % exc)
    if not isinstance(plan, dict):
        raise DockerError("plan_invalid", "Plan file must contain a JSON object.")
    if not isinstance(plan.get("items", []), list):
        raise DockerError("plan_invalid", "Plan 'items' must be a list.")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise DockerError("schema_mismatch", _("schema_mismatch"))
    for field in ("plan_id", "created_at", "context", "depth"):
        if not isinstance(plan.get(field), str) or not plan.get(field):
            raise DockerError("plan_invalid", "Plan has an invalid %s." % field)
    if plan.get("depth") not in ("safe", "standard", "deep", "custom"):
        raise DockerError("plan_invalid", "Plan has an unknown depth.")
    if "options" in plan and not isinstance(plan.get("options"), dict):
        raise DockerError("plan_invalid", "Plan options must be an object.")
    if not isinstance(plan.get("summary"), dict):
        raise DockerError("plan_invalid", "Plan summary must be an object.")
    if not isinstance(plan.get("cutoff_at"), (str, type(None))):
        raise DockerError("plan_invalid", "Plan cutoff_at must be a string or null.")
    if plan.get("cutoff_at"):
        cutoff_dt = parse_dt(plan.get("cutoff_at"))
        if cutoff_dt is None:
            raise DockerError("plan_invalid", "Plan cutoff_at is not a valid timestamp.")
        if cutoff_dt > now_utc() + datetime.timedelta(seconds=60):
            raise DockerError("plan_invalid", "Plan cutoff_at is in the future.")
    for index, item in enumerate(plan.get("items", [])):
        if not isinstance(item, dict):
            raise DockerError("plan_invalid", "Plan item %d is not an object." % index)
        for field in ("id", "kind", "name", "object_id", "verdict", "reason_code"):
            value = item.get(field)
            if not isinstance(value, str) or not value:
                raise DockerError("plan_invalid",
                                  "Plan item %d has an invalid %s." % (index, field))
        if item["kind"] not in KIND_ORDER:
            raise DockerError("plan_invalid",
                              "Plan item %d has unknown kind %r." % (index, item["kind"]))
        if item["verdict"] not in ("safe", "cost", "decision", "protected"):
            raise DockerError("plan_invalid",
                              "Plan item %d has unknown verdict %r." % (index, item["verdict"]))
        if not isinstance(item.get("approved"), bool):
            raise DockerError("plan_invalid",
                              "Plan item %d has a non-boolean approved flag." % index)
        if "selected" in item and not isinstance(item.get("selected"), bool):
            raise DockerError("plan_invalid",
                              "Plan item %d has a non-boolean selected flag." % index)
        if not isinstance(item.get("remove"), dict):
            raise DockerError("plan_invalid",
                              "Plan item %d has no remove object." % index)
        if "size_bytes" in item and not isinstance(item.get("size_bytes"), (int, float)):
            raise DockerError("plan_invalid",
                              "Plan item %d has a non-numeric size." % index)
        if "reason_code" in item and not isinstance(item.get("reason_code"), str):
            raise DockerError("plan_invalid",
                              "Plan item %d has a non-string reason_code." % index)
        if "restore" in item and not isinstance(item.get("restore"), dict):
            raise DockerError("plan_invalid",
                              "Plan item %d has a non-object restore spec." % index)
        if "details" in item and not isinstance(item.get("details"), dict):
            raise DockerError("plan_invalid",
                              "Plan item %d has a non-object details." % index)
        if "confirm" in item and not isinstance(item.get("confirm"), str):
            raise DockerError("plan_invalid",
                              "Plan item %d has a non-string confirm." % index)
        if "command" in item and not isinstance(item.get("command"), str):
            raise DockerError("plan_invalid",
                              "Plan item %d has a non-string command." % index)
        if "default_approved" in item and not isinstance(item.get("default_approved"), bool):
            raise DockerError("plan_invalid",
                              "Plan item %d has a non-boolean default_approved." % index)
    if "older_than_days" not in plan:
        raise DockerError("plan_invalid", "Plan older_than_days is missing.")
    try:
        days = int(plan.get("older_than_days") or 0)
    except (TypeError, ValueError):
        raise DockerError("plan_invalid", "Plan older_than_days is not an integer.")
    if days < 0:
        raise DockerError("plan_invalid", "Plan older_than_days is negative.")
    if days > 0 and not plan.get("cutoff_at"):
        raise DockerError("plan_invalid", "Plan cutoff_at is missing for an age-limited plan.")
    return plan


def refresh_plan_summary(plan):
    """Recompute summary, by_kind and commands after select changes."""
    items = plan.get("items", [])
    by_kind = {}
    for item in items:
        entry = by_kind.setdefault(item["kind"], {"items": 0, "bytes": 0, "approved": 0})
        entry["items"] += 1
        entry["bytes"] += int(item.get("size_bytes") or 0)
        if item.get("approved") and item.get("selected", True):
            entry["approved"] += 1
    approved = [i for i in items if i.get("approved") and i.get("selected", True)]
    plan["summary"] = {
        "items": len(items),
        "bytes": sum(int(i.get("size_bytes") or 0) for i in items),
        "approved": len(approved),
        "approved_bytes": sum(int(i.get("size_bytes") or 0) for i in approved),
        "by_kind": by_kind,
    }
    plan["commands"] = [c for c in (i.get("command") or "" for i in approved) if c]
    return plan
