"""Command-line interface and command handlers."""

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
from .docker_cli import *
from .collect import *
from .registry import *
from .peek import *
from .classify import *
from .plan import *
from .render import *
from .apply import *

def prepare_snapshot(docker, opts, allow_remote, want_peek, peek_volumes=None):
    doctor = doctor_data(docker)
    if doctor["context"]["remote"] and not allow_remote:
        raise DockerError("remote_refused", _("remote_refused"))
    warnings = list(doctor.get("warnings") or [])
    try:
        cache_path = os.path.join(ensure_state_dir(getattr(opts, "state_dir", None)),
                                  "registry-cache.json")
    except OSError:
        cache_path = None
    snapshot = collect_snapshot(docker, want_registry=not getattr(opts, "no_registry", False),
                                want_peek=want_peek, registry_cache_path=cache_path,
                                warnings=warnings, allow_remote=allow_remote)
    if not getattr(opts, "no_registry", False):
        check_registry(docker, snapshot["images"], cache_path)
    else:
        warnings.append({"code": "registry_disabled", "severity": "info", "params": {}})
    if want_peek:
        apply_peek(docker, snapshot, "", warnings, only=peek_volumes)
    snapshot["swarm_active"] = str(doctor["daemon"].get("swarm") or "").lower() == "active"
    snapshot["build_in_progress"] = any(w.get("code") == "build_in_progress"
                                        for w in warnings)
    classify(snapshot)
    return doctor, snapshot


def audit_command(opts):
    docker = Docker()
    preview_days = parse_older_than(opts.older_than)
    want_peek = bool(opts.peek or opts.peek_volume)
    doctor, snapshot = prepare_snapshot(docker, opts, opts.allow_remote, want_peek,
                                        opts.peek_volume)
    options = {"peek": want_peek, "peek_volumes": list(opts.peek_volume or []),
               "registry_checked": not opts.no_registry,
               "older_than_days": preview_days}
    payload = audit_payload(snapshot, doctor, options)
    if opts.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_audit(payload))
    # cache for `plan` so peek evidence survives a fresh audit
    try:
        cache_path = os.path.join(ensure_state_dir(opts.state_dir), "last-audit.json")
        write_json(cache_path, audit_payload(snapshot, doctor, options))
    except OSError:
        pass
    return EXIT_OK


def parse_older_than(value):
    if value is None:
        raise DockerError("usage", "--older-than is required.")
    s = str(value).strip().lower()
    if s in ("all", "any", "0"):
        return 0
    try:
        days = int(s)
    except ValueError:
        raise DockerError("usage", "--older-than must be an integer number of days or 'all'.")
    if days < 0:
        raise DockerError("usage", "--older-than cannot be negative.")
    return days


def plan_command(opts):
    docker = Docker()
    if opts.depth not in ("safe", "standard", "deep", "custom"):
        raise DockerError("usage", "Unknown depth: %s" % opts.depth)
    days = parse_older_than(opts.older_than)
    doctor, snapshot = prepare_snapshot(docker, opts, opts.allow_remote, False)
    cached = None
    try:
        cache_path = os.path.join(ensure_state_dir(opts.state_dir), "last-audit.json")
    except OSError:
        cache_path = None
    if cache_path and os.path.exists(cache_path):
        try:
            cached = read_json(cache_path)
        except (OSError, ValueError):
            cached = None
    if cached and not isinstance(cached, dict):
        cached = None
    if cached:
        env = cached.get("environment") if isinstance(cached.get("environment"), dict) else {}
        daemon = env.get("daemon") if isinstance(env.get("daemon"), dict) else {}
        cached_hash = daemon.get("id_hash")
        if cached_hash and cached_hash != doctor["daemon"]["id_hash"]:
            cached = None
    merge_peek_cache(snapshot, cached)
    classify(snapshot)
    flags = {
        "include_base_images": bool(opts.include_base_images),
        "include_local_images": bool(opts.include_local_images),
        "include_anonymous_volumes": bool(opts.include_anonymous_volumes),
        "include_data_volumes": bool(opts.include_data_volumes),
    }
    cached_options = (cached or {}).get("options") if isinstance((cached or {}).get("options"), dict) else {}
    plan = build_plan(snapshot, doctor, opts.depth, days, flags,
                      {"peek": bool(cached_options.get("peek")),
                       "registry_checked": not opts.no_registry})
    if opts.out:
        out_path = os.path.abspath(os.path.expanduser(opts.out))
        write_json(out_path, plan)
        md_path = os.path.splitext(out_path)[0] + ".md"
        atomic_write(md_path, plan_markdown(plan))
        print(plan_markdown(plan))
        print()
        print("- %s: `%s`" % (_("plan_json_saved"), out_path))
        print("- %s: `%s`" % (_("plan_md_saved"), md_path))
    else:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    return EXIT_OK


def select_command(opts):
    plan_path = os.path.abspath(os.path.expanduser(opts.plan))
    plan = load_plan(plan_path)
    by_id = {i["id"]: i for i in plan.get("items", [])}
    if opts.none:
        for item in plan["items"]:
            item["approved"] = False
            item["selected"] = False
            item["confirm"] = ""
    if opts.all_default:
        for item in plan["items"]:
            item["approved"] = bool(item.get("default_approved"))
            item["selected"] = bool(item.get("default_approved"))
            item["confirm"] = ""
    for token in split_ids(opts.include):
        item_id, _sep, confirm = token.partition("=")
        item = by_id.get(item_id)
        if not item:
            raise DockerError("usage", "Unknown plan item id: %s" % item_id)
        if item.get("verdict") == "protected" or item.get("risk") == "protected":
            raise DockerError("refused", _("protected_in_plan"))
        item["approved"] = True
        item["selected"] = True
        # A plain ID is not a typed confirmation: clear any stale confirm value.
        item["confirm"] = confirm or ""
    for token in split_ids(opts.exclude):
        item_id = token.partition("=")[0]
        item = by_id.get(item_id)
        if not item:
            raise DockerError("usage", "Unknown plan item id: %s" % item_id)
        item["approved"] = False
        item["selected"] = False
        item["confirm"] = ""
    refresh_plan_summary(plan)
    write_json(plan_path, plan)
    md_path = os.path.splitext(plan_path)[0] + ".md"
    atomic_write(md_path, plan_markdown(plan))
    print("## %s" % _("plan_title"))
    print()
    print(md_table(["id", "kind", "name", "risk", "approved", "confirm"], [
        [i["id"], i["kind"], i["name"], i.get("risk", ""),
         _("yes") if i.get("approved") and i.get("selected", True) else _("no"),
         i.get("confirm") or ""]
        for i in plan["items"]
    ]))
    print()
    print("- %s: `%s`" % (_("plan_saved"), plan_path))
    return EXIT_OK


def apply_command(opts):
    plan_path = os.path.abspath(os.path.expanduser(opts.plan))
    plan = load_plan(plan_path)
    docker = Docker()
    if opts.dry_run:
        opts.yes = True
    if not opts.yes:
        if sys.stdin.isatty():
            print("Type 'apply' to confirm removal, anything else aborts: ", end="")
            try:
                answer = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer != "apply":
                raise DockerError("refused", _("confirm_required"))
        else:
            raise DockerError("refused", _("confirm_required"))
    doctor = doctor_data(docker)
    if doctor["context"]["remote"] and not opts.allow_remote:
        raise DockerError("remote_refused", _("remote_refused"))
    opts.df_before = df_totals(docker)
    opts.disk_before = disk_info(doctor["daemon"]["root_dir"] or ".")
    return apply_plan(docker, plan, plan_path, opts)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="docker_cleanup.py",
        description="Audit Docker disk usage and safely remove approved objects.")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="check Docker CLI, daemon, context and warnings")
    doctor.add_argument("--json", action="store_true")

    audit = sub.add_parser("audit", help="read-only audit (Markdown or JSON)")
    audit.add_argument("--json", action="store_true")
    audit.add_argument("--no-registry", action="store_true")
    audit.add_argument("--peek", action="store_true")
    audit.add_argument("--peek-volume", action="append", default=None,
                       metavar="NAME", help="peek only this unused volume (repeatable)")
    audit.add_argument("--older-than", default="7",
                       help="age threshold for the depth preview only (default 7, 'all' = no filter)")
    audit.add_argument("--allow-remote", action="store_true")
    audit.add_argument("--state-dir", default=None)

    plan = sub.add_parser("plan", help="build a plan from a fresh audit")
    plan.add_argument("--depth", required=True, choices=("safe", "standard", "deep", "custom"))
    plan.add_argument("--older-than", required=True)
    plan.add_argument("--include-base-images", action="store_true")
    plan.add_argument("--include-local-images", action="store_true")
    plan.add_argument("--include-anonymous-volumes", action="store_true")
    plan.add_argument("--include-data-volumes", action="store_true")
    plan.add_argument("--out", default=None)
    plan.add_argument("--allow-remote", action="store_true")
    plan.add_argument("--state-dir", default=None)
    plan.add_argument("--no-registry", action="store_true")

    select = sub.add_parser("select", help="toggle approved items in a plan")
    select.add_argument("--plan", required=True)
    select.add_argument("--include", default=None)
    select.add_argument("--exclude", default=None)
    select.add_argument("--all-default", action="store_true")
    select.add_argument("--none", action="store_true")

    apply = sub.add_parser("apply", help="re-validate and remove approved items")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--yes", action="store_true")
    apply.add_argument("--allow-decision", action="store_true")
    apply.add_argument("--allow-remote", action="store_true")
    apply.add_argument("--force-count", action="store_true")
    apply.add_argument("--state-dir", default=None)
    return parser


def main(argv=None):
    parser = build_parser()
    opts = parser.parse_args(argv)
    if not opts.command:
        parser.print_help()
        return EXIT_ENV
    try:
        if opts.command == "doctor":
            data = doctor_data(Docker())
            print_doctor(data, opts.json)
            return EXIT_OK
        if opts.command == "audit":
            return audit_command(opts)
        if opts.command == "plan":
            return plan_command(opts)
        if opts.command == "select":
            return select_command(opts)
        if opts.command == "apply":
            return apply_command(opts)
    except DockerError as exc:
        message = exc.message or exc.code
        if exc.detail:
            message = "%s %s" % (message, exc.detail)
        sys.stderr.write("ERROR: %s\n" % message)
        if exc.code in ("refused", "remote_refused", "context_mismatch", "daemon_mismatch",
                        "schema_mismatch", "count_refused", "protected_in_plan"):
            return EXIT_REFUSED
        if exc.code in ("usage", "plan_missing", "plan_invalid"):
            return EXIT_ENV
        return EXIT_ENV
    except OSError as exc:
        sys.stderr.write("ERROR: %s\n" % mask_secret(str(exc)))
        return EXIT_ENV
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted.\n")
        return EXIT_ENV
    return EXIT_ENV
