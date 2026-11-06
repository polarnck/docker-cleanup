"""Registry digest checks."""

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

def registry_cache_load(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        data = read_json(path)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def registry_check_one(docker, ref, timeout=25):
    proc = docker.run(["buildx", "imagetools", "inspect", ref], timeout=timeout)
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        low = text.lower()
        if "not found" in low or "manifest unknown" in low or "no such manifest" in low:
            return {"checked": True, "exists": False, "digest": "", "media_type": "",
                    "matches_local": False, "error": "not found"}
        if "unknown command" in low or "not a docker command" in low:
            proc2 = docker.run(["manifest", "inspect", ref], timeout=timeout)
            text2 = (proc2.stdout or "") + "\n" + (proc2.stderr or "")
            if proc2.returncode != 0:
                return {"checked": True, "exists": None, "digest": "", "media_type": "",
                        "matches_local": False, "error": mask_secret(text2)[:300]}
            data = parse_json_any(proc2.stdout)
            digest = ""
            media = ""
            if isinstance(data, dict):
                digest = str(data.get("config", {}).get("digest") or "")
                media = str(data.get("mediaType") or "")
            return {"checked": True, "exists": bool(digest), "digest": digest,
                    "media_type": media, "matches_local": False, "error": ""}
        return {"checked": True, "exists": None, "digest": "", "media_type": "",
                "matches_local": False, "error": mask_secret(text)[:300]}
    digest = ""
    media = ""
    m = re.search(r"(?m)^Digest:\s*(sha256:[0-9a-f]{64})", proc.stdout or "")
    if m:
        digest = m.group(1)
    m2 = re.search(r"(?m)^MediaType:\s*(\S+)", proc.stdout or "")
    if m2:
        media = m2.group(1)
    return {"checked": True, "exists": (True if digest else None), "digest": digest,
            "media_type": media, "matches_local": False, "error": ""}


def check_registry(docker, images, cache_path=None, workers=6):
    cache = registry_cache_load(cache_path)
    now = time.time()
    todo = []
    for img in images:
        if img["dangling"] or img["base"] or img["used_by"]:
            img["registry"] = {"checked": False, "reason": "not_applicable"}
            continue
        ref = "%s:%s" % (img["repo"], img["tag"])
        hit = cache.get(ref)
        if isinstance(hit, dict) and now - float(hit.get("ts", 0) or 0) < 3600:
            img["registry"] = hit.get("result") if isinstance(hit.get("result"), dict) else {}
        else:
            todo.append((img, ref))
    results = {}

    def work(pair):
        img, ref = pair
        try:
            return ref, registry_check_one(docker, ref)
        except DockerError as exc:
            return ref, {"checked": True, "exists": None, "digest": "", "media_type": "",
                         "matches_local": False,
                         "error": mask_secret(exc.message or exc.code)[:300]}

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for ref, result in pool.map(work, todo):
                results[ref] = result
        for ref, result in results.items():
            cache[ref] = {"ts": now, "result": result}
        if cache_path:
            try:
                write_json(cache_path, cache)
            except OSError:
                pass
    for img in images:
        if img["dangling"] or img["base"] or img["used_by"]:
            continue
        ref = "%s:%s" % (img["repo"], img["tag"])
        res = results.get(ref) or (cache.get(ref) or {}).get("result") or {}
        local = set()
        for rd in img.get("repo_digests") or []:
            m = re.search(r"(sha256:[0-9a-f]{64})", str(rd))
            if m:
                local.add(m.group(1))
        remote = res.get("digest") or ""
        res = dict(res)
        res["local_digests"] = sorted(local)
        if not res.get("exists"):
            res["matches_local"] = False
        elif remote and remote in local:
            res["matches_local"] = True
        else:
            res["matches_local"] = False
            if remote and local and "index" in str(res.get("media_type", "")).lower():
                res["ambiguous"] = True
        img["registry"] = res
    return images
