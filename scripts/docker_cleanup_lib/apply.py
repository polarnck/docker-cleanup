"""Apply-time re-validation, removal and logging."""

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
from .helpers import _dt_lt, _id_short, _looks_like_id
from .docker_cli import *
from .collect import *
from .registry import *
from .peek import *
from .classify import *

def snapshot_indexes(snapshot):
    containers = {}
    for c in snapshot["containers"]:
        containers[c["container_id"]] = c
        short = _id_short(c["container_id"])
        if short:
            containers[short] = c
    images = {}
    for i in snapshot["images"]:
        images[i["image_id"]] = i
        short = _id_short(i["image_id"])
        if short:
            images[short] = i
    volumes = {v["name"]: v for v in snapshot["volumes"]}
    networks = {n["network_id"]: n for n in snapshot["networks"]}
    networks_by_name = {n["name"]: n for n in snapshot["networks"]}
    return containers, images, volumes, networks, networks_by_name


def validate_item(item, snapshot, plan=None):
    """Return (ok, reason_code, detail)."""
    plan = plan or {}
    if item.get("verdict") == "protected" or item.get("risk") == "protected":
        return False, "protected_in_plan", "PROTECTED item"
    cutoff = parse_dt(plan.get("cutoff_at"))
    idx = snapshot_indexes(snapshot)
    containers, images, volumes, networks, networks_by_name = idx
    kind = item.get("kind")
    oid = str(item.get("object_id") or "")
    if kind == "container":
        c = containers.get(oid)
        if not c:
            return False, "missing", "container no longer exists"
        if (c["state"] or "").lower() not in ("exited", "created", "dead"):
            return False, "state_changed", "container state is %s" % c["state"]
        if cutoff:
            current = parse_dt(c.get("last_activity_at") or c.get("finished_at"))
            if current is None:
                return False, "age_unknown", "container activity timestamp is missing"
            if current >= cutoff:
                return False, "too_new_since_plan", "container activity is newer than the plan cutoff"
            planned = parse_dt(item.get("last_activity_at"))
            current_finished = parse_dt(c.get("finished_at"))
            if planned and current_finished and abs((current_finished - planned).total_seconds()) > 1:
                return False, "state_changed", "container finished_at changed after the plan"
        return True, "", ""
    if kind == "network":
        # Exact ID only.  A compose network can be recreated with the same name
        # and a new ID; removing the new one would be an unplanned action.
        n = networks.get(oid)
        if not n:
            return False, "state_changed", "network no longer exists or was recreated with the same name"
        if n["builtin"]:
            return False, "builtin_network", "builtin network"
        if n["swarm"]:
            return False, "swarm_network", "swarm network"
        if n["used_by"]:
            return False, "in_use", "network is used by %s" % ", ".join(n["used_by"])
        if cutoff:
            current = parse_dt(n.get("created_at"))
            if current is None:
                return False, "age_unknown", "network creation timestamp is missing"
            if current >= cutoff:
                return False, "too_new_since_plan", "network is newer than the plan cutoff"
        return True, "", ""
    if kind == "image":
        img = images.get(oid)
        if not img:
            return False, "missing", "image no longer exists (exact ID required)"
        ref = str(item.get("remove", {}).get("ref") or "")
        image_raw = str(img["image_id"])
        if image_raw.startswith("sha256:"):
            image_raw = image_raw[7:]
        if _looks_like_id(ref):
            ref_raw = ref[7:] if ref.startswith("sha256:") else ref
            if not (image_raw == ref_raw or (len(ref_raw) >= 12 and image_raw.startswith(ref_raw))):
                return False, "object_id_mismatch", "removal reference is a different image ID"
        elif img["tags"]:
            if ref not in img["tags"]:
                return False, "tag_changed", "tag %s is gone" % ref
        elif ref != img["image_id"]:
            return False, "tag_changed", "image has no tag matching %s" % ref
        if img["used_by"]:
            return False, "in_use", "image is used by %s" % ", ".join(img["used_by"])
        if cutoff:
            current = parse_dt(img.get("created_at"))
            if current is None:
                return False, "age_unknown", "image creation timestamp is missing"
            if current >= cutoff:
                return False, "too_new_since_plan", "image is newer than the plan cutoff"
        return True, "", ""
    if kind == "volume":
        vol = volumes.get(oid)
        if not vol:
            return False, "missing", "volume no longer exists"
        expected_created = item.get("created_at")
        if expected_created and vol.get("created_at") and expected_created != vol["created_at"]:
            return False, "state_changed", "volume was recreated with the same name"
        if vol["used_by"]:
            return False, "in_use", "volume is in use by %s" % ", ".join(vol["used_by"])
        if cutoff:
            current = parse_dt(vol.get("last_activity_at"))
            if current is None:
                return False, "age_unknown", "volume activity timestamp is missing"
            if current >= cutoff:
                return False, "too_new_since_plan", "volume activity is newer than the plan cutoff"
        return True, "", ""
    if kind == "cache":
        days = int(plan.get("older_than_days") or 0)
        plan_cutoff = parse_dt(plan.get("cutoff_at"))
        if days > 0 and plan_cutoff is None:
            return False, "plan_invalid", "plan cutoff_at is missing; refusing an unbounded cache prune"
        records = (snapshot.get("cache") or {}).get("records_detail") or []
        if plan_cutoff is not None:
            remaining = sum(r["size"] for r in records
                            if not r["in_use"] and (parse_dt(r["last_used_at"]) is None or
                                                    _dt_lt(parse_dt(r["last_used_at"]), plan_cutoff)))
        else:
            remaining = (snapshot.get("cache") or {}).get("reclaimable_bytes", 0)
        if remaining <= 0:
            return False, "missing", "cache already pruned"
        return True, "", ""
    if item.get("verdict") == "protected":
        return False, "protected_in_plan", "PROTECTED item"
    return False, "unknown_kind", "unknown item kind %s" % kind


def sanitize_argv(argv):
    return [mask_secret(str(a)) for a in argv]


def run_removal(docker, item, dry_run=False):
    kind = item["kind"]
    remove = item.get("remove") or {}
    if kind == "container":
        argv = ["rm", str(item["object_id"])]
    elif kind == "network":
        argv = ["network", "rm", str(item["object_id"])]
    elif kind == "image":
        # Remove a single-tag image by its immutable ID; a tag can be moved
        # between validation and rmi.  Multi-tag images are untagged one tag at
        # a time and the result is verified afterwards.
        tags = (item.get("details") or {}).get("tags") or []
        ref = remove.get("ref") or item.get("name")
        if len(tags) <= 1:
            ref = item.get("object_id") or ref
        argv = ["rmi", str(ref)]
    elif kind == "volume":
        argv = ["volume", "rm", str(item["object_id"])]
    elif kind == "cache":
        hours = int(remove.get("filter_hours") or 0)
        until = remove.get("until")
        argv = ["buildx", "prune", "--force"]
        if until:
            argv += ["--filter", "until=%s" % until]
        elif hours:
            argv += ["--filter", "until=%dh" % hours]
    else:
        return {"status": "failed", "reason": "unknown kind", "argv": []}
    reclaimed = int(item.get("size_bytes") or 0)
    if dry_run:
        return {"status": "would_remove", "reason": "", "argv": argv,
                "reclaimed_bytes": reclaimed}
    proc = docker.run(argv, timeout=300)
    if kind == "cache" and proc.returncode != 0:
        low = ((proc.stderr or "") + (proc.stdout or "")).lower()
        if "not a docker command" in low or "unknown command" in low or "unknown flag" in low:
            argv = ["builder", "prune", "--force"]
            if until:
                argv += ["--filter", "until=%s" % until]
            elif hours:
                argv += ["--filter", "until=%dh" % hours]
            proc = docker.run(argv, timeout=300)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        message = mask_secret(err[-1] if err else "command failed")
        low = message.lower()
        race = ((kind == "container" and ("is running" in low or "is paused" in low or
                                          "is restarting" in low)) or
                (kind == "volume" and "in use" in low) or
                (kind == "image" and ("being used" in low or "in use" in low)) or
                (kind == "network" and "active endpoints" in low))
        return {"status": "skipped" if race else "failed",
                "reason": message, "argv": argv, "exit_code": proc.returncode,
                "reclaimed_bytes": 0}
    if kind == "image":
        still = docker.run(["image", "inspect", str(item["object_id"])], timeout=60)
        if still.returncode == 0:
            return {"status": "removed", "reason": "untagged only; image id still present",
                    "argv": argv, "reclaimed_bytes": 0}
    return {"status": "removed", "reason": "", "argv": argv,
            "reclaimed_bytes": reclaimed}


def write_apply_log(state, plan, plan_path, results, before, after, disk_before, disk_after,
                    dry_run, removed_bytes):
    log = {
        "schema_version": SCHEMA_VERSION,
        "kind": "dry-run" if dry_run else "apply",
        "generated_at": iso_now(),
        "plan_id": plan.get("plan_id"),
        "plan_path": os.path.abspath(plan_path),
        "context": plan.get("context"),
        "daemon_id_hash": plan.get("daemon_id_hash"),
        "depth": plan.get("depth"),
        "older_than_days": plan.get("older_than_days"),
        "cutoff_at": plan.get("cutoff_at"),
        "dry_run": dry_run,
        "before": before,
        "after": after,
        "disk_before": disk_before,
        "disk_after": disk_after,
        "reclaimed_bytes_estimate": removed_bytes,
        "reclaimed_bytes_disk_delta": max(0, (disk_after or {}).get("free", 0) - (disk_before or {}).get("free", 0)),
        "reclaimed_bytes_docker_delta": max(0, (before or {}).get("total", 0) - (after or {}).get("total", 0)),
        "results": results,
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = "%s-%s-%s" % ("dry-run" if dry_run else "apply", plan.get("plan_id", "plan")[:12], stamp)
    json_path = os.path.join(state, base + ".json")
    md_path = os.path.join(state, base + ".md")
    write_json(json_path, log)
    lines = ["# %s" % (_("dry_run_title") if dry_run else _("apply_title")), ""]
    lines.append("- plan: `%s`" % log["plan_path"])
    lines.append("- context: `%s`" % log["context"])
    lines.append("- daemon: `%s`" % log["daemon_id_hash"])
    if log.get("cutoff_at"):
        lines.append("- cutoff: `%s`" % log["cutoff_at"])
    lines.append("")
    lines.append("## %s" % _("summary"))
    lines.append("")
    removed = [r for r in results if r["status"] in ("removed", "would_remove")]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]
    lines.append("- %s: %d" % (_("would_remove") if dry_run else _("removed"), len(removed)))
    lines.append("- %s: %d" % (_("skipped"), len(skipped)))
    lines.append("- %s: %d" % (_("failed"), len(failed)))
    lines.append("- %s: %s" % (_("reclaimed"), human_bytes(removed_bytes)))
    if disk_before and disk_after:
        lines.append("- %s: %s" % (_("disk_before"), human_bytes(disk_before.get("free", 0))))
        lines.append("- %s: %s" % (_("disk_after"), human_bytes(disk_after.get("free", 0))))
    lines.append("")
    lines.append(md_table(["id", "name", "status", "size", "reason"], [
        [r["id"], r["name"], status_text(r["status"]), human_bytes(r.get("size_bytes", 0)),
         r.get("reason") or ""]
        for r in results
    ]))
    lines.append("")
    lines.append("## %s" % _("commands_preview"))
    lines.append("")
    lines.append("```")
    for r in results:
        if r.get("argv"):
            lines.append("docker " + " ".join(r["argv"]))
    lines.append("```")
    lines.append("")
    lines.append("## %s" % _("restore_hint"))
    lines.append("")
    for r in results:
        if r["status"] in ("removed", "would_remove") and r.get("restore"):
            lines.append("- `%s`: %s" % (r["name"], r["restore"]))
    lines.append("")
    atomic_write(md_path, "\n".join(lines))
    return json_path, md_path


def apply_plan(docker, plan, plan_path, opts):
    state = ensure_state_dir(opts.state_dir)
    remote = doctor_data(docker)  # raises on daemon problems
    current_ctx = remote["context"]["name"]
    if current_ctx != plan.get("context"):
        raise DockerError("context_mismatch", _("context_mismatch"),
                          "plan=%s current=%s" % (plan.get("context"), current_ctx))
    if remote["context"]["remote"] and not opts.allow_remote:
        raise DockerError("remote_refused", _("remote_refused"))
    if remote["daemon"]["id_hash"] != plan.get("daemon_id_hash"):
        raise DockerError("daemon_mismatch", _("daemon_mismatch"))

    selected = [i for i in plan.get("items", []) if i.get("approved") and i.get("selected", True)]
    selected.sort(key=lambda i: KIND_ORDER.get(i.get("kind"), 9))
    build_warn = any(w.get("code") == "build_in_progress"
                     for w in (remote.get("warnings") or []))
    swarm_active = str(remote["daemon"].get("swarm") or "").lower() == "active"
    if len(selected) > MAX_ITEMS_WITHOUT_FORCE and not opts.force_count:
        raise DockerError("count_refused", _("count_refused"))
    if any(i.get("verdict") == "protected" or i.get("risk") == "protected"
           for i in plan.get("items", [])):
        raise DockerError("protected_in_plan", _("protected_in_plan"))
    if not selected:
        print(_("nothing_to_do"))
        return EXIT_NOTHING

    snapshot = collect_snapshot(docker, want_registry=False, want_peek=False,
                                warnings=[], allow_remote=True)
    # Hard safety refusals must happen before the first removal (and before any
    # optional peek helper), not halfway through the plan.  A built-in network
    # in a plan is always tampering or a bug; refuse the whole apply.
    idx = snapshot_indexes(snapshot)
    networks_idx = idx[3]
    for item in selected:
        if item.get("kind") != "network":
            continue
        oid = str(item.get("object_id") or "")
        net = networks_idx.get(oid)
        if net and net.get("builtin"):
            raise DockerError("protected_in_plan", _("protected_in_plan"),
                              "built-in network in plan: %s" % net["name"])
        # A network still used by a container is handled by validate_item()
        # after the containers earlier in the plan have been removed.
    # Re-run the read-only peek for approved empty volumes.  Without it the
    # fresh snapshot has no content evidence and a volume that was empty at plan
    # time would look like an unknown decision item.  The plan records whether
    # the user consented to a peek, so this stays within that consent.
    peek_volumes = []
    plan_options = plan.get("options") if isinstance(plan.get("options"), dict) else {}
    if plan_options.get("peek"):
        for item in selected:
            if item.get("kind") == "volume" and item.get("reason_code") == "volume_empty":
                peek_volumes.append(str(item.get("object_id") or item.get("name") or ""))
    if peek_volumes:
        apply_peek(docker, snapshot, "", snapshot.setdefault("warnings", []),
                   only=[v for v in peek_volumes if v])

    # A safe image verdict that depends on registry identity must be re-proven
    # at apply time.  The plan is user-editable; a local-only image relabelled
    # "registry_identical" must not become removable without the decision gate.
    raw_image_by_id = {}
    for img in snapshot["images"]:
        raw_image_by_id[str(img["image_id"])] = img
        short = _id_short(img["image_id"])
        if short:
            raw_image_by_id[short] = img
    registry_recheck = []
    if plan_options.get("registry_checked", True):
        for item in selected:
            if item.get("kind") != "image":
                continue
            oid = str(item.get("object_id") or "")
            raw = raw_image_by_id.get(oid) or raw_image_by_id.get(_id_short(oid))
            if raw is not None and raw not in registry_recheck:
                registry_recheck.append(raw)
    if registry_recheck:
        check_registry(docker, registry_recheck, None)

    # Always classify the fresh snapshot.  classify_if_needed() used to skip
    # this when there were no containers, which left images/volumes/networks
    # without a verdict (KeyError) and disabled the live safety merge.
    classify(snapshot)
    live_by_key = live_items_by_key(snapshot)

    results = []
    for item in selected:
        live = live_by_key.get((item.get("kind"), str(item.get("object_id"))))
        if swarm_active and item.get("kind") == "image":
            live_dangling = bool(((live or {}).get("details") or {}).get("dangling"))
            if not live_dangling:
                results.append({
                    "id": item.get("id"), "name": str(item.get("name") or ""),
                    "kind": "image", "status": "skipped", "reason_code": "swarm_active",
                    "reason": "active swarm: tagged images are protected",
                    "size_bytes": item.get("size_bytes", 0), "argv": [],
                    "restore": (item.get("restore") or {}).get("hint"),
                })
                continue
        if item.get("kind") == "cache" and build_warn:
            results.append({
                "id": item.get("id"), "name": str(item.get("name") or ""),
                "kind": "cache", "status": "skipped", "reason_code": "build_in_progress",
                "reason": _("build_in_progress"), "size_bytes": item.get("size_bytes", 0),
                "argv": [], "restore": (item.get("restore") or {}).get("hint"),
            })
            continue
        # The plan is user-editable; do not trust its verdict for the data-loss
        # gate.  Re-classified live state wins.
        planned = item.get("verdict")
        planned_reason = item.get("reason_code")
        merged = planned
        if live:
            current = live.get("verdict")
            # Never downgrade a decision item because the apply snapshot skips
            # registry checks.  A local-only image must stay decision and pass
            # the explicit gate even when the live pass calls it "cost".
            if "protected" in (planned, current):
                merged = "protected"
            elif "decision" in (planned, current):
                merged = "decision"
            else:
                merged = current or planned
            # A decision image can be hand-edited to "cost"; the plan's live
            # registry pass is skipped by design, so preserve that decision.
            if item.get("kind") == "image" and planned_reason == "local_only_image":
                if merged != "protected":
                    merged = "decision"
            item["verdict"] = merged
            item["risk"] = merged
            if merged == current:
                item["reason_code"] = live.get("reason_code", item.get("reason_code"))
        if item.get("kind") == "cache":
            remove = item.get("remove")
            if not isinstance(remove, dict):
                remove = {}
                item["remove"] = remove
            # The absolute plan cutoff is the only accepted age boundary.  A
            # hand-edited relative duration or a later timestamp is ignored.
            if int(plan.get("older_than_days") or 0) > 0:
                remove["until"] = plan.get("cutoff_at")
            else:
                remove["until"] = None
            remove["filter_hours"] = 0
        ok, code, detail = validate_item(item, snapshot, plan)
        if ok and planned == "safe" and merged == "cost":
            # The object became less safe after the plan (larger writable layer,
            # registry mismatch, missing compose file, ...).  Cost items are
            # removable in a plan that called them cost, but a safe approval is
            # not a licence to remove a newly-costly object.
            ok, code, detail = False, "reclassified", _("reclassified")
        requires = item.get("verdict") == "decision"
        approved = bool(item.get("approved")) and bool(item.get("selected", True))
        confirm = str(item.get("confirm") or "")
        name = str(item.get("name") or "")
        if requires:
            expected_confirm = str(item.get("object_id") or name) if item.get("kind") == "volume" else name
            if not opts.allow_decision:
                ok, code, detail = False, "decision_skipped", _("decision_skipped")
            elif not approved:
                ok, code, detail = False, "decision_skipped", _("decision_skipped")
            elif confirm != expected_confirm:
                ok, code, detail = False, "confirm_mismatch", _("decision_skipped")
        if not ok:
            results.append({
                "id": item.get("id"), "name": name, "kind": item.get("kind"),
                "status": "skipped", "reason_code": code, "reason": detail,
                "size_bytes": item.get("size_bytes", 0), "reclaimed_bytes": 0,
                "argv": [], "restore": (item.get("restore") or {}).get("hint"),
            })
            continue
        try:
            outcome = run_removal(docker, item, dry_run=opts.dry_run)
        except DockerError as exc:
            outcome = {"status": "failed", "reason": mask_secret(exc.message or exc.code),
                       "argv": [], "reclaimed_bytes": 0}
        results.append({
            "id": item.get("id"), "name": name, "kind": item.get("kind"),
            "status": outcome["status"], "reason_code": "",
            "reason": outcome.get("reason", ""), "size_bytes": item.get("size_bytes", 0),
            "reclaimed_bytes": outcome.get("reclaimed_bytes", 0),
            "argv": sanitize_argv(outcome.get("argv") or []),
            "restore": (item.get("restore") or {}).get("hint"),
        })
        # Keep the in-memory snapshot in sync for later image/volume/network
        # checks.  A dry-run must simulate the same order, otherwise a network
        # freed by a container would be reported as skipped only in dry-run.
        if outcome["status"] in ("removed", "would_remove"):
            if item["kind"] == "container":
                removed_name = item["name"]
                snapshot["containers"] = [c for c in snapshot["containers"]
                                          if c["container_id"] != item["object_id"]]
                for img in snapshot["images"]:
                    img["used_by"] = [u for u in img.get("used_by", []) if u != removed_name]
                for vol in snapshot["volumes"]:
                    vol["used_by"] = [u for u in vol.get("used_by", []) if u != removed_name]
                for net in snapshot["networks"]:
                    net["used_by"] = [u for u in net.get("used_by", []) if u != removed_name]
            elif item["kind"] == "volume":
                snapshot["volumes"] = [v for v in snapshot["volumes"] if v["name"] != item["object_id"]]
            elif item["kind"] == "network":
                snapshot["networks"] = [n for n in snapshot["networks"]
                                        if n["network_id"] != item["object_id"]]

    removed = [r for r in results if r["status"] in ("removed", "would_remove")]
    failed = [r for r in results if r["status"] == "failed"]
    removed_bytes = sum(r.get("reclaimed_bytes", 0) for r in removed)
    try:
        after = df_totals(docker)
    except DockerError:
        after = {"total": 0, "reclaimable": 0, "rows": []}
    disk_after = disk_info(remote["daemon"]["root_dir"] or ".")
    disk_before = getattr(opts, "disk_before", None) or disk_after
    before = getattr(opts, "df_before", None) or after
    try:
        json_path, md_path = write_apply_log(state, plan, plan_path, results, before, after,
                                             disk_before, disk_after, opts.dry_run,
                                             removed_bytes)
    except OSError as exc:
        json_path, md_path = "", ""
        sys.stderr.write("WARNING: could not write the apply log: %s\n" % mask_secret(str(exc)))

    print("## %s" % (_("dry_run") if opts.dry_run else _("apply_title")))
    print()
    print(md_table(["id", "name", "status", "size", "reason"], [
        [r["id"], r["name"], status_text(r["status"]), human_bytes(r.get("size_bytes", 0)),
         r.get("reason") or ""]
        for r in results
    ]))
    print()
    if opts.dry_run:
        print("## %s" % _("commands_preview"))
        print()
        print("```")
        for r in results:
            if r["status"] == "would_remove" and r.get("argv"):
                print("docker " + " ".join(r["argv"]))
        print("```")
        print()
    print("- %s: %d" % (_("would_remove") if opts.dry_run else _("removed"), len(removed)))
    print("- %s: %d" % (_("skipped"), len([r for r in results if r["status"] == "skipped"])))
    print("- %s: %d" % (_("failed"), len(failed)))
    print("- %s: %s" % (_("reclaimed"), human_bytes(removed_bytes)))
    if not opts.dry_run:
        print("- %s: %s -> %s" % (_("disk"), human_bytes(disk_before.get("free", 0)),
                                  human_bytes(disk_after.get("free", 0))))
    if md_path:
        print("- %s: `%s`" % (_("log_saved"), md_path))
    print()
    restore_lines = [r for r in results
                     if r.get("status") in ("removed", "would_remove") and r.get("restore")]
    if restore_lines:
        print("## %s" % _("restore_hint"))
        print()
        for r in restore_lines:
            print("- `%s`: %s" % (r.get("name", ""), r.get("restore")))
        print()
    if not opts.dry_run:
        executed = [r for r in results if r.get("argv")]
        if executed:
            print("## %s" % _("commands_preview"))
            print()
            print("```")
            for r in executed:
                print("docker " + " ".join(r["argv"]))
            print("```")
            print()
    print("> %s" % _("dry_run") if opts.dry_run else "")
    if failed:
        return EXIT_PARTIAL
    if not removed:
        return EXIT_NOTHING
    return EXIT_OK


def classify_if_needed(snapshot):
    """Compatibility wrapper: a fresh snapshot always needs classification.

    The old implementation checked only containers, so a daemon with no
    containers left images, volumes and networks without a verdict.
    """
    classify(snapshot)
    return snapshot


def live_items_by_key(snapshot):
    """Map (kind, object_id) to the freshly classified live item.

    Apply must never trust the verdict stored in a user-editable plan file: a
    decision volume could be relabelled ``safe`` by hand.  The live snapshot is
    the authority for the decision/protected gate.  Short IDs from ``docker ps``
    / ``docker images`` are indexed too, because a plan can contain a short ID
    when an inspect call failed.
    """
    result = {}
    for item in build_items(snapshot):
        oid = str(item["object_id"])
        result[(item["kind"], oid)] = item
        short = _id_short(oid)
        if short:
            result[(item["kind"], short)] = item
    return result
