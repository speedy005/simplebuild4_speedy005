"""Build engine for oscam — manages the complete build pipeline."""

from __future__ import annotations

import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core.env import deliver_link
from core.sysroot import read_linux_version
from core.target import Target, SourceConfig, ToolchainConfig


class BuildEngine:
    """Executes the oscam build pipeline for a target configuration.

    Each build runs through phases:
    - PHASE 1: Prepare Repository (update, reset)
    - PHASE 2: Apply Patches
    - PHASE 3: Configure (config.sh modules)
    - PHASE 4: Build + Post-Build (make distclean, make, save artifacts)
    - PHASE 5: Deploy (SSH/local)
    """

    def __init__(
        self,
        target: Target,
        repos_dir: Path,
        backup_dir: Path,
        repos_config: Path,
        patches_dir: Path,
        logs_dir: Path,
        binaries_dir: Path,
        bin_dir: Path,
        toolchains_dir: Path,
        toolchains_local: Path,
        toolchains_defaults: Path,
        max_cpus: int = 0,
        compiler_opt: str = "",
        sign_cert: str = "",
        sign_key: str = "",
        delete_debug: bool = True,
        save_smargo: bool = True,
        patch_webif: bool = True,
        qemu_test: bool = False,
        qemu_timeout: int = 5,
        qemu_expect: str = "Server running",
        qemu_run_unit_tests: bool = False,
        build_overrides: str = "",
        log_max_age_days: int = 0,
        log_max_count: int = 0,
        log_max_size_mb: int = 0,
        organize_logs: bool = False,
        default_confdir: str = "/etc/tuxbox/config",
        default_build_system: str = "make",
        auto_fix_stale_host_keys: bool = False,
        log: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        write_log_file: bool = True,
    ):
        self.target = target
        self.repos_dir = repos_dir
        self.backup_dir = backup_dir
        self.repos_config = repos_config
        self.patches_dir = patches_dir
        self.logs_dir = logs_dir
        self.binaries_dir = binaries_dir
        self.bin_dir = bin_dir
        self.toolchains_dir = toolchains_dir
        self.toolchains_local = toolchains_local
        self.toolchains_defaults = toolchains_defaults
        self.max_cpus = max_cpus or os.cpu_count() or 4
        self.compiler_opt = compiler_opt
        self.sign_cert = sign_cert
        self.sign_key = sign_key
        self.delete_debug = delete_debug
        self.save_smargo = save_smargo
        self.patch_webif = patch_webif
        self.qemu_test = qemu_test
        self.qemu_timeout = qemu_timeout
        self.qemu_expect = qemu_expect
        self.qemu_run_unit_tests = qemu_run_unit_tests
        self.build_overrides = build_overrides
        self.log_max_age_days = log_max_age_days
        self.log_max_count = log_max_count
        self.log_max_size_mb = log_max_size_mb
        self._log = log or (lambda msg: None)
        self._cancel = cancel_check or (lambda: False)
        self._log_file = None  # set in build()
        self._write_log_file = write_log_file
        self._organize_logs = organize_logs
        self._default_confdir = default_confdir
        self._default_build_system = default_build_system
        self._auto_fix_stale_host_keys = auto_fix_stale_host_keys
        self._warning_count = 0  # per-build warning counter

    def _log_file_only(self, msg: str) -> None:
        """Write a line only to the log file, not to the GUI."""
        if self._log_file:
            self._log_file.write(msg + "\n")
            self._log_file.flush()


    def build(self) -> bool:
        """Run the full build pipeline for all source x toolchain combinations."""
        import re

        # Setup build log file (skipped when invoked from automation —
        # the job log already captures everything via the callback).
        log_path = None
        if self._write_log_file:
            from core.log_cleanup import resolve_log_dir
            _log_dir = resolve_log_dir(self.logs_dir, "build", self._organize_logs)
            timestamp = datetime.now().strftime("%Y-%m-%d.%H-%M-%S")
            bs = self.target.build.build_system or self._default_build_system
            log_path = _log_dir / f"{timestamp}.{self.target.name}.build-{bs}.log"
            self._log_file = open(log_path, "w", encoding="utf-8")
            original_log = self._log

            def _log_both(msg: str):
                # Strip Rich markup for file
                plain = re.sub(r'\[/?[a-z_ #0-9]+\]', '', msg)
                if self._log_file is not None:
                    try:
                        self._log_file.write(plain + "\n")
                        self._log_file.flush()
                    except (ValueError, OSError):
                        pass
                original_log(msg)

            self._log = _log_both

        try:
            return self._do_build(log_path)
        finally:
            self._close_log(log_path)

    def _setup_upx_symlink(self) -> None:
        """Create/update data/bin/upx symlink based on target's upx_binary setting.

        - If use_compress is False or no UPX is configured: remove the symlink.
        - If a custom upx_binary is set: link to it.
        - Otherwise: link to system upx (which upx).
        """
        import shutil as _shutil
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        upx_link = self.bin_dir / "upx"

        def _remove_link():
            if upx_link.is_symlink() or upx_link.exists():
                try:
                    upx_link.unlink()
                except Exception:
                    pass

        # If UPX is not used at all, remove any stale symlink
        if not self.target.build.use_compress:
            _remove_link()
            return

        # Determine target path: custom or system default
        upx_target = self.target.build.upx_binary.strip() if self.target.build.upx_binary else ""
        if not upx_target:
            upx_target = _shutil.which("upx") or ""

        if not upx_target:
            self._log("[yellow]USE_COMPRESS=1 but no upx binary found — disabling symlink[/yellow]")
            _remove_link()
            return

        # Validate target exists
        upx_target_path = Path(upx_target)
        if not upx_target_path.exists():
            self._log(f"[yellow]upx binary not found: {upx_target}[/yellow]")
            _remove_link()
            return

        upx_target_resolved = upx_target_path.resolve()

        # If link already correct, skip
        if upx_link.is_symlink():
            try:
                if upx_link.resolve() == upx_target_resolved:
                    self._log(f"  upx: [dim]{upx_link} → {upx_target_resolved}[/dim]")
                    return
            except Exception:
                pass

        # Recreate the link
        _remove_link()
        try:
            upx_link.symlink_to(upx_target_resolved)
            self._log(f"  upx: [dim]{upx_link} → {upx_target_resolved}[/dim]")
        except Exception as e:
            self._log(f"[yellow]Failed to create upx symlink: {e}[/yellow]")

    def _do_build(self, log_path: Path) -> bool:
        """Internal build logic."""
        sources = self.target.sources
        toolchains = self.target.toolchains

        if not sources:
            self._log("[red]No sources configured[/red]")
            return False
        if not toolchains:
            self._log("[red]No toolchains configured[/red]")
            return False

        # Setup UPX symlink and prepend data/bin to PATH
        self._setup_upx_symlink()
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"

        total = len(sources) * len(toolchains)
        self._log(f"[bold]Building {self.target.name}: {len(sources)} source(s) x {len(toolchains)} toolchain(s) = {total} build(s)[/bold]")
        self._log("")

        # Resolve repo directories — auto-clone if missing
        repo_dirs: dict[str, Path] = {}
        for src in sources:
            repo_dir = self._find_repo_dir(src.name)
            if not repo_dir:
                # Try to clone the repo
                repo_dir = self._auto_clone_repo(src.name)
                if not repo_dir:
                    self._log(f"[red]Repository '{src.name}' not found — skipping[/red]")
            if repo_dir:
                repo_dirs[src.name] = repo_dir

        if not repo_dirs:
            self._log("[red]No repositories found[/red]")
            return False

        # PHASE 1: Prepare all repositories
        self._log("[bold]PHASE 1: Prepare Repositories[/bold]")
        phase1_ok = True
        for src in sources:
            if src.name not in repo_dirs:
                continue
            if not self._phase1_prepare_repo(src, repo_dirs[src.name]):
                phase1_ok = False
            if self._cancel():
                self._log("[yellow]Build cancelled[/yellow]")
                return False

        if not phase1_ok:
            self._log("[red]PHASE 1 failed — aborting[/red]")
            return False

        # PHASE 2: Apply patches to all repositories
        self._log("")
        self._log("[bold]PHASE 2: Apply Patches[/bold]")
        phase2_ok = True
        for src in sources:
            if src.name not in repo_dirs:
                continue
            if not self._phase2_apply_patches(src, repo_dirs[src.name]):
                phase2_ok = False
            if self._cancel():
                self._log("[yellow]Build cancelled[/yellow]")
                return False

        if not phase2_ok:
            self._log("[red]PHASE 2 failed — aborting[/red]")
            return False

        # PHASE 3: Configure oscam for all repositories
        self._log("")
        self._log("[bold]PHASE 3: Configure oscam[/bold]")
        phase3_ok = True
        for src in sources:
            if src.name not in repo_dirs:
                continue
            if not self._phase3_configure(src, repo_dirs[src.name]):
                phase3_ok = False
            if self._cancel():
                self._log("[yellow]Build cancelled[/yellow]")
                return False

        if not phase3_ok:
            self._log("[red]PHASE 3 failed — aborting[/red]")
            return False

        # Signing: add certificate to repos if WITH_SIGNING enabled
        if self.sign_cert and self.sign_key:
            for src in sources:
                if src.name not in repo_dirs:
                    continue
                repo_dir = repo_dirs[src.name]
                config_sh = repo_dir / "config.sh"
                if config_sh.exists():
                    from core.oscam_config import _run_config_sh
                    is_signing = _run_config_sh(config_sh, ["--enabled", "WITH_SIGNING"])
                    if is_signing == "Y":
                        self._log(f"  Adding signing certificate to '{src.name}'...")
                        result = _run_config_sh(config_sh, ["--add-cert", self.sign_cert, self.sign_key])
                        if result is not None:
                            self._log(f"  [green]Certificate added[/green]")
                        else:
                            self._log(f"  [yellow]Warning: could not add certificate[/yellow]")

        # Load toolchain registry for PHASE 4
        from core.toolchain import ToolchainRegistry
        tc_registry = ToolchainRegistry(self.toolchains_defaults, self.toolchains_local, self.toolchains_dir)
        tc_registry.load()

        # PHASE 4+5: Build and Post-Build for each source x toolchain
        self._log("")
        self._log("[bold]PHASE 4: Build[/bold]")
        success_count = 0
        fail_count = 0
        total_warnings = 0
        for src in sources:
            if src.name not in repo_dirs:
                fail_count += len(toolchains)
                continue
            for tcc in toolchains:
                if self._cancel():
                    self._log("[yellow]Build cancelled[/yellow]")
                    return False
                tc = tc_registry.get(tcc.name)
                if not tc:
                    self._log(f"  [red]Toolchain '{tcc.name}' not found — skipping[/red]")
                    fail_count += 1
                    continue
                bin_path = self._phase4_build(src, repo_dirs[src.name], tcc, tc)
                total_warnings += self._warning_count
                if bin_path:
                    # PHASE 5: Deploy matching destinations
                    deploy_ok = self._phase5_deploy(src, tcc, bin_path)
                    if deploy_ok:
                        success_count += 1
                    else:
                        fail_count += 1
                else:
                    fail_count += 1

        self._log(f"\n[bold]Build complete: {success_count} succeeded, {total_warnings} warning{'s' if total_warnings != 1 else ''}, {fail_count} failed[/bold]")
        return fail_count == 0

    # -------------------------------------------------------------------------
    # Post-Build (runs immediately after each make, before next distclean)
    # -------------------------------------------------------------------------

    def _postbuild_single(self, bin_path: Path, repo_dir: Path,
                          cross: str = "", sysroot: str = "", is_native: bool = True,
                          tc_dir: Path | None = None) -> None:
        """Post-build tasks for a single build — runs immediately after each make.

        Must happen before distclean of the next build, otherwise Distribution/
        artifacts (list_smargo, .debug) would be deleted.
        """
        # Harvest any freshly-downloaded mbedTLS tarball from the repo's
        # local cache into s4's central downloads dir (and replace with
        # a symlink). Next build reuses it, and ``git clean`` or a repo
        # reset doesn't lose the cached copy.
        self._mbedtls_tarball_cache_sync(repo_dir, pre=False)

        # Delete debug binary
        debug_bin = Path(f"{bin_path}.debug")
        if self.delete_debug and debug_bin.exists():
            debug_bin.unlink()
            self._log(f"Removed: {debug_bin.name}")

        # Save list_smargo (built by make when USE_LIBUSB is active)
        if self.save_smargo:
            dist_dir = repo_dir / "Distribution"
            if dist_dir.is_dir():
                import shutil
                for smargo in list(dist_dir.glob("list_smargo*")):
                    dest = self.binaries_dir / smargo.name
                    shutil.move(str(smargo), str(dest))
                    self._log(f"Saved: {smargo.name}")

        # Smoke test: QEMU for cross, direct for native.
        # Skip for Android — binaries require /system/bin/linker64 which
        # is part of the Android OS, not the NDK. QEMU can't run them.
        if self.qemu_test:
            if cross and "android" in cross.lower():
                self._log("[dim]QEMU test skipped (Android binaries need /system/bin/linker)[/dim]")
            elif is_native:
                self._native_smoke_test(bin_path, tc_dir)
            elif cross:
                self._qemu_smoke_test(bin_path, cross, sysroot, tc_dir)

        # Unit tests: run oscam's ``tests.bin`` via qemu-user (or
        # native) if the user opted in via F1 → QEMU Post-Build Test.
        # make path: ``make tests`` — oscam's rule already routes
        # the test-mode objects into ``build-tests/`` internally.
        # cmake path: second configure/build with ``-DBUILD_TESTS=1``
        # into a separate ``build_s4_tests/`` directory.
        if self.qemu_run_unit_tests:
            if cross and "android" in cross.lower():
                self._log("[dim]Unit tests skipped (Android cross-compile)[/dim]")
            else:
                bs = (self.target.build.build_system or "make").lower()
                if bs == "cmake":
                    self._run_unit_tests_cmake(
                        repo_dir, cross, sysroot, is_native, tc_dir,
                    )
                else:
                    self._run_unit_tests(
                        repo_dir, cross, sysroot, is_native, tc_dir,
                    )

    def _native_smoke_test(self, bin_path: Path, tc_dir: Path | None = None) -> None:
        """Run a smoke test on a native binary (direct execution)."""
        import time
        import threading

        timeout = self.qemu_timeout
        expect = self.qemu_expect

        self._log(f"[cyan]TEST -------> [/cyan]testing {bin_path} ([dark_orange]{timeout}s timeout[/dark_orange])")

        # Add local native libs to LD_LIBRARY_PATH so dynamically linked
        # libraries from data/toolchains/native/lib are found at runtime
        env = os.environ.copy()
        if tc_dir:
            local_lib = tc_dir / "lib"
            if local_lib.is_dir():
                old = env.get("LD_LIBRARY_PATH", "")
                env["LD_LIBRARY_PATH"] = f"{local_lib}:{old}" if old else str(local_lib)

        native_cmd = [str(bin_path), "-c", "/tmp/"]
        ld_info = f"LD_LIBRARY_PATH={env.get('LD_LIBRARY_PATH', '')} " if env.get("LD_LIBRARY_PATH") else ""
        self._log(f"[dim]          $ {ld_info}{' '.join(native_cmd)}[/dim]")

        try:
            proc = subprocess.Popen(
                native_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                preexec_fn=os.setsid,
                env=env,
            )

            found = False

            def reader():
                nonlocal found
                for line in proc.stdout:
                    stripped = line.rstrip()
                    if stripped:
                        self._log(f"          {stripped}")
                    if expect and expect in stripped:
                        found = True
                        return

            t = threading.Thread(target=reader, daemon=True)
            t.start()

            start = time.time()
            while time.time() - start < timeout:
                if found or self._cancel():
                    break
                time.sleep(0.2)

            try:
                os.killpg(os.getpgid(proc.pid), 9)
                proc.wait(timeout=2)
            except Exception:
                pass

            if self._cancel():
                self._log("[yellow]TEST -------> CANCELLED[/yellow]")
            elif found:
                self._log(f"[green]TEST -------> PASS ({expect} found)[/green]")
            else:
                self._log(f"[red]TEST -------> FAIL ({expect} not found within {timeout}s)[/red]")

        except Exception as e:
            self._log(f"[red]TEST -------> ERROR: {e}[/red]")

    def _qemu_uname_env(self, sysroot: str) -> dict[str, str] | None:
        """Build an environment dict with ``QEMU_UNAME`` set to the
        sysroot's kernel version, or ``None`` if it could not be
        determined (caller falls back to the inherited environment).

        Works around qemu-user's hardcoded ``uname()`` kernel version
        which can be older than what the sysroot's glibc was built to
        expect; without the override, target binaries abort with
        ``FATAL: kernel too old`` even on a recent host kernel.
        """
        kernelv = read_linux_version(Path(sysroot.rstrip("/")))
        if not kernelv:
            return None
        env = os.environ.copy()
        env["QEMU_UNAME"] = kernelv
        return env

    def _qemu_smoke_test(self, bin_path: Path, cross: str, sysroot: str, tc_dir: Path | None = None) -> None:
        """Run a QEMU smoke test on a cross-compiled binary (like s3).

        Starts the binary under qemu-<arch>, waits for expected output string,
        kills after timeout. PASS if expected string found, FAIL otherwise.
        """
        import shutil as _shutil
        import time
        import threading

        # Determine qemu arch from cross-compiler's target
        _QEMU_ARCH_MAP = {
            "arm": "arm", "armv7": "arm", "armv7l": "arm", "armv7ve": "arm",
            "armv7a": "arm", "armv7ahf": "arm", "armv6": "arm", "armv5te": "arm",
            "armv8": "arm", "armv8l": "arm",
            "aarch64": "aarch64", "arm64": "aarch64", "armv8a": "aarch64", "armv8_64": "aarch64",
            "mipsel": "mipsel", "mips32el": "mipsel",
            "mips": "mips",
            "i686": "i386", "i386": "i386",
            "x86_64": "x86_64",
            "riscv64": "riscv64",
            "sh4": "sh4",
        }

        try:
            dumpmachine = subprocess.run(
                [f"{cross}gcc", "-dumpmachine"], capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            raw_arch = dumpmachine.split("-")[0]
            qemu_arch = _QEMU_ARCH_MAP.get(raw_arch, raw_arch)
        except Exception:
            self._log("[yellow]QEMU -------> SKIP (cannot determine arch)[/yellow]")
            return

        # Prepare sysroot (strip trailing /usr)
        qemu_sysroot = sysroot.rstrip("/")
        if qemu_sysroot.endswith("/usr"):
            qemu_sysroot = qemu_sysroot[:-4]

        # Same-arch: qemu-user can't properly isolate the sysroot on the
        # same architecture — the host kernel uses the ELF interpreter path
        # directly, bypassing -L.  Run the binary via the toolchain's own
        # ld-linux with --library-path instead.
        import platform as _platform
        host_arch = _platform.machine()  # e.g. "x86_64", "aarch64"
        host_qemu = _QEMU_ARCH_MAP.get(host_arch, host_arch)
        same_arch = (qemu_arch == host_qemu)

        if same_arch:
            # Find the toolchain's dynamic linker in the sysroot
            sysroot_lib = Path(qemu_sysroot) / "lib"
            ld_candidates = list(sysroot_lib.glob("ld-linux*")) + list(sysroot_lib.glob("ld-*.so*"))
            ld_bin = None
            for candidate in ld_candidates:
                if candidate.is_file() or candidate.is_symlink():
                    resolved = candidate.resolve()
                    if resolved.exists() and os.access(str(resolved), os.X_OK):
                        ld_bin = resolved
                        break
            if not ld_bin:
                self._log(f"[yellow]QEMU -------> SKIP (no ld-linux found in {sysroot_lib})[/yellow]")
                return
            # Build library path from all lib dirs in sysroot (lib + usr/lib)
            lib_paths = []
            for subdir in ["lib", "usr/lib"]:
                p = Path(qemu_sysroot) / subdir
                if p.is_dir():
                    lib_paths.append(str(p))
            lib_path_str = ":".join(lib_paths) if lib_paths else str(sysroot_lib)
            cmd = [str(ld_bin), "--library-path", lib_path_str, str(bin_path), "-c", "/tmp/"]
        else:
            qemu_bin = f"qemu-{qemu_arch}"
            if not _shutil.which(qemu_bin):
                self._log(f"[yellow]QEMU -------> SKIP ({qemu_bin} not found)[/yellow]")
                return
            # Add toolchain prefix lib dir so QEMU finds cross-built shared libs
            # (e.g. libcrypto, libusb) that live outside the gcc sysroot
            extra_lib_dirs = []
            if tc_dir:
                for candidate in [
                    tc_dir / (dumpmachine or "") / "lib",
                    tc_dir / "lib",
                ]:
                    if candidate.is_dir() and str(candidate) not in qemu_sysroot:
                        extra_lib_dirs.append(str(candidate))
            if extra_lib_dirs:
                cmd = [qemu_bin, "-L", qemu_sysroot,
                       "-E", f"LD_LIBRARY_PATH={':'.join(extra_lib_dirs)}",
                       str(bin_path), "-c", "/tmp/"]
            else:
                cmd = [qemu_bin, "-L", qemu_sysroot, str(bin_path), "-c", "/tmp/"]

        timeout = self.qemu_timeout
        expect = self.qemu_expect

        label = "TEST" if same_arch else "QEMU"
        self._log(f"[cyan]{label} -------> [/cyan]testing {bin_path.name} ([dark_orange]{timeout}s timeout[/dark_orange])")
        self._log(f"[dim]          $ {' '.join(cmd)}[/dim]")

        qemu_env = None if same_arch else self._qemu_uname_env(sysroot)
        if qemu_env:
            self._log(f"[dim]          $ QEMU_UNAME={qemu_env['QEMU_UNAME']}[/dim]")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                preexec_fn=os.setsid,
                env=qemu_env,
            )

            found = False

            # Reader thread to avoid blocking on readline
            def reader():
                nonlocal found
                for line in proc.stdout:
                    stripped = line.rstrip()
                    if stripped and not stripped.startswith("qemu: Unsupported syscall"):
                        self._log(f"          {stripped}")
                    if expect and expect in stripped:
                        found = True
                        return

            t = threading.Thread(target=reader, daemon=True)
            t.start()

            # Wait for expect string or timeout
            start = time.time()
            while time.time() - start < timeout:
                if found or self._cancel():
                    break
                time.sleep(0.2)

            # Kill entire process group (oscam may fork)
            try:
                os.killpg(os.getpgid(proc.pid), 9)
                proc.wait(timeout=2)
            except Exception:
                pass

            if self._cancel():
                self._log(f"[yellow]{label} -------> CANCELLED[/yellow]")
            elif found:
                self._log(f"[green]{label} -------> PASS ({expect} found)[/green]")
            else:
                self._log(f"[red]{label} -------> FAIL ({expect} not found within {timeout}s)[/red]")

        except Exception as e:
            self._log(f"[red]{label} -------> ERROR: {e}[/red]")

    def _run_unit_tests(self, repo_dir: Path, cross: str, sysroot: str,
                        is_native: bool, tc_dir: Path | None) -> None:
        """Build and run oscam's self-tests via ``make tests`` (make-built
        targets). Depends on the upstream commits that isolate the test
        build into ``build-tests/`` and make ``tests.bin`` exit non-zero
        on failure; on older checkouts the run still happens but exit
        code is unreliable."""
        args = list(getattr(self, "_last_make_args", []) or [])
        tests_cmd = ["make", "tests", f"-j{self.max_cpus}",
                     "TESTS_BIN=tests.bin", *args]
        self._log("")
        self._log("[bold]Running unit tests (make tests)[/bold]")
        self._log(f"[dim]          $ {' '.join(tests_cmd)}[/dim]")
        ok_build = self._run(tests_cmd, cwd=repo_dir, colorize=True)
        tests_bin = repo_dir / "tests.bin"
        if not ok_build or not tests_bin.exists():
            self._log("[red]TESTS ------> FAIL (compile error)[/red]")
            return
        self._exec_tests_bin(tests_bin, cross, sysroot, is_native, tc_dir)

    def _run_unit_tests_cmake(self, repo_dir: Path, cross: str, sysroot: str,
                              is_native: bool, tc_dir: Path | None) -> None:
        """cmake analog to ``_run_unit_tests``. oscam's CMakeLists honors
        ``-DBUILD_TESTS=ON`` by *replacing* the main executable target
        with ``tests.bin`` (signing/UPX/strip skipped). So we can't reuse
        ``build_s4/`` — a second configure in ``build_s4_tests/`` on top
        of the same cross-compile args produces the test binary next to
        the untouched main build."""
        import shutil as _shutil

        cmake_args = list(getattr(self, "_last_cmake_args", []) or [])
        test_build_dir = repo_dir / "build_s4_tests"
        if test_build_dir.exists():
            _shutil.rmtree(test_build_dir, ignore_errors=True)
        test_build_dir.mkdir(parents=True, exist_ok=True)

        configure_cmd = ["cmake", str(repo_dir),
                         "-DBUILD_TESTS=ON", *cmake_args]
        self._log("")
        self._log("[bold]Running unit tests (cmake -DBUILD_TESTS=ON)[/bold]")
        self._log(f"[dim]          $ {' '.join(configure_cmd)}[/dim]")
        if not self._run(configure_cmd, cwd=test_build_dir):
            self._log("[red]TESTS ------> FAIL (cmake configure)[/red]")
            return
        build_cmd = ["cmake", "--build", ".", "--", f"-j{self.max_cpus}"]
        if not self._run(build_cmd, cwd=test_build_dir, colorize=True):
            self._log("[red]TESTS ------> FAIL (cmake build)[/red]")
            return
        tests_bin = test_build_dir / "tests.bin"
        if not tests_bin.is_file():
            found = list(test_build_dir.rglob("tests.bin"))
            found = [p for p in found
                     if p.is_file() and os.access(p, os.X_OK)]
            if found:
                tests_bin = found[0]
        if not tests_bin.is_file():
            self._log(
                "[yellow]TESTS ------> SKIP (no tests.bin produced — "
                "oscam revision may predate the cmake BUILD_TESTS option)"
                "[/yellow]"
            )
            return
        self._exec_tests_bin(tests_bin, cross, sysroot, is_native, tc_dir)

    def _exec_tests_bin(self, tests_bin: Path, cross: str, sysroot: str,
                        is_native: bool, tc_dir: Path | None) -> None:
        """Run a prebuilt ``tests.bin`` via qemu-user (cross) or directly
        (native) and report PASS / FAIL based on its exit code. Shared
        by the make and cmake unit-test paths."""
        import time
        import shutil as _shutil
        import threading

        # Build the command that runs tests.bin. Mirrors the qemu-
        # smoke-test path but without the expect-string logic — we
        # trust the binary's exit code (non-zero = failure).
        cmd: list[str] = []
        run_label = "TESTS"
        if is_native:
            env = os.environ.copy()
            if tc_dir:
                local_lib = tc_dir / "lib"
                if local_lib.is_dir():
                    old = env.get("LD_LIBRARY_PATH", "")
                    env["LD_LIBRARY_PATH"] = (
                        f"{local_lib}:{old}" if old else str(local_lib)
                    )
            cmd = [str(tests_bin)]
        else:
            # Cross: qemu-<arch>. Reuse the arch-map from _qemu_smoke_test.
            _QEMU_ARCH_MAP = {
                "arm": "arm", "armv7": "arm", "armv7l": "arm", "armv7ve": "arm",
                "armv7a": "arm", "armv7ahf": "arm", "armv6": "arm", "armv5te": "arm",
                "armv8": "arm", "armv8l": "arm",
                "aarch64": "aarch64", "arm64": "aarch64", "armv8a": "aarch64",
                "armv8_64": "aarch64",
                "mipsel": "mipsel", "mips32el": "mipsel",
                "mips": "mips",
                "i686": "i386", "i386": "i386",
                "x86_64": "x86_64", "riscv64": "riscv64", "sh4": "sh4",
            }
            try:
                dumpmachine = subprocess.run(
                    [f"{cross}gcc", "-dumpmachine"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                qemu_arch = _QEMU_ARCH_MAP.get(
                    dumpmachine.split("-")[0], dumpmachine.split("-")[0],
                )
            except Exception:
                self._log("[yellow]TESTS ------> SKIP (cannot determine arch)[/yellow]")
                return
            qemu_bin = f"qemu-{qemu_arch}"
            if not _shutil.which(qemu_bin):
                self._log(f"[yellow]TESTS ------> SKIP ({qemu_bin} not found)[/yellow]")
                return
            qemu_sysroot = sysroot.rstrip("/")
            if qemu_sysroot.endswith("/usr"):
                qemu_sysroot = qemu_sysroot[:-4]
            extra_lib_dirs: list[str] = []
            if tc_dir:
                for candidate in [tc_dir / (dumpmachine or "") / "lib", tc_dir / "lib"]:
                    if candidate.is_dir() and str(candidate) not in qemu_sysroot:
                        extra_lib_dirs.append(str(candidate))
            cmd = [qemu_bin, "-L", qemu_sysroot]
            if extra_lib_dirs:
                cmd += ["-E", f"LD_LIBRARY_PATH={':'.join(extra_lib_dirs)}"]
            cmd.append(str(tests_bin))
            env = self._qemu_uname_env(sysroot)
            run_label = "QEMU-TESTS"

        self._log(f"[cyan]{run_label} -> [/cyan]executing {tests_bin.name}")
        self._log(f"[dim]          $ {' '.join(cmd)}[/dim]")

        # Run synchronously but with a hard timeout so a hung test can't
        # stall the build forever. 120 s is generous — current tests.c
        # finishes in well under a second.
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                preexec_fn=os.setsid,
                env=env,
            )
            done = False

            def reader():
                nonlocal done
                from rich.markup import escape as _rich_escape
                import re as _re
                _summary_re = _re.compile(r"^\s*Summary:\s*(\d+)\s*failure")
                for line in proc.stdout:
                    stripped = line.rstrip()
                    if not stripped or stripped.startswith("qemu: Unsupported syscall"):
                        continue
                    # Escape so '[OK]' and other bracketed test-binary
                    # output isn't (mis)parsed as Rich markup. Without
                    # this, the trailing ``Summary: N failure(s)`` ends
                    # up styled by an unclosed tag from earlier.
                    safe = _rich_escape(stripped)
                    m = _summary_re.match(stripped)
                    if m:
                        n = int(m.group(1))
                        colour = "green" if n == 0 else "red"
                        self._log(f"          [{colour}]{safe}[/{colour}]")
                    else:
                        self._log(f"          {safe}")
                done = True

            t = threading.Thread(target=reader, daemon=True)
            t.start()
            start = time.time()
            timeout = 120
            while not done and time.time() - start < timeout:
                if self._cancel():
                    break
                time.sleep(0.1)
                if proc.poll() is not None:
                    break
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), 9)
                except Exception:
                    pass
            t.join(timeout=2)

            rc = proc.returncode
            if self._cancel():
                self._log(f"[yellow]{run_label} -> CANCELLED[/yellow]")
            elif rc == 0:
                self._log(f"[green]{run_label} -> PASS (exit 0)[/green]")
            else:
                self._log(f"[red]{run_label} -> FAIL (exit {rc})[/red]")
        except Exception as e:
            self._log(f"[red]{run_label} -> ERROR: {e}[/red]")

    def _mbedtls_tarball_cache_sync(self, repo_dir: Path, pre: bool) -> None:
        """Bridge oscam's ``.mbedtls-cache/`` with s4's ``data/downloads/``.

        ``config.sh --mbedtls-fetch`` downloads the pinned tarball
        named in ``mbedtls/version.conf`` into the repo-local
        ``.mbedtls-cache/``. Without intervention the cache is
        repo-scoped — a backup-restore or fresh clone loses it and
        forces a 7 MB re-download.

        ``pre=True``: before ``make``, if ``data/downloads/<tarball>``
        already exists, place a symlink under ``.mbedtls-cache/`` so
        the fetch script finds it and skips the download.

        ``pre=False``: after a successful build, if the repo downloaded
        a fresh tarball (regular file, not symlink), relocate it to
        ``data/downloads/`` and replace with a symlink.

        No-op on branches that don't use tarball-based mbedtls
        (``mbedtls/version.conf`` absent).
        """
        version_conf = repo_dir / "mbedtls" / "version.conf"
        if not version_conf.is_file():
            return
        conf: dict[str, str] = {}
        try:
            for _line in version_conf.read_text(
                encoding="utf-8", errors="replace",
            ).splitlines():
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                k, _, v = _line.partition("=")
                conf[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            return
        # version.conf is a shell source-file and uses ``${VAR}`` /
        # ``$VAR`` interpolation (e.g. URL embeds ``${VERSION}``).
        # Expand references using the values we already parsed —
        # limited to a single pass, which is all oscam's file needs.
        import re as _re
        def _expand(s: str) -> str:
            return _re.sub(
                r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
                lambda m: conf.get(m.group(1), m.group(0)),
                s,
            )
        conf = {k: _expand(v) for k, v in conf.items()}
        url = conf.get("URL", "")
        if not url:
            return
        tarball_name = url.rsplit("/", 1)[-1]
        if not tarball_name:
            return

        # ``self.bin_dir`` is ``data/bin``; downloads lives next to it.
        downloads_dir = self.bin_dir.parent / "downloads"
        cache_dir = repo_dir / ".mbedtls-cache"
        dl_path = downloads_dir / tarball_name
        cache_path = cache_dir / tarball_name

        # Relative symlink so the repo tree can be tar-balled /
        # moved / bind-mounted without breaking the cache reference.
        # Note: oscam's ``config.sh --mbedtls-fetch`` prints the cached
        # tarball size via ``du -h`` which reports 0 for a symlink —
        # fix belongs upstream (``du -hL``), not in s4.
        rel_target = os.path.relpath(dl_path, start=cache_dir)

        try:
            if pre:
                # Seed from central store if available and cache empty.
                if dl_path.is_file() and not cache_path.exists():
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_path.symlink_to(rel_target)
                    self._log(
                        f"[cyan]    mbedTLS tarball: reusing {tarball_name} "
                        f"from data/downloads/[/cyan]"
                    )
                elif cache_path.is_symlink():
                    # Existing symlink — if the target is missing
                    # (downloads got wiped) remove the dangling link so
                    # the fetch script re-downloads. A healthy symlink
                    # pointing at the central copy stays silent; no
                    # need to clutter every build log with a "nothing
                    # to do" confirmation.
                    try:
                        same = dl_path.is_file() and os.path.samefile(
                            cache_path, dl_path,
                        )
                    except OSError:
                        same = False
                    if not same:
                        cache_path.unlink()
                        self._log(
                            f"[yellow]    mbedTLS tarball: removed stale "
                            f"symlink (target missing), will re-fetch"
                            f"[/yellow]"
                        )
            else:
                # Harvest a freshly-downloaded copy into data/downloads/.
                if cache_path.is_file() and not cache_path.is_symlink():
                    downloads_dir.mkdir(parents=True, exist_ok=True)
                    if dl_path.exists():
                        # Another build centralised it first; just drop
                        # the duplicate local copy + resymlink.
                        cache_path.unlink()
                    else:
                        import shutil as _sh
                        _sh.move(str(cache_path), str(dl_path))
                    cache_path.symlink_to(rel_target)
                    self._log(
                        f"[cyan]    mbedTLS tarball: moved {tarball_name} "
                        f"to data/downloads/ (symlinked back)[/cyan]"
                    )
        except Exception as e:
            self._log(
                f"    [yellow]mbedTLS tarball cache sync failed: {e}[/yellow]"
            )

    def _close_log(self, log_path: Optional[Path]) -> None:
        """Close the log file first, then show the log path + retention.

        Log-related summary lines (``Log:`` and ``Log retention:``) are
        intentionally kept out of the persisted log file — they only
        reach the GUI. Closing the file first makes ``_log_both``'s
        None-check drop the file write; the wrapped callback still
        forwards to the original widget log.
        """
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        if log_path is not None:
            try:
                self._log("")
                self._log(f"Log: {deliver_link(log_path)}")
            except Exception:
                pass
        try:
            from core.log_cleanup import cleanup_logs
            removed, freed = cleanup_logs(
                self.logs_dir,
                max_age_days=self.log_max_age_days,
                max_count=self.log_max_count,
                max_size_mb=self.log_max_size_mb,
            )
            if removed:
                mb = freed / (1024 * 1024)
                self._log(f"Log retention: removed {removed} old log(s), freed {mb:.2f} MB")
        except Exception:
            pass


    # -------------------------------------------------------------------------
    # PHASE 5: Deploy (delegated to core.deployer.Deployer)
    # -------------------------------------------------------------------------

    def _phase5_deploy(self, src, tcc, bin_path: Path) -> bool:
        """Deploy the built binary to all matching enabled deployment destinations.
        Returns True if all deployments succeeded, False if any failed."""
        from core.deployer import Deployer

        deployments = [
            d for d in self.target.deployment
            if d.enabled and d.host
            and (not d.source or d.source == src.name)
            and d.toolchain == tcc.name
        ]
        if not deployments:
            return True

        self._log(f"\n[bold]PHASE 5: Deploy ({len(deployments)} destination{'s' if len(deployments) > 1 else ''})[/bold]")

        # Announce delete-after-deploy intent up front so the user sees
        # the plan before the deploy loop runs. Only announce when a
        # binary actually exists — a "no binary" phase-5 wouldn't touch
        # data/binaries/ anyway.
        will_delete = (
            self.target.delete_binary_after_deploy
            and bin_path is not None
            and bin_path.exists()
        )
        if will_delete:
            self._log(
                f"[yellow]Local binary will be deleted after successful deploy: "
                f"{bin_path.name}[/yellow]"
            )

        # Snapshot cached SCP modes so we can detect — and persist —
        # any updates the deployer writes back during the run.
        scp_cache_before = [d.last_working_scp_mode for d in deployments]

        all_ok = True
        for d in deployments:
            if self._cancel():
                self._log("[yellow]Deployment cancelled[/yellow]")
                return False
            name = d.name or d.host
            if d.host in ("localhost", "127.0.0.1", "::1"):
                self._log(f"\n  [bold]━━━ {name}: local → {d.target_path} ━━━[/bold]")
            else:
                self._log(f"\n  [bold]━━━ {name}: {d.user}@{d.host}:{d.target_path} ━━━[/bold]")

            deployer = Deployer(
                config=d, log=self._log, binary_path=bin_path,
                auto_fix_host_keys=self._auto_fix_stale_host_keys,
            )
            if not deployer.run(verbose=False):
                all_ok = False

        # Flush any SCP mode cache updates back to the target file so the
        # next build/deploy benefits. Silent on error — persistence is a
        # convenience, not something to fail the build over.
        if [d.last_working_scp_mode for d in deployments] != scp_cache_before:
            try:
                self.target.save_in_place()
            except Exception:
                pass

        # Post-loop delete — only when every destination succeeded, a
        # real binary is on disk, and the target has the flag set. Log
        # explicit outcome either way for the audit trail.
        if will_delete:
            try:
                size_kb = bin_path.stat().st_size // 1024
                size_str = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb} KB"
            except OSError:
                size_str = "?"
            if not all_ok:
                self._log(
                    f"[yellow]Local binary kept (deploy failed): "
                    f"{bin_path.name}[/yellow]"
                )
            else:
                try:
                    bin_path.unlink()
                    self._log(
                        f"[dim]Deleted local binary: {bin_path.name} "
                        f"({size_str})[/dim]"
                    )
                except OSError as e:
                    self._log(
                        f"[yellow]Could not delete {bin_path.name}: "
                        f"{e}[/yellow]"
                    )
        return all_ok

    def _auto_clone_repo(self, source_name: str) -> Optional[Path]:
        """Auto-clone a repository if it doesn't exist yet."""
        try:
            from core.repos_config import load_repos
            from core.repo import RepoManager
            repos = load_repos(self.repos_config)
            for rc in repos:
                if rc.name == source_name:
                    mgr = RepoManager(rc, self.repos_dir, self.backup_dir)
                    self._log(f"[cyan]Cloning repository '{source_name}' from {rc.url}...[/cyan]")
                    if mgr.clone(log=self._log):
                        return mgr.repo_path
                    return None
        except Exception as e:
            self._log(f"[red]Auto-clone failed: {e}[/red]")
        return None

    # -------------------------------------------------------------------------
    # PHASE 1: Prepare Repository
    # -------------------------------------------------------------------------

    def _phase1_prepare_repo(self, src: SourceConfig, repo_dir: Path) -> bool:
        """PHASE 1: Prepare the source repository.

        If update_before_build:
          1. Restore repo from backup (clean state, removes patches)
             Fallback: git checkout + clean if no backup exists
          2. Pull latest changes
          3. Create new backup
        If not update_before_build:
          Don't touch the repo at all
        """
        if not src.update_before_build:
            self._log(f"[dim]PHASE 1: Skipping repo update for '{src.name}'[/dim]")
            return True

        self._log(f"[bold]PHASE 1: Preparing repository '{src.name}'[/bold]")

        # Get RepoManager for this source
        repo_mgr = self._get_repo_manager(src.name)

        if repo_mgr:
            # Restore from backup (fast, guaranteed clean state)
            backups = repo_mgr.list_backups()
            if backups:
                self._log("  Restoring from backup...")
                if not repo_mgr.restore(log=self._log):
                    self._log("[red]  Restore failed — falling back to git reset[/red]")
                    self._git_reset(repo_dir)
            else:
                self._log("  No backup found — using git reset")
                self._git_reset(repo_dir)

            # Always ensure a clean working tree after restore — the
            # backup tarball may not cover build artifacts (build_s4/,
            # config.h, etc.) that the previous build generated, and
            # git pull will refuse to merge over dirty tracked files.
            self._git_reset(repo_dir)

            # Pull latest (using RepoManager which has auth credentials)
            self._log("  Pulling latest changes...")
            if not repo_mgr.update(log=self._log):
                self._log("[red]  Failed to update repository[/red]")
                return False

            # Show current state
            self._log_repo_state(repo_dir)

            # Create new backup only if revision changed
            current_rev = self._run_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir) if (repo_dir / ".git").exists() else self._run_output(["svnversion"], cwd=repo_dir)
            if current_rev and current_rev not in repo_mgr.list_backups():
                repo_mgr._backup(log=self._log)

        else:
            # No RepoManager available — fallback to git reset + pull
            self._log("  [yellow]No repo config found — using git fallback[/yellow]")
            self._git_reset(repo_dir)

            self._log("  Pulling latest changes...")
            if not self._run(["git", "pull", "--ff-only"], cwd=repo_dir):
                self._log("[red]  Failed to pull latest changes[/red]")
                return False

            self._log_repo_state(repo_dir)

        return True

    def _get_repo_manager(self, source_name: str):
        """Get a RepoManager for the given source name."""
        try:
            from core.repos_config import load_repos
            from core.repo import RepoManager
            repos = load_repos(self.repos_config)
            for rc in repos:
                if rc.name == source_name:
                    return RepoManager(rc, self.repos_dir, self.backup_dir)
        except Exception:
            pass
        return None

    def _git_reset(self, repo_dir: Path) -> None:
        """Fallback: reset repo via git checkout + clean."""
        is_git = (repo_dir / ".git").exists()
        if is_git:
            self._run(["git", "checkout", "."], cwd=repo_dir)
            self._run(["git", "clean", "-fd"], cwd=repo_dir)
        else:
            self._run(["svn", "revert", "-R", "."], cwd=repo_dir)

    def _log_repo_state(self, repo_dir: Path) -> None:
        """Log current repository revision info."""
        is_git = (repo_dir / ".git").exists()
        if is_git:
            rev = self._run_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir)
            branch = self._run_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
            count = self._run_output(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir)
            self._log(f"  Repository: {branch} @ {rev} (r{count})")
        else:
            rev = self._run_output(["svnversion"], cwd=repo_dir)
            self._log(f"  Repository: r{rev}")

    # -------------------------------------------------------------------------
    # PHASE 2: Apply Patches
    # -------------------------------------------------------------------------

    def _phase2_apply_patches(self, src: SourceConfig, repo_dir: Path) -> bool:
        """PHASE 2: Apply patches to the source repository.

        If patch_before_build:
          - If update_before_build was active: repo is already clean
          - If update_before_build was NOT active: reset repo first, then apply
          - Apply all configured patches
        If not patch_before_build:
          Don't touch the repo
        """
        if not src.patch_before_build:
            self._log(f"[dim]PHASE 2: Skipping patches for '{src.name}'[/dim]")
            return True

        if not src.patches:
            self._log(f"[dim]PHASE 2: No patches selected for '{src.name}'[/dim]")
            return True

        self._log(f"[bold]PHASE 2: Applying patches to '{src.name}'[/bold]")

        is_git = (repo_dir / ".git").exists()

        # If repo was NOT updated, we need to ensure a clean state first
        if not src.update_before_build:
            if is_git:
                self._log("  Cleaning repo for patching...")
                self._run(["git", "checkout", "."], cwd=repo_dir)
                self._run(["git", "clean", "-fd"], cwd=repo_dir)
            else:
                self._run(["svn", "revert", "-R", "."], cwd=repo_dir)

        # Apply each patch
        applied = 0
        failed = 0
        for patch_name in src.patches:
            patch_file = self.patches_dir / patch_name
            if not patch_file.exists():
                self._log(f"  [red]Patch not found: {patch_name}[/red]")
                failed += 1
                continue

            self._log(f"  Applying: {patch_name}")
            if is_git:
                ok = self._run(
                    ["git", "apply", "--verbose", str(patch_file)],
                    cwd=repo_dir,
                )
            else:
                ok = self._run(
                    ["patch", "-p1", "-i", str(patch_file)],
                    cwd=repo_dir,
                )

            if ok:
                applied += 1
                self._log(f"  [green]OK: {patch_name}[/green]")
            else:
                failed += 1
                self._log(f"  [red]FAILED: {patch_name}[/red]")

        self._log(f"  Patches: {applied} applied, {failed} failed")

        # Patch WebIF with build/patch info (if setting enabled and patches applied)
        if applied > 0 and self.patch_webif:
            self._patch_webif(repo_dir, src.patches)

        return failed == 0

    def _patch_webif(self, repo_dir: Path, patches: list[str]) -> None:
        """Inject patch info into oscam source so WebIF shows which patches are applied."""
        config_h = repo_dir / "config.h"
        oscam_c = repo_dir / "oscam.c"

        if not config_h.exists() or not oscam_c.exists():
            return

        try:
            # Build patch info string
            patch_names = "\\n\\t\\t".join(patches)
            patch_str = f"yes\\n\\t\\t{patch_names}"

            # Modify config.h: add #define S4PATCHED before #endif
            content = config_h.read_text(encoding="utf-8", errors="replace")
            # Remove previous S4PATCHED if exists
            content = "\n".join(line for line in content.splitlines() if "S4PATCHED" not in line)
            # Add before final #endif
            config_h_marker = "#endif //OSCAM_CONFIG_H_"
            new_content = content.replace(
                config_h_marker,
                f'#define S4PATCHED "{patch_str}"\n{config_h_marker}',
            )
            if new_content == content:
                self._log(f"  [yellow]WebIF patch failed: marker '{config_h_marker}' not found in config.h[/yellow]")
                return
            # splitlines()+"\n".join() drops any trailing newline — re-append.
            if not new_content.endswith("\n"):
                new_content += "\n"
            config_h.write_text(new_content, encoding="utf-8")

            # Modify oscam.c: add fprintf for patch info after WebifPort line
            content = oscam_c.read_text(encoding="utf-8", errors="replace")
            # Remove previous S4PATCHED block if exists
            lines = content.splitlines()
            filtered = []
            skip = False
            for line in lines:
                if "#ifdef S4PATCHED" in line:
                    skip = True
                    continue
                if skip and "#endif" in line:
                    skip = False
                    continue
                if skip:
                    continue
                filtered.append(line)
            content = "\n".join(filtered)

            # Insert after WebifPort fprintf
            oscam_c_marker = 'fprintf(fp, "WebifPort:      %d\\n", cfg.http_port);'
            new_content = content.replace(
                oscam_c_marker,
                oscam_c_marker + '\n'
                '#ifdef S4PATCHED\n'
                '\tfprintf(fp, "Patched:        %s\\n", S4PATCHED);\n'
                '#endif',
            )
            if new_content == content:
                self._log(f"  [yellow]WebIF patch failed: WebifPort fprintf not found in oscam.c[/yellow]")
                return
            if not new_content.endswith("\n"):
                new_content += "\n"
            oscam_c.write_text(new_content, encoding="utf-8")

            self._log(f"  [green]WebIF patched with build info[/green]")
        except Exception as e:
            self._log(f"  [yellow]WebIF patch failed: {e}[/yellow]")

    # -------------------------------------------------------------------------
    # PHASE 3: Configure oscam
    # -------------------------------------------------------------------------

    def _phase3_configure(self, src: SourceConfig, repo_dir: Path) -> bool:
        """PHASE 3: Configure oscam modules via config.sh.

        Flow:
        1. Restore oscam defaults (config.sh -R)
        2. Disable ALL options (clean slate)
        3. Enable only the target's selected options
        4. config.sh resolves dependencies automatically
        """
        rel_path = repo_dir.relative_to(repo_dir.parent.parent.parent) if repo_dir.parent.parent.parent.exists() else repo_dir
        self._log(f"  Configuring '{src.name}' @ {rel_path}/...")

        from core.oscam_config import read_options, apply_config

        # Read available options from this repo
        opts = read_options(repo_dir)
        if not opts:
            self._log(f"  [red]Cannot read config.sh from {repo_dir}[/red]")
            return False

        # Build the enabled dict from target's build settings
        b = self.target.build
        enabled = {
            "addons": b.modules,
            "protocols": b.protocols,
            "readers": b.readers,
            "card_readers": b.card_readers,
        }

        # Apply configuration
        ok = apply_config(repo_dir, enabled, opts, log=self._log)
        if not ok:
            self._log(f"  [red]Failed to configure '{src.name}'[/red]")
            return False

        # Apply build overrides (e.g. "USE_SSL USE_PCSC_off MODULE_GBOX_off")
        if self.build_overrides:
            from core.oscam_config import _run_config_sh
            config_sh = repo_dir / "config.sh"
            self._log(f"  Applying build overrides...")
            for token in self.build_overrides.split():
                if token.endswith("_off"):
                    opt = token[:-4]  # strip _off
                    _run_config_sh(config_sh, ["-D", opt])
                    self._log(f"    [red]Disable[/red] {opt}")
                else:
                    _run_config_sh(config_sh, ["-E", token])
                    self._log(f"    [green]Enable[/green] {token}")

        return True

    # -------------------------------------------------------------------------
    # PHASE 4: Build (make)
    # -------------------------------------------------------------------------

    def _phase4_build(self, src: SourceConfig, repo_dir: Path, tcc: ToolchainConfig, tc) -> bool:
        """PHASE 4: Execute make for each source x toolchain combination."""
        self._log(f"\n  [bold]Building {src.name} x {tcc.name}[/bold]")

        b = self.target.build
        is_native = tc.native

        # --- Toolchain paths ---
        tc_dir = self.toolchains_dir / tc.name
        if is_native:
            cross = ""
            sysroot = ""
        else:
            cross = str(tc_dir / "bin" / tc.prefix)
            sysroot_path = tc_dir / tc.sysroot if tc.sysroot else tc_dir
            sysroot = str(sysroot_path)

        # --- Auto-inject --sysroot if compiler's built-in sysroot is invalid ---
        # Like s3: override CC= so every gcc invocation (including Makefile's
        # internal $(CC) -print-sysroot) sees the correct sysroot.
        #
        # Ancient toolchains (gcc < 4.0, introduced ~2005) don't understand
        # ``--sysroot`` at all — cc1 misparses the flag and bails with
        # ``unrecognized command line option "-fsysroot=…"``. For those we
        # skip the auto-injection entirely; the compiler's compiled-in
        # include/library paths have to be correct (usually are, for custom
        # legacy cross-compilers). If they aren't, the user can still set
        # explicit cflags/ldflags on the target's toolchain entry.
        cross_sysroot = ""
        if not is_native and cross:
            import shutil as _shutil
            gcc_path = _shutil.which(f"{tc.prefix}gcc", path=f"{tc_dir}/bin:{os.environ.get('PATH', '')}")
            if gcc_path:
                # Detect gcc major version before probing for sysroot —
                # ``-print-sysroot`` is also a gcc 4+ feature, and the
                # subsequent ``--sysroot=`` injection would break gcc 3.x.
                gcc_major = 0
                try:
                    dumpver = subprocess.run(
                        [gcc_path, "-dumpversion"], capture_output=True, text=True, timeout=5,
                    ).stdout.strip()
                    if dumpver and dumpver[0].isdigit():
                        gcc_major = int(dumpver.split(".")[0])
                except Exception:
                    pass

                if gcc_major and gcc_major < 4:
                    self._log(
                        f"  [yellow]Skipping --sysroot injection: "
                        f"{tc.prefix}gcc is version {dumpver} "
                        f"(gcc < 4.0 doesn't support --sysroot)[/yellow]"
                    )
                else:
                    try:
                        gcc_sysroot = subprocess.run(
                            [gcc_path, "-print-sysroot"], capture_output=True, text=True, timeout=5,
                        ).stdout.strip()
                    except Exception:
                        gcc_sysroot = ""
                    if not gcc_sysroot or not Path(gcc_sysroot).is_dir():
                        cross_sysroot = f"--sysroot={sysroot}"
                        self._log(f"  Auto-injected: {cross_sysroot}")
                    elif gcc_sysroot:
                        # Use compiler's real sysroot (e.g. for QEMU -L)
                        sysroot = gcc_sysroot

        # --- CC_OPTS, CFLAGS, LDFLAGS ---
        # s4 settings = base, toolchain appends, target overrides toolchain
        tc_cc = tcc.cc_opts if tcc.cc_opts else (tc.cc_opts or "")
        cc_opts = f"{self.compiler_opt} {tc_cc}".strip() if tc_cc else (self.compiler_opt or "")
        cflags = tcc.cflags if tcc.cflags else (tc.cflags or "")
        cflags = cflags.replace("@TOOLCHAIN@", str(tc_dir))
        if cross_sysroot and "--sysroot" not in cflags:
            cflags = f"{cflags} {cross_sysroot}".strip()
        ldflags = tcc.ldflags if tcc.ldflags else (tc.ldflags or "")
        ldflags = ldflags.replace("@TOOLCHAIN@", str(tc_dir))
        extra_make = tcc.make_vars if tcc.make_vars else (tc.make_vars or "")

        # --- Native toolchain: prepend local include/lib paths so the build
        #     finds libraries installed into data/toolchains/native/ ---
        self._native_env_backup = {}
        if is_native:
            # Find the prefix (native/ or native/usr/)
            _usr = tc_dir / "usr"
            _prefix = _usr if _usr.is_dir() else tc_dir
            _inc = _prefix / "include"
            _lib = _prefix / "lib"
            if _inc.is_dir():
                cflags = f"-I{_inc} {cflags}".strip()
            if _lib.is_dir():
                ldflags = f"-L{_lib} {ldflags}".strip()
            pkg_dirs = [str(d) for d in tc_dir.glob("**/pkgconfig") if d.is_dir()]
            if pkg_dirs:
                pkg_path = ":".join(pkg_dirs)
                old_pkg = os.environ.get("PKG_CONFIG_PATH", "")
                self._native_env_backup["PKG_CONFIG_PATH"] = old_pkg
                os.environ["PKG_CONFIG_PATH"] = f"{pkg_path}:{old_pkg}" if old_pkg else pkg_path
                self._log(f"  PKG_CONFIG_PATH: {os.environ['PKG_CONFIG_PATH']}")

        # --- USE_FLAGS (dynamic from dict) ---
        use_flags = []
        for flag, enabled in b.use_flags.items():
            if enabled:
                use_flags.append(f"USE_{flag}=1")
        # UPX: per-toolchain override
        if tcc.use_compress == "1":
            _tc_compress = True
        elif tcc.use_compress == "0":
            _tc_compress = False
        else:
            _tc_compress = b.use_compress
        _tc_compress_level = tcc.compress_level if tcc.compress_level else b.compress_level
        if _tc_compress:
            use_flags.append("USE_COMPRESS=1")
            if _tc_compress_level and _tc_compress_level != "--best":
                use_flags.append(f"COMP_LEVEL={_tc_compress_level}")

        # --- Apply build overrides to USE_FLAGS ---
        if self.build_overrides:
            for token in self.build_overrides.split():
                if token.startswith("USE_"):
                    if token.endswith("_off"):
                        # Remove: USE_SSL_off → remove USE_SSL=1
                        flag = token[:-4] + "=1"
                        use_flags = [f for f in use_flags if f != flag]
                    else:
                        # Add: USE_STAPI → add USE_STAPI=1
                        flag = token + "=1"
                        if flag not in use_flags:
                            use_flags.append(flag)

        # --- Static library detection ---
        static_libs = {}
        if is_native:
            # Search system paths + native toolchain local libs
            search_dirs = self._get_native_lib_paths(tc_dir)
        else:
            # Search the configured target sysroot (tc.sysroot), which contains
            # both system libs and cross-built libs. This avoids matching libs
            # from a native host sysroot in OE-SDK toolchains.
            target_root = tc_dir / tc.sysroot if tc.sysroot else tc_dir
            search_dirs = [str(target_root)]

        # Map flag names to static library filenames
        _STATIC_LIB_FILES = {
            "LIBCRYPTO": "libcrypto.a",
            "SSL": "libssl.a",
            "LIBUSB": "libusb-1.0.a",
            "PCSC": "libpcsclite.a",
            "LIBDVBCSA": "libdvbcsa.a",
        }
        for key, lib_file in _STATIC_LIB_FILES.items():
            use_flag = b.use_flags.get(key, False)
            static_flag = b.static_flags.get(key, False)
            if use_flag and static_flag:
                found = self._find_static_lib(lib_file, search_dirs)
                if found:
                    static_libs[f"{key}_LIB"] = found
                    self._log(f"  Static: {key}_LIB={found}")
                else:
                    self._log(f"  [yellow]Static {lib_file} not found[/yellow]")

        # --- STAPI / SU980 support-lib dir as -L ---
        # Any of these flags tells oscam's Makefile to auto-inject sibling
        # libs (``-lentropic`` for SU980, ``-lwi`` / ``-lwi2`` for the
        # Wi-interface patches, plus the oscam_stapi[_5] wrappers). Those
        # need a search path to resolve — add ``support/stapi`` if the
        # build pulls in any of the relevant Makefile subsystems.
        _stapi_support = self.repos_dir.parent.parent / "support" / "stapi"
        _needs_stapi_L = (
            b.use_flags.get("STAPI")
            or b.use_flags.get("STAPI5")
            or b.use_flags.get("SU980")
        )
        if _needs_stapi_L and _stapi_support.is_dir():
            _stapi_L = f"-L{_stapi_support}"
            if _stapi_L not in ldflags:
                ldflags = f"{_stapi_L} {ldflags}".strip()
                self._log(f"  Auto-added to ldflags: {_stapi_L}")

        # --- STAPI library ---
        stapi_lib = ""
        if b.use_flags.get("STAPI") or b.use_flags.get("STAPI5"):
            stapi_val = b.stapi_lib if b.stapi_lib else (tc.stapi_lib or "")
            # Resolve bare filenames (e.g. "libwi.a") to full path in support/stapi/
            if stapi_val and not os.path.isabs(stapi_val) and not os.path.exists(stapi_val):
                stapi_dir_check = self.repos_dir.parent.parent / "support" / "stapi" / stapi_val
                if stapi_dir_check.exists():
                    stapi_val = str(stapi_dir_check)
            # Fallback: use shipped stapi libs (like s3's support/stapi/)
            if not stapi_val:
                stapi_dir = self.repos_dir.parent.parent / "support" / "stapi"
                if b.use_flags.get("STAPI5"):
                    # STAPI5: try platform-specific libs
                    for candidate in ("liboscam_stapi5_UFS916.a", "liboscam_stapi5_OPENBOX.a"):
                        if (stapi_dir / candidate).exists():
                            stapi_val = str(stapi_dir / candidate)
                            break
                else:
                    default_lib = stapi_dir / "liboscam_stapi.a"
                    if default_lib.exists():
                        stapi_val = str(default_lib)
            if stapi_val:
                stapi_key = "STAPI5_LIB" if b.use_flags.get("STAPI5") else "STAPI_LIB"
                stapi_lib = f"{stapi_key}={stapi_val}"
                self._log(f"  {stapi_lib}")
            else:
                self._log(f"  [yellow]Warning: STAPI enabled but no library found[/yellow]")

        # --- CONF_DIR ---
        # CONF_DIR: per-toolchain override
        confdir = tcc.confdir if tcc.confdir else (b.confdir or self._default_confdir)

        # --- Output path (handles use_oscamname variants, per-TC override) ---
        bin_path = self._resolve_binary_path(repo_dir, src.name, tcc.name, is_native, b, tcc)
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        # Remove existing binary so make/copy doesn't fail on busy/locked files
        if bin_path.exists():
            try:
                bin_path.unlink()
            except OSError:
                pass

        # --- Log common settings ---
        self._log(f"  Build system: {b.build_system}")
        self._log(f"  Output: {bin_path}")
        self._log(f"  CROSS: {cross or '(native)'}")
        if cross_sysroot:
            self._log(f"  CC: {cross}gcc {cross_sysroot}")
        self._log(f"  CC_OPTS: {cc_opts}")
        if cflags:
            self._log(f"  EXTRA_CFLAGS: {cflags}")
        if ldflags:
            self._log(f"  EXTRA_LDFLAGS: {ldflags}")
        self._log(f"  CONF_DIR: {confdir}")
        self._log(f"  CPUs: {self.max_cpus}")
        self._log("")

        # --- Per-TC UPX symlink ---
        if _tc_compress:
            _tc_upx = tcc.upx_binary if tcc.upx_binary else (b.upx_binary or "")
            if _tc_upx:
                import shutil as _shutil2
                upx_link = self.bin_dir / "upx"
                try:
                    if upx_link.is_symlink() or upx_link.exists():
                        upx_link.unlink()
                    upx_link.symlink_to(_tc_upx)
                except Exception:
                    pass

        # --- Reset warning counter and dispatch ---
        self._warning_count = 0
        started = datetime.now()
        if b.build_system == "cmake":
            ok, bin_path = self._build_cmake(
                repo_dir, bin_path, cross, sysroot, cross_sysroot,
                cc_opts, cflags, ldflags, confdir, use_flags, static_libs,
                stapi_lib, is_native, tc, tcc, tc_dir,
            )
        else:
            ok, bin_path = self._build_make(
                repo_dir, bin_path, cross, cross_sysroot,
                cc_opts, cflags, ldflags, extra_make, confdir,
                use_flags, static_libs, stapi_lib,
            )
        elapsed = (datetime.now() - started).total_seconds()

        # Restore environment after native build
        for key, val in self._native_env_backup.items():
            if val:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        self._native_env_backup = {}

        if ok and bin_path and bin_path.exists():
            size_kb = bin_path.stat().st_size / 1024
            self._log("")
            warn_str = f", {self._warning_count} warning{'s' if self._warning_count != 1 else ''}" if self._warning_count else ""
            self._log(f"[green]OK: {deliver_link(bin_path, bin_path.name)} ({size_kb:.0f} KB) in {elapsed:.0f}s{warn_str}[/green]")

            # Post-build: immediately after each build (before distclean of next build)
            self._postbuild_single(bin_path, repo_dir, cross, sysroot, is_native, tc_dir)

            return bin_path
        else:
            self._log(f"[red]Build FAILED after {elapsed:.0f}s[/red]")
            return None

    def _build_make(self, repo_dir, bin_path, cross, cross_sysroot,
                    cc_opts, cflags, ldflags, extra_make, confdir,
                    use_flags, static_libs, stapi_lib):
        """Build using make (default)."""
        make_cmd = ["make", f"-j{self.max_cpus}"]
        make_cmd.append(f"CONF_DIR={confdir}")
        make_cmd.append(f"OSCAM_BIN={bin_path}")
        # Detect Clang-based toolchains — they don't support some GCC-only
        # flags that oscam's Makefile sets by default (e.g. -fno-schedule-insns).
        # Check if {cross}gcc is actually a symlink to clang.
        _is_clang = False
        if cross:
            _gcc_path = Path(f"{cross}gcc")
            if _gcc_path.exists():
                try:
                    if _gcc_path.is_symlink():
                        _is_clang = "clang" in os.readlink(str(_gcc_path))
                    if not _is_clang:
                        _is_clang = "clang" in _gcc_path.resolve().name
                except Exception:
                    pass
        if _is_clang:
            _clang_cc_opts = cc_opts or "-O2 -ggdb -pipe -ffunction-sections -fdata-sections -fomit-frame-pointer"
            for f in ("-fno-schedule-insns",):
                _clang_cc_opts = _clang_cc_opts.replace(f, "")
            make_cmd.append(f"CC_OPTS={' '.join(_clang_cc_opts.split())}")
        elif cc_opts:
            make_cmd.append(f"CC_OPTS={cc_opts}")
        if cflags:
            make_cmd.append(f"EXTRA_CFLAGS={cflags}")
        if ldflags:
            make_cmd.append(f"EXTRA_LDFLAGS={ldflags}")
        if cross:
            make_cmd.append(f"CROSS={cross}")
        if cross_sysroot:
            make_cmd.append(f"CC={cross}gcc {cross_sysroot}")
        for flag in use_flags:
            make_cmd.append(flag)
        for key, val in static_libs.items():
            make_cmd.append(f"{key}={val}")
        # Static libusb pulls in extra libraries that a shared build
        # would already have resolved for us.
        # * ``-lrt`` — clock_gettime on older glibc lives in librt
        # * ``-ludev`` — some SDKs (e.g. OpenEmbedded Scarthgap) ship
        #   libusb-1.0.a compiled with udev support, so the archive
        #   drags in ``udev_*`` symbols via ``os/linux_udev.o``. We
        #   probe the archive for those references and only add
        #   ``-ludev`` if libusb actually uses udev — keeps the
        #   older no-udev SDKs unchanged.
        if "LIBUSB_LIB" in static_libs:
            extra_libs = ["-lrt"]
            libusb_a = static_libs.get("LIBUSB_LIB", "")
            if libusb_a and Path(libusb_a).is_file() and cross:
                nm = f"{cross}nm"
                try:
                    out = subprocess.check_output(
                        [nm, "--undefined-only", libusb_a],
                        stderr=subprocess.DEVNULL, text=True, timeout=10,
                    )
                    if "udev_" in out:
                        extra_libs.append("-ludev")
                        self._log(
                            "  Auto-added to EXTRA_LIBS: -ludev "
                            "(libusb.a references udev symbols)"
                        )
                except Exception:
                    pass
            make_cmd.append(f"EXTRA_LIBS={' '.join(extra_libs)}")
        if stapi_lib:
            make_cmd.append(stapi_lib)
        if extra_make:
            make_cmd.extend(extra_make.split())

        # Clean
        self._log("  Cleaning...")
        self._run(["make", "distclean"], cwd=repo_dir, colorize=True)

        # Build
        self._log("  Compiling...")
        # Bridge oscam's ``.mbedtls-cache/`` with s4's central
        # ``data/downloads/`` — positioned here so the reuse log
        # line groups visually with the submodule / mbedtls fetch
        # output from make below.
        self._mbedtls_tarball_cache_sync(repo_dir, pre=True)
        ok = self._run(make_cmd, cwd=repo_dir, colorize=True)
        # Stash the env-carrying args of make_cmd so the post-build
        # unit-test step can drive ``make tests`` with the SAME
        # cross-compile setup (CROSS, CC, EXTRA_CFLAGS, USE_*, …) —
        # without that, a cross toolchain would run the host's gcc.
        # We drop ``OSCAM_BIN=`` (handled by the tests target via
        # TESTS_BIN) and the ``-j`` flag (we append our own).
        self._last_make_args = [
            a for a in make_cmd[1:]
            if not a.startswith("OSCAM_BIN=") and not a.startswith("-j")
        ]
        return ok, bin_path

    def _build_cmake(self, repo_dir, bin_path, cross, sysroot, cross_sysroot,
                     cc_opts, cflags, ldflags, confdir, use_flags, static_libs,
                     stapi_lib, is_native, tc, tcc, tc_dir):
        """Build using cmake."""
        import shutil as _shutil

        build_dir = repo_dir / "build_s4"

        # Clean previous cmake build
        self._log("  Cleaning...")
        if build_dir.exists():
            _shutil.rmtree(build_dir)
        # Also distclean make artifacts
        self._run(["make", "distclean"], cwd=repo_dir, colorize=True)

        build_dir.mkdir(parents=True, exist_ok=True)

        # --- Generate cmake toolchain file for cross-compilation ---
        toolchain_file = None
        if not is_native and cross:
            toolchain_file = build_dir / "s4-toolchain.cmake"
            gcc_path = f"{cross}gcc"
            gxx_path = f"{cross}g++"
            if cross_sysroot:
                gcc_path = f"{gcc_path} {cross_sysroot}"
                gxx_path = f"{gxx_path} {cross_sysroot}"

            # Combine all C flags for cmake toolchain
            all_cflags = f"{cc_opts} {cflags}".strip()

            tc_content = f"""# Auto-generated by s4 BuildEngine
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR {tc.arch or 'arm'})
set(CMAKE_C_COMPILER {cross}gcc)
set(CMAKE_CXX_COMPILER {cross}g++)
set(CMAKE_C_FLAGS_INIT "{all_cflags}")
set(CMAKE_CXX_FLAGS_INIT "{all_cflags}")
set(CMAKE_EXE_LINKER_FLAGS_INIT "{ldflags}")
set(CMAKE_FIND_ROOT_PATH "{sysroot}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(OSCAM_SYSTEM_NAME "Tuxbox")
"""
            if cross_sysroot:
                tc_content += f'set(CMAKE_SYSROOT "{sysroot}")\n'

            toolchain_file.write_text(tc_content)
            self._log(f"  Generated toolchain file: {toolchain_file.name}")

        # --- cmake configure ---
        cmake_cmd = ["cmake", str(repo_dir)]

        # Toolchain
        if toolchain_file:
            cmake_cmd.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain_file}")

        # Native: help cmake find libs in data/toolchains/native/
        if is_native:
            _usr = tc_dir / "usr"
            _prefix = _usr if _usr.is_dir() else tc_dir
            cmake_cmd.append(f"-DCMAKE_PREFIX_PATH={_prefix}")

        # Compiler flags (only for native builds — cross uses toolchain file)
        if not toolchain_file:
            all_cflags = f"{cc_opts} {cflags}".strip()
            if all_cflags:
                cmake_cmd.append(f"-DCMAKE_C_FLAGS={all_cflags}")
                cmake_cmd.append(f"-DCMAKE_CXX_FLAGS={all_cflags}")
            if ldflags:
                cmake_cmd.append(f"-DCMAKE_EXE_LINKER_FLAGS={ldflags}")

        # Config dir
        cmake_cmd.append(f"-DCS_CONFDIR={confdir}")

        # USE_FLAGS as cmake -D variables (translated to cmake names)
        _CMAKE_FLAG_MAP = {
            "USE_SSL": "WITH_SSL",
            "USE_LIBCRYPTO": "HAVE_LIBCRYPTO",
            "USE_LIBUSB": "HAVE_LIBUSB",
            "USE_PCSC": "HAVE_PCSC",
            "USE_LIBDVBCSA": "HAVE_LIBDVBCSA",
            "USE_STAPI": "WITH_STAPI",
            "USE_STAPI5": "WITH_STAPI5",
            "USE_COMPRESS": "USE_COMPRESS",  # same name
            "USE_COOLAPI": "WITH_COOLAPI",
            "USE_COOLAPI2": "WITH_COOLAPI2",
            "USE_SU980": "WITH_SU980",
            "USE_AZBOX": "WITH_AZBOX",
            "USE_AMSMC": "WITH_AMSMC",
            "USE_MCA": "WITH_MCA",
            "USE_OPENSSL": "USE_OPENSSL",  # same name in cmake
            "USE_MBEDTLS": None,           # mbedtls is cmake default, no flag needed
        }
        for flag in use_flags:
            if "=" in flag:
                key, val = flag.split("=", 1)
                cmake_key = _CMAKE_FLAG_MAP.get(key, key)
                if cmake_key is not None:
                    cmake_cmd.append(f"-D{cmake_key}={val}")

        # Static libraries — cmake variable names differ from make
        b = self.target.build
        # Static library flags — cmake finds the .a files via sysroot
        if b.static_flags.get("SSL"): cmake_cmd.append("-DSTATIC_SSL=1")
        if b.static_flags.get("LIBCRYPTO"): cmake_cmd.append("-DSTATIC_LIBCRYPTO=1")
        if b.static_flags.get("LIBUSB"): cmake_cmd.append("-DSTATIC_LIBUSB=1")
        if b.static_flags.get("LIBDVBCSA"): cmake_cmd.append("-DSTATIC_LIBDVBCSA=1")
        if b.static_flags.get("PCSC"): cmake_cmd.append("-DSTATIC_PCSC=1")
        # Absolute-path overrides for Makefile parity — oscam cmake ≥
        # fix-cmake-unconditially-linking honours ``LIBCRYPTO_LIB`` and
        # ``SSL_LIB`` and uses them verbatim, bypassing its own
        # ``find_library`` auto-discovery. Passing them alongside the
        # ``STATIC_*`` flags guarantees cmake links the exact same .a
        # that ``make`` would — avoids drift when a sysroot contains
        # more than one OpenSSL install.
        if "LIBCRYPTO_LIB" in static_libs:
            cmake_cmd.append(f"-DLIBCRYPTO_LIB={static_libs['LIBCRYPTO_LIB']}")
        if "SSL_LIB" in static_libs:
            cmake_cmd.append(f"-DSSL_LIB={static_libs['SSL_LIB']}")

        # STAPI
        if stapi_lib and "=" in stapi_lib:
            key, val = stapi_lib.split("=", 1)
            cmake_cmd.append(f"-D{key}={val}")

        self._log("  Configuring (cmake)...")
        ok = self._run(cmake_cmd, cwd=build_dir)
        if not ok:
            self._log("[red]cmake configure FAILED[/red]")
            return False, None
        # Stash the -D args so _run_unit_tests_cmake can reconfigure a
        # separate ``build_s4_tests/`` dir with the same cross-compile
        # setup plus ``-DBUILD_TESTS=ON``. oscam's cmake replaces the
        # main executable target with ``tests.bin`` when that flag is
        # on, so the test configure has to live in its own build dir
        # to avoid clobbering the real binary.
        self._last_cmake_args = [a for a in cmake_cmd[2:] if a.startswith("-D")]

        # --- cmake build ---
        self._log("  Compiling...")
        build_cmd = ["cmake", "--build", ".", "--", f"-j{self.max_cpus}"]
        ok = self._run(build_cmd, cwd=build_dir, colorize=True)

        if not ok:
            return False, None

        # --- Find and copy the built binary ---
        # cmake builds to build_dir/oscam or build_dir/oscam-upx
        cmake_bin = None
        for candidate in ("oscam-upx", "oscam"):
            p = build_dir / candidate
            if p.exists():
                cmake_bin = p
                break

        if not cmake_bin:
            self._log("[red]cmake binary not found in build directory[/red]")
            return False, None

        # Copy to our standard binaries location with proper name
        import shutil
        shutil.copy2(str(cmake_bin), str(bin_path))
        return True, bin_path

    def _resolve_binary_path(self, repo_dir: Path, repo_name: str, tc_name: str, is_native: bool, b, tcc=None) -> Path:
        """Resolve the final output binary Path from b.use_oscamname.

        Variants:
          1. empty                          → data/binaries/<auto-generated>
          2. bare filename (no '/')         → handled by editor as data/binaries/<name>;
                                              here it would behave like variant 4
          3. directory ending with '/'      → <dir>/<auto-generated>
          4. full path with filename        → <path>
        Tilde is expanded; relative paths are resolved against the
        project root (binaries_dir.parent.parent).
        """
        import os
        # Per-TC override of oscam path
        tc_path = tcc.oscam_path if tcc and tcc.oscam_path else ""
        raw = (tc_path or b.use_oscamname or "").strip()
        # Variant 1 — empty: default location, auto name
        if not raw:
            name = self._generate_binary_name(repo_dir, repo_name, tc_name, is_native, b)
            return self.binaries_dir / name
        raw = os.path.expanduser(raw)
        root = self.binaries_dir.parent.parent
        # Variant 3 — directory only (trailing slash): auto name in that dir
        if raw.endswith("/"):
            d = Path(raw)
            if not d.is_absolute():
                d = root / d
            name = self._generate_binary_name(repo_dir, repo_name, tc_name, is_native, b)
            return d / name
        # Variants 2 and 4 — full file path
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        return p

    def _generate_binary_name(self, repo_dir: Path, repo_name: str, tc_name: str, is_native: bool, b) -> str:
        """Generate the output binary name.

        Format: oscam-<vcs><rev>@<commit>-<repo>-<toolchain>-<backend>-<features>-<linking>
        """
        # NOTE: use_oscamname is now handled in _resolve_binary_path. This
        # method is only called when an auto-generated name is needed.

        # Revision info — detect VCS type
        # Like s3: use config.sh --oscam-revision for the revision number
        is_git = (repo_dir / ".git").exists()
        is_svn = (repo_dir / ".svn").exists()
        config_sh = repo_dir / "config.sh"

        if is_git:
            vcs = "git"
            # Use config.sh --oscam-revision like s3
            rev = ""
            if config_sh.exists():
                from core.oscam_config import _run_config_sh
                rev = _run_config_sh(config_sh, ["--oscam-revision"]) or ""
                if not rev:
                    ver = _run_config_sh(config_sh, ["--oscam-version"]) or ""
                    rev = ver.split("-", 1)[1] if "-" in ver else ver
            if not rev:
                rev = self._run_output(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir) or "0"
            commit = self._run_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir) or "unknown"
        elif is_svn:
            vcs = "svn"
            # Use config.sh --oscam-revision like s3 (cleaner than svnversion)
            if config_sh.exists():
                from core.oscam_config import _run_config_sh
                rev = _run_config_sh(config_sh, ["--oscam-revision"]) or ""
                if not rev:
                    # Fallback: config.sh --oscam-version | cut revision part
                    ver = _run_config_sh(config_sh, ["--oscam-version"]) or ""
                    rev = ver.split("-", 1)[1] if "-" in ver else ver
            if not rev:
                rev = self._run_output(["svnversion"], cwd=repo_dir) or "0"
            commit = ""
        else:
            vcs = "git"
            rev = "0"
            commit = "unknown"

        # Toolchain part
        if is_native:
            tc_part = f"{socket.gethostname().split('.')[0]}-{os.uname().machine}"
        else:
            tc_part = tc_name

        # SSL backend (openssl vs mbedtls) — check Makefile for dual backend support
        backend = ""
        makefile = repo_dir / "Makefile"
        if makefile.exists():
            try:
                mk_content = makefile.read_text(encoding="utf-8", errors="replace")
                if "USE_OPENSSL" in mk_content:
                    if b.use_flags.get("OPENSSL"):
                        backend = "-openssl"
                    else:
                        backend = "-mbedtls"
            except Exception:
                pass

        # Feature suffixes (alphabetically sorted)
        features = []
        from core.oscam_config import _run_config_sh
        config_sh = repo_dir / "config.sh"
        if config_sh.exists():
            checks = [
                ("HAVE_DVBAPI", "dvbapi"),
                ("WITH_EMU", "emu"),
                ("CARDREADER_GXAPI", "gxapi"),
                ("MODULE_STREAMRELAY", "icam"),
                ("IPV6SUPPORT", "ipv6"),
                ("WITH_ARM_NEON", "neon"),
                ("WITH_SIGNING", "signed"),
                ("WEBIF", "webif"),
            ]
            for option, suffix in checks:
                result = _run_config_sh(config_sh, ["--enabled", option])
                if result == "Y":
                    features.append(suffix)

        # USE_FLAGS based features
        _feature_map = {
            "SSL": "ssl",
            "LIBDVBCSA": "libdvbcsa", "LIBUSB": "libusb", "PCSC": "pcsc",
            "STAPI": "stapi", "STAPI5": "stapi5",
        }
        for flag, suffix in _feature_map.items():
            if b.use_flags.get(flag) and suffix not in features:
                features.append(suffix)
        if b.use_compress:
            features.append("upx")

        features.sort()

        # Linking type
        _linkable = {"LIBCRYPTO", "SSL", "LIBUSB", "PCSC", "LIBDVBCSA"}
        lib_count = sum(1 for f in _linkable if b.use_flags.get(f))
        stat_count = sum(1 for f in _linkable if b.static_flags.get(f))
        if stat_count > 0 and stat_count >= lib_count:
            linking = "-static"
        elif stat_count > 0:
            linking = "-mixed"
        else:
            linking = ""

        # Assemble
        feat_str = "-" + "-".join(features) if features else ""
        # Git: oscam-git11945@6c8324e0-default-cortexa15hf-features
        # SVN: oscam-svn2039-default-cortexa15hf-features (no @commit)
        if commit:
            return f"oscam-{vcs}{rev}@{commit}-{repo_name}-{tc_part}{backend}{feat_str}{linking}"
        else:
            return f"oscam-{vcs}{rev}-{repo_name}-{tc_part}{backend}{feat_str}{linking}"

    def _get_native_lib_paths(self, tc_dir: Path) -> list[str]:
        """Get library search paths for native toolchain.

        Includes native toolchain local libs (data/toolchains/native/)
        AND system library paths from ldconfig.
        """
        paths = []
        # Native toolchain local libs first (higher priority)
        for d in tc_dir.glob("**/lib"):
            if d.is_dir():
                paths.append(str(d))
        # System paths
        try:
            result = subprocess.run(
                ["ldconfig", "-v"],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "LC_ALL": "C"},
            )
            for line in result.stderr.splitlines() + result.stdout.splitlines():
                if line and not line.startswith("\t") and ":" in line:
                    path = line.split(":")[0].strip()
                    if path and Path(path).is_dir():
                        paths.append(path)
        except Exception:
            paths.extend(["/usr/lib", "/usr/local/lib", "/lib"])
        return paths

    def _find_static_lib(self, lib_name: str, search_dirs: list[str]) -> str:
        """Find a static library (.a) file in search directories."""
        for search_dir in search_dirs:
            for root, dirs, files in os.walk(search_dir):
                if lib_name in files:
                    return os.path.join(root, lib_name)
        return ""

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _find_repo_dir(self, name: str) -> Optional[Path]:
        """Find the repository directory for a given source name."""
        candidates = [
            self.repos_dir / name,
            self.repos_dir / f"oscam-{name}",
        ]
        if self.repos_dir.is_dir():
            for d in self.repos_dir.iterdir():
                if d.is_dir() and name in d.name and d not in candidates:
                    candidates.append(d)
        for d in candidates:
            if d.is_dir():
                return d
        return None

    def _run(self, cmd: list[str], cwd: Path, colorize: bool = False) -> bool:
        """Run a command and log output. Returns True on success.

        If colorize=True, applies s3-style coloring to make/build output.
        Filtered lines still go to the log file (plain), just not to the GUI.
        """
        try:
            from rich.markup import escape as rich_escape
            proc = subprocess.Popen(
                cmd, cwd=str(cwd),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:
                if self._cancel():
                    proc.kill()
                    proc.wait()
                    return False
                stripped = line.rstrip()
                if colorize:
                    colored = self._colorize_build_line(stripped)
                    if colored is not None:
                        self._log(f"    {colored}")
                    else:
                        # Filtered from GUI but still write to log file
                        self._log_file_only(f"    {stripped}")
                else:
                    self._log(f"    {rich_escape(stripped)}")
            proc.wait()
            return proc.returncode == 0
        except Exception as e:
            self._log(f"  [red]Error: {e}[/red]")
            return False

    # Build output coloring (like s3)
    _BUILD_COLORS = [
        # (prefix, rich color, label)       — order matters, first match wins
        ("HOSTCC",  "green",        "BUILD ------> "),
        ("CC\t",    "green",        "BUILD ------> "),
        ("BUILD\t", "green",        "BUILD ------> "),
        ("GEN\t",   "cyan",         "GEN --------> "),
        ("WIKI\t",  "cyan",         "WIKI -------> "),
        ("CONF\t",  "cyan",         "CONFIG -----> "),
        ("LINK\t",  "plum1", "LINK -------> "),
        ("STRIP\t", "plum1", "STRIP ------> "),
        ("RM\t",    "red",          "REMOVE -----> "),
        ("UPX\t",   "bright_green", "UPX --------> "),
        ("UPX ",    "bright_green", "UPX --------> "),
        ("SIGN\t",  "bright_yellow","SIGN -------> "),
        ("SIGN ",   "bright_yellow","SIGN -------> "),
    ]

    def _colorize_build_line(self, line: str) -> str | None:
        """Colorize a make/cmake output line like s3. Returns None to skip the line."""
        from rich.markup import escape as rich_escape

        stripped = line.lstrip()

        # Info block lines (| ...) → yellow
        if stripped.startswith("|") or stripped.startswith("+---"):
            return f"[yellow]{rich_escape(stripped)}[/yellow]"

        # Submodule lines + mbedTLS tarball-fetch status
        # (``mbedTLS 4.1.0 ready (tarball 6.7M, cached)`` / ``fetched``)
        _low = stripped.lower()
        if "submodule" in _low or (
            "mbedtls " in _low and "ready" in _low and "tarball" in _low
        ):
            return f"[cyan]{rich_escape(stripped)}[/cyan]"

        # Known make prefixes
        for prefix, color, label in self._BUILD_COLORS:
            if stripped.startswith(prefix):
                rest = stripped[len(prefix):].strip()
                return f"[{color}]{label}[/{color}]{rich_escape(rest)}"

        # cmake output: [  5%] Building C object ..., Linking C ..., Built target ...
        if stripped.startswith("[") and "%]" in stripped[:8]:
            rest = stripped.split("]", 1)[1].strip() if "]" in stripped else stripped
            pct = stripped.split("]")[0].strip("[ %")
            if "Linking" in rest:
                return f"[plum1]LINK -------> \\[{pct}%][/plum1] {rich_escape(rest)}"
            elif "Built target" in rest:
                return f"[green]DONE -------> \\[{pct}%][/green] {rich_escape(rest)}"
            elif "Building" in rest:
                # Shorten: "Building C object CMakeFiles/csreaders.dir/reader-nagra.c.o" → "reader-nagra.c"
                short = rest
                if ".dir/" in rest:
                    short = rest.split(".dir/")[-1].replace(".o", "")
                return f"[green]BUILD ------> \\[{pct}%][/green] {rich_escape(short)}"
            else:
                return f"    \\[{pct}%] {rich_escape(rest)}"

        # cmake status messages (-- ...)
        if stripped.startswith("-- "):
            return f"[cyan]{rich_escape(stripped)}[/cyan]"

        # cmake [SIGN] [UPX] markers
        if stripped.startswith("[SIGN]"):
            return f"[bright_yellow]SIGN -------> [/bright_yellow]{rich_escape(stripped[6:].strip())}"
        if stripped.startswith("[ UPX]") or stripped.startswith("[UPX]"):
            rest = stripped.split("]", 1)[1].strip()
            return f"[bright_green]UPX --------> [/bright_green]{rich_escape(rest)}"

        # Error/warning lines (colon suffix avoids false positives from filenames)
        lower = stripped.lower()
        if any(kw in lower for kw in ("error:", "warning:", "fatal:", "undefined")):
            if "warning:" in lower:
                self._warning_count += 1
            return f"[red]{rich_escape(stripped)}[/red]"

        # Skip unknown lines (noise from compiler, BFD warnings, etc.)
        return None

    def _run_output(self, cmd: list[str], cwd: Path) -> str:
        """Run a command and return stdout (stripped)."""
        try:
            result = subprocess.run(
                cmd, cwd=str(cwd),
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout.strip()
        except Exception:
            return ""
