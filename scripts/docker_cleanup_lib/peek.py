"""Read-only volume preview."""

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
from .docker_cli import *

PEEK_SCRIPT = (
    "export LC_ALL=C\n"
    "list=$(ls -A /v 2>/dev/null); if [ $? -eq 0 ]; then "
    "printf '%s\\n' \"$list\" | head -50; echo ---LSOK---; else echo ---LSFAIL---; fi\n"
    "echo ---SIZE---\n"
    "du -sh /v 2>/dev/null | cut -f1\n"
    "echo ---MTIME---\n"
    "find /v -type f 2>/dev/null | head -2000 | while read f; do stat -c %Y \"$f\" 2>/dev/null; done | sort -nr | head -1\n"
    "echo ---COUNT---\n"
    "ls -A /v 2>/dev/null | wc -l\n"
)


def find_helper_image(images):
    for cand in HELPER_IMAGES:
        for img in images:
            if cand in img.get("tags", []):
                return cand
    return ""


def peek_volume(docker, volume, helper, timeout=30):
    name = "docker-cleanup-peek-%s" % uuid.uuid4().hex[:8]
    args = ["run", "--rm", "--name", name, "--network", "none", "--read-only",
            "--pull", "never",
            "-v", "%s:/v:ro" % volume, "--entrypoint", "sh", helper, "-c", PEEK_SCRIPT]
    try:
        proc = docker.run(args, timeout=timeout)
    except DockerError as exc:
        if exc.code == "timeout":
            try:
                docker.run(["rm", "-f", name], timeout=10)
            except DockerError:
                pass
        return {"peeked": False, "error": mask_secret(exc.message or exc.code)[:200]}
    if proc.returncode != 0:
        return {"peeked": False, "error": mask_secret(proc.stderr or "peek failed")[:200]}
    text = proc.stdout or ""
    if "---LSFAIL---" in text:
        return {"peeked": False, "error": "volume directory could not be listed (permission or I/O error)"}
    head, ok_marker, rest = text.rpartition("---LSOK---")
    if not ok_marker:
        return {"peeked": False, "error": "unexpected peek output (no listing marker)"}
    _before, _sep_size, tail = rest.rpartition("---SIZE---")
    size, _sep_mtime, after_mtime = tail.rpartition("---MTIME---")
    mtime, _sep_count, count_raw = after_mtime.rpartition("---COUNT---")
    files = [x.strip() for x in head.splitlines() if x.strip()]
    try:
        count = int((count_raw.strip().splitlines() or ["0"])[0] or 0)
    except (ValueError, IndexError):
        count = len(files)
    size_text = size.strip()
    if re.match(r"^\d+,\d+", size_text):
        size_text = size_text.replace(",", ".", 1)
    try:
        ts = int((mtime.strip().splitlines() or ["0"])[0] or 0)
    except (ValueError, IndexError):
        ts = 0
    return {
        "peeked": True,
        "empty": not files and count == 0,
        "files": files[:50],
        "size_human": size_text,
        "size_bytes": parse_size(size_text),
        "newest_mtime": iso_z(datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)) if ts else None,
        "error": "",
    }


def apply_peek(docker, snapshot, helper, warnings, only=None):
    helper_img = find_helper_image(snapshot["images"])
    if not helper_img:
        warnings.append({"code": "peek_no_helper", "severity": "info", "params": {}})
        return
    if only:
        known = {v["name"]: v for v in snapshot["volumes"]}
        selected = []
        for name in only:
            vol = known.get(name)
            if not vol:
                warnings.append({"code": "peek_unknown_volume", "severity": "low",
                                 "params": {"volume": name}})
                continue
            if vol["used_by"]:
                warnings.append({"code": "peek_used_volume", "severity": "low",
                                 "params": {"volume": name}})
                continue
            selected.append(vol)
    else:
        selected = [v for v in snapshot["volumes"] if not v["used_by"]]
    for vol in selected:
        result = peek_volume(docker, vol["name"], helper_img)
        vol["content"] = result
        if result.get("peeked"):
            if result.get("newest_mtime"):
                new = parse_dt(result["newest_mtime"])
                current = parse_dt(vol.get("last_activity_at"))
                if new is not None and (current is None or _dt_lt(current, new)):
                    vol["last_activity_at"] = result["newest_mtime"]
                    vol["activity_source"] = "file_mtime"
            if result.get("size_bytes"):
                vol["size_bytes"] = max(vol["size_bytes"], result["size_bytes"])
        elif result.get("error"):
            warnings.append({"code": "peek_failed", "severity": "low",
                             "params": {"volume": vol["name"]}})
