"""Docker CLI wrapper, daemon checks and doctor output."""

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

class DockerError(Exception):
    def __init__(self, code, message="", detail=""):
        Exception.__init__(self, message or code)
        self.code = code
        self.message = message
        self.detail = mask_secret(detail or "")


class Docker(object):
    def __init__(self, timeout=60):
        self.binary = os.environ.get("DOCKER_CLEANUP_DOCKER", "docker")
        self.timeout = timeout

    def run(self, args, timeout=None):
        cmd = [self.binary] + [str(a) for a in args]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout or self.timeout)
        except FileNotFoundError:
            raise DockerError("docker_missing", "Docker CLI not found in PATH.")
        except PermissionError:
            raise DockerError("permission", "Permission denied while starting the Docker CLI.")
        except subprocess.TimeoutExpired:
            raise DockerError("timeout", "Docker command timed out after %ss." % (timeout or self.timeout))

    def out(self, args, timeout=None, allow_failure=False):
        proc = self.run(args, timeout=timeout)
        if proc.returncode != 0:
            if allow_failure:
                return ""
            raise self.error_from(proc)
        return proc.stdout

    @staticmethod
    def error_from(proc):
        err = (proc.stderr or proc.stdout or "").strip()
        low = err.lower()
        if "permission denied" in low:
            return DockerError("permission", "Permission denied by the Docker daemon.",
                               "Add your user to the 'docker' group or use rootless Docker. "
                               "This tool never uses sudo.")
        if "cannot connect to the docker daemon" in low or "is the docker daemon running" in low:
            return DockerError("daemon_down", "Cannot connect to the Docker daemon.",
                               err or "Start Docker and try again.")
        if "command not found" in low or "not recognized" in low:
            return DockerError("docker_missing", "Docker CLI not found in PATH.", err)
        return DockerError("command_failed", "Docker command failed.",
                           err or "exit code %s" % proc.returncode)


def context_endpoint(docker, name):
    data = parse_json_any(docker.out(["context", "inspect", name, "--format", "{{json .}}"],
                                     allow_failure=True))
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return ""
    endpoints = data.get("Endpoints") or {}
    if not isinstance(endpoints, dict):
        return ""
    docker_ep = endpoints.get("docker") or {}
    if not isinstance(docker_ep, dict):
        return ""
    return str(docker_ep.get("Host") or "")


def endpoint_is_remote(endpoint):
    if not endpoint:
        return False
    low = endpoint.lower()
    return low.startswith("tcp://") or low.startswith("ssh://") or low.startswith("http://")


def build_in_progress(docker):
    rows = json_lines(docker, ["ps", "--format", "{{json .}}"])
    for row in rows:
        blob = " ".join(str(row.get(k, "")) for k in ("Names", "Image", "Command"))
        if "buildkit" in blob.lower() or "buildx_buildkit" in blob.lower():
            return True
    return False


def doctor_data(docker):
    version_raw = docker.out(["version", "--format", "{{json .}}"], allow_failure=True)
    version = parse_json_any(version_raw) or {}
    if not isinstance(version, dict):
        version = {}
    client = version.get("Client") if isinstance(version.get("Client"), dict) else {}
    server = version.get("Server") if isinstance(version.get("Server"), dict) else {}
    if not server:
        raise DockerError("daemon_down", "Cannot connect to the Docker daemon.",
                          "Start Docker (or check DOCKER_HOST) and try again.")
    context = (docker.out(["context", "show"], allow_failure=True) or "").strip() or "default"
    endpoint = context_endpoint(docker, context)
    info_raw = docker.out(["info", "--format", "{{json .}}"])
    info = parse_json_any(info_raw) or {}
    if isinstance(info, list):
        info = info[0] if info else {}
    if not isinstance(info, dict):
        info = {}
    if not info:
        raise DockerError("daemon_down", "Docker daemon did not return info.")
    daemon_id = str(info.get("ID") or server.get("ID") or "")
    root_dir = str(info.get("DockerRootDir") or "")
    swarm = info.get("Swarm") or {}
    if not isinstance(swarm, dict):
        swarm = {}
    swarm_state = str(swarm.get("LocalNodeState") or "inactive")
    security = info.get("SecurityOptions") or []
    if not isinstance(security, list):
        security = []
    rootless = any("rootless" in str(s).lower() for s in security)
    desktop = "docker desktop" in str(info.get("OperatingSystem", "")).lower()
    remote_env = os.environ.get("DOCKER_HOST", "")
    remote = endpoint_is_remote(endpoint) or endpoint_is_remote(remote_env)
    warnings = []
    if remote:
        warnings.append({"code": "remote_context", "severity": "high",
                         "params": {"context": context}})
    if rootless:
        warnings.append({"code": "rootless", "severity": "info", "params": {}})
    if desktop:
        warnings.append({"code": "docker_desktop", "severity": "info", "params": {}})
    if swarm_state == "active":
        warnings.append({"code": "swarm_active", "severity": "high", "params": {}})
    if build_in_progress(docker):
        warnings.append({"code": "build_in_progress", "severity": "medium", "params": {}})
    disk = disk_info(root_dir or ".")
    if disk["total"] and (disk["free"] < 5 * 1000 ** 3 or disk["percent"] >= 90):
        warnings.append({"code": "low_disk", "severity": "medium",
                         "params": {"free": disk["free"], "percent": disk["percent"]}})
    daemon_hash = "sha256:" + hashlib.sha256(daemon_id.encode("utf-8")).hexdigest() if daemon_id else ""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "docker_cli": {"available": True, "version": str(client.get("Version") or "")},
        "daemon": {
            "id_hash": daemon_hash,
            "server_version": str(server.get("Version") or info.get("ServerVersion") or ""),
            "storage_driver": str(info.get("Driver") or ""),
            "root_dir": root_dir,
            "os": str(info.get("OperatingSystem") or ""),
            "arch": str(info.get("Architecture") or platform.machine()),
            "swarm": swarm_state,
            "rootless": rootless,
            "docker_desktop": desktop,
            "ncpu": info.get("NCPU"),
            "memory_total": info.get("MemTotal"),
        },
        "context": {"name": context, "endpoint_kind": endpoint.split(":")[0] if endpoint else "unknown",
                    "remote": remote, "endpoint_present": bool(endpoint)},
        "disk": disk,
        "warnings": warnings,
        "permissions": {"ok": True},
    }


def print_doctor(data, as_json):
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    d = data["daemon"]
    print("# %s" % _("audit_title").split("(")[0].strip())
    print()
    print("- %s: `%s` (%s)" % (_("context"), data["context"]["name"],
                               _("remote") if data["context"]["remote"] else _("local")))
    print("- %s: %s %s" % (_("version"), data["docker_cli"]["version"], d["server_version"]))
    print("- %s: `%s`" % (_("storage"), d["storage_driver"]))
    print("- %s: `%s`" % (_("root_dir"), d["root_dir"]))
    print("- %s: %s / %s" % (_("os"), d["os"], d["arch"]))
    print("- %s: %s" % (_("swarm"), d["swarm"]))
    print("- %s: %s free of %s (%d%% used)" % (_("disk"), human_bytes(data["disk"]["free"]),
                                               human_bytes(data["disk"]["total"]),
                                               data["disk"]["percent"]))
    print("- daemon: `%s`" % (d["id_hash"][:23] + "..." if d["id_hash"] else "-"))
    print()
    if data["warnings"]:
        print("## %s" % _("warnings"))
        for w in data["warnings"]:
            print("- **%s** (%s)" % (w["code"], w["severity"]))
    else:
        print("## %s\n\n- %s" % (_("warnings"), _("none")))
