"""Target-related MCP tools.

Read
  list_targets
  get_target

Build
  build_target           (needs --allow-actions)
  get_build_status
  cancel_build           (needs --allow-actions)

Write (all need --allow-actions)
  create_target          create a new target.yaml from a spec
  update_target          patch fields of an existing target
  delete_target          remove the target.yaml
  rename_target          rename name + file
  duplicate_target       copy an existing target under a new name
"""

from __future__ import annotations

import re
import shutil
from typing import Any

from mcp.server import Server
from mcp.types import Tool

from core.target import (
    Target, ToolchainConfig, SourceConfig, DeploymentConfig, BuildSettings,
)
from core import jobs

from mcp_server.context import McpContext


_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_name(name: str, label: str = "target") -> None:
    if not name:
        raise ValueError(f"{label} name is required")
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid {label} name '{name}' — only letters, digits, '_', '-', '.' allowed"
        )


def _target_path(ctx: McpContext, name: str):
    return ctx.paths.targets_dir / f"{name}.yaml"


# ------------------------------------------------------------------ read


def _target_summary(target: Target) -> dict[str, Any]:
    """Flatten a Target into a JSON-friendly dict.

    Shape matches the structure the write tools accept, so an LLM can
    round-trip via get_target → edit → update_target.
    """
    def _tc_entry(tc) -> dict[str, Any]:
        return {
            "name": getattr(tc, "name", ""),
            "cc_opts": getattr(tc, "cc_opts", ""),
            "cflags": getattr(tc, "cflags", ""),
            "ldflags": getattr(tc, "ldflags", ""),
            "make_vars": getattr(tc, "make_vars", ""),
            "stapi_lib": getattr(tc, "stapi_lib", ""),
            "oscam_path": getattr(tc, "oscam_path", ""),
            "confdir": getattr(tc, "confdir", ""),
        }

    deployments = []
    for d in (target.deployment or []):
        if d is None:
            continue
        deployments.append({
            "name": getattr(d, "name", ""),
            "enabled": bool(getattr(d, "enabled", True)),
            "source": getattr(d, "source", ""),
            "toolchain": getattr(d, "toolchain", ""),
            "host": getattr(d, "host", ""),
            "user": getattr(d, "user", "root"),
            "port": int(getattr(d, "port", 22) or 22),
            "key": getattr(d, "key", ""),
            "target_path": getattr(d, "target_path", "/usr/bin/oscam"),
            "target_backup": bool(getattr(d, "target_backup", False)),
            "pre_cmd": getattr(d, "pre_cmd", ""),
            "post_cmd": getattr(d, "post_cmd", ""),
            "scp_mode": getattr(d, "scp_mode", "last_working") or "last_working",
            "last_working_scp_mode": getattr(d, "last_working_scp_mode", ""),
        })

    sources = [
        {
            "name": s.name,
            "update_before_build": bool(s.update_before_build),
            "patch_before_build": bool(s.patch_before_build),
            "patches": list(s.patches or []),
        }
        for s in (target.sources or [])
    ]

    # USE_* flags are a flat dict in the stored YAML, but the TUI /
    # main summary split them into "hardware" (STAPI, SU980, GXAPI, WI,
    # …) vs. "library" (SSL, LIBDVBCSA, LIBCRYPTO, …). Expose the same
    # split here so LLM clients can explain a target without guessing
    # which flag is which.
    from core.oscam_config import HARDWARE_USE_FLAGS
    _use = dict(target.build.use_flags or {})
    _hw_enabled = sorted(f"USE_{f}" for f, v in _use.items() if v and f in HARDWARE_USE_FLAGS)
    _lib_enabled = sorted(f"USE_{f}" for f, v in _use.items() if v and f not in HARDWARE_USE_FLAGS)

    return {
        "name": target.name,
        "description": target.description,
        "sources": sources,
        "toolchains": [_tc_entry(tc) for tc in target.toolchains],
        "build": {
            "build_system": getattr(target.build, "build_system", ""),
            "modules": list(target.build.modules or []),
            "protocols": list(target.build.protocols or []),
            "readers": list(target.build.readers or []),
            "card_readers": list(target.build.card_readers or []),
            "use_flags": _use,
            "use_flags_hardware_enabled": _hw_enabled,
            "use_flags_library_enabled": _lib_enabled,
            "static_flags": dict(target.build.static_flags or {}),
            "use_compress": bool(target.build.use_compress),
            "compress_level": target.build.compress_level,
            "upx_binary": target.build.upx_binary,
            "use_oscamname": target.build.use_oscamname,
            "stapi_lib": target.build.stapi_lib,
            "confdir": target.build.confdir,
            "make_vars": target.build.make_vars,
        },
        "deployment": deployments,
        "exclude_from_automation": bool(target.exclude_from_automation),
        "delete_binary_after_deploy": bool(target.delete_binary_after_deploy),
    }


def _list_targets_impl(ctx: McpContext) -> list[dict[str, Any]]:
    result = []
    for name, path in Target.list_targets(ctx.paths.targets_dir):
        try:
            t = Target.load(path)
            result.append({
                "name": name,
                "description": t.description,
                "toolchains": [tc.name for tc in t.toolchains],
                "yaml_path": str(path),
            })
        except Exception as exc:
            result.append({
                "name": name,
                "description": f"(load error: {exc})",
                "toolchains": [],
                "yaml_path": str(path),
            })
    return result


def _get_target_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    available = dict(Target.list_targets(ctx.paths.targets_dir))
    if name not in available:
        raise ValueError(f"Unknown target: {name}")
    t = Target.load(available[name])
    return _target_summary(t)


# ------------------------------------------------------------------ write helpers


def _apply_patch_to_target(target: Target, patch: dict[str, Any]) -> None:
    """Apply a JSON patch to an in-memory Target.

    Semantics:
      * Simple top-level fields (name, description, exclude_from_automation,
        delete_binary_after_deploy) are replaced.
      * ``build`` is a dict — only keys mentioned in the patch are replaced
        (other keys of ``build`` are preserved).
      * ``sources`` / ``toolchains`` / ``deployment`` are lists — the whole
        list is replaced when present (no per-entry merge). Pass the
        complete list you want to end up with.
    """
    # Name / description / automation flag
    if "description" in patch:
        target.description = str(patch["description"] or "")
    if "exclude_from_automation" in patch:
        target.exclude_from_automation = bool(patch["exclude_from_automation"])
    if "delete_binary_after_deploy" in patch:
        target.delete_binary_after_deploy = bool(patch["delete_binary_after_deploy"])
    if "name" in patch:
        # note: the *file* is renamed elsewhere; keep fields in sync
        target.name = str(patch["name"] or "")

    # build — shallow key-by-key replace
    if "build" in patch and isinstance(patch["build"], dict):
        b = patch["build"]
        bs = target.build
        _build_allowed = {
            "build_system", "use_compress", "compress_level", "upx_binary",
            "use_oscamname", "stapi_lib", "confdir", "make_vars",
        }
        for key in _build_allowed:
            if key in b:
                setattr(bs, key, b[key])
        for list_key in ("modules", "protocols", "readers", "card_readers"):
            if list_key in b:
                setattr(bs, list_key, list(b[list_key] or []))
        if "use_flags" in b:
            bs.use_flags = {str(k): bool(v) for k, v in (b["use_flags"] or {}).items()}
        if "static_flags" in b:
            bs.static_flags = {str(k): bool(v) for k, v in (b["static_flags"] or {}).items()}

    # sources — full list replace
    if "sources" in patch:
        target.sources = [
            SourceConfig(
                name=(s["name"] if isinstance(s, dict) else str(s)),
                update_before_build=bool(s.get("update_before_build", False)) if isinstance(s, dict) else False,
                patch_before_build=bool(s.get("patch_before_build", False)) if isinstance(s, dict) else False,
                patches=list(s.get("patches") or []) if isinstance(s, dict) else [],
            )
            for s in (patch["sources"] or [])
        ]

    # toolchains — full list replace
    if "toolchains" in patch:
        target.toolchains = [
            ToolchainConfig(
                name=(tc["name"] if isinstance(tc, dict) else str(tc)),
                cc_opts=str(tc.get("cc_opts", "")) if isinstance(tc, dict) else "",
                cflags=str(tc.get("cflags", "")) if isinstance(tc, dict) else "",
                ldflags=str(tc.get("ldflags", "")) if isinstance(tc, dict) else "",
                make_vars=str(tc.get("make_vars", "")) if isinstance(tc, dict) else "",
                stapi_lib=str(tc.get("stapi_lib", "")) if isinstance(tc, dict) else "",
                oscam_path=str(tc.get("oscam_path", "")) if isinstance(tc, dict) else "",
                confdir=str(tc.get("confdir", "")) if isinstance(tc, dict) else "",
            )
            for tc in (patch["toolchains"] or [])
        ]

    # deployment — full list replace, but preserve secrets (password,
    # su_password) from matching existing entries. MCP callers cannot
    # set/read those fields, so a naive replace would silently wipe
    # working credentials on any update_target(deployment=...) call.
    # Match by ``name`` first, then fall back to ``host:user``.
    if "deployment" in patch:
        def _dkey(name: str, host: str, user: str) -> str:
            return name if name else f"{host}:{user}"

        old_by_key: dict[str, DeploymentConfig] = {}
        for old in (target.deployment or []):
            old_by_key[_dkey(old.name, old.host, old.user)] = old

        new_list: list[DeploymentConfig] = []
        for d in (patch["deployment"] or []):
            if not isinstance(d, dict):
                continue
            name = str(d.get("name", ""))
            host = str(d.get("host", ""))
            user = str(d.get("user", "root"))
            preserved = old_by_key.get(_dkey(name, host, user))
            # scp_mode: honour the patch; fall back to the preserved
            # value (or "last_working" default) if the LLM omits it, so
            # a round-trip edit never silently resets the user's choice.
            _scp_mode = str(d.get("scp_mode", "")).strip()
            if _scp_mode not in ("last_working", "automatic"):
                _scp_mode = preserved.scp_mode if preserved else "last_working"
            # last_working_scp_mode is deployer-managed state; let the
            # caller reset it (useful when the box's SSH stack changed)
            # but default to keeping whatever we had.
            if "last_working_scp_mode" in d:
                _lw = str(d.get("last_working_scp_mode", "") or "")
            else:
                _lw = preserved.last_working_scp_mode if preserved else ""
            new_list.append(DeploymentConfig(
                name=name,
                enabled=bool(d.get("enabled", True)),
                source=str(d.get("source", "")),
                toolchain=str(d.get("toolchain", "")),
                host=host,
                user=user,
                port=int(d.get("port", 22) or 22),
                key=str(d.get("key", "")),
                target_path=str(d.get("target_path", "/usr/bin/oscam")),
                target_backup=bool(d.get("target_backup", False)),
                pre_cmd=str(d.get("pre_cmd", "")),
                post_cmd=str(d.get("post_cmd", "")),
                password=preserved.password if preserved else "",
                su_password=preserved.su_password if preserved else "",
                scp_mode=_scp_mode,
                last_working_scp_mode=_lw,
            ))
        target.deployment = new_list


# ------------------------------------------------------------------ write impls


def _seed_oscam_defaults(ctx: McpContext, target: Target) -> dict[str, int]:
    """Populate empty option categories from the assigned repos' config.sh.

    Mirrors the TUI's target-editor behaviour for a fresh target: if the
    caller didn't provide explicit ``modules``/``protocols``/``readers``/
    ``card_readers`` lists, seed each empty category with the oscam
    defaults (from ``config.sh``'s ``$defconfig``). Non-empty categories
    are left alone — the caller may have supplied a deliberately minimal
    or curated set.

    Returns a dict of category → count-of-seeded-entries for logging /
    the tool response. Silently returns {} if none of the assigned
    source repos is cloned under ``data/repos/`` (nothing to read).
    """
    from core.oscam_config import merge_options
    from core.repos_config import load_repos

    cats = {
        "modules": "addons",
        "protocols": "protocols",
        "readers": "readers",
        "card_readers": "card_readers",
    }
    b = target.build
    # Short-circuit when the caller already populated everything.
    if all(getattr(b, tgt_key) for tgt_key in cats):
        return {}

    repos_by_name = {r.name: r for r in load_repos(ctx.paths.repos_config)}
    repo_dirs: list = []
    for src in target.sources:
        # Consider the clone path the RepoManager uses:
        # data/repos/oscam-<name>/. Only include repos that are
        # actually on disk — can't parse config.sh otherwise.
        if src.name not in repos_by_name:
            continue
        clone = ctx.paths.repos_dir / f"oscam-{src.name}"
        if clone.is_dir() and clone not in repo_dirs:
            repo_dirs.append(clone)
    if not repo_dirs:
        return {}

    oscam = merge_options(repo_dirs)
    seeded: dict[str, int] = {}
    for tgt_key, oscam_cat in cats.items():
        if getattr(b, tgt_key):
            continue  # already populated, respect caller
        defaults = oscam.default_enabled.get(oscam_cat, [])
        if defaults:
            setattr(b, tgt_key, list(defaults))
            seeded[tgt_key] = len(defaults)
    return seeded


def _create_target_impl(ctx: McpContext, name: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "create_target is disabled — start the MCP server with --allow-actions"
        )
    _validate_name(name)
    path = _target_path(ctx, name)
    if path.exists():
        raise ValueError(f"Target '{name}' already exists")
    target = Target(name=name)
    if spec:
        _apply_patch_to_target(target, spec)
    target.name = name
    if not target.sources:
        target.sources = [SourceConfig(name="default")]
    # Seed oscam options from the assigned repos' config.sh when the
    # caller didn't specify them. Without this, a "create_target" call
    # would end up with modules=[]/protocols=[]/... and the build
    # would fail with "Enabling 0 options". The TUI's target editor
    # does the same seeding on first compose.
    seeded = _seed_oscam_defaults(ctx, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    target.save(path)
    result = {"name": name, "yaml_path": str(path), "created": True}
    if seeded:
        result["seeded_oscam_defaults"] = seeded
    return result


def _update_target_impl(ctx: McpContext, name: str, patch: dict[str, Any]) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "update_target is disabled — start the MCP server with --allow-actions"
        )
    available = dict(Target.list_targets(ctx.paths.targets_dir))
    if name not in available:
        raise ValueError(f"Unknown target: {name}")
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    target = Target.load(available[name])
    _apply_patch_to_target(target, patch)
    # The name inside the YAML file always mirrors the file stem — the
    # write tool for renaming is rename_target, not a "name" patch.
    target.name = name
    target.save(available[name])
    return {"name": name, "saved": True, "applied_keys": sorted(patch.keys())}


def _delete_target_impl(ctx: McpContext, name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "delete_target is disabled — start the MCP server with --allow-actions"
        )
    path = _target_path(ctx, name)
    if not path.exists():
        raise ValueError(f"Unknown target: {name}")
    path.unlink()
    return {"name": name, "deleted": True, "yaml_path": str(path)}


def _rename_target_impl(ctx: McpContext, old_name: str, new_name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "rename_target is disabled — start the MCP server with --allow-actions"
        )
    _validate_name(new_name)
    old_path = _target_path(ctx, old_name)
    new_path = _target_path(ctx, new_name)
    if not old_path.exists():
        raise ValueError(f"Unknown target: {old_name}")
    if new_path.exists():
        raise ValueError(f"Target '{new_name}' already exists")
    target = Target.load(old_path)
    target.name = new_name
    target.save(new_path)
    old_path.unlink()
    return {"old_name": old_name, "new_name": new_name, "renamed": True}


def _duplicate_target_impl(ctx: McpContext, source: str, new_name: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "duplicate_target is disabled — start the MCP server with --allow-actions"
        )
    _validate_name(new_name)
    src_path = _target_path(ctx, source)
    dst_path = _target_path(ctx, new_name)
    if not src_path.exists():
        raise ValueError(f"Unknown source target: {source}")
    if dst_path.exists():
        raise ValueError(f"Target '{new_name}' already exists")
    target = Target.load(src_path)
    target.name = new_name
    target.description = f"Copy of {target.description}" if target.description else f"Copy of {source}"
    target.save(dst_path)
    return {"source": source, "new_name": new_name, "duplicated": True}


# ------------------------------------------------------------------ build / cancel


def _build_target_impl(
    ctx: McpContext,
    name: str,
    overrides: str = "",
    build_system: str = "",
) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "build_target is disabled — start the MCP server with --allow-actions"
        )
    bs = build_system.strip() or None
    if bs and bs not in ("make", "cmake"):
        raise ValueError("build_system must be 'make' or 'cmake'")
    ctx.refresh_settings()
    handle = jobs.start_build_job(
        ctx.paths, ctx.settings, name,
        overrides=overrides.strip(),
        build_system=bs,
    )
    return {
        "job_id": handle.job_id,
        "log_path": str(handle.log_path),
        "status": "running",
        "tip": "Poll with get_build_status(job_id).",
    }


def _get_build_status_impl(ctx: McpContext, job_id: str, tail_bytes: int = 4096) -> dict[str, Any]:
    status = jobs.load_job_status(ctx.paths.logs_dir, job_id)
    if not status:
        raise ValueError(f"Unknown job_id: {job_id}")
    out: dict[str, Any] = dict(status)
    out["running"] = status.get("finished") is None
    out["tail"] = jobs.tail_log(ctx.paths.logs_dir, job_id, max_bytes=tail_bytes)
    return out


def _cancel_build_impl(ctx: McpContext, job_id: str) -> dict[str, Any]:
    if not ctx.allow_actions:
        raise PermissionError(
            "cancel_build is disabled — start the MCP server with --allow-actions"
        )
    signalled = jobs.cancel_job(job_id)
    return {
        "job_id": job_id,
        "signalled": signalled,
        "note": (
            "Cooperative cancel sent. Poll get_build_status to see the final state."
            if signalled
            else "No running job with this id (may already be finished)."
        ),
    }


# ------------------------------------------------------------------ register


def register(server: Server, ctx: McpContext) -> list[Tool]:
    """Register target-related tools. Returns their Tool definitions."""

    tools: list[Tool] = [
        Tool(
            name="list_targets",
            description=(
                "List all configured s4 targets "
                "(device-specific oscam build configurations)."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="get_target",
            description=(
                "Return full configuration of a single target by name. "
                "The returned shape mirrors the patch format used by "
                "update_target / create_target — round-trip friendly. "
                "``build`` additionally carries read-only convenience "
                "fields ``use_flags_hardware_enabled`` and "
                "``use_flags_library_enabled`` — pre-split lists of "
                "enabled USE_* flags (hardware = STAPI, STAPI5, SU980, "
                "WI, GXAPI, COOLAPI, AZBOX, …; library = everything "
                "else). Ignore them on write-back; ``use_flags`` is "
                "the authoritative source."
            ),
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="get_build_status",
            description=(
                "Poll the status of a build job. Returns running state, "
                "success flag (once finished), and a tail of the log."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "tail_bytes": {"type": "integer", "default": 4096},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        ),
    ]

    if ctx.allow_actions:
        tools.extend([
            Tool(
                name="build_target",
                description=(
                    "Start a build of an s4 target in the background. Returns a "
                    "job_id — poll with get_build_status to watch progress. "
                    "Builds can take minutes to hours."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Target name"},
                        "overrides": {
                            "type": "string",
                            "description": "Space-separated build overrides (USE_SSL MODULE_GBOX_off ...)",
                            "default": "",
                        },
                        "build_system": {
                            "type": "string",
                            "enum": ["", "make", "cmake"],
                            "default": "",
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="cancel_build",
                description="Signal cooperative cancellation to a running build.",
                inputSchema={
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="create_target",
                description=(
                    "Create a new target.yaml. 'spec' is optional — its shape "
                    "matches get_target's return value (top-level: description, "
                    "sources, toolchains, build, deployment, exclude_from_automation, "
                    "delete_binary_after_deploy). "
                    "If the spec omits build.modules / protocols / readers / "
                    "card_readers (or leaves them empty), s4 seeds each empty "
                    "category from the assigned source repo's config.sh "
                    "defaults — same behaviour as the TUI's target editor. "
                    "The returned dict includes ``seeded_oscam_defaults`` "
                    "with the per-category count when that kicks in. "
                    "TIP: call get_oscam_options first to discover valid "
                    "addon / protocol / reader / card_reader / USE_* flag "
                    "names and their meanings for the source repo you're "
                    "targeting."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "spec": {"type": "object", "description": "Target fields (optional)"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="update_target",
                description=(
                    "Patch fields of an existing target. Simple top-level "
                    "fields and build.* keys are replaced per-key; sources, "
                    "toolchains and deployment are replaced as a whole list. "
                    "Use get_target first to see the current shape, and "
                    "get_oscam_options if you need to look up valid option "
                    "names + descriptions for build.modules / protocols / "
                    "readers / card_readers / use_flags."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "patch": {"type": "object", "description": "Fields to apply"},
                    },
                    "required": ["name", "patch"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="delete_target",
                description="Delete a target's YAML file.",
                inputSchema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="rename_target",
                description="Rename an existing target (file + name field).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "old_name": {"type": "string"},
                        "new_name": {"type": "string"},
                    },
                    "required": ["old_name", "new_name"],
                    "additionalProperties": False,
                },
            ),
            Tool(
                name="duplicate_target",
                description="Clone an existing target under a new name.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "new_name": {"type": "string"},
                    },
                    "required": ["source", "new_name"],
                    "additionalProperties": False,
                },
            ),
        ])

    return tools


def dispatch(ctx: McpContext, name: str, args: dict[str, Any]) -> Any:
    """Execute one of the tools above. Returns a JSON-serializable value."""
    if name == "list_targets":
        return _list_targets_impl(ctx)
    if name == "get_target":
        return _get_target_impl(ctx, args["name"])
    if name == "build_target":
        return _build_target_impl(
            ctx,
            args["name"],
            overrides=args.get("overrides", ""),
            build_system=args.get("build_system", ""),
        )
    if name == "get_build_status":
        return _get_build_status_impl(
            ctx, args["job_id"], tail_bytes=int(args.get("tail_bytes", 4096)),
        )
    if name == "cancel_build":
        return _cancel_build_impl(ctx, args["job_id"])
    if name == "create_target":
        return _create_target_impl(ctx, args["name"], args.get("spec") or {})
    if name == "update_target":
        return _update_target_impl(ctx, args["name"], args["patch"])
    if name == "delete_target":
        return _delete_target_impl(ctx, args["name"])
    if name == "rename_target":
        return _rename_target_impl(ctx, args["old_name"], args["new_name"])
    if name == "duplicate_target":
        return _duplicate_target_impl(ctx, args["source"], args["new_name"])
    raise KeyError(name)


TOOL_NAMES = {
    "list_targets", "get_target",
    "build_target", "cancel_build", "get_build_status",
    "create_target", "update_target", "delete_target",
    "rename_target", "duplicate_target",
}
