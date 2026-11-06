"""Markdown renderers for the audit and the plan."""

from .constants import *
from .constants import _
from .helpers import *

def render_audit(data):
    env = data["environment"]
    summary = data["summary"]
    out = []
    out.append("# %s" % _("audit_title"))
    out.append("")
    out.append("> %s: %s" % (_("generated"), data["generated_at"]))
    out.append("")
    out.append("## %s" % _("environment"))
    out.append("")
    out.append("- %s: `%s` (%s)" % (_("context"), env["context"]["name"],
                                    _("remote") if env["context"]["remote"] else _("local")))
    out.append("- %s: client `%s`, server `%s`" % (
        _("version"), env["docker_cli"]["version"] or "?", env["daemon"]["server_version"] or "?"))
    out.append("- %s: `%s`" % (_("storage"), env["daemon"]["storage_driver"]))
    out.append("- %s: `%s`" % (_("root_dir"), env["daemon"]["root_dir"]))
    out.append("- %s: %s / %s" % (_("os"), env["daemon"]["os"], env["daemon"]["arch"]))
    out.append("- %s: %s" % (_("swarm"), env["daemon"]["swarm"]))
    out.append("- %s: %s free / %s total (%d%%)" % (
        _("disk"), human_bytes(env["disk"]["free"]), human_bytes(env["disk"]["total"]),
        env["disk"]["percent"]))
    out.append("")
    if env["warnings"]:
        out.append("### %s" % _("warnings"))
        out.append("")
        for w in env["warnings"]:
            out.append("- **%s** (%s)" % (w["code"], w["severity"]))
        out.append("")
    env_codes = set(w.get("code") for w in env["warnings"])
    extra_warnings = [w for w in data.get("warnings", []) if w.get("code") not in env_codes]
    if extra_warnings:
        out.append("### %s" % _("warnings"))
        out.append("")
        for w in extra_warnings:
            out.append("- **%s** (%s)" % (w.get("code"), w.get("severity")))
        out.append("")

    out.append("## %s" % _("summary"))
    out.append("")
    reclaim = summary["reclaimable"]
    out.append(md_table([_("verdict"), _("size")], [
        ["**%s**" % _("safe"), human_bytes(reclaim["safe"])],
        ["**%s**" % _("cost"), human_bytes(reclaim["cost"])],
        ["**%s**" % _("decision"), human_bytes(reclaim["decision"])],
        ["**%s**" % _("protected"), human_bytes(reclaim["protected"])],
    ]))
    out.append("")
    out.append("> %s" % _("est_note"))
    out.append("")
    preview_days = data["options"].get("older_than_days") or 0
    if preview_days:
        out.append("### %s" % (_("depth_preview_at") % preview_days))
    else:
        out.append("### %s (%s)" % (_("depth_preview"), _("all_ages")))
    out.append("")
    rows = []
    for depth in ("safe", "standard", "deep"):
        p = summary["depth_preview"][depth]
        rows.append([depth, str(p["items"]), human_bytes(p["bytes"])])
    out.append(md_table([_("depth"), _("items"), _("size")], rows))
    out.append("")
    out.append("> %s" % _("decision_items"))
    out.append("")

    # containers
    out.append("## %s" % _("containers"))
    out.append("")
    if data["containers"]:
        rows = []
        for c in data["containers"]:
            d = c["details"]
            mounts = ", ".join("%s:%s" % (m.get("name") or m.get("source") or "?",
                                           m.get("destination") or "?")
                               for m in d.get("mounts") or [])
            rows.append([
                "`%s`" % c["name"], "`%s`" % (d.get("image_ref") or "-"),
                human_bytes(c["size_bytes"]), d.get("state") or "-",
                _("running") if d.get("state") == "running" else ago_text(c["last_activity_at"]),
                c["verdict"],
                "%s%s%s%s" % (reason_text(c["reason_code"]),
                              " | compose: %s" % d["compose_project"] if d.get("compose_project") else "",
                              " | networks: %s" % ", ".join(d.get("networks") or [])
                              if d.get("networks") else "",
                              " | mounts: %s" % mounts if mounts else ""),
            ])
        out.append(md_table([_("name"), "image", _("size"), _("state"),
                             _("activity"), _("verdict"), _("reason")], rows))
    else:
        out.append(_("no_items"))
    out.append("")

    # images
    out.append("## %s" % _("images"))
    out.append("")
    if data["images"]:
        rows = []
        for i in data["images"]:
            reg = (i["details"].get("registry") or {})
            evidence = ""
            if reg.get("checked"):
                if reg.get("exists") is False:
                    evidence = "not in registry"
                elif reg.get("matches_local"):
                    evidence = "digest match"
                elif reg.get("error"):
                    evidence = "registry error"
                else:
                    evidence = "digest differs"
            rows.append(["`%s`" % i["name"], human_bytes(i["size_bytes"]),
                         ", ".join(i["details"].get("used_by") or []) or "-",
                         i["verdict"], reason_text(i["reason_code"]),
                         evidence or "-"])
        out.append(md_table([_("name"), _("size"), "used by", _("verdict"),
                             _("reason"), _("evidence")], rows))
        out.append("")
        out.append("> %s" % _("image_age_note"))
    else:
        out.append(_("no_items"))
    out.append("")

    # volumes
    out.append("## %s" % _("volumes"))
    out.append("")
    if data["volumes"]:
        rows = []
        for v in data["volumes"]:
            d = v["details"]
            rows.append(["`%s`" % v["name"], human_bytes(v["size_bytes"]),
                         "`%s`" % (d.get("mountpoint") or "-"),
                         d.get("project") or "-",
                         ", ".join(d.get("used_by") or []) or "-",
                         ("%s (%s)" % (ago_text(v["last_activity_at"]), _("approximate"))
                          if v["last_activity_at"] else _("unknown")),
                         v["verdict"], reason_text(v["reason_code"])])
        out.append(md_table([_("name"), _("size"), _("host_path"), _("project"),
                             "used by", _("activity"), _("verdict"), _("reason")], rows))
        out.append("")
        out.append("> %s" % _("volume_path_note"))
        peeked = [v for v in data["volumes"]
                  if ((v.get("details") or {}).get("content") or {}).get("peeked")]
        if peeked:
            out.append("")
            out.append("**Peeked entries (names only):**")
            out.append("")
            for v in peeked:
                content = (v.get("details") or {}).get("content") or {}
                files = content.get("files") or []
                shown = ", ".join("`%s`" % f for f in files[:10])
                more = " (+%d more)" % (len(files) - 10) if len(files) > 10 else ""
                out.append("- `%s`: %s%s" % (v["name"], shown or _("none"), more))
    else:
        out.append(_("no_items"))
    out.append("")

    # networks
    out.append("## %s" % _("networks"))
    out.append("")
    if data["networks"]:
        rows = []
        for n in data["networks"]:
            d = n["details"]
            rows.append(["`%s`" % n["name"], d.get("driver") or "-", d.get("scope") or "-",
                         n["verdict"], reason_text(n["reason_code"]),
                         ", ".join(d.get("used_by") or []) or "-"])
        out.append(md_table([_("name"), "driver", "scope", _("verdict"), _("reason"),
                             "used by"], rows))
    else:
        out.append(_("no_items"))
    out.append("")

    # cache
    cache = data["build_cache"]
    out.append("## %s - %s" % (_("build_cache"), human_bytes(cache.get("total_bytes", 0))))
    out.append("")
    buckets = cache.get("buckets") or {}
    out.append(md_table(["bucket", _("size")], [
        ["older than 7 d", human_bytes(buckets.get("7d", 0))],
        ["older than 30 d", human_bytes(buckets.get("30d", 0))],
        ["older than 90 d", human_bytes(buckets.get("90d", 0))],
        ["all reclaimable", human_bytes(buckets.get("all", 0))],
    ]))
    out.append("")
    out.append("> %d / %d. %s" % (
        cache.get("records", 0), cache.get("in_use_records", 0), _("cache_note")))
    out.append("")

    # compose
    out.append("## %s" % _("compose"))
    out.append("")
    if data["compose_projects"]:
        rows = []
        for p in data["compose_projects"]:
            rows.append(["`%s`" % p["name"], ", ".join(p["services"]) or "-",
                         ", ".join(p["config_files"]) or "-",
                         _("yes") if p["config_exists"] else "**%s**" % _("no"),
                         ", ".join(p["containers"])])
        out.append(md_table([_("project"), "services", "config files", "exists", "containers"], rows))
    else:
        out.append(_("no_items"))
    out.append("")

    # binds
    out.append("## %s" % _("binds"))
    out.append("")
    if data["bind_mounts"]:
        rows = []
        for b in data["bind_mounts"]:
            rows.append(["`%s`" % b["container"], b["container_state"],
                         "`%s`" % b["source"], "`%s`" % b["destination"],
                         ago_text(b["last_activity_at"]) if b["last_activity_at"] else "-",
                         b["note"] or "-"])
        out.append(md_table([_("containers"), _("state"), _("host_path"),
                             _("container_path"), _("activity"), "note"], rows))
        out.append("")
        out.append("> %s" % _("bind_note"))
    else:
        out.append(_("no_items"))
    out.append("")

    # do not remove
    out.append("## %s" % _("do_not_remove"))
    out.append("")
    protected = [i for i in data["containers"] + data["images"] + data["volumes"] + data["networks"]
                 if i["verdict"] == "protected"]
    decisions = [i for i in data["containers"] + data["images"] + data["volumes"] + data["networks"]
                 if i["verdict"] == "decision"]
    if protected or decisions:
        for i in protected:
            out.append("- **%s** `%s` - %s" % (_("protected"), i["name"], reason_text(i["reason_code"])))
        for i in decisions:
            out.append("- **%s** `%s` - %s" % (_("decision"), i["name"], reason_text(i["reason_code"])))
    else:
        out.append("- %s" % _("none"))
    out.append("")

    out.append("## %s" % _("unknowns"))
    out.append("")
    out.append("- %s" % _("unknown_image"))
    out.append("- %s" % _("unknown_volume"))
    out.append("- %s" % _("unknown_bind"))
    out.append("- %s" % _("unknown_stopped_image"))
    if not data["options"].get("peek"):
        out.append("- %s" % _("no_peek"))
    if not data["options"].get("registry_checked", True):
        out.append("- %s" % _("registry_skipped"))
    out.append("")
    out.append("## %s" % _("next_step"))
    out.append("")
    out.append("```\n%s\n```" % _("next_audit"))
    out.append("")
    return "\n".join(out)


def plan_markdown(plan):
    out = ["# %s" % _("plan_title"), ""]
    out.append("- plan_id: `%s`" % plan["plan_id"])
    out.append("- %s: %s" % (_("created"), plan["created_at"]))
    out.append("- %s: `%s`%s" % (_("context"), plan["context"],
                                 " (%s)" % _("remote") if plan.get("context_is_remote") else ""))
    out.append("- %s: %s" % (_("depth"), plan["depth"]))
    out.append("- %s: %s" % (_("age"), plan["older_than_days"] if plan["older_than_days"] else _("all_ages")))
    if plan.get("cutoff_at"):
        out.append("- Cutoff: `%s`" % plan["cutoff_at"])
    out.append("")
    out.append("## %s" % _("summary"))
    out.append("")
    s = plan["summary"]
    by_kind = s.get("by_kind") if isinstance(s.get("by_kind"), dict) else {}
    out.append(md_table([_("kind"), _("items"), _("size"), _("approved")], [
        ["container", str(by_kind.get("container", {}).get("items", 0)),
         human_bytes(by_kind.get("container", {}).get("bytes", 0)),
         str(by_kind.get("container", {}).get("approved", 0))],
        ["network", str(by_kind.get("network", {}).get("items", 0)),
         human_bytes(by_kind.get("network", {}).get("bytes", 0)),
         str(by_kind.get("network", {}).get("approved", 0))],
        ["image", str(by_kind.get("image", {}).get("items", 0)),
         human_bytes(by_kind.get("image", {}).get("bytes", 0)),
         str(by_kind.get("image", {}).get("approved", 0))],
        ["volume", str(by_kind.get("volume", {}).get("items", 0)),
         human_bytes(by_kind.get("volume", {}).get("bytes", 0)),
         str(by_kind.get("volume", {}).get("approved", 0))],
        ["cache", str(by_kind.get("cache", {}).get("items", 0)),
         human_bytes(by_kind.get("cache", {}).get("bytes", 0)),
         str(by_kind.get("cache", {}).get("approved", 0))],
        ["**%s**" % _("total"), str(s.get("items", 0)), human_bytes(s.get("bytes", 0)),
         str(s.get("approved", 0))],
    ]))
    out.append("")
    out.append("## %s" % _("items"))
    out.append("")
    if plan["items"]:
        rows = []
        for i in plan["items"]:
            selected = i.get("selected", True)
            rows.append(["`%s`" % i["id"], i["kind"], "`%s`" % i["name"],
                         human_bytes(i.get("size_bytes", 0)), i.get("risk", ""),
                         reason_text(i.get("reason_code", "")),
                         _("yes") if i.get("approved") and selected else _("no"),
                         i.get("confirm") or "",
                         ((i.get("details") or {}).get("mountpoint") or
                          (i.get("details") or {}).get("source") or "-")])
        out.append(md_table(["id", _("kind"), _("name"), _("size"), _("verdict"),
                             _("reason"), _("approved"), _("confirm_label"),
                             _("host_path")], rows))
    else:
        out.append(_("no_items"))
    out.append("")
    peeked = [i for i in plan["items"]
              if isinstance(((i.get("details") or {}).get("content")), dict)
              and ((i.get("details") or {}).get("content") or {}).get("peeked")]
    if peeked:
        out.append("### %s" % _("volumes"))
        out.append("")
        for i in peeked:
            content = (i.get("details") or {}).get("content") or {}
            if not isinstance(content, dict):
                content = {}
            files = content.get("files") or []
            shown = ", ".join("`%s`" % f for f in files[:10])
            more = " (+%d more)" % (len(files) - 10) if len(files) > 10 else ""
            out.append("- `%s`: %s%s" % (i["name"], shown or _("none"), more))
        out.append("")
    out.append("## %s" % _("commands_preview"))
    out.append("")
    approved = [i for i in plan["items"] if i.get("approved") and i.get("selected", True)]
    if approved:
        out.append("```")
        for i in approved:
            command = i.get("command") or ""
            if command:
                out.append(command)
        out.append("```")
    else:
        out.append(_("nothing_to_do"))
    out.append("")
    out.append("## %s" % _("restore_hint"))
    out.append("")
    for i in plan["items"]:
        if i.get("approved") and i.get("selected", True):
            restore = i.get("restore") or {}
            hint = restore.get("hint") or "-"
            note = restore.get("note")
            out.append("- `%s`: %s%s" % (i.get("name", ""), hint, " (%s)" % note if note else ""))
    out.append("")
    if plan.get("warnings"):
        out.append("## %s" % _("warnings"))
        out.append("")
        for w in plan["warnings"]:
            out.append("- **%s** (%s)" % (w.get("code"), w.get("severity")))
        out.append("")
    return "\n".join(out)
