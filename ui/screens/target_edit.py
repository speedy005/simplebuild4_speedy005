"""Target editor screen — create or edit a target configuration."""

from __future__ import annotations

from pathlib import Path
from ui.icons import icon

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Switch, Input, Collapsible, Select, Button, Checkbox
from textual.containers import Horizontal, Vertical, VerticalScroll, Grid

from core.target import Target, BuildSettings, DeploymentConfig, SourceConfig, ToolchainConfig
from core.toolchain import ToolchainRegistry
from core.repos_config import load_repos
from core.oscam_config import (
    HARDWARE_USE_FLAGS, OscamOptions, merge_options,
    parse_makefile_flags, read_options,
)
from translation import t
from ui.header import S4Header
from ui.footer import S4Footer


def _switch_row(label: str, value: bool, widget_id: str) -> Horizontal:
    return Horizontal(
        Static(f" {label}", classes="s-label"),
        Switch(value=value, id=widget_id),
        classes="s-toggle",
    )


def _safe_id(name: str) -> str:
    """Convert a name to a safe Textual widget ID.

    Textual requires IDs to contain only [a-zA-Z0-9_-] and not start with a digit.
    Anything else is replaced with '-'. A leading digit gets an 's' prefix.
    """
    import re as _re
    s = _re.sub(r'[^a-zA-Z0-9_-]', '-', name)
    if s and s[0].isdigit():
        s = "s" + s
    return s


def _column_major(items: list, cols: int = 4) -> list:
    """Reorder items so a row-major grid renders them column by column.

    Default Textual Grid fills row-by-row (left → right, then next row).
    With this reordering, reading the rendered grid top → bottom in
    column 1, then column 2, etc., yields the original order.

    The returned list keeps ``None`` placeholders for empty grid cells
    in the last column when ``len(items) % cols != 0`` — without them
    the trailing items would slide forward into the gap and break the
    visual column order. Callers must yield a blank widget for any
    ``None`` they iterate over.
    """
    n = len(items)
    if n == 0 or cols <= 1:
        return list(items)
    rows = (n + cols - 1) // cols
    placed: list = [None] * (rows * cols)
    for i, item in enumerate(items):
        col = i // rows
        row = i % rows
        placed[row * cols + col] = item
    return placed


def _switch_compact(label: str, value: bool, widget_id: str, removed: bool = False, row_class: str = "", tooltip: str = "") -> Horizontal:
    """Compact switch for multi-column grid layout."""
    display_label = f" [red strike]{label}[/red strike]" if removed else f" {label}"
    classes = "s-compact"
    if row_class:
        classes = f"s-compact {row_class}"
    sw = Switch(value=value, id=widget_id)
    if tooltip:
        sw.tooltip = tooltip
    lbl = Static(display_label, classes="sc-label")
    return Horizontal(sw, lbl, classes=classes)


class TargetEditScreen(Screen):
    """Edit or create a target configuration."""

    BINDINGS = [
        ("g", "switch_tab('general')", "General"),
        ("t", "switch_tab('source')", "Toolchain"),
        ("o", "switch_tab('oscam')", "Options"),
        ("d", "switch_tab('deploy')", "Deployment"),
    ]

    _collapsible_states: dict[str, bool] = {}  # class-level: id → collapsed
    _tc_flags_editing: str = ""  # class-level: sid of the TC whose flags are shown

    def __init__(
        self,
        target: Target | None = None,
        save_path: Path | None = None,
        active_tab: str = "general",
        focus_id: str = "",
        options_seeded: bool = False,
        sticky_stale: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__()
        self.target = target or Target()
        self.save_path = save_path
        self._oscam_opts: OscamOptions | None = None
        self._active_tab = active_tab
        self._focus_id = focus_id
        self._tc_defaults: dict[str, dict] = {}  # sid → {cc, cf, ld, mv}
        self._tc_info_cache: dict[str, tuple[str, str]] = {}  # sid → (name, arch)
        # Options have been "seeded" once an existing yaml was loaded
        # OR the user has touched the screen (refreshes from _collect
        # always set this). A truly new untouched target stays unseeded
        # so the first compose can fill in oscam defaults.
        self._options_seeded = options_seeded or bool(save_path)
        # Stale (red, no longer in repo) options survive toggle-all
        # actions until the user explicitly Resets the category.
        self._sticky_stale: dict[str, list[str]] = dict(sticky_stale or {})

    def _load_oscam_options(self) -> OscamOptions:
        """Load and merge oscam options from all assigned source repos."""
        if self._oscam_opts:
            return self._oscam_opts
        repos = load_repos(self.app.paths.repos_config)
        repo_dirs = []

        # Collect repo dirs from assigned sources only
        src_names = [s.name for s in self.target.sources]
        for src_name in src_names:
            for r in repos:
                if r.name == src_name:
                    for d in self._find_repo_dirs(r.name):
                        if d not in repo_dirs:
                            repo_dirs.append(d)

        self._oscam_opts = merge_options(repo_dirs) if repo_dirs else OscamOptions()
        return self._oscam_opts

    # Hardware flags — everything else goes into Library section.
    # Defined once in core.oscam_config.HARDWARE_USE_FLAGS so the main
    # screen's target summary and this editor agree on what counts as
    # hardware vs. library.
    _HARDWARE_FLAGS = HARDWARE_USE_FLAGS

    def _load_makefile_flags(self) -> dict:
        """Load USE_*/STATIC_* flags from Makefiles of assigned repos."""
        if hasattr(self, "_makefile_flags") and self._makefile_flags is not None:
            return self._makefile_flags
        repos = load_repos(self.app.paths.repos_config)
        repo_dirs = []
        src_names = [s.name for s in self.target.sources]
        for src_name in src_names:
            for r in repos:
                if r.name == src_name:
                    for d in self._find_repo_dirs(r.name):
                        if d not in repo_dirs:
                            repo_dirs.append(d)
        self._makefile_flags = parse_makefile_flags(repo_dirs) if repo_dirs else {"use": [], "static": []}
        return self._makefile_flags

    def _find_repo_dirs(self, name: str) -> list[Path]:
        """Find possible repo directories for a given repo name."""
        repos_dir = self.app.paths.repos_dir
        candidates = [
            repos_dir / name,
            repos_dir / f"oscam-{name}",
        ]
        # Also check any directory containing the name
        if repos_dir.is_dir():
            for d in repos_dir.iterdir():
                if d.is_dir() and name in d.name and d not in candidates:
                    candidates.append(d)
        return [d for d in candidates if d.is_dir()]

    def compose(self) -> ComposeResult:
        tgt = self.target
        b = tgt.build

        repos = load_repos(self.app.paths.repos_config)
        registry = ToolchainRegistry(
            self.app.paths.toolchains_defaults,
            self.app.paths.toolchains_local,
            self.app.paths.toolchains_dir,
        )
        registry.load()
        installed = registry.installed_toolchains()
        tc_names = [tc.name for tc in tgt.toolchains]
        tc_map = {tc.name: tc for tc in tgt.toolchains}
        oscam = self._load_oscam_options()

        yield S4Header()

        # Tab bar
        with Horizontal(id="te-tab-bar"):
            yield Static(" [b]G[/b]eneral ", id="te-tab-general", classes="tab-active")
            yield Static(" [b]T[/b]oolchain", id="te-tab-source", classes="tab-inactive")
            yield Static(" [b]O[/b]ptions", id="te-tab-oscam", classes="tab-inactive")
            yield Static(" [b]D[/b]eployment ", id="te-tab-deploy", classes="tab-inactive")

        # === Tab 1: General ===
        # UPX detection (once)
        import shutil as _shutil
        import subprocess as _sp
        self._upx_path = _shutil.which("upx") or ""
        self._upx_info = ""
        if self._upx_path:
            try:
                _upx_ver = _sp.check_output([self._upx_path, "--version"], text=True, stderr=_sp.DEVNULL, timeout=5).splitlines()[0]
                self._upx_info = f" [dim]{self._upx_path} — {_upx_ver}[/dim]"
            except Exception:
                self._upx_info = f" [dim]{self._upx_path}[/dim]"

        has_source = bool(oscam.addons)  # True if any source repo is assigned and cloned

        with VerticalScroll(id="te-panel-general"):
            with Collapsible(title=t("section_common"), collapsed=False, id="col-common"):
                yield Horizontal(
                    Static(f" {t('te_name')}", classes="s-label-tall"),
                    Input(value=tgt.name, placeholder=t("ph_target_name"), id="t-name"),
                    Checkbox("Exclude from automation", value=tgt.exclude_from_automation, id="t-excl-auto"),
                    classes="s-row",
                )
                _del_cb = Checkbox(
                    t("te_delete_binary_after_deploy"),
                    value=tgt.delete_binary_after_deploy,
                    id="t-del-binary",
                )
                _del_cb.tooltip = t("tt_delete_binary_after_deploy")
                yield Horizontal(
                    Static(f" {t('te_description')}", classes="s-label-tall"),
                    Input(value=tgt.description, placeholder=t("ph_target_desc"), id="t-desc"),
                    _del_cb,
                    classes="s-row",
                )
                compress_enabled = b.use_compress and bool(self._upx_path)
                upx_switch = Switch(value=compress_enabled, id="f-use-compress", disabled=not bool(self._upx_path))
                import re as _re
                upx_tooltip = _re.sub(r'\[/?[a-z_ ]+\]', '', self._upx_info).strip() if self._upx_path else "upx not found"
                upx_switch.tooltip = upx_tooltip
                upx_binary_val = b.upx_binary or self._upx_path or ""
                upx_path_inp = Input(value=upx_binary_val, placeholder=t("ph_system_default"), id="t-upx-binary")
                upx_path_inp.styles.width = "1fr"
                if compress_enabled:
                    lvl_inp = Input(value=b.compress_level, placeholder=self.app.settings.default_compress_level, id="t-compress-level")
                    lvl_inp.styles.max_width = 25
                    from textual.widgets import Button as _Btn
                    upx_browse_btn = _Btn(icon("browse"), id="btn-upx-browse", classes="tc-reset-btn")
                    upx_browse_btn.tooltip = t("ph_browse_upx")
                    yield Horizontal(
                        Static(" USE_COMPRESS", classes="flag-label"),
                        upx_switch,
                        Static(f" {t('te_binary')}", classes="src-opt-label"),
                        upx_path_inp,
                        upx_browse_btn,
                        Static(f" {t('te_compression_level')}", classes="src-opt-label"),
                        lvl_inp,
                        classes="flag-row",
                    )
                else:
                    yield Horizontal(
                        Static(" USE_COMPRESS", classes="flag-label"),
                        upx_switch,
                        classes="flag-row",
                    )
                yield Horizontal(
                    Static(f" {t('te_build_system')}", classes="s-label-tall"),
                    Select(
                        options=[("make", "make"), ("cmake", "cmake")],
                        value=b.build_system or self.app.settings.default_build_system,
                        id="t-build-system",
                        prompt=t("select_prompt"),
                    ),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_oscam_binary_path')}", classes="s-label-tall"),
                    Input(value=b.use_oscamname, placeholder=t("ph_oscam_binary_path"), id="t-oscamname"),
                    Button(icon("browse"), id="btn-binary-browse", classes="tc-reset-btn"),
                    classes="s-row",
                )
                yield Horizontal(
                    Static(f" {t('te_config_directory')}", classes="s-label-tall"),
                    Input(value=b.confdir, id="t-confdir"),
                    classes="s-row",
                )
            # Repositories (moved from Toolchain tab)
            src_names = [s.name for s in tgt.sources]
            src_map = {s.name: s for s in tgt.sources}
            src_count = len(tgt.sources)
            patches_dir = self.app.paths.patches_dir
            available_patches = sorted(p.name for p in patches_dir.glob("*.patch")) if patches_dir.is_dir() else []

            with Collapsible(title=f"Repositories ({src_count} {t('te_selected')})", collapsed=True, id="col-sources"):
                for r in repos:
                    enabled = r.name in src_names
                    sc = src_map.get(r.name, SourceConfig(name=r.name))
                    rid = _safe_id(r.name)
                    src_label = Static(f" {r.name}\n [dim]{r.url}[/dim]", classes="src-name")
                    yield Horizontal(
                        src_label,
                        Switch(value=enabled, id=f"src-{rid}"),
                        *([
                            Static(f" [dim]{t('te_update_before_build')}[/dim]", classes="src-opt-label"),
                            Switch(value=sc.update_before_build, id=f"src-upd-{rid}"),
                            Static(f" [dim]{t('te_patch_before_build')}[/dim]", classes="src-opt-label"),
                            Switch(value=sc.patch_before_build, id=f"src-patch-{rid}"),
                        ] if enabled else []),
                        classes="src-row",
                    )
                    if enabled and sc.patch_before_build and available_patches:
                        with Grid(classes="mod-grid patch-grid"):
                            for patch in available_patches:
                                pid = _safe_id(patch.replace(".patch", ""))
                                selected = patch in sc.patches
                                yield _switch_compact(patch, selected, f"p-{rid}-{pid}")
                if not repos:
                    yield Static(f"  [dim]{t('te_no_repos_hint')}[/dim]")

        # === Tab 2: Toolchain ===
        with VerticalScroll(id="te-panel-source"):
            # Toolchains
            tc_count = len(tgt.toolchains)
            with Collapsible(title=f"Toolchains ({tc_count} {t('te_selected')})", collapsed=False, id="col-toolchains"):
                from textual.widgets import Button as Btn
                editing_sid = TargetEditScreen._tc_flags_editing

                def _tc_cell(tc):
                    sid = _safe_id(tc.name)
                    enabled = tc.name in tc_names
                    is_editing = (sid == editing_sid)
                    # Cache tc info for live bold updates
                    self._tc_info_cache[sid] = (tc.name, tc.arch or "")
                    # Bold if has custom settings
                    tcc = tc_map.get(tc.name)
                    has_custom = tcc and any([tcc.cc_opts, tcc.cflags, tcc.ldflags, tcc.make_vars, tcc.use_compress, tcc.oscam_path, tcc.confdir])
                    bold = has_custom
                    name_str = f"[bold]{tc.name}[/bold]" if bold else tc.name
                    if tc.arch:
                        label = f" {name_str} [dim]({tc.arch})[/dim]"
                    else:
                        label = f" {name_str}"
                    widgets = [
                        Switch(value=enabled, id=f"tc-{sid}"),
                        Static(label, classes="tc-toggle-label"),
                    ]
                    if enabled:
                        btn_label = icon("gear")
                        flags_btn = Btn(btn_label, id=f"tcflag-{sid}",
                                        classes="tc-reset-btn tc-editing" if is_editing else "tc-reset-btn")
                        flags_btn.tooltip = t("te_edit_tc_flags")
                        widgets.append(flags_btn)
                    _tcsp = Static("")
                    _tcsp.styles.width = 1
                    _tcsp.styles.min_width = 1
                    widgets.append(_tcsp)
                    return Horizontal(*widgets, classes="tc-toggle-cell")

                # Two-column toggle rows with inline flag editor
                i = 0
                while i < len(installed):
                    tc1 = installed[i]
                    tc2 = installed[i + 1] if i + 1 < len(installed) else None
                    yield Horizontal(
                        _tc_cell(tc1),
                        _tc_cell(tc2) if tc2 else Static("", classes="tc-toggle-cell"),
                        classes="tc-toggle-row",
                    )
                    # Flag editor directly below this row if the editing TC is in this pair
                    edit_tc = None
                    for _candidate in (tc1, tc2):
                        if _candidate and _safe_id(_candidate.name) == editing_sid and _candidate.name in tc_names:
                            edit_tc = _candidate
                            break
                    if edit_tc:
                        tcc = tc_map.get(edit_tc.name, ToolchainConfig(name=edit_tc.name))
                        sid = editing_sid
                        def_cc = edit_tc.cc_opts or ""
                        def_cf = edit_tc.cflags or ""
                        def_ld = edit_tc.ldflags or ""
                        def_mv = edit_tc.make_vars or ""

                        def _tc_input(val: str, default: str, input_id: str) -> Input:
                            display = val if val else default
                            is_override = val != "" and val != default
                            inp = Input(value=display, id=input_id)
                            if not is_override:
                                inp.add_class("tc-default")
                            return inp

                        lbl_cc = Static("  [dim]CC_OPTS[/dim]", classes="tc-field-label")
                        lbl_cc.tooltip = "Compiler optimization flags (e.g. -O2, -ggdb)"
                        lbl_cf = Static("  [dim]EXTRA_CFLAGS[/dim]", classes="tc-field-label")
                        lbl_cf.tooltip = "Architecture-specific C flags (e.g. -mfpu=neon, -mcpu=cortex-a15)"
                        lbl_ld = Static("  [dim]EXTRA_LDFLAGS[/dim]", classes="tc-field-label")
                        lbl_ld.tooltip = "Linker flags (e.g. -Wl,--dynamic-linker=...)"
                        lbl_mv = Static("  [dim]EXTRA_MAKE[/dim]", classes="tc-field-label")
                        lbl_mv.tooltip = "Additional make variable assignments (e.g. LIB_RT= EXTRA_TARGET=-mybox)"
                        def _rst_btn(bid: str) -> "Btn":
                            b = Btn(icon("reset"), id=bid, classes="tc-reset-btn")
                            b.tooltip = t("te_load_default")
                            return b

                        # Row 1: UPX (same layout as General)
                        def_compress = b.use_compress
                        def_upx = b.upx_binary or self._upx_path or ""
                        def_complevel = b.compress_level or self.app.settings.default_compress_level
                        tc_compress_on = tcc.use_compress == "1" if tcc.use_compress else def_compress
                        tc_upx = tcc.upx_binary if tcc.upx_binary else def_upx
                        tc_complevel = tcc.compress_level if tcc.compress_level else def_complevel

                        def _sp1():
                            s = Static("")
                            s.styles.width = 1
                            s.styles.min_width = 1
                            return s

                        _tc_compress_sw = Switch(value=tc_compress_on, id=f"tc-compress-{sid}")
                        # Tooltip: show version of the configured UPX binary
                        _tc_upx_tooltip = ""
                        _tc_upx_check = tc_upx or self._upx_path
                        if _tc_upx_check:
                            try:
                                import subprocess as _sp
                                _ver = _sp.check_output([_tc_upx_check, "--version"], text=True, stderr=_sp.DEVNULL, timeout=5).splitlines()[0]
                                _tc_upx_tooltip = f"{_tc_upx_check} — {_ver}"
                            except Exception:
                                _tc_upx_tooltip = f"{_tc_upx_check}"
                        else:
                            _tc_upx_tooltip = "upx not found"
                        _tc_compress_sw.tooltip = _tc_upx_tooltip
                        if tc_compress_on:
                            _tc_upx_inp = Input(value=tc_upx, placeholder=t("ph_system_default"), id=f"tc-upx-{sid}")
                            _tc_upx_inp.styles.width = "1fr"
                            _tc_lvl_inp = Input(value=tc_complevel, placeholder=self.app.settings.default_compress_level, id=f"tc-complevel-{sid}")
                            _tc_lvl_inp.styles.max_width = 25
                            _tc_upx_browse = Btn(icon("browse"), id=f"btn-tc-upx-{sid}", classes="tc-reset-btn")
                            _tc_upx_browse.tooltip = t("ph_browse_upx")
                            if not tcc.use_compress:
                                _tc_upx_inp.add_class("tc-default")
                                _tc_lvl_inp.add_class("tc-default")
                            yield Horizontal(
                                Static("  [dim]USE_COMPRESS[/dim]", classes="tc-field-label"),
                                _tc_compress_sw,
                                Static(f"  [dim]{t('te_binary')}[/dim]", classes="src-opt-label"),
                                _tc_upx_inp,
                                _tc_upx_browse,
                                Static(f"  [dim]{t('te_compression_level')}[/dim]", classes="src-opt-label"),
                                _tc_lvl_inp,
                                _rst_btn(f"rst-compress-{sid}"),
                                _sp1(),
                                classes="tc-fields-row-first",
                            )
                        else:
                            yield Horizontal(
                                Static("  [dim]USE_COMPRESS[/dim]", classes="tc-field-label"),
                                _tc_compress_sw,
                                _rst_btn(f"rst-compress-{sid}"),
                                _sp1(),
                                classes="tc-fields-row-first",
                            )

                        # Row 2: OScam path + CONF_DIR (same layout as CFLAGS/CC_OPTS)
                        def_oscampath = b.use_oscamname or ""
                        def_confdir = b.confdir or self.app.settings.default_confdir
                        _tc_oscampath_inp = _tc_input(tcc.oscam_path, def_oscampath, f"tc-oscampath-{sid}")
                        if not _tc_oscampath_inp.value:
                            _tc_oscampath_inp.placeholder = t("ph_oscam_binary_path")
                        _tc_oscam_browse = Btn(icon("browse"), id=f"btn-tc-oscam-{sid}", classes="tc-reset-btn")
                        _tc_oscam_browse.tooltip = t("ph_browse_file")
                        _tc_confdir_inp = _tc_input(tcc.confdir, def_confdir, f"tc-confdir-{sid}")
                        lbl_oscam = Static(f"  [dim]{t('te_oscam_binary_path')}[/dim]", classes="tc-field-label")
                        lbl_confdir = Static(f"  [dim]{t('te_config_directory')}[/dim]", classes="tc-field-label")
                        with Grid(classes="tc-fields-grid"):
                            yield Horizontal(
                                lbl_oscam,
                                _tc_oscampath_inp,
                                _tc_oscam_browse,
                                _rst_btn(f"rst-oscampath-{sid}"),
                                _sp1(),
                                classes="s-row",
                            )
                            yield Horizontal(
                                lbl_confdir,
                                _tc_confdir_inp,
                                _rst_btn(f"rst-confdir-{sid}"),
                                _sp1(),
                                classes="s-row",
                            )

                        with Grid(classes="tc-fields-grid"):
                            yield Horizontal(
                                lbl_cf,
                                _tc_input(tcc.cflags, def_cf, f"tc-cf-{sid}"),
                                _rst_btn(f"rst-cf-{sid}"),
                                _sp1(),
                                classes="s-row",
                            )
                            yield Horizontal(
                                lbl_cc,
                                _tc_input(tcc.cc_opts, def_cc, f"tc-cc-{sid}"),
                                _rst_btn(f"rst-cc-{sid}"),
                                _sp1(),
                                classes="s-row",
                            )

                        with Grid(classes="tc-fields-grid-last"):
                            yield Horizontal(
                                lbl_ld,
                                _tc_input(tcc.ldflags, def_ld, f"tc-ld-{sid}"),
                                _rst_btn(f"rst-ld-{sid}"),
                                _sp1(),
                                classes="s-row",
                            )
                            yield Horizontal(
                                lbl_mv,
                                _tc_input(tcc.make_vars, def_mv, f"tc-mv-{sid}"),
                                _rst_btn(f"rst-mv-{sid}"),
                                _sp1(),
                                classes="s-row",
                            )

                        self._tc_defaults[sid] = {
                            "cc": def_cc, "cf": def_cf, "ld": def_ld, "mv": def_mv,
                            "compress": "1" if def_compress else "0",
                            "upx": def_upx, "complevel": def_complevel,
                            "oscampath": def_oscampath, "confdir": def_confdir,
                        }
                    i += 2

                if not installed:
                    yield Static(f"  [dim]{t('te_no_tc_hint')}[/dim]")

            # Library and Hardware flags (moved from General tab)
            if has_source:
                mk_flags = self._load_makefile_flags()
                mk_use = mk_flags.get("use", [])
                mk_use_set = set(mk_use)
                has_dual_backend = "MBEDTLS" in mk_use and "OPENSSL" in mk_use

                _STATIC_CAPABLE = {"LIBCRYPTO", "SSL", "LIBUSB", "PCSC", "LIBDVBCSA"}

                lib_flags = [f for f in mk_use if f not in self._HARDWARE_FLAGS and f not in ("MBEDTLS", "OPENSSL")]
                hw_flags = [f for f in mk_use if f in self._HARDWARE_FLAGS]

                # Detect stale USE_* flags (enabled in target but no longer
                # provided by any assigned repo) — shown in red like Options.
                stale_lib = sorted(
                    f for f, v in b.use_flags.items()
                    if v and f not in mk_use_set
                    and f not in self._HARDWARE_FLAGS
                )
                stale_hw = sorted(
                    f for f, v in b.use_flags.items()
                    if v and f not in mk_use_set
                    and f in self._HARDWARE_FLAGS
                )
                stale_hw_set = set(stale_hw)
                stale_static = sorted(
                    f for f, v in b.static_flags.items()
                    if v and f not in mk_use_set
                )

                def _flag_label(name: str, stale: bool = False) -> str:
                    if stale:
                        return f" [red strike]{name}[/red strike]"
                    return f" {name}"

                # Merge normal + stale lib flags, sorted together.
                # Always exclude MBEDTLS/OPENSSL from the main loop — they
                # are rendered as a special pair row at the top (normal or stale).
                stale_lib_set = set(stale_lib)
                stale_static_set = set(stale_static)
                all_lib_flags = sorted((set(lib_flags) | stale_lib_set) - {"MBEDTLS", "OPENSSL"})

                _lib_active = sum(1 for f in (set(lib_flags) | stale_lib_set | {"MBEDTLS", "OPENSSL"}) if b.use_flags.get(f, False))
                with Collapsible(title=f"{t('section_library_flags')} ({_lib_active} {t('te_selected')})", collapsed=True, id="col-lib-flags"):
                    # MBEDTLS / OPENSSL pair — always at top, normal or stale.
                    # They always appear together as a pair.
                    stale_dual = "MBEDTLS" in stale_lib_set or "OPENSSL" in stale_lib_set
                    if has_dual_backend or stale_dual:
                        openssl_on = b.use_flags.get("OPENSSL", False)
                        mbedtls_on = b.use_flags.get("MBEDTLS", not openssl_on if has_dual_backend else False)
                        _msw = Switch(value=mbedtls_on, id="f-use-MBEDTLS")
                        _msw.tooltip = oscam.use_flag_descriptions.get("MBEDTLS", "")
                        _osw = Switch(value=openssl_on, id="f-use-OPENSSL")
                        _osw.tooltip = oscam.use_flag_descriptions.get("OPENSSL", "")
                        yield Horizontal(
                            Static(_flag_label("USE_MBEDTLS", stale=stale_dual), classes="flag-label"), _msw,
                            Static(_flag_label("USE_OPENSSL", stale=stale_dual), classes="flag-label"), _osw,
                            classes="flag-row",
                        )
                    for flag in all_lib_flags:
                        is_stale = flag in stale_lib_set
                        _sw = Switch(value=b.use_flags.get(flag, False), id=f"f-use-{flag}")
                        _sw.tooltip = oscam.use_flag_descriptions.get(flag, "")
                        widgets = [
                            Static(_flag_label(f"USE_{flag}", stale=is_stale), classes="flag-label"),
                            _sw,
                        ]
                        if flag in _STATIC_CAPABLE:
                            s_stale = is_stale or flag in stale_static_set
                            _ssw = Switch(value=b.static_flags.get(flag, False), id=f"f-static-{flag}")
                            _ssw.tooltip = f"Link {flag} statically into the binary."
                            widgets += [
                                Static(_flag_label(f"STATIC_{flag}", stale=s_stale), classes="flag-label"),
                                _ssw,
                            ]
                        yield Horizontal(*widgets, classes="flag-row")

                _hw_active = sum(1 for f in (set(hw_flags) | stale_hw_set) if b.use_flags.get(f, False))
                with Collapsible(title=f"{t('section_hardware_flags')} ({_hw_active} {t('te_selected')})", collapsed=True, id="col-hw-flags"):
                    if "STAPI" in hw_flags or "STAPI5" in hw_flags:
                        _stapi_widgets = []
                        if "STAPI" in hw_flags:
                            _ssw = Switch(value=b.use_flags.get("STAPI", False), id="f-use-STAPI")
                            _ssw.tooltip = oscam.use_flag_descriptions.get("STAPI", "")
                            _stapi_widgets += [
                                Static(" USE_STAPI", classes="flag-label"),
                                _ssw,
                            ]
                        if "STAPI5" in hw_flags:
                            _stapi5_sw = Switch(value=b.use_flags.get("STAPI5", False), id="f-use-STAPI5")
                            _stapi5_sw.tooltip = oscam.use_flag_descriptions.get("STAPI5", "")
                            _stapi_widgets += [
                                Static(" USE_STAPI5", classes="flag-label"),
                                _stapi5_sw,
                            ]
                        if b.use_flags.get("STAPI") or b.use_flags.get("STAPI5"):
                            _stapi_default = ""
                            for _tc in installed:
                                if _tc.name in tc_names:
                                    _stapi_default = _tc.stapi_lib or ""
                                    if _stapi_default:
                                        break
                            _stapi_val = b.stapi_lib if b.stapi_lib else _stapi_default
                            _stapi_is_default = (_stapi_val == _stapi_default)
                            from textual.widgets import Button as _Btn
                            _stapi_inp = Input(value=_stapi_val, placeholder=t("ph_stapi_lib"), id="f-stapi-lib")
                            _stapi_inp.styles.max_width = 45
                            if _stapi_is_default:
                                _stapi_inp.add_class("tc-default")
                            _stapi_lbl = Static(" STAPI_LIB", classes="tc-field-label")
                            _stapi_lbl.tooltip = t("tt_stapi_lib")
                            _stapi_brw = _Btn(icon("browse"), id="brw-stapi-lib", classes="tc-reset-btn")
                            _stapi_brw.tooltip = t("tt_stapi_lib_browse")
                            _stapi_rst = _Btn(icon("reset"), id="rst-stapi-lib", classes="tc-reset-btn")
                            _stapi_rst.tooltip = t("te_load_default")
                            self._stapi_default = _stapi_default
                            _stapi_widgets += [_stapi_lbl, _stapi_inp, _stapi_brw, _stapi_rst]
                        yield Horizontal(*_stapi_widgets, classes="flag-row")

                    # Merge normal + stale hw flags (excl. STAPI/STAPI5), sorted
                    stale_hw_set = set(stale_hw)
                    other_hw = sorted(
                        set(f for f in hw_flags if f not in ("STAPI", "STAPI5"))
                        | set(f for f in stale_hw if f not in ("STAPI", "STAPI5"))
                    )
                    for i in range(0, len(other_hw), 2):
                        widgets = []
                        for f in other_hw[i:i+2]:
                            is_stale = f in stale_hw_set
                            _hwsw = Switch(value=b.use_flags.get(f, False), id=f"f-use-{f}")
                            _hwsw.tooltip = oscam.use_flag_descriptions.get(f, "")
                            widgets += [
                                Static(_flag_label(f"USE_{f}", stale=is_stale), classes="flag-label"),
                                _hwsw,
                            ]
                        yield Horizontal(*widgets, classes="flag-row")
            else:
                yield Static(f"  [dim]{t('te_select_source_flags')}[/dim]")

        # === Tab 3: Options (oscam modules) ===
        # Merge target config with oscam options:
        # - Untouched new target → seed with oscam defaults exactly once
        # - Existing target / touched new target → trust target.yaml
        # - Removed options: shown strike-through in red so the user can
        #                    explicitly drop them via the Reset button
        is_new = not self._options_seeded
        # Carry sticky stale (those the user has not yet Reset away).
        self._removed_opts = {k: list(v) for k, v in self._sticky_stale.items()}

        def _merge(saved: list[str], available: list[str], defaults: list[str], category: str) -> dict[str, bool]:
            """Merge saved target config with available oscam options.

            New target → use oscam defaults.
            Existing target → trust the saved list verbatim. Options that
            were deselected MUST stay off, even if they happen to be in
            oscam's default_enabled list. Options that were never seen
            before (e.g. newly added in oscam) default to off; the user
            can enable them explicitly.
            """
            result = {}
            for opt in available:
                if is_new:
                    result[opt] = opt in defaults
                else:
                    result[opt] = opt in saved
            # Detect removed options. A name counts as stale if either
            # (a) it is in the saved list but not in the current repo,
            # or (b) the previous compose already flagged it via
            # _sticky_stale. Sticky entries survive toggle-all so they
            # only disappear via Reset.
            removed_set: set[str] = set(self._removed_opts.get(category, []))
            for opt in saved:
                if opt not in available and available:
                    removed_set.add(opt)
            if removed_set:
                self._removed_opts[category] = sorted(removed_set)
            # Render stale options with their actual on/off state from
            # the saved list — same treatment as available items.
            for stale in self._removed_opts.get(category, []):
                result[stale] = stale in saved
            return result

        merged_addons = _merge(b.modules, oscam.addons, oscam.default_enabled.get("addons", []), "addons")
        merged_protos = _merge(b.protocols, oscam.protocols, oscam.default_enabled.get("protocols", []), "protocols")
        merged_readers = _merge(b.readers, oscam.readers, oscam.default_enabled.get("readers", []), "readers")
        merged_crs = _merge(b.card_readers, oscam.card_readers, oscam.default_enabled.get("card_readers", []), "card_readers")

        def _opt_widget(name: str, merged: dict, removed_set: set, pos: int = 0):
            # Banding pairs the row class to the grid column count
            # (.mod-grid is 4 columns, so flip every 4 cells).
            row_class = "row-even" if (pos // 4) % 2 == 0 else "row-odd"
            return _switch_compact(
                name,
                merged.get(name, False),
                f"m-{name}",
                removed=name in removed_set,
                row_class=row_class,
                tooltip=oscam.descriptions.get(name, ""),
            )

        def _section_toggle(category: str, items: list[str], merged: dict, section_label: str):
            """A first-row Switch that toggles every option in the
            collapsible. Default state mirrors the live selection: ON
            if at least one option is enabled, OFF if everything is
            off. Sits in the top-left grid cell so it aligns with the
            real options below. The label echoes the action so the
            user does not need a hover tooltip.
            """
            any_on = any(merged.get(name, False) for name in items)
            all_on = bool(items) and all(merged.get(name, False) for name in items)
            classes = "section-switch all-on" if all_on else "section-switch"
            sw = Switch(value=any_on, id=f"sec-{category}", classes=classes)
            label = Static(
                f" [b]{t('te_section_toggle_tip', section=section_label)}[/b]",
                classes="sc-label",
            )
            cell = Horizontal(sw, label, classes="s-compact")
            return Grid(cell, classes="mod-grid")

        with VerticalScroll(id="te-panel-oscam"):
            addons_sorted = sorted(oscam.addons)
            removed_addons = sorted(self._removed_opts.get("addons", []))
            all_addons = addons_sorted + removed_addons
            addon_count = sum(1 for m in all_addons if merged_addons.get(m) or m in removed_addons)
            with Collapsible(title=f"Addons ({addon_count}/{len(addons_sorted)})", collapsed=False, id="col-addons"):
                if oscam.addons or removed_addons:
                    yield _section_toggle("addons", all_addons, merged_addons, "Addons")
                    with Grid(classes="mod-grid"):
                        for _i, mod in enumerate(_column_major(all_addons)):
                            if mod is None:
                                yield Static("", classes="s-compact")
                            else:
                                yield _opt_widget(mod, merged_addons, set(removed_addons), pos=_i)
                else:
                    yield Static(f"  [dim]{t('te_select_source_first')}[/dim]")

            protos_sorted = sorted(oscam.protocols)
            removed_protos = sorted(self._removed_opts.get("protocols", []))
            all_protos = protos_sorted + removed_protos
            proto_count = sum(1 for m in all_protos if merged_protos.get(m) or m in removed_protos)
            with Collapsible(title=f"Protocols ({proto_count}/{len(all_protos)})", collapsed=False, id="col-protocols"):
                if all_protos:
                    yield _section_toggle("protocols", all_protos, merged_protos, "Protocols")
                with Grid(classes="mod-grid"):
                    for _i, mod in enumerate(_column_major(all_protos)):
                        if mod is None:
                            yield Static("", classes="s-compact")
                        else:
                            yield _opt_widget(mod, merged_protos, set(removed_protos), pos=_i)

            readers_sorted = sorted(oscam.readers)
            removed_readers = sorted(self._removed_opts.get("readers", []))
            all_readers = readers_sorted + removed_readers
            reader_count = sum(1 for m in all_readers if merged_readers.get(m) or m in removed_readers)
            with Collapsible(title=f"Readers ({reader_count}/{len(all_readers)})", collapsed=False, id="col-readers"):
                if all_readers:
                    yield _section_toggle("readers", all_readers, merged_readers, "Readers")
                with Grid(classes="mod-grid"):
                    for _i, mod in enumerate(_column_major(all_readers)):
                        if mod is None:
                            yield Static("", classes="s-compact")
                        else:
                            yield _opt_widget(mod, merged_readers, set(removed_readers), pos=_i)

            crs_sorted = sorted(oscam.card_readers)
            removed_crs = sorted(self._removed_opts.get("card_readers", []))
            all_crs = crs_sorted + removed_crs
            cr_count = sum(1 for m in all_crs if merged_crs.get(m) or m in removed_crs)
            with Collapsible(title=f"Card Readers ({cr_count}/{len(all_crs)})", collapsed=False, id="col-card-readers"):
                if all_crs:
                    yield _section_toggle("card_readers", all_crs, merged_crs, "Card Readers")
                with Grid(classes="mod-grid"):
                    for _i, mod in enumerate(_column_major(all_crs)):
                        if mod is None:
                            yield Static("", classes="s-compact")
                        else:
                            yield _opt_widget(mod, merged_crs, set(removed_crs), pos=_i)

            # The reset action lives on F11 in the footer (only when
            # the Options tab is active) — see do_remove.

        # === Tab 3: Deployment ===
        # Build source/toolchain options for dropdowns
        _has_sources = bool(tgt.sources)
        _has_toolchains = bool(tgt.toolchains)
        src_options = [(s.name, s.name) for s in tgt.sources] or [("(none)", "(none)")]
        tc_options = [(tc.name, tc.name) for tc in tgt.toolchains] or [("(none)", "(none)")]

        with VerticalScroll(id="te-panel-deploy"):
            # Ensure at least one deployment entry, pre-filled with first source/toolchain
            if not tgt.deployment:
                default_src = tgt.sources[0].name if tgt.sources else ""
                default_tc = tgt.toolchains[0].name if tgt.toolchains else ""
                tgt.deployment.append(DeploymentConfig(source=default_src, toolchain=default_tc))
            deploy_count = len([u for u in tgt.deployment if u.host])
            for idx, up in enumerate(tgt.deployment):
                up_name = up.name or f"#{idx + 1}"
                up_title = f"Deploy {up_name}" if up.enabled else f"Deploy {up_name} (disabled)"
                with Collapsible(title=up_title, collapsed=idx > 0, id=f"col-dep-{idx}"):
                    _src_val = up.source if up.source and any(v == up.source for _, v in src_options) else (src_options[0][1] if src_options else "")
                    _tc_val = up.toolchain if up.toolchain and any(v == up.toolchain for _, v in tc_options) else (tc_options[0][1] if tc_options else "")
                    _src_sel = Select(options=src_options, value=_src_val, id=f"t-dep-src-{idx}", allow_blank=False)
                    _src_sel.styles.width = "1fr"
                    _tc_sel = Select(options=tc_options, value=_tc_val, id=f"t-dep-tc-{idx}", allow_blank=False)
                    _tc_sel.styles.width = "1fr"
                    _port_inp = Input(value=str(up.port), id=f"t-dep-port-{idx}", type="integer", max_length=5)
                    _port_inp.styles.width = 12
                    _port_inp.styles.min_width = 12
                    _port_inp.styles.max_width = 12
                    _pass_inp = Input(value=up.password, placeholder=t("ph_password"), id=f"t-dep-pass-{idx}", password=True)
                    _key_browse = Button(icon("browse"), id=f"btn-dep-key-{idx}", classes="tc-reset-btn")
                    _key_browse.tooltip = t("ph_browse_file")
                    def _sp():
                        s = Static("")
                        s.styles.width = 1
                        s.styles.min_width = 1
                        return s

                    with Grid(classes="up-grid"):
                        # Row 1 left: Checkbox + Name
                        _cb = Checkbox("", value=up.enabled, id=f"t-dep-enabled-{idx}")
                        _cb.styles.width = 5
                        _cb.styles.min_width = 5
                        _cb.styles.max_width = 5
                        _cb.styles.height = 3
                        _cb.styles.padding = (0, 0)
                        _cb.tooltip = t("te_deploy_active") if up.enabled else t("te_deploy_disabled")
                        yield Horizontal(
                            Static(f" {t('te_enabled_name')}", classes="s-label-tall"),
                            _cb,
                            Input(value=up.name, placeholder=f"#{idx + 1}", id=f"t-dep-name-{idx}"),
                            classes="s-row",
                        )
                        # Row 1 right: Repository / Toolchain
                        yield Horizontal(
                            Static(f" {t('te_source')} / {t('te_toolchain')}", classes="s-label-tall"),
                            _src_sel,
                            _tc_sel,
                            _sp(), _sp(),
                            classes="s-row",
                        )
                        # Row 2 left: Host / Port
                        yield Horizontal(
                            Static(f" {t('te_host')} / {t('te_port')}", classes="s-label-tall"),
                            Input(value=up.host, placeholder=t("ph_host"), id=f"t-dep-host-{idx}"),
                            _port_inp,
                            classes="s-row",
                        )
                        # Row 2 right: User / Password + eye toggle
                        _eye_btn = Button(icon("eye"), id=f"btn-dep-eye-{idx}", classes="tc-reset-btn")
                        _eye_btn.tooltip = t("tt_show_password")
                        yield Horizontal(
                            Static(f" {t('te_user')} / {t('te_password')}(phrase)", classes="s-label-tall"),
                            Input(value=up.user, id=f"t-dep-user-{idx}"),
                            _pass_inp,
                            _eye_btn,
                            _sp(), _sp(), _sp(),
                            classes="s-row",
                        )
                        # Row 3 left: SCP mode selector (previously empty).
                        # Width mirrors the pre/post command input so the
                        # row aligns with the other input-wide fields.
                        _scp_mode_val = up.scp_mode if up.scp_mode in ("last_working", "automatic") else "last_working"
                        _scp_sel = Select(
                            options=[
                                (t("te_scp_mode_last_working"), "last_working"),
                                (t("te_scp_mode_automatic"), "automatic"),
                            ],
                            value=_scp_mode_val,
                            id=f"t-dep-scpmode-{idx}",
                            allow_blank=False,
                        )
                        _scp_sel.tooltip = t("tt_scp_mode")
                        _scp_sel.styles.width = "1fr"
                        yield Horizontal(
                            Static(f" {t('te_scp_mode')}", classes="s-label-tall"),
                            _scp_sel,
                            classes="s-row",
                        )
                        # Row 3 right: SSH Key + browse
                        yield Horizontal(
                            Static(f" {t('te_ssh_key')}", classes="s-label-tall"),
                            Input(value=up.key, placeholder=t("ph_ssh_key"), id=f"t-dep-key-{idx}"),
                            _key_browse,
                            _sp(), _sp(), _sp(),
                            classes="s-row",
                        )
                        # Row 4 left: Backup checkbox + Target path + browse
                        _path_browse = Button(icon("browse"), id=f"btn-dep-browse-path-{idx}", classes="tc-reset-btn")
                        _path_browse.tooltip = t("rfp_browse_remote")
                        _bk_cb = Checkbox(
                            "", value=up.target_backup, id=f"t-dep-backup-{idx}",
                        )
                        _bk_cb.styles.width = 5
                        _bk_cb.styles.min_width = 5
                        _bk_cb.styles.max_width = 5
                        _bk_cb.styles.height = 3
                        _bk_cb.styles.padding = (0, 0)
                        _bk_cb.tooltip = t("tt_dep_target_backup")
                        yield Horizontal(
                            Static(f" {t('te_target_path')}", classes="s-label-tall"),
                            _bk_cb,
                            Input(value=up.target_path, id=f"t-dep-path-{idx}"),
                            _path_browse,
                            _sp(),
                            classes="s-row",
                        )
                        # Row 4 right: SU password + eye toggle
                        _su_eye_btn = Button(icon("eye"), id=f"btn-dep-su-eye-{idx}", classes="tc-reset-btn")
                        _su_eye_btn.tooltip = t("tt_show_password")
                        _su_pass_inp = Input(value=up.su_password, placeholder=t("ph_su_password"), id=f"t-dep-su-pass-{idx}", password=True)
                        yield Horizontal(
                            Static(f" {t('te_su_password')}", classes="s-label-tall"),
                            _su_pass_inp,
                            _su_eye_btn,
                            _sp(), _sp(), _sp(),
                            classes="s-row",
                        )
                        # Row 5 left: Pre command
                        _pre_inp = Input(value=up.pre_cmd, placeholder=t("ph_pre_cmd"), id=f"t-dep-pre-{idx}")
                        _pre_inp.tooltip = t("tt_pre_cmd")
                        yield Horizontal(
                            Static(f" {t('te_pre_command')}", classes="s-label-tall"),
                            _pre_inp,
                            classes="s-row",
                        )
                        # Row 5 right: Post command
                        _post_inp = Input(value=up.post_cmd, placeholder=t("ph_post_cmd"), id=f"t-dep-post-{idx}")
                        _post_inp.tooltip = t("tt_post_cmd")
                        yield Horizontal(
                            Static(f" {t('te_post_command')}", classes="s-label-tall"),
                            _post_inp,
                            _sp(), _sp(),
                            classes="s-row",
                        )

        yield S4Footer(active_screen="target_edit")

    def on_mount(self) -> None:
        self._show_tab(self._active_tab)
        # Restore collapsible states from previous screen instance
        for cid, collapsed in TargetEditScreen._collapsible_states.items():
            try:
                self.query_one(f"#{cid}", Collapsible).collapsed = collapsed
            except Exception:
                pass
        if self._focus_id:
            self.set_timer(0.1, self._restore_focus)
        else:
            self.set_timer(0.1, self._focus_common)

    def _focus_common(self) -> None:
        """Set focus to the Common collapsible header so the user can
        immediately collapse/expand it with Enter/Space without typing
        anything into the Name field by accident.
        """
        from textual.widgets._collapsible import CollapsibleTitle
        col = None
        try:
            col = self.query_one("#col-common", Collapsible)
        except Exception:
            pass
        if col is not None:
            for child in col.query(CollapsibleTitle):
                try:
                    child.focus()
                    return
                except Exception:
                    break
        try:
            self.query_one("#t-name", Input).focus()
        except Exception:
            pass

    def _restore_focus(self) -> None:
        try:
            self.query_one(f"#{self._focus_id}", Switch).focus()
        except Exception:
            pass

    def _save_collapsible_states(self) -> None:
        """Save current collapsible states before screen refresh."""
        for col in self.query(Collapsible):
            if col.id:
                TargetEditScreen._collapsible_states[col.id] = col.collapsed

    def _refresh_screen(self, target: Target, active_tab: str, focus_id: str = "", clear_stale: bool = False) -> None:
        """Save state and refresh the screen.

        The new screen is always considered seeded — anything the user
        touched between the previous compose and this refresh is in
        `target` already, so we must NOT re-apply oscam defaults over
        it. Sticky stale entries are forwarded so they survive
        all-on/all-off until the user explicitly Resets the category.
        """
        self._save_collapsible_states()
        sticky = {} if clear_stale else {k: list(v) for k, v in self._removed_opts.items()}
        self.app.pop_screen()
        self.app.push_screen(TargetEditScreen(
            target=target,
            save_path=self.save_path,
            active_tab=active_tab,
            focus_id=focus_id,
            options_seeded=True,
            sticky_stale=sticky,
        ))

    def on_click(self, event) -> None:
        widget = event.widget if hasattr(event, 'widget') else None
        if widget:
            if widget.id == "te-tab-general":
                self._show_tab("general")
            elif widget.id == "te-tab-source":
                self._show_tab("source")
            elif widget.id == "te-tab-oscam":
                self._show_tab("oscam")
            elif widget.id == "te-tab-deploy":
                self._show_tab("deploy")

    _suppress_section_sync: bool = False

    def on_checkbox_changed(self, event) -> None:
        """Deployment enabled checkbox → update Collapsible title + tooltip."""
        cid = event.checkbox.id or ""
        if cid.startswith("t-dep-enabled-"):
            try:
                idx = cid.split("-")[-1]
                name_inp = self.query_one(f"#t-dep-name-{idx}", Input)
                name = name_inp.value.strip() or f"#{int(idx) + 1}"
                title = f"Deploy {name}" if event.value else f"Deploy {name} (disabled)"
                self.query_one(f"#col-dep-{idx}", Collapsible).title = title
                event.checkbox.tooltip = t("te_deploy_active") if event.value else t("te_deploy_disabled")
            except Exception:
                pass

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """When a source or toolchain switch changes, refresh to update UI."""
        sid = event.switch.id or ""
        # Section toggle (sec-addons / sec-protocols / sec-readers /
        # sec-card_readers) → flip every option in the matching
        # collapsible to the new value, then refresh. Programmatic
        # syncs from the option-side handler suppress themselves.
        if sid.startswith("sec-"):
            if self._suppress_section_sync:
                return
            self._do_section_toggle(sid[len("sec-"):], event.value)
            return
        # Individual option (m-*) toggled → keep the matching section
        # switch in sync. ON if any option in the category is on.
        if sid.startswith("m-"):
            self._sync_section_for_option(sid[len("m-"):])
            return

        is_source = sid.startswith("src-") and not sid.startswith("src-upd-") and not sid.startswith("src-patch-")
        is_patch_toggle = sid.startswith("src-patch-")
        _tc_field_prefixes = ("tc-cc-", "tc-cf-", "tc-ld-", "tc-mv-", "tc-compress-", "tc-upx-", "tc-complevel-", "tc-oscampath-", "tc-confdir-")
        is_toolchain = sid.startswith("tc-") and not any(sid.startswith(p) for p in _tc_field_prefixes)
        if is_source or is_patch_toggle:
            self._refresh_screen(self._collect(), "general", sid)
            return
        if is_toolchain:
            self._refresh_screen(self._collect(), "source", sid)
            return

        # Dynamic USE ↔ STATIC dependencies
        if sid.startswith("f-use-") and not event.value:
            # USE deactivated → also deactivate matching STATIC
            flag = sid.replace("f-use-", "")
            try:
                self.query_one(f"#f-static-{flag}", Switch).value = False
            except Exception:
                pass
        if sid.startswith("f-static-") and event.value:
            # STATIC activated → also activate matching USE
            flag = sid.replace("f-static-", "")
            try:
                self.query_one(f"#f-use-{flag}", Switch).value = True
            except Exception:
                pass

        # Exclusive MBEDTLS ↔ OPENSSL toggle
        if sid == "f-use-OPENSSL" and event.value:
            try:
                self.query_one("#f-use-MBEDTLS", Switch).value = False
                # USE_OPENSSL requires USE_LIBCRYPTO
                self.query_one("#f-use-LIBCRYPTO", Switch).value = True
            except Exception:
                pass
        if sid == "f-use-MBEDTLS" and event.value:
            try:
                self.query_one("#f-use-OPENSSL", Switch).value = False
            except Exception:
                pass

        # Update Library/Hardware Flags collapsible titles with active count
        if sid.startswith("f-use-") or sid.startswith("f-static-"):
            self._update_flag_counts()

        # STAPI toggle — refresh to show/hide stapi_lib field
        if sid in ("f-use-STAPI", "f-use-STAPI5"):
            self._refresh_screen(self._collect(), "source", sid)
            return

        # UPX toggle — refresh to show/hide compress level field
        if sid == "f-use-compress":
            self._refresh_screen(self._collect(), "general", sid)
            return
        # Per-TC UPX toggle — refresh to show/hide per-TC compress fields
        if sid.startswith("tc-compress-"):
            self._refresh_screen(self._collect(), "source", sid)
            return

    def on_button_pressed(self, event) -> None:
        if event.button.id == "btn-upx-browse":
            self._open_upx_picker()
        elif event.button.id and event.button.id.startswith("btn-tc-upx-"):
            _tc_sid = event.button.id.split("btn-tc-upx-", 1)[1]
            self._open_upx_picker(target_input_id=f"tc-upx-{_tc_sid}")
        elif event.button.id and event.button.id.startswith("btn-tc-oscam-"):
            _tc_sid = event.button.id.split("btn-tc-oscam-", 1)[1]
            self._open_binary_dir_picker(target_input_id=f"tc-oscampath-{_tc_sid}")
        elif event.button.id == "btn-binary-browse":
            self._open_binary_dir_picker()
        elif event.button.id == "brw-stapi-lib":
            self._open_stapi_picker()
        elif event.button.id == "rst-stapi-lib":
            try:
                inp = self.query_one("#f-stapi-lib", Input)
                inp.value = getattr(self, '_stapi_default', '')
                inp.add_class("tc-default")
            except Exception:
                pass
        elif event.button.id and event.button.id.startswith("tcflag-"):
            # Toggle flag editor for this toolchain (only one at a time)
            sid = event.button.id[len("tcflag-"):]
            if TargetEditScreen._tc_flags_editing == sid:
                TargetEditScreen._tc_flags_editing = ""
            else:
                TargetEditScreen._tc_flags_editing = sid
            self._refresh_screen(self._collect(), "source", event.button.id)
        elif event.button.id and event.button.id.startswith("btn-dep-eye-"):
            idx = event.button.id.split("-")[-1]
            try:
                pw = self.query_one(f"#t-dep-pass-{idx}", Input)
                pw.password = not pw.password
            except Exception:
                pass
            return
        elif event.button.id and event.button.id.startswith("btn-dep-su-eye-"):
            idx = event.button.id.split("-")[-1]
            try:
                pw = self.query_one(f"#t-dep-su-pass-{idx}", Input)
                pw.password = not pw.password
            except Exception:
                pass
            return
        elif event.button.id and event.button.id.startswith("btn-dep-key-"):
            idx = event.button.id.split("-")[-1]
            self._open_deploy_key_picker(idx)
        elif event.button.id and event.button.id.startswith("btn-dep-browse-path-"):
            idx = event.button.id.split("-")[-1]
            self._open_remote_path_picker(idx)
        elif event.button.id and event.button.id.startswith("rst-"):
            self._do_reset_field(event.button.id)

    def _open_deploy_key_picker(self, idx: str) -> None:
        """Open file picker for deployment SSH key."""
        from ui.screens.toolchains import FilePickerScreen
        from pathlib import Path as _P
        ssh_dir = _P.home() / ".ssh"
        start = str(ssh_dir) if ssh_dir.exists() else str(_P.home())
        self._deploy_key_idx = idx
        self.app.push_screen(
            FilePickerScreen(start_path=start, title=t("te_ssh_key"), simple=True, select_label=t("select_btn")),
            callback=self._on_deploy_key_selected,
        )

    def _on_deploy_key_selected(self, result) -> None:
        if not result:
            return
        path = result[0] if isinstance(result, tuple) else result
        idx = getattr(self, '_deploy_key_idx', '')
        if path and idx:
            try:
                self.query_one(f"#t-dep-key-{idx}", Input).value = str(path)
            except Exception:
                pass

    def _open_remote_path_picker(self, idx: str) -> None:
        """Open remote file picker for deployment target path."""
        try:
            host = self.query_one(f"#t-dep-host-{idx}", Input).value.strip()
        except Exception:
            host = ""
        if not host:
            self.notify(t("te_no_host"), severity="warning")
            return
        # Localhost → local file picker
        if host in ("localhost", "127.0.0.1", "::1"):
            from ui.screens.toolchains import FilePickerScreen
            current = self.query_one(f"#t-dep-path-{idx}", Input).value.strip()
            start = str(Path(current).parent) if current else "/usr/bin"
            self._deploy_path_idx = idx
            self.app.push_screen(
                FilePickerScreen(start_path=start, title=f"Local: {host}",
                                 simple=True, select_label=t("rfp_select")),
                callback=self._on_remote_path_selected,
            )
            return
        # Remote → SSH file picker
        from ui.screens.remote_file_picker import RemoteFilePickerScreen
        from core.credentials import decrypt
        try:
            _ds = self.app.settings
            user = self.query_one(f"#t-dep-user-{idx}", Input).value.strip() or _ds.default_deploy_user
            password = decrypt(self.query_one(f"#t-dep-pass-{idx}", Input).value.strip())
            port = int(self.query_one(f"#t-dep-port-{idx}", Input).value.strip() or str(_ds.default_deploy_port))
            key = self.query_one(f"#t-dep-key-{idx}", Input).value.strip()
        except Exception:
            _ds = self.app.settings
            user, password, port, key = _ds.default_deploy_user, "", _ds.default_deploy_port, ""
        current = self.query_one(f"#t-dep-path-{idx}", Input).value.strip()
        self._deploy_path_idx = idx
        self.app.push_screen(
            RemoteFilePickerScreen(
                host=host, user=user, password=password,
                port=port, key=key,
                start_path="/usr/bin",
                current_value=current,
            ),
            callback=self._on_remote_path_selected,
        )

    def _on_remote_path_selected(self, result) -> None:
        """Callback for remote/local path picker."""
        if not result:
            return
        path = result[0] if isinstance(result, tuple) else result
        idx = getattr(self, '_deploy_path_idx', '')
        if path and idx:
            try:
                self.query_one(f"#t-dep-path-{idx}", Input).value = str(path)
            except Exception:
                pass

    def _add_deployment_entry(self) -> None:
        """Add a new deployment entry, copying the last one as template."""
        target = self._collect()
        default_src = target.sources[0].name if target.sources else ""
        default_tc = target.toolchains[0].name if target.toolchains else ""
        new_idx = len(target.deployment) + 1
        if target.deployment:
            last = target.deployment[-1]
            new = DeploymentConfig(
                name=f"#{new_idx}",
                source=last.source or default_src,
                toolchain=last.toolchain or default_tc,
                host=last.host, user=last.user, password=last.password,
                port=last.port, key=last.key, target_path=last.target_path,
                pre_cmd=last.pre_cmd, post_cmd=last.post_cmd,
            )
        else:
            new = DeploymentConfig(name=f"#{new_idx}", source=default_src, toolchain=default_tc)
        target.deployment.append(new)
        self._refresh_screen(target, "deploy")

    def _delete_deployment_entry(self, idx: int) -> None:
        """Delete deployment entry at index (keep at least one)."""
        target = self._collect()
        if len(target.deployment) > 1 and 0 <= idx < len(target.deployment):
            target.deployment.pop(idx)
            self._refresh_screen(target, "deploy")

    def _delete_last_deployment_entry(self) -> None:
        """Delete the last deployment entry (F11). Keep at least one."""
        target = self._collect()
        if len(target.deployment) > 1:
            target.deployment.pop()
            self._refresh_screen(target, "deploy")
        else:
            # Clear the single remaining entry instead of deleting
            default_src = target.sources[0].name if target.sources else ""
            default_tc = target.toolchains[0].name if target.toolchains else ""
            target.deployment = [DeploymentConfig(source=default_src, toolchain=default_tc)]
            self._refresh_screen(target, "deploy")

    # ---------------------------------------------------- oscam binary path
    def _normalize_binary_path(self, raw: str) -> tuple[str, str | None]:
        """Normalize the OScam binary path field.

        Returns (normalized, error). If error is not None, the input
        should be rejected and the error shown to the user.

        - empty                          → ""
        - bare filename (no '/')         → "data/binaries/<name>"
        - path ending with '/'           → directory (kept verbatim, ~ expanded)
        - path with name (no trailing /) → kept verbatim (~ expanded);
                                           rejected if it points to an
                                           existing directory
        """
        import os
        v = (raw or "").strip()
        if not v:
            return "", None
        # Expand ~
        v = os.path.expanduser(v)
        # Bare filename → data/binaries/<name>
        if "/" not in v and "\\" not in v:
            return f"data/binaries/{v}", None
        # Directory (trailing slash)
        if v.endswith("/"):
            return v, None
        # Path with filename — reject if existing directory
        try:
            check = Path(v)
            if not check.is_absolute():
                check = self.app.paths.root / check
            if check.is_dir():
                return v, t("oscam_binary_path_is_dir")
        except Exception:
            pass
        return v, None

    def _open_binary_dir_picker(self, target_input_id: str = "t-oscamname") -> None:
        """Open directory picker for the oscam binary output dir."""
        from ui.screens.toolchains import FilePickerScreen
        self._binary_dir_target_input = target_input_id
        # Start path: current value (its directory part) or data/binaries
        try:
            current = self.query_one(f"#{target_input_id}", Input).value.strip()
        except Exception:
            current = ""
        import os
        start = ""
        if current:
            cur = os.path.expanduser(current)
            cur_path = Path(cur)
            if not cur_path.is_absolute():
                cur_path = self.app.paths.root / cur_path
            if cur_path.is_dir():
                start = str(cur_path)
            elif cur_path.parent.is_dir():
                start = str(cur_path.parent)
        if not start:
            default = self.app.paths.binaries_dir
            default.mkdir(parents=True, exist_ok=True)
            start = str(default)
        self.app.push_screen(
            FilePickerScreen(
                start_path=start,
                title=f"{t('select_file_label')}: {t('te_oscam_binary_path')}",
                directories_only=True,
                select_label=t("select_btn"),
            ),
            callback=self._on_binary_dir_selected,
        )

    def _on_binary_dir_selected(self, result) -> None:
        if not result:
            return
        path, _ = result if isinstance(result, tuple) else (result, "")
        if not path:
            return
        # Always trailing slash for directory variant
        if not path.endswith("/"):
            path = path + "/"
        target_id = getattr(self, '_binary_dir_target_input', 't-oscamname')
        try:
            self.query_one(f"#{target_id}", Input).value = path
        except Exception:
            pass

    def on_input_blurred(self, event) -> None:
        """Normalize the oscam binary path on blur."""
        try:
            if event.input.id == "t-oscamname":
                normalized, err = self._normalize_binary_path(event.input.value)
                if err:
                    self.notify(err, severity="warning")
                    return
                if normalized != event.input.value:
                    event.input.value = normalized
        except Exception:
            pass

    def _open_upx_picker(self, target_input_id: str = "t-upx-binary") -> None:
        """Open file picker dialog to select UPX binary."""
        from ui.screens.toolchains import FilePickerScreen
        # Default start: data/bin (where s4 keeps the upx symlink)
        start = str(self.app.paths.bin_dir) if self.app.paths.bin_dir.is_dir() else "/"
        self._upx_target_input = target_input_id
        self.app.push_screen(
            FilePickerScreen(
                start_path=start,
                title=f"{t('select_file_label')}: {t('te_binary')}",
                simple=True,
                select_label=t("select_btn"),
            ),
            callback=self._on_upx_selected,
        )

    def _on_upx_selected(self, result) -> None:
        """Callback after file picker — set the UPX binary path."""
        if not result:
            return
        path, _ = result if isinstance(result, tuple) else (result, "")
        if not path:
            return
        # Reject the s4-managed symlink to avoid pointing it at itself
        try:
            sel = Path(path)
            bin_link = self.app.paths.bin_dir / "upx"
            if sel.resolve() == bin_link.resolve():
                self.notify(t("upx_pick_self_blocked"), severity="warning", timeout=8)
                return
        except Exception:
            pass
        target_id = getattr(self, '_upx_target_input', 't-upx-binary')
        try:
            self.query_one(f"#{target_id}", Input).value = path
        except Exception:
            pass

    def _open_stapi_picker(self) -> None:
        """Open file picker for STAPI_LIB — starts in support/stapi/."""
        from ui.screens.toolchains import FilePickerScreen
        stapi_dir = self.app.paths.root / "support" / "stapi"
        start = str(stapi_dir) if stapi_dir.is_dir() else str(self.app.paths.root)
        self.app.push_screen(
            FilePickerScreen(
                start_path=start,
                title=f"{t('select_file_label')}: STAPI_LIB",
                simple=True,
                select_label=t("select_btn"),
            ),
            callback=self._on_stapi_selected,
        )

    def _on_stapi_selected(self, result) -> None:
        """Callback: set STAPI_LIB input value.

        If the selected file is under ``support/stapi/``, store only
        the bare filename — builder.py auto-resolves bare names to
        ``support/stapi/<name>`` at build time, so the target YAML
        stays portable across machines. Otherwise store the full path.
        """
        if not result:
            return
        path, _ = result if isinstance(result, tuple) else (result, "")
        if not path:
            return
        try:
            sel = Path(path)
            stapi_dir = self.app.paths.root / "support" / "stapi"
            if sel.is_file() and sel.parent.resolve() == stapi_dir.resolve():
                value = sel.name
            else:
                value = str(sel)
        except Exception:
            value = path
        try:
            inp = self.query_one("#f-stapi-lib", Input)
            inp.value = value
            # Field is no longer at default after an explicit pick
            inp.remove_class("tc-default")
        except Exception:
            pass

    def _do_reset_field(self, btn_id: str) -> None:
        """Reset a toolchain field to its default value."""
        # btn_id format: rst-{field}-{sid}
        parts = btn_id.split("-", 2)
        if len(parts) < 3:
            return
        field = parts[1]  # cc, cf, ld, mv
        sid = parts[2]
        defaults = self._tc_defaults.get(sid, {})
        if field == "compress":
            # Reset UPX: restore global defaults and refresh
            try:
                sw = self.query_one(f"#tc-compress-{sid}", Switch)
                sw.value = defaults.get("compress", "0") == "1"
            except Exception:
                pass
            try:
                self.query_one(f"#tc-upx-{sid}", Input).value = defaults.get("upx", "")
                self.query_one(f"#tc-upx-{sid}", Input).add_class("tc-default")
            except Exception:
                pass
            try:
                self.query_one(f"#tc-complevel-{sid}", Input).value = defaults.get("complevel", self.app.settings.default_compress_level)
                self.query_one(f"#tc-complevel-{sid}", Input).add_class("tc-default")
            except Exception:
                pass
            self._update_tc_bold(sid)
            return
        if field == "oscampath":
            try:
                inp = self.query_one(f"#tc-oscampath-{sid}", Input)
                inp.value = defaults.get("oscampath", "")
                inp.add_class("tc-default")
            except Exception:
                pass
        elif field == "confdir":
            try:
                inp = self.query_one(f"#tc-confdir-{sid}", Input)
                inp.value = defaults.get("confdir", self.app.settings.default_confdir)
                inp.add_class("tc-default")
            except Exception:
                pass
        elif field in ("cc", "cf", "ld", "mv"):
            default_val = defaults.get(field, "")
            try:
                inp = self.query_one(f"#tc-{field}-{sid}", Input)
                inp.value = default_val
                inp.add_class("tc-default")
            except Exception:
                pass
        else:
            return
        # Refresh to rebuild UI with correct bold state, re-focus the reset button
        self._refresh_screen(self._collect(), "source", btn_id)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Track input changes for dynamic UI updates."""
        iid = event.input.id or ""
        # Update deployment collapsible title when name changes
        if iid.startswith("t-dep-name-"):
            try:
                idx = iid.split("-")[-1]
                name = event.value.strip() or f"#{int(idx) + 1}"
                from textual.widgets import Checkbox
                enabled = self.query_one(f"#t-dep-enabled-{idx}", Checkbox).value
                title = f"Deploy {name}" if enabled else f"Deploy {name} (disabled)"
                self.query_one(f"#col-dep-{idx}", Collapsible).title = title
            except Exception:
                pass
            return
        _tc_input_prefixes = ("tc-cc-", "tc-cf-", "tc-ld-", "tc-mv-", "tc-upx-", "tc-complevel-", "tc-oscampath-", "tc-confdir-")
        if any(iid.startswith(p) for p in _tc_input_prefixes):
            # Determine if value differs from default
            parts = iid.split("-", 2)
            field = parts[1]  # cc, cf, ld, mv, upx, complevel, confdir
            sid = parts[2]
            defaults = self._tc_defaults.get(sid, {})
            default_val = defaults.get(field, "")
            if event.value != default_val:
                event.input.remove_class("tc-default")
            else:
                event.input.add_class("tc-default")

        # NOTE: UPX binary path validation used to run here on every
        # keystroke, which spammed "not found" warnings while the user
        # was still typing the path. It now runs on Submitted (Enter /
        # focus leaves the field) — see ``on_input_submitted``.

        # STAPI_LIB field style tracking
        if iid == "f-stapi-lib":
            default_val = getattr(self, '_stapi_default', '')
            if event.value != default_val:
                event.input.remove_class("tc-default")
            else:
                event.input.add_class("tc-default")

    def on_input_blurred(self, event) -> None:
        """Update toolchain label bold state when leaving a tc flag field."""
        try:
            iid = event.input.id or ""
        except Exception:
            return
        # Common UPX binary path validation — runs when focus leaves
        # the field (or on Enter), not on every keystroke, so typing
        # an absolute path doesn't spam "not found" warnings char by
        # char.
        if iid == "t-upx-binary":
            from pathlib import Path as _P
            import subprocess as _sp
            upx_path = event.input.value.strip()
            try:
                switch = self.query_one("#f-use-compress", Switch)
                if upx_path and _P(upx_path).exists():
                    try:
                        upx_ver = _sp.check_output(
                            [upx_path, "--version"],
                            text=True, stderr=_sp.DEVNULL, timeout=5,
                        ).splitlines()[0]
                    except Exception:
                        upx_ver = "OK"
                    switch.tooltip = f"{upx_path} — {upx_ver}"
                elif upx_path:
                    switch.tooltip = f"{upx_path} — not found!"
                    self.notify(
                        t("upx_not_found", path=upx_path),
                        severity="warning", timeout=3,
                    )
                else:
                    switch.tooltip = "upx not configured"
            except Exception:
                pass
        # Update UPX switch tooltip when UPX binary path changes
        if iid.startswith("tc-upx-"):
            sid = iid.split("-", 2)[2]
            upx_path = event.input.value.strip()
            try:
                sw = self.query_one(f"#tc-compress-{sid}", Switch)
                if upx_path:
                    try:
                        import subprocess as _sp
                        _ver = _sp.check_output([upx_path, "--version"], text=True, stderr=_sp.DEVNULL, timeout=5).splitlines()[0]
                        sw.tooltip = f"{upx_path} — {_ver}"
                    except Exception:
                        sw.tooltip = f"{upx_path} — not found"
                else:
                    import re as _re
                    sw.tooltip = _re.sub(r'\[/?[a-z_ ]+\]', '', self._upx_info).strip() if self._upx_path else "upx not found"
            except Exception:
                pass
        _tc_input_prefixes = ("tc-cc-", "tc-cf-", "tc-ld-", "tc-mv-", "tc-upx-", "tc-complevel-", "tc-oscampath-", "tc-confdir-")
        if any(iid.startswith(p) for p in _tc_input_prefixes):
            sid = iid.split("-", 2)[2]
            self._update_tc_bold(sid)

    def _update_flag_counts(self) -> None:
        """Update Library/Hardware Flags collapsible titles with active counts."""
        try:
            lib_count = 0
            hw_count = 0
            for sw in self.query("Switch"):
                sid = sw.id or ""
                if sid.startswith("f-use-") and sw.value:
                    flag = sid[6:]  # strip "f-use-"
                    if flag in self._HARDWARE_FLAGS:
                        hw_count += 1
                    elif flag != "compress":
                        lib_count += 1
            try:
                self.query_one("#col-lib-flags", Collapsible).title = f"{t('section_library_flags')} ({lib_count} {t('te_selected')})"
            except Exception:
                pass
            try:
                self.query_one("#col-hw-flags", Collapsible).title = f"{t('section_hardware_flags')} ({hw_count} {t('te_selected')})"
            except Exception:
                pass
        except Exception:
            pass

    def _update_tc_bold(self, sid: str) -> None:
        """Update toolchain label bold/normal based on whether any field has custom value."""
        try:
            defaults = self._tc_defaults.get(sid, {})
            has_custom = any(
                self._input_val(f"tc-{f}-{sid}") != defaults.get(f, "")
                for f in ("cc", "cf", "ld", "mv", "upx", "complevel", "oscampath", "confdir")
            )
            # Also check compress switch
            if not has_custom:
                try:
                    sw_val = "1" if self.query_one(f"#tc-compress-{sid}", Switch).value else "0"
                    if sw_val != defaults.get("compress", "0"):
                        has_custom = True
                except Exception:
                    pass
            tc_info = self._tc_info_cache.get(sid)
            if tc_info:
                name, arch = tc_info
                name_str = f"[bold]{name}[/bold]" if has_custom else name
                arch_str = f" [dim]({arch})[/dim]" if arch else ""
                sw = self.query_one(f"#tc-{sid}", Switch)
                for child in sw.parent.children:
                    if hasattr(child, 'classes') and "tc-toggle-label" in child.classes:
                        child.update(f" {name_str}{arch_str}")
                        break
        except Exception:
            pass

    def _sync_section_for_option(self, opt_name: str) -> None:
        """Recompute the section toggle state after a user toggle on
        an individual option.
        """
        oscam = self._load_oscam_options()
        category = None
        if opt_name in oscam.addons or opt_name in self._removed_opts.get("addons", []):
            category = "addons"
        elif opt_name in oscam.protocols or opt_name in self._removed_opts.get("protocols", []):
            category = "protocols"
        elif opt_name in oscam.readers or opt_name in self._removed_opts.get("readers", []):
            category = "readers"
        elif opt_name in oscam.card_readers or opt_name in self._removed_opts.get("card_readers", []):
            category = "card_readers"
        if not category:
            return
        items_map = {
            "addons": oscam.addons,
            "protocols": oscam.protocols,
            "readers": oscam.readers,
            "card_readers": oscam.card_readers,
        }
        all_items = list(items_map[category]) + list(self._removed_opts.get(category, []))
        any_on = any(self._switch_on(f"m-{name}") for name in all_items)
        all_on = bool(all_items) and all(self._switch_on(f"m-{name}") for name in all_items)
        try:
            sec = self.query_one(f"#sec-{category}", Switch)
        except Exception:
            return
        if sec.value != any_on:
            # Suppress the resulting Switch.Changed message so the
            # sec-* handler does not flip every option to match.
            with sec.prevent(Switch.Changed):
                sec.value = any_on
        # Update the all-on class so the slider tints differently
        # when every option in the section is enabled.
        if all_on:
            sec.add_class("all-on")
        else:
            sec.remove_class("all-on")
        self._update_section_title(category)

    def _update_section_title(self, category: str) -> None:
        """Refresh the "(enabled/total)" counter in the Collapsible header
        for the given options category."""
        oscam = self._load_oscam_options()
        items_map = {
            "addons": (sorted(oscam.addons), "Addons", "col-addons"),
            "protocols": (sorted(oscam.protocols), "Protocols", "col-protocols"),
            "readers": (sorted(oscam.readers), "Readers", "col-readers"),
            "card_readers": (sorted(oscam.card_readers), "Card Readers", "col-card-readers"),
        }
        if category not in items_map:
            return
        items, label, col_id = items_map[category]
        removed = list(self._removed_opts.get(category, []))
        if category == "addons":
            all_items = items + removed
            count = sum(1 for m in all_items if self._switch_on(f"m-{m}") or m in removed)
        else:
            count = sum(1 for m in items if self._switch_on(f"m-{m}"))
        try:
            col = self.query_one(f"#{col_id}", Collapsible)
            col.title = f"{label} ({count}/{len(items)})"
        except Exception:
            pass

    def _do_section_toggle(self, category: str, new_state: bool) -> None:
        """Set every option in a category to new_state.

        Stale (red) entries are toggled along with the regular ones —
        they only disappear via Reset.
        """
        oscam = self._load_oscam_options()
        attr_map = {
            "addons": ("modules", oscam.addons),
            "protocols": ("protocols", oscam.protocols),
            "readers": ("readers", oscam.readers),
            "card_readers": ("card_readers", oscam.card_readers),
        }
        if category not in attr_map:
            return
        target_attr, available = attr_map[category]
        stale = list(self._removed_opts.get(category, []))
        live_items = list(available) + stale
        target = self._collect()
        if new_state:
            setattr(target.build, target_attr, list(live_items))
        else:
            setattr(target.build, target_attr, [])
        self._refresh_screen(target, "oscam")

    def _do_reset_general_flags(self) -> None:
        """Restore USE_*/STATIC_* flags on the Toolchain tab to their
        Python-code defaults (LIBCRYPTO on, everything else off). F12 is
        still required to persist the result.
        """
        focus_id = self.app.focused.id if self.app.focused else ""
        target = self._collect()
        target.build.use_flags = {"LIBCRYPTO": True}
        target.build.static_flags = {}
        self._refresh_screen(target, "source", focus_id)
        self.notify(t("te_reset_done"), severity="information")

    def _do_reset_options(self) -> None:
        """Restore Options to the oscam defaults from the assigned
        repos and drop any stale (red) entries. F12 still required to
        persist the result.
        """
        focus_id = self.app.focused.id if self.app.focused else ""
        target = self._collect()
        oscam = self._load_oscam_options()
        target.build.modules = list(oscam.default_enabled.get("addons", []))
        target.build.protocols = list(oscam.default_enabled.get("protocols", []))
        target.build.readers = list(oscam.default_enabled.get("readers", []))
        target.build.card_readers = list(oscam.default_enabled.get("card_readers", []))
        self._removed_opts = {}
        self._refresh_screen(target, "oscam", focus_id, clear_stale=True)
        self.notify(t("te_reset_done"), severity="information")

    def action_switch_tab(self, tab: str) -> None:
        self._show_tab(tab)

    def _show_tab(self, tab: str) -> None:
        panels = {
            "general": ("#te-panel-general", "#te-tab-general"),
            "source": ("#te-panel-source", "#te-tab-source"),
            "oscam": ("#te-panel-oscam", "#te-tab-oscam"),
            "deploy": ("#te-panel-deploy", "#te-tab-deploy"),
        }
        for key, (panel_id, tab_id) in panels.items():
            try:
                self.query_one(panel_id).display = (key == tab)
                self.query_one(tab_id).set_classes("tab-active" if key == tab else "tab-inactive")
            except Exception:
                pass
        self._active_tab = tab
        self._update_footer_state()
        # Focus the first focusable widget inside the active panel.
        panel_ids = {
            "general": "#te-panel-general",
            "source": "#te-panel-source",
            "oscam": "#te-panel-oscam",
            "deploy": "#te-panel-deploy",
        }
        pid = panel_ids.get(tab)
        if pid:
            try:
                panel = self.query_one(pid)
                for child in panel.query("*"):
                    if child.focusable:
                        child.focus()
                        break
            except Exception:
                pass

    def _update_footer_state(self) -> None:
        """F9/F10/F11 context-dependent on active tab."""
        try:
            from ui.footer import FooterItem
            for item in self.query(FooterItem):
                if item._key == "F9":
                    if self._active_tab == "deploy":
                        item._label = t("te_check_deploy")
                        item._disabled = False
                        item.remove_class("disabled")
                    else:
                        item._disabled = True
                        item.add_class("disabled")
                    item.refresh()
                if item._key == "F11":
                    if self._active_tab == "deploy":
                        item._label = t("te_delete_deploy")
                        item._disabled = False
                        item.remove_class("disabled")
                    elif self._active_tab in ("oscam", "source"):
                        item._label = t("te_reset")
                        item._disabled = False
                        item.remove_class("disabled")
                    else:
                        item._label = t("te_reset")
                        item._disabled = True
                        item.add_class("disabled")
                    item.refresh()
                if item._key == "F10":
                    enabled = self._active_tab == "deploy"
                    item._disabled = not enabled
                    if enabled:
                        item.remove_class("disabled")
                    else:
                        item.add_class("disabled")
                    item.refresh()
        except Exception:
            pass

    def do_tc_update(self) -> None:
        """F9 on Deployment tab → check deployment connectivity."""
        if self._active_tab == "deploy":
            self._check_deployment()

    def _check_deployment(self) -> None:
        """Open the deployment check modal for the active deployment entry.

        The active entry is determined by finding which collapsible contains
        the currently focused widget, or the last expanded one.
        """
        target = self._collect()
        # Find which collapsible contains the focused widget
        focused_idx = -1
        focused = self.app.focused
        if focused:
            for idx in range(len(target.deployment)):
                try:
                    col = self.query_one(f"#col-dep-{idx}", Collapsible)
                    if col in focused.ancestors_with_self or focused in col.query("*"):
                        focused_idx = idx
                        break
                except Exception:
                    pass
        # Fallback: last expanded collapsible
        if focused_idx < 0:
            for idx in range(len(target.deployment)):
                try:
                    col = self.query_one(f"#col-dep-{idx}", Collapsible)
                    if not col.collapsed:
                        focused_idx = idx
                except Exception:
                    pass
        if focused_idx < 0:
            self.notify("Expand a deployment entry to test it", severity="warning")
            return
        if not target.deployment[focused_idx].host:
            self.notify("No host configured for this deployment entry", severity="warning")
            return
        from ui.screens.deployment_check import DeploymentCheckModal
        self.app.push_screen(DeploymentCheckModal(
            deployments=[target.deployment[focused_idx]],
            target_name=target.name,
            binaries_dir=self.app.paths.binaries_dir,
            logs_dir=self.app.paths.logs_dir,
            delete_binary_after_deploy=target.delete_binary_after_deploy,
            # Needed so the deployer's scp_mode cache update is persisted;
            # self.target is the live object whose _source_path is set.
            target=self.target,
        ))

    def do_add(self) -> None:
        """F10 on Deployment tab → add new deployment entry."""
        if self._active_tab == "deploy":
            self._add_deployment_entry()

    def do_remove(self) -> None:
        """F11 → context-dependent: Reset or Delete deployment."""
        if self._active_tab == "oscam":
            self._do_reset_options()
        elif self._active_tab == "source":
            self._do_reset_general_flags()
        elif self._active_tab == "deploy":
            self._delete_last_deployment_entry()

    def _collect(self) -> Target:
        """Collect all values from widgets into a Target."""
        name = self.query_one("#t-name", Input).value.strip()
        desc = self.query_one("#t-desc", Input).value.strip()

        # Collect selected sources with per-source options
        repos = load_repos(self.app.paths.repos_config)
        sources = []
        for r in repos:
            try:
                rid = _safe_id(r.name)
                if self.query_one(f"#src-{rid}", Switch).value:
                    patch_on = self._switch_on(f"src-patch-{rid}")
                    # Collect selected patches
                    patches = []
                    if patch_on:
                        patches_dir = self.app.paths.patches_dir
                        if patches_dir.is_dir():
                            for p in sorted(patches_dir.glob("*.patch")):
                                pid = _safe_id(p.name.replace(".patch", ""))
                                if self._switch_on(f"p-{rid}-{pid}"):
                                    patches.append(p.name)
                    sources.append(SourceConfig(
                        name=r.name,
                        update_before_build=self._switch_on(f"src-upd-{rid}"),
                        patch_before_build=patch_on,
                        patches=patches,
                    ))
            except Exception:
                pass

        # Collect selected toolchains
        registry = ToolchainRegistry(
            self.app.paths.toolchains_defaults,
            self.app.paths.toolchains_local,
            self.app.paths.toolchains_dir,
        )
        registry.load()
        # Build map of existing toolchain overrides so we can preserve them
        # when the flag-editor fields are not visible in the DOM
        _existing_tc = {tc.name: tc for tc in self.target.toolchains}
        toolchains = []
        for tc in registry.installed_toolchains():
            try:
                sid = _safe_id(tc.name)
                if self.query_one(f"#tc-{sid}", Switch).value:
                    # Check if flag-editor fields are visible (only when editing this TC)
                    try:
                        self.query_one(f"#tc-cc-{sid}", Input)
                        fields_visible = True
                    except Exception:
                        fields_visible = False

                    if fields_visible:
                        # Read override fields — if value == toolchain default, store empty
                        defaults = self._tc_defaults.get(sid, {})
                        cc = self._input_val(f"tc-cc-{sid}")
                        cf = self._input_val(f"tc-cf-{sid}")
                        ld = self._input_val(f"tc-ld-{sid}")
                        mv = self._input_val(f"tc-mv-{sid}")
                        # UPX + confdir per-TC
                        try:
                            tc_compress_on = self.query_one(f"#tc-compress-{sid}", Switch).value
                            tc_compress = "1" if tc_compress_on else "0"
                        except Exception:
                            tc_compress = ""
                        tc_upx = self._input_val(f"tc-upx-{sid}")
                        tc_complevel = self._input_val(f"tc-complevel-{sid}")
                        tc_oscampath = self._input_val(f"tc-oscampath-{sid}")
                        tc_confdir = self._input_val(f"tc-confdir-{sid}")
                        toolchains.append(ToolchainConfig(
                            name=tc.name,
                            cc_opts="" if cc == defaults.get("cc", "") else cc,
                            cflags="" if cf == defaults.get("cf", "") else cf,
                            ldflags="" if ld == defaults.get("ld", "") else ld,
                            make_vars="" if mv == defaults.get("mv", "") else mv,
                            use_compress="" if tc_compress == defaults.get("compress", "") else tc_compress,
                            compress_level="" if tc_complevel == defaults.get("complevel", "") else tc_complevel,
                            upx_binary="" if tc_upx == defaults.get("upx", "") else tc_upx,
                            oscam_path="" if tc_oscampath == defaults.get("oscampath", "") else tc_oscampath,
                            confdir="" if tc_confdir == defaults.get("confdir", "") else tc_confdir,
                        ))
                    else:
                        # Fields not visible — preserve existing overrides
                        existing = _existing_tc.get(tc.name, ToolchainConfig(name=tc.name))
                        toolchains.append(existing)
            except Exception:
                pass

        # Collect enabled modules (dynamically from oscam options)
        # Removed options are shown as switches too — collect them the same way.
        # When no sources are selected the options/flags panels show
        # placeholders instead of switches — reading the DOM would yield
        # all-off.  Preserve the previous target values so a temporary
        # "no source" state during a repo switch does not wipe the config.
        if self.target.sources:
            oscam = self._load_oscam_options()
            all_addons = oscam.addons + self._removed_opts.get("addons", [])
            all_protos = oscam.protocols + self._removed_opts.get("protocols", [])
            all_readers = oscam.readers + self._removed_opts.get("readers", [])
            all_crs = oscam.card_readers + self._removed_opts.get("card_readers", [])
            modules = sorted(m for m in all_addons if self._switch_on(f"m-{m}"))
            protocols = sorted(m for m in all_protos if self._switch_on(f"m-{m}"))
            readers = sorted(m for m in all_readers if self._switch_on(f"m-{m}"))
            card_readers = sorted(m for m in all_crs if self._switch_on(f"m-{m}"))

            mk_flags = self._load_makefile_flags()
            all_use = mk_flags.get("use", [])
            use_flags = {}
            for flag in all_use:
                if self._switch_on(f"f-use-{flag}"):
                    use_flags[flag] = True
            _STATIC_CAPABLE = {"LIBCRYPTO", "SSL", "LIBUSB", "PCSC", "LIBDVBCSA"}
            static_flags = {}
            for flag in _STATIC_CAPABLE:
                if self._switch_on(f"f-static-{flag}"):
                    static_flags[flag] = True
        else:
            prev = self.target.build
            modules = list(prev.modules)
            protocols = list(prev.protocols)
            readers = list(prev.readers)
            card_readers = list(prev.card_readers)
            use_flags = dict(prev.use_flags)
            static_flags = dict(prev.static_flags)

        build = BuildSettings(
            modules=modules,
            protocols=protocols,
            readers=readers,
            card_readers=card_readers,
            use_flags=use_flags,
            static_flags=static_flags,
            use_compress=self._switch_on("f-use-compress"),
            compress_level=self._input_val("t-compress-level") or self.app.settings.default_compress_level,
            upx_binary=self._input_val("t-upx-binary"),
            stapi_lib=self._input_val("f-stapi-lib"),
            use_oscamname=self._normalize_binary_path(self.query_one("#t-oscamname", Input).value)[0],
            build_system=self.query_one("#t-build-system", Select).value or self.app.settings.default_build_system,
            confdir=self.query_one("#t-confdir", Input).value.strip(),
        )

        # Deployment — collect all deployment entries by index
        deployment = []
        for idx in range(100):  # safe upper bound
            try:
                host = self.query_one(f"#t-dep-host-{idx}", Input).value.strip()
            except Exception:
                break  # no more entries
            # Keep entries even without host (for UI state);
            # target.save() filters out empty hosts.
            try:
                port = int(self.query_one(f"#t-dep-port-{idx}", Input).value)
            except (ValueError, Exception):
                port = 22
            try:
                src_sel = self.query_one(f"#t-dep-src-{idx}", Select)
                src = str(src_sel.value) if src_sel.value and src_sel.value is not Select.BLANK else ""
            except Exception:
                src = ""
            try:
                tc_sel = self.query_one(f"#t-dep-tc-{idx}", Select)
                tc = str(tc_sel.value) if tc_sel.value and tc_sel.value is not Select.BLANK else ""
            except Exception:
                tc = ""
            try:
                password = self.query_one(f"#t-dep-pass-{idx}", Input).value
            except Exception:
                password = ""
            try:
                su_password = self.query_one(f"#t-dep-su-pass-{idx}", Input).value
            except Exception:
                su_password = ""
            try:
                up_name = self.query_one(f"#t-dep-name-{idx}", Input).value.strip()
            except Exception:
                up_name = ""
            try:
                from textual.widgets import Checkbox
                enabled = self.query_one(f"#t-dep-enabled-{idx}", Checkbox).value
            except Exception:
                enabled = True
            try:
                target_backup = self.query_one(f"#t-dep-backup-{idx}", Checkbox).value
            except Exception:
                target_backup = False
            try:
                _sm = self.query_one(f"#t-dep-scpmode-{idx}", Select).value
                scp_mode = str(_sm) if _sm and _sm is not Select.BLANK else "last_working"
            except Exception:
                scp_mode = "last_working"
            # Carry the cached mode across saves — it's deployer state,
            # not editable in the UI; the original up entry still has it.
            prev_cached = ""
            if idx < len(self.target.deployment):
                prev_cached = self.target.deployment[idx].last_working_scp_mode
            deployment.append(DeploymentConfig(
                name=up_name,
                enabled=enabled,
                source=src,
                toolchain=tc,
                host=host,
                user=self.query_one(f"#t-dep-user-{idx}", Input).value.strip() or self.app.settings.default_deploy_user,
                password=password,
                port=port,
                key=self.query_one(f"#t-dep-key-{idx}", Input).value.strip(),
                target_path=self.query_one(f"#t-dep-path-{idx}", Input).value.strip(),
                target_backup=target_backup,
                su_password=su_password,
                pre_cmd=self.query_one(f"#t-dep-pre-{idx}", Input).value.strip(),
                post_cmd=self.query_one(f"#t-dep-post-{idx}", Input).value.strip(),
                scp_mode=scp_mode,
                last_working_scp_mode=prev_cached,
            ))

        try:
            excl_auto = self.query_one("#t-excl-auto", Checkbox).value
        except Exception:
            excl_auto = self.target.exclude_from_automation

        try:
            del_binary = self.query_one("#t-del-binary", Checkbox).value
        except Exception:
            del_binary = self.target.delete_binary_after_deploy

        return Target(
            name=name,
            description=desc,
            sources=sources,
            toolchains=toolchains,
            build=build,
            deployment=deployment,
            exclude_from_automation=excl_auto,
            delete_binary_after_deploy=del_binary,
        )

    def _input_val(self, widget_id: str) -> str:
        try:
            return self.query_one(f"#{widget_id}", Input).value.strip()
        except Exception:
            return ""

    def _switch_on(self, widget_id: str) -> bool:
        try:
            return self.query_one(f"#{widget_id}", Switch).value
        except Exception:
            return False

    def is_dirty(self) -> bool:
        """True if the in-memory target differs from the file on disk."""
        try:
            collected = self._collect()
            if not self.save_path or not self.save_path.exists():
                return bool(collected.name)
            current = self._serialize_target(collected)
            from core.target import Target as _T
            disk = self._serialize_target(_T.load(self.save_path))
            return current != disk
        except Exception:
            return False

    @staticmethod
    def _serialize_target(target) -> str:
        """Stable string representation of a Target for diffing."""
        import tempfile
        from pathlib import Path as _P
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            tmp_path = _P(f.name)
        try:
            target.save(tmp_path)
            return tmp_path.read_text(encoding="utf-8")
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass

    def do_save(self) -> str:
        """Save target (called by App)."""
        # Validate the oscam binary path before collecting
        try:
            raw = self.query_one("#t-oscamname", Input).value
            _norm, err = self._normalize_binary_path(raw)
            if err:
                self.notify(err, severity="error")
                return ""
        except Exception:
            pass
        target = self._collect()
        if not target.name:
            self.notify(t("target_name_required"), severity="error")
            return ""
        new_path = self.app.paths.targets_dir / f"{target.name}.yaml"
        # If renamed, delete old file
        if self.save_path and self.save_path != new_path and self.save_path.exists():
            self.save_path.unlink()
        target.save(new_path)
        self.save_path = new_path
        # Password-decryption warnings are stale after a save: the user
        # just re-entered the passwords and they're now encrypted with
        # the current machine key. Clear both the persistent list and
        # the transient per-target list, then dismiss any toast that's
        # still on screen from an earlier scan.
        from core.credentials import (
            clear_persistent_warnings, clear_transient_warnings,
        )
        clear_persistent_warnings()
        clear_transient_warnings()
        try:
            self.app.clear_notifications()
        except Exception:
            pass
        if target.exclude_from_automation:
            from core.automation import remove_from_jobs
            remove_from_jobs(self.app.paths.automation_yaml, target=target.name)
        # Remember for main screen to select after returning
        self.app._last_saved_target = str(new_path)
        return t("target_saved", name=target.name)

    def action_go_back(self) -> None:
        self.app.pop_screen()
