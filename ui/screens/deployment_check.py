"""Deployment check modal — test SSH connectivity and optionally deploy a binary."""

from __future__ import annotations

import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, RichLog, Button, Select
from ui.widgets.artifact_richlog import ArtifactRichLog

from core.target import DeploymentConfig, Target
from core.deployer import Deployer
from translation import t
from ui.icons import is_ascii


class DeploymentCheckModal(ModalScreen[None]):
    """Modal that tests SSH connectivity and performs deployment check or real deployment."""

    BINDINGS = [("escape", "close", "Close")]

    DEFAULT_CSS = """
    DeploymentCheckModal {
        align: center middle;
    }
    #check-box {
        width: 120;
        height: 34;
        border: solid $primary;
        background: $surface;
        padding: 1;
    }
    #check-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    #check-select-row {
        height: 3;
        margin: 0 0 1 0;
    }
    #check-select-row Select {
        width: 1fr;
    }
    #check-log {
        height: 1fr;
        background: $boost;
        border: solid $accent;
        border-title-color: $text;
        border-title-style: bold;
        padding: 0 1;
        overflow-y: auto;
    }
    #check-log:focus {
        border: heavy $accent;
    }
    #check-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    #check-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self, deployments: list[DeploymentConfig], target_name: str,
                 binaries_dir: Optional[Path] = None,
                 logs_dir: Optional[Path] = None,
                 toolchain_filter: str = "",
                 delete_binary_after_deploy: bool = False,
                 target: "Target | None" = None) -> None:
        super().__init__()
        self._deployments = [d for d in deployments if d.host]
        self._target_name = target_name
        self._binaries_dir = binaries_dir
        self._logs_dir = logs_dir
        self._toolchain_filter = toolchain_filter or (self._deployments[0].toolchain if self._deployments else "")
        self._check_running = False
        self._binary_path: Optional[Path] = None
        self._delete_binary_after_deploy = delete_binary_after_deploy
        self._delete_confirm_time = 0.0
        # Optional parent target — only used so the deployer's cached
        # scp_mode updates can be persisted via target.save_in_place().
        self._target = target

    def compose(self) -> ComposeResult:
        bin_options = [(t("dc_dry_run"), "dry-run")]
        if self._binaries_dir and self._binaries_dir.is_dir():
            tc = self._toolchain_filter
            _hostname = socket.gethostname()
            for f in sorted(self._binaries_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if not f.is_file():
                    continue
                if f.name.endswith((".debug", ".zip", ".log")):
                    continue
                if tc and tc != "native" and tc not in f.name:
                    continue
                if tc == "native" and _hostname not in f.name and "native" not in f.name:
                    continue
                size_kb = f.stat().st_size // 1024
                mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                label = f"{f.name} ({size_kb} KB, {mtime})"
                bin_options.append((label, str(f)))

        with Vertical(id="check-box"):
            dep_names = [d.name or d.host for d in self._deployments]
            dep_info = f" → {', '.join(dep_names)}" if dep_names else ""
            yield Static(f"[bold]Deployment — {self._target_name}{dep_info}[/bold]", id="check-title")
            with Horizontal(id="check-select-row"):
                yield Static(f" {t('dc_binary')}", classes="s-label-tall")
                yield Select(options=bin_options, value="dry-run", id="check-binary", allow_blank=False)
            log = ArtifactRichLog(id="check-log", highlight=False, markup=True, wrap=True)
            log.border_title = t("output_title")
            yield log
            with Horizontal(id="check-buttons"):
                yield Button(t("dc_start"), id="check-start", variant="primary")
                yield Button(t("cancel"), id="check-close")

    def on_mount(self) -> None:
        self._log_widget = self.query_one("#check-log", RichLog)
        self._log_widget.write(f"[dim]{t('dc_press_start')}[/dim]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "check-close":
            self.dismiss(None)
        elif event.button.id == "check-start":
            self._start_check()

    def _start_check(self) -> None:
        if self._check_running:
            return
        try:
            binary_val = self.query_one("#check-binary", Select).value
            binary_path = None if binary_val == "dry-run" else Path(str(binary_val))
        except Exception:
            binary_path = None

        if (binary_path is not None
                and self._delete_binary_after_deploy
                and getattr(self.app.settings, "confirm_dangerous", True)):
            import time
            now = time.time()
            if now - self._delete_confirm_time >= 3:
                self._delete_confirm_time = now
                self.notify(
                    t("dc_delete_binary_confirm").format(name=binary_path.name),
                    severity="warning",
                    timeout=3,
                )
                return
            self._delete_confirm_time = 0.0

        self._check_running = True
        self._log_widget.write(f"[bold]{t('dc_starting')}[/bold]")
        self._binary_path = binary_path
        threading.Thread(target=self._run_thread, daemon=True).start()

    def _run_thread(self) -> None:
        try:
            self._run_checks(self._binary_path)
        except Exception:
            import traceback
            msg = traceback.format_exc()
            try:
                self.app.call_from_thread(self._log_widget.write, f"[red]{msg}[/red]")
            except Exception:
                pass
        finally:
            self._check_running = False

    def action_close(self) -> None:
        self.dismiss(None)

    def _log(self, msg: str) -> None:
        self.app.call_from_thread(self._log_widget.write, msg)

    def _run_checks(self, binary_path: Optional[Path] = None) -> None:
        log_lines: list[str] = []
        is_dry_run = binary_path is None
        _arrow = "->" if is_ascii() else "\u2192"
        _sep = "---" if is_ascii() else "\u2501\u2501\u2501"

        def log(msg: str) -> None:
            log_lines.append(msg)
            self._log(msg)

        mode = t("dc_mode_dry") if is_dry_run else t("dc_mode_deploy").format(name=binary_path.name)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"[bold]{t('dc_deploy_started').format(time=now)}[/bold]")
        log(t("dc_target").format(name=self._target_name))
        log(t("dc_mode").format(mode=mode))
        log(t("dc_destinations").format(count=len(self._deployments)))

        will_delete = (
            binary_path is not None
            and self._delete_binary_after_deploy
        )
        if will_delete and is_dry_run:
            log(f"[yellow]{t('dc_delete_binary_warn_dry').format(name=binary_path.name)}[/yellow]")
        elif will_delete:
            log(f"[yellow]{t('dc_delete_binary_warn').format(name=binary_path.name)}[/yellow]")
        log("")

        # Snapshot cached SCP modes so a dry-run can be ignored and a
        # real deploy can persist whatever the fallback chain discovered.
        scp_cache_before = [d.last_working_scp_mode for d in self._deployments]

        all_ok = True
        for idx, d in enumerate(self._deployments):
            name = d.name or f"#{idx + 1}"
            is_local = d.host in ("localhost", "127.0.0.1", "::1")

            if is_local:
                header = t("dc_separator_local").format(name=name, path=d.target_path).replace("\u2192", _arrow)
            else:
                header = t("dc_separator_remote").format(
                    name=name, user=d.user, host=d.host, path=d.target_path)
            log(f"[bold]{_sep} {header} {_sep}[/bold]")
            log("")

            deployer = Deployer(
                config=d, log=log,
                binary_path=binary_path,
                dry_run=is_dry_run,
                auto_fix_host_keys=getattr(
                    self.app.settings, "auto_fix_stale_host_keys", False),
            )
            if not deployer.run():
                all_ok = False
            log("")

        # Persist SCP cache updates \u2014 only after a real deploy, never
        # from a dry run (dry run doesn't actually transfer anything).
        # The tested deployment objects may be a fresh _collect() of the
        # UI (not identity-equal to the entries in self._target), so merge
        # cached-mode updates back by (host, port, user, target_path).
        if (not is_dry_run and self._target is not None
                and [d.last_working_scp_mode for d in self._deployments] != scp_cache_before):
            for tested in self._deployments:
                for orig in self._target.deployment:
                    if (orig.host == tested.host and orig.port == tested.port
                            and orig.user == tested.user and orig.target_path == tested.target_path):
                        orig.last_working_scp_mode = tested.last_working_scp_mode
                        break
            try:
                self._target.save_in_place()
            except Exception:
                pass

        if self._delete_binary_after_deploy and binary_path is not None:
            try:
                size_kb = binary_path.stat().st_size // 1024
                size_str = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb} KB"
            except OSError:
                size_str = "?"
            if is_dry_run:
                log(f"[dim]{t('dc_delete_binary_dry').format(name=binary_path.name, size=size_str)}[/dim]")
            elif not all_ok:
                log(f"[yellow]{t('dc_delete_binary_kept').format(name=binary_path.name)}[/yellow]")
            else:
                try:
                    binary_path.unlink()
                    log(f"[dim]{t('dc_delete_binary_done').format(name=binary_path.name, size=size_str)}[/dim]")
                except OSError as e:
                    log(f"[yellow]{t('dc_delete_binary_error').format(name=binary_path.name, error=e)}[/yellow]")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"[bold]{t('dc_complete').format(time=now)}[/bold]")

        # Write log file
        if self._logs_dir:
            try:
                import re
                from core.log_cleanup import resolve_log_dir
                _organize = getattr(self.app.settings, "organize_logs", False)
                _log_dir = resolve_log_dir(self._logs_dir, "deploy", _organize)
                timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
                deploy_names = "-".join(
                    (d.name or d.host).replace(" ", "_").replace("/", "_")
                    for d in self._deployments
                )
                log_path = _log_dir / f"{timestamp}.{self._target_name}.{deploy_names}.deployment-check.log"
                plain_lines = [re.sub(r'\[/?[a-z_ #0-9]+\]', '', line) for line in log_lines]
                log_path.write_text("\n".join(plain_lines) + "\n", encoding="utf-8")
                log(f"Log: {log_path}")
            except Exception:
                pass

        try:
            from core.log_cleanup import run_retention
            run_retention(self._logs_dir, self.app.settings)
        except Exception:
            pass
