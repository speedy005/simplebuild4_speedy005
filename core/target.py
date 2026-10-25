"""Target configuration — the central data model of s4.

A Target bundles everything needed to build oscam for a specific device:
toolchain selection, build settings (modules/protocols/readers), and
optional deployment destinations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from core.credentials import encrypt as _encrypt, decrypt as _decrypt
from translation import t


@dataclass
class DeploymentConfig:
    """SSH/local deployment destination for a specific source × toolchain build."""

    name: str = ""                  # display name for the deployment entry
    enabled: bool = True            # False = skip during build deployment
    source: str = ""                # repo name
    toolchain: str = ""             # toolchain name
    host: str = ""
    user: str = "root"
    password: str = ""              # encrypted, for sshpass
    port: int = 22
    key: str = ""                   # SSH key path
    target_path: str = "/usr/bin/oscam"
    # When true, deployment copies the existing binary on the device
    # aside before overwriting it with the fresh build. Mirrors the
    # repo-backup flow: <target_path>.bak is kept so a rollback is a
    # single ``mv`` away. Off by default to match historical behavior.
    target_backup: bool = False
    su_password: str = ""              # encrypted, root password for su -c wrapping
    pre_cmd: str = ""
    post_cmd: str = ""
    # SCP transfer mode selector:
    #  "last_working" (default) — try the cached mode first; on failure
    #    rerun the full fallback chain and remember what worked.
    #  "automatic" — always run the full fallback chain, cache nothing.
    scp_mode: str = "last_working"
    # Cached mode that last succeeded, written by the deployer:
    # "", "default", "sftp" or "scp_protocol". Not meant for manual edits.
    last_working_scp_mode: str = ""


# Backward compat alias
UploadConfig = DeploymentConfig


@dataclass
class SourceConfig:
    """Source repository configuration for a target."""
    name: str = ""
    update_before_build: bool = False
    patch_before_build: bool = False
    patches: list[str] = field(default_factory=list)  # selected patch filenames


@dataclass
class ToolchainConfig:
    """Per-toolchain build settings override for a target."""
    name: str = ""
    cc_opts: str = ""              # Override CC_OPTS (empty = use toolchain default)
    cflags: str = ""               # Override EXTRA_CFLAGS
    ldflags: str = ""              # Override EXTRA_LDFLAGS
    make_vars: str = ""            # Override make variables
    stapi_lib: str = ""            # Override STAPI library
    use_compress: str = ""         # ""=use global, "0"=off, "1"=on
    compress_level: str = ""       # ""=use global, e.g. "--lzma"
    upx_binary: str = ""           # ""=use global, path to upx
    oscam_path: str = ""           # ""=use global, e.g. "/opt/oscam/bin/oscam"
    confdir: str = ""              # ""=use global, e.g. "/usr/local/etc"


@dataclass
class BuildSettings:
    """Oscam build configuration."""

    # Module selection
    modules: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    readers: list[str] = field(default_factory=list)
    card_readers: list[str] = field(default_factory=list)

    # USE/STATIC flags — dynamically parsed from Makefile
    use_flags: dict = field(default_factory=lambda: {"LIBCRYPTO": True})
    static_flags: dict = field(default_factory=dict)

    # Other build settings
    use_compress: bool = False
    compress_level: str = "--lzma"
    upx_binary: str = ""
    use_oscamname: str = ""
    stapi_lib: str = ""
    build_system: str = "make"
    confdir: str = "/etc/tuxbox/config"
    make_vars: str = ""


def _load_sources(raw: list) -> list[SourceConfig]:
    """Load sources from YAML data, handling both old (string) and new (dict) format."""
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append(SourceConfig(name=item))
        elif isinstance(item, dict):
            result.append(SourceConfig(
                name=item.get("name", ""),
                update_before_build=item.get("update_before_build", False),
                patch_before_build=item.get("patch_before_build", False),
                patches=item.get("patches", []),
            ))
    return result if result else [SourceConfig(name="default")]


def _load_toolchains(raw: list) -> list[ToolchainConfig]:
    """Load toolchains from YAML data, handling both old (string) and new (dict) format."""
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append(ToolchainConfig(name=item))
        elif isinstance(item, dict):
            result.append(ToolchainConfig(
                name=item.get("name", ""),
                cc_opts=item.get("cc_opts", ""),
                cflags=item.get("cflags", ""),
                ldflags=item.get("ldflags", ""),
                make_vars=item.get("make_vars", ""),
                stapi_lib=item.get("stapi_lib", ""),
                use_compress=item.get("use_compress", ""),
                compress_level=item.get("compress_level", ""),
                upx_binary=item.get("upx_binary", ""),
                oscam_path=item.get("oscam_path", ""),
                confdir=item.get("confdir", ""),
            ))
    return result


@dataclass
class Target:
    """A complete build target configuration."""

    name: str = ""
    description: str = ""
    sources: list[SourceConfig] = field(default_factory=lambda: [SourceConfig(name="default")])
    toolchains: list[ToolchainConfig] = field(default_factory=list)
    build: BuildSettings = field(default_factory=BuildSettings)
    deployment: list[DeploymentConfig] = field(default_factory=list)
    exclude_from_automation: bool = False
    delete_binary_after_deploy: bool = False
    # Path this target was loaded from, so the deployer can persist
    # cached scp_mode updates without the caller passing a save path.
    # Not serialized to YAML. Set by Target.load(); None for in-memory
    # instances built from scratch.
    _source_path: "Path | None" = field(default=None, repr=False, compare=False)

    def save_in_place(self) -> bool:
        """Persist back to the file this target was loaded from.

        Returns True on write, False when the target has no source path
        (freshly created / unsaved). Callers that only need to flush a
        deployer-side cache update can call this and ignore the result.
        """
        if self._source_path is None:
            return False
        self.save(self._source_path)
        return True

    def save(self, path: Path) -> None:
        """Save target to YAML file."""
        data = {
            "name": self.name,
            "description": self.description,
            "sources": [
                {k: v for k, v in {
                    "name": s.name,
                    "update_before_build": s.update_before_build,
                    "patch_before_build": s.patch_before_build,
                    "patches": s.patches if s.patches else None,
                }.items() if v is not None and v != False and v != ""}
                for s in self.sources
            ],
            "toolchains": [
                {k: v for k, v in {
                    "name": tc.name,
                    "cc_opts": tc.cc_opts,
                    "cflags": tc.cflags,
                    "ldflags": tc.ldflags,
                    "make_vars": tc.make_vars,
                    "stapi_lib": tc.stapi_lib,
                    "use_compress": tc.use_compress,
                    "compress_level": tc.compress_level,
                    "upx_binary": tc.upx_binary,
                    "oscam_path": tc.oscam_path,
                    "confdir": tc.confdir,
                }.items() if v}  # omit empty
                for tc in self.toolchains
            ],
            "build": {
                "modules": self.build.modules,
                "protocols": self.build.protocols,
                "readers": self.build.readers,
                "card_readers": self.build.card_readers,
                "use_flags": {k: v for k, v in self.build.use_flags.items()},
                "static_flags": {k: v for k, v in self.build.static_flags.items()},
                "use_compress": self.build.use_compress,
                "compress_level": self.build.compress_level,
                "upx_binary": self.build.upx_binary,
                "use_oscamname": self.build.use_oscamname,
                "stapi_lib": self.build.stapi_lib,
                "build_system": self.build.build_system,
                "confdir": self.build.confdir,
                "make_vars": self.build.make_vars,
            },
            "deployment": [
                {k: v for k, v in {
                    "name": u.name,
                    "enabled": u.enabled,
                    "source": u.source,
                    "toolchain": u.toolchain,
                    "host": u.host,
                    "user": u.user,
                    "password": _encrypt(u.password),
                    "port": u.port,
                    "key": u.key,
                    "target_path": u.target_path,
                    "target_backup": u.target_backup,
                    "su_password": _encrypt(u.su_password),
                    "pre_cmd": u.pre_cmd,
                    "post_cmd": u.post_cmd,
                    # Omit default to keep YAML tidy; load() falls back to it.
                    "scp_mode": u.scp_mode if u.scp_mode != "last_working" else "",
                    "last_working_scp_mode": u.last_working_scp_mode,
                }.items() if not (v == "" or v is None or (v == 0 and v is not False))}
                for u in self.deployment
                if u.host  # only save entries with a host
            ],
        }
        if self.exclude_from_automation:
            data["exclude_from_automation"] = True
        if self.delete_binary_after_deploy:
            data["delete_binary_after_deploy"] = True
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")

    @staticmethod
    def _source_names(sources: list["SourceConfig"]) -> list[str]:
        """Get just the names from source configs."""
        return [s.name for s in sources]

    @classmethod
    def load(cls, path: Path) -> "Target":
        """Load target from YAML file. Ignores unknown fields gracefully."""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        if not data:
            return cls()

        build_data = data.get("build", {})

        # Load use_flags/static_flags — support both new dict format and old field format.
        # Important: distinguish "key missing" (legacy YAML → migrate) from
        # "key present but empty" (modern YAML with everything off).
        use_flags = build_data.get("use_flags", {}) or {}
        static_flags = build_data.get("static_flags", {}) or {}

        # Backward compat: migrate old individual fields into dicts only if
        # the new key is not present in the YAML at all.
        if "use_flags" not in build_data:
            _old_use = {
                "LIBCRYPTO": ("use_libcrypto", True),
                "SSL": ("use_ssl", False),
                "OPENSSL": ("use_openssl", False),
                "LIBUSB": ("use_libusb", False),
                "PCSC": ("use_pcsc", False),
                "LIBDVBCSA": ("use_libdvbcsa", False),
                "STAPI": ("use_stapi", False),
                "STAPI5": ("use_stapi5", False),
                "COOLAPI": ("use_coolapi", False),
                "COOLAPI2": ("use_coolapi2", False),
                "SU980": ("use_su980", False),
                "AZBOX": ("use_azbox", False),
                "AMSMC": ("use_amsmc", False),
                "MCA": ("use_mca", False),
            }
            for flag, (field_name, default) in _old_use.items():
                val = build_data.get(field_name, default)
                if val:
                    use_flags[flag] = True
        if "static_flags" not in build_data:
            _old_static = {
                "LIBCRYPTO": ("static_libcrypto", False),
                "SSL": ("static_ssl", False),
                "LIBUSB": ("static_libusb", False),
                "PCSC": ("static_pcsc", False),
                "LIBDVBCSA": ("static_libdvbcsa", False),
            }
            for flag, (field_name, default) in _old_static.items():
                val = build_data.get(field_name, default)
                if val:
                    static_flags[flag] = True

        # WITH_SSL moved upstream from addons to the USE_SSL build flag —
        # drop leftover entries; users re-enable via use_flags.SSL.
        _modules = [m for m in (build_data.get("modules") or []) if m != "WITH_SSL"]
        build = BuildSettings(
            modules=sorted(_modules),
            protocols=sorted(build_data.get("protocols") or []),
            readers=sorted(build_data.get("readers") or []),
            card_readers=sorted(build_data.get("card_readers") or []),
            use_flags=use_flags,
            static_flags=static_flags,
            use_compress=build_data.get("use_compress", False),
            compress_level=build_data.get("compress_level", "--lzma"),
            upx_binary=build_data.get("upx_binary", ""),
            use_oscamname=build_data.get("use_oscamname", ""),
            stapi_lib=build_data.get("stapi_lib", ""),
            build_system=build_data.get("build_system", "make"),
            confdir=build_data.get("confdir", "/etc/tuxbox/config"),
            make_vars=build_data.get("make_vars", ""),
        )

        # Backward compat: read "deployment" first, fall back to "upload" for old files
        deploy_data = data.get("deployment") or data.get("upload") or []
        _tgt_name = data.get("name", "?")
        deployments = []
        for u in deploy_data:
            _dep_id = u.get('name') or u.get('host') or '?'
            deployments.append(DeploymentConfig(
                name=u.get("name", ""),
                enabled=u.get("enabled", True),
                source=u.get("source", ""),
                toolchain=u.get("toolchain", ""),
                host=u.get("host", ""),
                user=u.get("user", "root"),
                password=_decrypt(u.get("password", ""), label=lambda tn=_tgt_name, di=_dep_id: f"{t('te_password')} (Target '{tn}' / '{di}')"),
                port=u.get("port", 22),
                key=u.get("key", ""),
                target_path=u.get("target_path", "/usr/bin/oscam"),
                target_backup=u.get("target_backup", False),
                su_password=_decrypt(u.get("su_password", ""), label=lambda tn=_tgt_name, di=_dep_id: f"{t('te_su_password')} (Target '{tn}' / '{di}')"),
                pre_cmd=u.get("pre_cmd", ""),
                post_cmd=u.get("post_cmd", ""),
                scp_mode=u.get("scp_mode", "") or "last_working",
                last_working_scp_mode=u.get("last_working_scp_mode", ""),
            ))

        obj = cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            sources=_load_sources(data.get("sources", [{"name": "default"}])),
            toolchains=_load_toolchains(data.get("toolchains", [])),
            build=build,
            deployment=deployments,
            exclude_from_automation=bool(data.get("exclude_from_automation", False)),
            delete_binary_after_deploy=bool(data.get("delete_binary_after_deploy", False)),
        )
        obj._source_path = path
        return obj

    @classmethod
    def list_targets(cls, targets_dir: Path) -> list[tuple[str, Path]]:
        """List all saved targets. Returns [(name, path), ...]."""
        targets = []
        if targets_dir.is_dir():
            for f in sorted(targets_dir.glob("*.yaml")):
                targets.append((f.stem, f))
        return targets
