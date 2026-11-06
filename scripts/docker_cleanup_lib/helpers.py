"""Parsing, formatting, filesystem and inspection helpers."""

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

def parse_size(value):
    """Parse only the leading ``<number><unit>`` token.

    ``"114MB (virtual 1.2GB)"`` must become 114 MB, not 1141 bytes.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    m = SIZE_RE.match(str(value).replace(",", ""))
    if not m:
        return 0
    try:
        number = float(m.group(1))
    except ValueError:
        return 0
    unit = (m.group(2) or "b").lower()
    return int(number * SIZE_MULT.get(unit, 1))


def human_bytes(value):
    n = int(value or 0)
    if n < 1000:
        return "%d B" % n
    v = float(n)
    for unit in ("kB", "MB", "GB", "TB", "PB"):
        v /= 1000.0
        if v < 1000 or unit == "PB":
            return "%.2f %s" % (v, unit)
    return "%.2f PB" % v


_DT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)
_REL_RE = re.compile(
    r"(?:about\s+)?(?:an?\s+)?(\d+)?\s*(second|minute|hour|day|week|month|year)s?\s+ago",
    re.I,
)


def parse_dt(value):
    """Parse Docker timestamps (RFC3339 with nanoseconds, CLI dates, relative)."""
    if not value:
        return None
    s = str(value).strip()
    if not s or s.startswith("0001-01-01"):
        return None
    m = _DT_RE.match(s)
    if m:
        frac = (m.group(3) or "")[:6]
        tz = m.group(4) or ""
        if tz == "Z":
            tz = "+00:00"
        if re.match(r"^[+-]\d{4}$", tz):
            tz = tz[:3] + ":" + tz[3:]
        iso = "%sT%s%s%s" % (m.group(1), m.group(2), ("." + frac) if frac else "", tz)
        try:
            return datetime.datetime.fromisoformat(iso)
        except ValueError:
            pass
    plain = re.sub(r"\s+UTC$", "", s, flags=re.I)
    for fmt in ("%Y-%m-%d %H:%M:%S %z %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.datetime.strptime(plain, fmt)
        except ValueError:
            pass
    rel = _REL_RE.search(s)
    if rel:
        qty = int(rel.group(1) or 1)
        unit = rel.group(2).lower()
        seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400,
                   "week": 604800, "month": 2592000, "year": 31536000}[unit]
        return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=qty * seconds)
    return None


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def age_days(value, now=None):
    dt = parse_dt(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    now = now or now_utc()
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def iso_now():
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_z(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ago_text(value):
    days = age_days(value)
    if days is None:
        return "-"
    if days < 1.0 / 24:
        return "<1 h"
    if days < 1:
        return "%d h" % int(days * 24)
    if days < 60:
        return "%d d" % int(days)
    if days < 365:
        return "%d mo" % int(days / 30)
    return "%.1f y" % (days / 365.0)


def md_cell(value):
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def md_table(headers, rows):
    out = ["| " + " | ".join(md_cell(h) for h in headers) + " |",
           "|" + "|".join("---" for _sep in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(md_cell(c) for c in row) + " |")
    return "\n".join(out)


def short_id(value, length=12):
    s = str(value or "")
    if s.startswith("sha256:"):
        s = s[7:]
    return s[:length]


def mask_secret(text):
    """Remove credentials that a registry error could echo back."""
    if not text:
        return ""
    text = re.sub(r"(?i)(token|password|passwd|authorization|secret)=\S+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@", r"\1<redacted>@", text)
    return text.strip()


def atomic_write(path, text):
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(text)
    if os.name == "posix":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def write_json(path, data):
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_json(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def state_dir(explicit=None):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "docker-cleanup")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/docker-cleanup")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "docker-cleanup")


def ensure_state_dir(explicit=None):
    path = state_dir(explicit)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def disk_info(path):
    probe = os.path.abspath(path or ".")
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe)
        pct = int(round((usage.used / usage.total) * 100)) if usage.total else 0
        return {"path": path, "total": usage.total, "used": usage.used,
                "free": usage.free, "percent": pct}
    except OSError:
        return {"path": path, "total": 0, "used": 0, "free": 0, "percent": 0}


def parse_json_any(text):
    """Accept a JSON document, a single object, or JSONL."""
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "[{":
            continue
        try:
            rows.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return rows or None


def json_lines(docker, args, timeout=None):
    data = parse_json_any(docker.out(args, timeout=timeout, allow_failure=True))
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        flat = []
        for row in data:
            if isinstance(row, list):
                flat.extend(row)
            else:
                flat.append(row)
        return [r for r in flat if isinstance(r, dict)]
    return []


def lookup_id(mapping, oid):
    """Look up an object by full ID, short ID or a unique hex prefix."""
    if not mapping or not oid:
        return None
    s = str(oid)
    raw = s[7:] if s.startswith("sha256:") else s
    for key in (s, raw):
        if key in mapping:
            return mapping[key]
    if len(raw) >= 12:
        for key, value in mapping.items():
            kraw = key[7:] if key.startswith("sha256:") else key
            if len(kraw) >= 12 and (kraw.startswith(raw) or raw.startswith(kraw)):
                return value
    return None


def _remember_inspect(result, key, obj):
    result[str(key)] = obj
    raw = str(key)
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if re.match(r"^[0-9a-f]{12,64}$", raw):
        result[raw[:12]] = obj


def inspect_many(docker, ids, prefix=None, chunk=80):
    """Run ``docker [prefix] inspect`` in batches; tolerate vanished objects.

    Docker lists short (12-char) IDs but inspect returns the full ID, so the
    result is indexed under both forms plus a 12-char prefix.
    """
    result = {}
    ids = [i for i in ids if i]
    for start in range(0, len(ids), chunk):
        batch = ids[start:start + chunk]
        args = list(prefix or []) + ["inspect"] + batch
        text = docker.out(args, timeout=120, allow_failure=True)
        data = parse_json_any(text)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            data = []
        for obj in data:
            if not isinstance(obj, dict):
                continue
            key = obj.get("Id") or obj.get("Name") or obj.get("ID")
            if key:
                _remember_inspect(result, key, obj)
        if len(data) < len(batch):
            for one in batch:
                if any((o.get("Id") == one or o.get("Name") == one or o.get("ID") == one)
                       for o in data if isinstance(o, dict)):
                    continue
                text = docker.out(list(prefix or []) + ["inspect", one],
                                  timeout=60, allow_failure=True)
                single = parse_json_any(text)
                if isinstance(single, list) and single:
                    single = single[0]
                if isinstance(single, dict):
                    key = single.get("Id") or single.get("Name") or single.get("ID")
                    if key:
                        _remember_inspect(result, key, single)
    return result


def as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def is_anonymous_volume(name):
    return bool(name) and len(str(name)) == 64 and re.match(r"^[0-9a-f]{64}$", str(name)) is not None


def mount_source(m):
    """Return the source to use in a restore hint.

    A named/anonymous volume must be referenced by its volume name, not by the
    host mountpoint that Docker stores in ``Source``.  Bind mounts use the host
    path.
    """
    if m.get("type") == "volume":
        return m.get("name") or m.get("source") or ""
    return m.get("source") or m.get("name") or ""


def _dt_lt(a, b):
    if a is None or b is None:
        return False
    if a.tzinfo is None:
        a = a.replace(tzinfo=datetime.timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=datetime.timezone.utc)
    return a < b


def _id_short(raw):
    s = str(raw or "")
    if s.startswith("sha256:"):
        s = s[7:]
    return s[:12] if len(s) >= 12 else ""


def _looks_like_id(value):
    raw = str(value or "")
    if raw.startswith("sha256:"):
        raw = raw[7:]
    return bool(re.match(r"^[0-9a-f]{12,64}$", raw))


def split_ids(value):
    if not value:
        return []
    out = []
    for chunk in str(value).split(","):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out
