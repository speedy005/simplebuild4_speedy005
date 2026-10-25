"""Shared deployment logic for PHASE 5 (builder) and F9 Deployment Check.

Provides a single ``Deployer`` class that handles both remote (SSH/SCP)
and local (file copy) deployments with pre/post install commands. Used by
``core.builder.BuildEngine._phase5_deploy`` and
``ui.screens.deployment_check.DeploymentCheckModal``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from rich.markup import escape as rich_escape

from core.target import DeploymentConfig

SSH_OPTS = [
    # Auto-accept previously-unseen host keys (StrictHostKeyChecking=no
    # writes them to known_hosts on first contact); but a CHANGED key
    # is still refused so the user sees an MITM warning and decides.
    "-o", "StrictHostKeyChecking=no",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "ConnectTimeout=10",
]


class Deployer:
    """Execute deployment steps for a single DeploymentConfig destination."""

    def __init__(self, config: DeploymentConfig,
                 log: Callable[[str], None],
                 binary_path: Optional[Path] = None,
                 dry_run: bool = False,
                 auto_fix_host_keys: bool = False):
        self.config = config
        self._log = log
        self.binary_path = binary_path
        self.dry_run = dry_run
        # When True, a "REMOTE HOST IDENTIFICATION HAS CHANGED" failure
        # triggers a single ``ssh-keygen -R <host>`` + retry. Wired from
        # Settings.auto_fix_stale_host_keys; the SSH warning itself is
        # still logged in full before any auto-fix runs.
        self.auto_fix_host_keys = auto_fix_host_keys
        self.is_local = config.host in ("localhost", "127.0.0.1", "::1")
        self.name = config.name or config.host

        # Build SSH/SCP base commands for remote
        if not self.is_local:
            self._ssh_base = ["ssh", "-p", str(config.port)] + SSH_OPTS
            self._scp_base = ["scp", "-p", "-P", str(config.port)] + SSH_OPTS
            if config.key:
                self._ssh_base += ["-i", config.key]
                self._scp_base += ["-i", config.key]
            self._ssh_target = f"{config.user}@{config.host}"

            self._sshpass = []
            if config.key and config.password:
                # SSH key with passphrase: use sshpass -P "Enter passphrase" to feed it
                if shutil.which("sshpass"):
                    self._sshpass = ["sshpass", "-P", "Enter passphrase", "-p", config.password]
                else:
                    self._log("  [yellow]sshpass not installed — key passphrase must be entered manually or use ssh-agent[/yellow]")
            elif not config.key and config.password:
                # No key, password auth: use sshpass for login password
                if shutil.which("sshpass"):
                    self._sshpass = ["sshpass", "-p", config.password]
                else:
                    self._log("  [red]sshpass not installed — cannot use password auth[/red]")

    # -- SSH helpers ----------------------------------------------------------

    def _ssh(self, remote_cmd: str, timeout: int = 10) -> subprocess.CompletedProcess:
        """Run a command on the remote host via SSH."""
        return subprocess.run(
            self._sshpass + self._ssh_base + [self._ssh_target, remote_cmd],
            capture_output=True, text=True, timeout=timeout,
        )

    # SCP mode table — name → extra argv passed to scp. The order is
    # the fallback chain used by automatic mode and by last_working
    # when the cache is cold or stale.
    _SCP_MODES = (
        ("default", []),
        ("sftp", ["-s"]),
        ("scp_protocol", ["-O"]),
    )
    _SCP_FAIL_LOG = {
        "default": "  [yellow]SCP failed, trying SFTP mode (-s)...[/yellow]",
        "sftp": "  [yellow]SFTP failed, trying SCP protocol (-O)...[/yellow]",
    }

    def _scp(self, local: str, remote: str) -> subprocess.CompletedProcess:
        """SCP deploy with 3-tier fallback (default → SFTP → SCP protocol).

        When ``config.scp_mode == "last_working"`` (default) the cached
        mode from the previous success is attempted first and the full
        chain only runs on failure; whatever ultimately works is written
        back to ``config.last_working_scp_mode`` so the caller can persist
        it. ``"automatic"`` skips the cache and always runs the chain.
        """
        cfg = self.config
        scp_mode = getattr(cfg, "scp_mode", "last_working") or "last_working"
        cached = getattr(cfg, "last_working_scp_mode", "") or ""

        def _run(extra_args: list[str]) -> subprocess.CompletedProcess:
            cmd = self._sshpass + self._scp_base + extra_args + [
                local, f"{self._ssh_target}:{remote}"
            ]
            return subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        mode_map = dict(self._SCP_MODES)

        # Last-working fast path: try the cached mode first.
        if scp_mode == "last_working" and cached in mode_map:
            result = _run(mode_map[cached])
            if result.returncode == 0:
                return result
            # Cache was stale — fall through to the full chain and let
            # it overwrite the cache with whatever actually works now.
            self._log(
                f"  [yellow]Cached SCP mode '{cached}' failed, retrying full chain...[/yellow]"
            )

        # Full fallback chain, always runs for "automatic" and as the
        # fallback for "last_working".
        result = None
        for name, extra in self._SCP_MODES:
            if result is not None and result.returncode != 0:
                # Previous attempt failed; announce next step with the
                # historical wording so existing log readers still parse.
                prev = self._SCP_MODES[self._SCP_MODES.index((name, extra)) - 1][0]
                msg = self._SCP_FAIL_LOG.get(prev)
                if msg:
                    self._log(msg)
            result = _run(extra)
            if result.returncode == 0:
                if scp_mode == "last_working" and cached != name:
                    cfg.last_working_scp_mode = name
                return result
        return result  # type: ignore[return-value]

    def _run_local(self, cmd: str, timeout: int = 10) -> subprocess.CompletedProcess:
        """Run a command locally."""
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

    # -- Steps ----------------------------------------------------------------

    def step_connect(self) -> bool:
        """Step 1: Test SSH connection (remote) or confirm local mode.
        Returns False if connection failed."""
        if self.is_local:
            self._log("[bold]1. Local mode (no SSH)[/bold]")
            self._log("  [green]Host is localhost — using file copy[/green]")
            return True

        self._log("[bold]1. SSH connection... [dim](timeout: 10s)[/dim][/bold]")
        if self._sshpass and not shutil.which("sshpass"):
            self._log("  [red]sshpass not installed[/red]")
            return False
        # allow_auto_fix flips off after one retry so a still-failing
        # host key doesn't loop.
        return self._try_ssh_connect(allow_auto_fix=self.auto_fix_host_keys)

    def _try_ssh_connect(self, allow_auto_fix: bool) -> bool:
        """Run a single ``ssh … echo connected`` probe; on a host-key
        mismatch AND when ``allow_auto_fix`` is set, drop the stale
        known_hosts entry and retry ONCE. Original SSH stderr is always
        logged verbatim first so the security signal never gets hidden.
        """
        try:
            result = subprocess.run(
                self._sshpass + self._ssh_base + [self._ssh_target, "echo connected"],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            self._log("  [red]Connection timeout[/red]")
            return False
        except FileNotFoundError:
            self._log("  [red]ssh not found[/red]")
            return False
        except Exception as e:
            self._log(f"  [red]Error: {e}[/red]")
            return False

        if result.returncode == 0:
            self._log("  [green]Connected successfully[/green]")
            return True

        # Preserve every stderr line the ssh client emitted — the
        # last line is often terse ("Permission denied") while the
        # actual cause (host-key change, wrong key, unreachable
        # port …) sits several lines above with the fix-it hint.
        stderr = (result.stderr or "").rstrip()
        lines = [ln.rstrip() for ln in stderr.splitlines() if ln.strip()] if stderr else []
        if not lines:
            self._log("  [red]Connection failed: unknown error[/red]")
            return False

        self._log(f"  [red]Connection failed:[/red]")
        for ln in lines:
            self._log(f"    [red]{rich_escape(ln)}[/red]")

        host_key_changed = (
            "REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr
            or "Host key verification failed" in stderr
        )
        if not host_key_changed:
            return False

        target = (
            f"[{self.config.host}]:{self.config.port}"
            if self.config.port != 22 else self.config.host
        )
        self._log(
            "  [yellow]Host key changed. "
            "If you trust this box, drop the stale key with:[/yellow]"
        )
        self._log(f"    [bold]ssh-keygen -R {target}[/bold]")

        if not allow_auto_fix:
            # Warning already surfaced, hint printed — user's turn.
            return False

        # Setting says "trust me, just fix it". Log the auto-fix
        # explicitly (the warning above still stands as the audit trail).
        self._log(
            f"  [yellow]Auto-fix enabled: running "
            f"[bold]ssh-keygen -R {target}[/bold][/yellow]"
        )
        try:
            fix = subprocess.run(
                ["ssh-keygen", "-R", target],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            self._log(f"  [red]ssh-keygen -R failed: {rich_escape(str(e))}[/red]")
            return False
        # ssh-keygen -R is chatty on both stdout and stderr; log the
        # useful lines so the audit trail shows what got removed.
        fix_out = ((fix.stdout or "") + (fix.stderr or "")).strip()
        for ln in [l for l in fix_out.splitlines() if l.strip()]:
            self._log(f"    [dim]{rich_escape(ln)}[/dim]")
        if fix.returncode != 0:
            self._log(
                f"  [red]ssh-keygen -R exit {fix.returncode} — "
                f"cannot retry connection[/red]"
            )
            return False

        self._log("  [yellow]Retrying SSH connection...[/yellow]")
        return self._try_ssh_connect(allow_auto_fix=False)

    def step_system_info(self) -> None:
        """Step 2: Show remote/local system information."""
        if self.is_local:
            import platform
            self._log("[bold]2. Local system info...[/bold]")
            self._log(f"  [dim]{platform.node()} {platform.system()} {platform.release()} {platform.machine()}[/dim]")
            try:
                total, used, free = shutil.disk_usage(
                    Path(self.config.target_path).parent
                    if Path(self.config.target_path).parent.exists() else "/"
                )
                self._log(f"  [dim]Disk: {total // (1024**3)}G total, {used // (1024**3)}G used, {free // (1024**3)}G free[/dim]")
            except Exception:
                pass
            return

        self._log("[bold]2. Remote system info... [dim](timeout: 5s)[/dim][/bold]")
        try:
            info_cmd = (
                "uname -snrm 2>/dev/null; "
                "cat /etc/os-release 2>/dev/null | head -2; "
                f"df -h $(dirname '{self.config.target_path}') 2>/dev/null | tail -1"
            )
            result = self._ssh(info_cmd, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    self._log(f"  [dim]{rich_escape(line)}[/dim]")
            else:
                self._log("  [dim](could not retrieve)[/dim]")
        except Exception:
            self._log("  [dim](could not retrieve)[/dim]")

    def step_check_binary(self) -> None:
        """Step 3: Check if target binary exists and is running."""
        path = self.config.target_path
        self._log(f"[bold]3. Target binary: {path} [dim](timeout: 5s)[/dim][/bold]")

        if self.is_local:
            target = Path(path)
            if target.exists():
                self._log(f"  [green]File exists ({target.stat().st_size} bytes)[/green]")
                try:
                    result = subprocess.run(
                        ["pidof", target.name], capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        self._log(f"  [green]RUNNING pid={result.stdout.strip()}[/green]")
                    else:
                        self._log("  [yellow]NOT RUNNING[/yellow]")
                except Exception:
                    self._log("  [dim](could not check running state)[/dim]")
            else:
                self._log("  [yellow]File not found (first deploy)[/yellow]")
            return

        try:
            check_cmd = (
                f"if [ -f '{path}' ]; then "
                f"  echo 'EXISTS'; ls -la '{path}'; "
                f"  pid=$(pidof $(basename '{path}') 2>/dev/null); "
                f"  if [ -n \"$pid\" ]; then echo \"RUNNING pid=$pid\"; else echo 'NOT RUNNING'; fi; "
                f"else echo 'NOT FOUND'; fi"
            )
            result = self._ssh(check_cmd, timeout=5)
            output = result.stdout.strip()
            if "EXISTS" in output:
                self._log("  [green]File exists[/green]")
                for line in output.splitlines():
                    if line.startswith("-") or line.startswith("l"):
                        self._log(f"  [dim]{rich_escape(line)}[/dim]")
                    elif "RUNNING" in line:
                        self._log(f"  [green]{rich_escape(line)}[/green]")
                    elif "NOT RUNNING" in line:
                        self._log(f"  [yellow]{rich_escape(line)}[/yellow]")
            else:
                self._log("  [yellow]File not found (first deploy)[/yellow]")
        except Exception as e:
            self._log(f"  [red]Error: {e}[/red]")

    def _mask_password(self, text: str) -> str:
        """Replace any occurrence of su_password or ssh password in text with ***."""
        result = text
        if self.config.su_password:
            result = result.replace(self.config.su_password, "***")
        if self.config.password:
            result = result.replace(self.config.password, "***")
        return result

    def _wrap_su(self, cmd: str) -> tuple[str, str]:
        """Wrap a command with su -c if su_password is set.

        Returns (actual_cmd, display_cmd) where display_cmd has the password masked.
        """
        su_pwd = self.config.su_password
        if not su_pwd:
            return cmd, cmd
        # Escape single quotes in the command for su -c '...'
        escaped = cmd.replace("'", "'\\''")
        actual = f"echo '{su_pwd}' | su -c '{escaped}'"
        display = f"echo '***' | su -c '{escaped}'"
        return actual, display

    def step_pre_install(self) -> None:
        """Step 4: Execute pre deploy commands."""
        cmd = self.config.pre_cmd
        if not cmd:
            self._log("[bold]4. Pre deploy commands: [dim](none)[/dim][/bold]")
            return

        self._log("[bold]4. Pre deploy commands... [dim](timeout: 10s)[/dim][/bold]")

        # Warn about background commands in pre-deploy — they may race with the deploy
        stripped = cmd.strip()
        if stripped.endswith("&") or stripped.startswith("nohup "):
            self._log("  [yellow]Warning: background command in pre-deploy may race with deploy[/yellow]")
        actual_cmd, display_cmd = self._wrap_su(cmd)
        self._log(f"  [dim]$ {display_cmd}[/dim]")
        try:
            if self.is_local:
                result = self._run_local(actual_cmd, timeout=10)
            else:
                result = self._ssh(actual_cmd, timeout=10)
            if result.returncode == 0:
                if result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        self._log(f"  {rich_escape(self._mask_password(line))}")
                self._log("  [green]OK (exit 0)[/green]")
            else:
                err = result.stderr.strip() or result.stdout.strip()
                self._log(f"  [red]Failed (exit {result.returncode}): {rich_escape(self._mask_password(err))}[/red]")
        except Exception as e:
            self._log(f"  [red]Error: {rich_escape(self._mask_password(str(e)))}[/red]")

    def step_backup_target(self) -> None:
        """Step 4.5: copy the existing target binary to ``<target_path>.bak``
        before the fresh one replaces it. No-op when the deployment's
        ``target_backup`` flag is off or when nothing is yet at
        ``target_path``. Failures are logged yellow but don't abort the
        deploy — a missing ``.bak`` is not worth blocking a working
        rollout over."""
        if not getattr(self.config, "target_backup", False):
            return
        path = self.config.target_path
        if self.dry_run:
            self._log("[bold]4b. Backup target binary (dry-run)[/bold]")
            if self.is_local:
                self._log(f"  [dim]$ cp -a {path} {path}.bak[/dim]")
            else:
                self._log(f"  [dim]$ ssh {self._ssh_target} cp -a {path} {path}.bak[/dim]")
            return
        self._log(f"[bold]4b. Backup target binary... [dim]({path}.bak)[/dim][/bold]")
        cmd = f"test -e '{path}' && cp -a '{path}' '{path}.bak'"
        # Respect the su-wrap pattern the pre/post command steps use
        # so writing to privileged locations (e.g. /usr/bin/oscam)
        # works the same way here.
        actual_cmd, display_cmd = self._wrap_su(cmd)
        self._log(f"  [dim]$ {display_cmd}[/dim]")
        try:
            if self.is_local:
                result = self._run_local(actual_cmd, timeout=15)
            else:
                result = self._ssh(actual_cmd, timeout=15)
            if result.returncode == 0:
                self._log(f"  [green]Saved: {path}.bak[/green]")
            elif result.returncode == 1:
                # test -e failed — nothing at target yet, first deploy
                self._log(f"  [dim]skip: no existing binary at {path}[/dim]")
            else:
                err = (result.stderr or result.stdout or "").strip() or "unknown"
                self._log(
                    f"  [yellow]Backup failed (exit {result.returncode}): "
                    f"{rich_escape(self._mask_password(err))} "
                    f"— continuing with deploy[/yellow]"
                )
        except Exception as e:
            self._log(
                f"  [yellow]Backup error: "
                f"{rich_escape(self._mask_password(str(e)))}"
                f" — continuing with deploy[/yellow]"
            )

    def step_deploy(self) -> bool:
        """Step 5: Deploy/copy the binary. Returns True on success."""
        path = self.config.target_path

        if self.dry_run or self.binary_path is None:
            self._log("[bold]5. Deploy (dry-run)... [dim](timeout: 60s)[/dim][/bold]")
            if self.is_local:
                self._log(f"  [dim]$ cp <binary> {path}[/dim]")
            else:
                scp_str = " ".join(self._sshpass + self._scp_base + ["<binary>", f"{self._ssh_target}:{path}"])
                self._log(f"  [dim]$ {scp_str}[/dim]")
            self._log("  [yellow]Skipped (dry-run)[/yellow]")
            return True

        binary_name = self.binary_path.name
        self._log(f"[bold]5. Deploy: {binary_name}... [dim](timeout: 60s)[/dim][/bold]")

        if self.is_local:
            try:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(self.binary_path), str(target))
                target.chmod(target.stat().st_mode | 0o111)
                self._log("  [green]Copy OK[/green]")
                return True
            except Exception as e:
                self._log(f"  [red]Copy failed: {e}[/red]")
                return False

        # Remote SCP
        try:
            result = self._scp(str(self.binary_path), path)
            if result.returncode == 0:
                self._log("  [green]Deploy OK[/green]")
                # chmod +x
                try:
                    self._ssh(f"chmod +x '{path}'", timeout=10)
                except Exception:
                    pass
                return True
            else:
                err = result.stderr.strip() or "unknown error"
                self._log(f"  [red]Deploy failed: {rich_escape(err)}[/red]")
                return False
        except subprocess.TimeoutExpired:
            self._log("  [red]Deploy timeout (120s)[/red]")
            return False
        except Exception as e:
            self._log(f"  [red]Deploy error: {e}[/red]")
            return False

    def step_post_install(self) -> None:
        """Step 6: Execute post deploy commands."""
        cmd = self.config.post_cmd
        if not cmd:
            self._log("[bold]6. Post deploy commands: [dim](none)[/dim][/bold]")
            return

        self._log("[bold]6. Post deploy commands... [dim](timeout: 10s)[/dim][/bold]")
        actual_cmd, display_cmd = self._wrap_su(cmd)
        self._log(f"  [dim]$ {display_cmd}[/dim]")
        try:
            if self.is_local:
                result = self._run_local(actual_cmd, timeout=10)
            else:
                result = self._ssh(actual_cmd, timeout=10)
            if result.returncode == 0:
                if result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        self._log(f"  {rich_escape(self._mask_password(line))}")
                self._log("  [green]OK (exit 0)[/green]")
            else:
                err = result.stderr.strip() or result.stdout.strip()
                self._log(f"  [red]Failed (exit {result.returncode}): {rich_escape(self._mask_password(err))}[/red]")
        except Exception as e:
            self._log(f"  [red]Error: {rich_escape(self._mask_password(str(e)))}[/red]")

    # -- Full run -------------------------------------------------------------

    def run(self, verbose: bool = True) -> bool:
        """Execute deployment steps. Returns True if deployment succeeded.

        When *verbose* is True (default, used by F9 Deployment modal),
        system info and binary check steps are included. When False
        (used by Phase 5 during build), they are skipped for speed.
        """
        if not self.step_connect():
            return False
        if verbose:
            self.step_system_info()
            self.step_check_binary()
        self.step_pre_install()
        self.step_backup_target()
        ok = self.step_deploy()
        self.step_post_install()
        self._log(f"  [green]Done: {self.name}[/green]")
        return ok


# Backward compat alias
Uploader = Deployer
