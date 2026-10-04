"""Private gVisor execution workers for model-controlled commands and files.

This module must run inside the dedicated Linux distro. The controller may hold
provider credentials; none are passed to worker containers.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import socket
import stat
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_FILE_LIMIT = 1_000_000
_OUTPUT_LIMIT = 1_000_000
_FILE_ERROR_DIAGNOSTIC_LIMIT = 512

_FILE_PROGRAM = r'''
import os, stat, sys
mode, path = sys.argv[1:3]
parts = path.split('/')
if not path or path.startswith('/') or any(p in ('', '.', '..') for p in parts):
    raise ValueError('path must be a relative path beneath /work')
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
fd = os.open('/work', flags)
try:
    for part in parts[:-1]:
        if mode == 'write':
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
        next_fd = os.open(part, flags, dir_fd=fd)
        os.close(fd)
        fd = next_fd
    if mode == 'read':
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ValueError('not a regular file')
            data = os.read(file_fd, 1000001)
            if len(data) > 1000000:
                raise ValueError('file exceeds limit')
            sys.stdout.buffer.write(data)
        finally:
            os.close(file_fd)
    elif mode == 'write':
        data = sys.stdin.buffer.read(1000001)
        if len(data) > 1000000:
            raise ValueError('file exceeds limit')
        file_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ValueError('not a regular file')
            os.write(file_fd, data)
        finally:
            os.close(file_fd)
    else:
        raise ValueError('unknown file operation')
finally:
    os.close(fd)
'''

_LIST_PROGRAM = r'''
import json, os, stat, sys
path = sys.argv[1]
fd = os.open('/work', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
if path != '.':
    for part in path.split('/'):
        next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd)
        fd = next_fd
files = []
try:
    for root, dirs, names, root_fd in os.fwalk('.', dir_fd=fd, follow_symlinks=False):
        dirs[:] = sorted(dirs)
        for name in sorted(names):
            if stat.S_ISREG(os.stat(name, dir_fd=root_fd, follow_symlinks=False).st_mode):
                relative = os.path.join(path, root, name).replace('\\', '/')
                files.append(os.path.normpath(relative))
                if len(files) > 10000:
                    raise ValueError('too many files')
finally:
    os.close(fd)
print(json.dumps(files))
'''

_SEAL_PROGRAM = r'''
import os
for root, dirs, files in os.walk('/work', topdown=False, followlinks=False):
    for name in files:
        path = os.path.join(root, name)
        if not os.path.islink(path):
            os.chmod(path, 0o444)
    for name in dirs:
        path = os.path.join(root, name)
        if not os.path.islink(path):
            os.chmod(path, 0o555)
os.chmod('/work', 0o555)
'''


class IsolationError(RuntimeError):
    """The worker could not be created, used, or verified."""


class OutputLimitError(IsolationError):
    """The worker produced more output than the controller accepts."""


class PathAccessError(ValueError):
    """An agent requested a path it cannot access."""


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False
    duration_s: float = 0.0


class GVisorIsolation:
    """One persistent, networkless runsc container per worker ID.

    Each `/work` is a private 64 MiB tmpfs. No Windows or controller directory is
    bind mounted. `docker exec` commands run with the container's non-root UID.
    """

    _qualified_cache: OrderedDict[tuple[object, ...], dict[str, object]] = OrderedDict()
    _qualified_cache_limit = 8
    _qualified_cache_lock = threading.Lock()
    _qualification_tasks: dict[
        tuple[asyncio.AbstractEventLoop, tuple[object, ...]], asyncio.Task[dict[str, object]]
    ] = {}

    def __init__(
        self,
        *,
        image: str = "python:3.11-slim",
        memory: str = "256m",
        cpus: float = 0.5,
        pids_limit: int = 64,
        output_limit: int = _OUTPUT_LIMIT,
        max_concurrent_docker_operations: int = 8,
    ) -> None:
        if type(max_concurrent_docker_operations) is not int or max_concurrent_docker_operations < 1:
            raise ValueError("max_concurrent_docker_operations must be a positive integer")
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.output_limit = output_limit
        self.max_concurrent_docker_operations = max_concurrent_docker_operations
        self._docker_slots = asyncio.Semaphore(max_concurrent_docker_operations)
        self._workers: dict[str, str] = {}
        self._qualification: dict[str, object] | None = None
        self._qualification_key: tuple[object, ...] | None = None

    @staticmethod
    def _worker_id(worker_id: str) -> None:
        if not _ID.fullmatch(worker_id) or worker_id in {".", ".."}:
            raise ValueError("invalid worker ID")

    @staticmethod
    def _path(path: str) -> None:
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise PathAccessError("file path must be relative to /work")
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise PathAccessError("path traversal or empty component")
        if "\x00" in path or "\\" in path:
            raise PathAccessError("invalid file path")

    async def _docker(
        self,
        *args: str,
        input_data: bytes | None = None,
        timeout: float = 30,
        output_limit: int = _OUTPUT_LIMIT,
    ) -> tuple[int, bytes, bytes]:
        # Queueing is covered by the caller's agent/population deadline. The
        # command timeout starts only after this instance has a subprocess slot.
        async with self._docker_slots:
            return await self._run_docker(
                *args, input_data=input_data, timeout=timeout, output_limit=output_limit,
            )

    async def _run_docker(
        self,
        *args: str,
        input_data: bytes | None = None,
        timeout: float = 30,
        output_limit: int = _OUTPUT_LIMIT,
    ) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read_capped(stream: asyncio.StreamReader) -> bytes:
            data = bytearray()
            while chunk := await stream.read(65536):
                data.extend(chunk)
                if len(data) > output_limit:
                    raise OutputLimitError("Docker command exceeded output limit")
            return bytes(data)

        async def write_input() -> None:
            if input_data is None:
                return
            assert proc.stdin is not None
            try:
                proc.stdin.write(input_data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        try:
            assert proc.stdout is not None and proc.stderr is not None
            input_task = asyncio.create_task(write_input())
            stdout_task = asyncio.create_task(read_capped(proc.stdout))
            stderr_task = asyncio.create_task(read_capped(proc.stderr))
            wait_task = asyncio.create_task(proc.wait())
            try:
                _, stdout, stderr, code = await asyncio.wait_for(
                    asyncio.gather(input_task, stdout_task, stderr_task, wait_task), timeout=timeout
                )
            finally:
                for task in (input_task, stdout_task, stderr_task, wait_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(input_task, stdout_task, stderr_task, wait_task, return_exceptions=True)
            return code, stdout, stderr
        except (TimeoutError, OutputLimitError, asyncio.CancelledError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.communicate(), timeout=3)
            except TimeoutError:
                pass
            raise

    async def _check_runtime(self) -> None:
        if os.name != "posix":
            raise IsolationError("GVisorIsolation must run inside the Linux distro")
        code, out, err = await self._docker("info", "--format", "{{json .Runtimes}}")
        if code or "runsc" not in json.loads(out):
            raise IsolationError(f"Docker runsc runtime unavailable: {err.decode(errors='replace')}")

    async def create(
        self, worker_id: str, files: dict[str, str | bytes] | None = None,
        readonlywork: bool = False,
    ) -> None:
        self._worker_id(worker_id)
        if worker_id in self._workers:
            raise IsolationError(f"worker already exists: {worker_id}")
        await self._check_runtime()
        if worker_id in self._workers:
            raise IsolationError(f"worker already exists: {worker_id}")
        name = f"sab-{uuid.uuid4().hex[:20]}"
        args = (
            "run", "-d", "--name", name, "--runtime=runsc", "--network=none",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user=1000:1000", "--workdir=/work", "--env=HOME=/work",
            "--memory", self.memory, "--memory-swap", self.memory,
            "--cpus", str(self.cpus), "--pids-limit", str(self.pids_limit),
            "--ulimit", "fsize=8388608:8388608",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m,mode=1777",
            "--tmpfs", (
                "/work:rw,nosuid,nodev,size=64m,uid=0,gid=0,mode=0755"
                if readonlywork else
                "/work:rw,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=0700"
            ),
            self.image, "sleep", "infinity",
        )
        # Docker may create the named container before its client returns. Keep
        # that exact identity available for cleanup even on timeout/cancellation.
        self._workers[worker_id] = name
        try:
            code, out, err = await self._docker(*args, timeout=90)
            if code:
                raise IsolationError(
                    "runsc worker start failed: " + self._operation_diagnostic("create", code, out, err)
                )
            for path, content in (files or {}).items():
                await self._write_file(
                    worker_id, path, content, as_root=readonlywork,
                    operation="worker_initial_file_write",
                )
            if readonlywork:
                await self.seal(worker_id)
        except (Exception, asyncio.CancelledError) as error:
            try:
                await self.destroy(worker_id)
            except (Exception, asyncio.CancelledError) as cleanup_error:
                primary = self._redact_diagnostic(f"{type(error).__name__}: {error}")[:2048]
                cleanup = self._redact_diagnostic(f"{type(cleanup_error).__name__}: {cleanup_error}")[:2048]
                raise IsolationError(
                    f"worker provisioning failed ({primary}); exact container cleanup failed "
                    f"(worker_id={worker_id!r}, container_name={name!r}, {cleanup})"
                ) from error
            raise

    async def execute(self, worker_id: str, argv: list[str], timeout: float = 30) -> ExecutionResult:
        name = self._workers[worker_id]
        started = time.monotonic()
        if not argv or not all(isinstance(value, str) and value and "\x00" not in value for value in argv):
            raise ValueError("argv must contain nonempty strings")
        if timeout <= 0 or timeout > 3600:
            raise ValueError("timeout must be between 0 and 3600 seconds")
        try:
            code, out, err = await self._docker(
                "exec", name, "timeout", "-s", "KILL", "-k", "2", str(timeout), *argv,
                timeout=timeout + 10,
                output_limit=self.output_limit,
            )
        except (TimeoutError, OutputLimitError) as exc:
            await self._docker("kill", name, timeout=10)
            return ExecutionResult(
                137, "", str(exc), timed_out=isinstance(exc, TimeoutError),
                output_limited=isinstance(exc, OutputLimitError), duration_s=time.monotonic() - started,
            )
        return ExecutionResult(
            code, out.decode(errors="replace"), err.decode(errors="replace"),
            timed_out=code in (124, 137), duration_s=time.monotonic() - started,
        )

    async def read_file(self, worker_id: str, path: str) -> bytes:
        self._path(path)
        name = self._workers[worker_id]
        code, out, err = await self._docker(
            "exec", name, "python", "-I", "-S", "-c", _FILE_PROGRAM, "read", path,
            output_limit=_FILE_LIMIT + 1024,
        )
        if code:
            self._raise_file_error("read_file", code, out, err)
        return out

    async def write_file(self, worker_id: str, path: str, data: str | bytes) -> None:
        await self._write_file(worker_id, path, data, as_root=False)

    async def _write_file(
        self, worker_id: str, path: str, data: str | bytes, *, as_root: bool,
        operation: str = "write_file",
    ) -> None:
        self._path(path)
        payload = data.encode() if isinstance(data, str) else data
        if len(payload) > _FILE_LIMIT:
            raise ValueError("file exceeds 1 MB limit")
        name = self._workers[worker_id]
        code, out, err = await self._docker(
            "exec", "-i", *(("--user=0:0",) if as_root else ()), name,
            "python", "-I", "-S", "-c", _FILE_PROGRAM, "write", path,
            input_data=payload,
        )
        if code:
            self._raise_file_error(operation, code, out, err)

    @staticmethod
    def _raise_file_error(operation: str, exit_code: int, stdout: bytes, stderr: bytes) -> None:
        message = stderr.decode(errors="replace")
        if "Traceback" in message:
            final_line = next((line for line in reversed(message.splitlines()) if line.strip()), message)
            raise PathAccessError(GVisorIsolation._redact_diagnostic(final_line))

        raise IsolationError(GVisorIsolation._operation_diagnostic(operation, exit_code, stdout, stderr))

    @staticmethod
    def _operation_diagnostic(operation: str, exit_code: int, stdout: bytes, stderr: bytes) -> str:
        safe_stdout = GVisorIsolation._redact_diagnostic(stdout.decode(errors="replace"))
        safe_stderr = GVisorIsolation._redact_diagnostic(stderr.decode(errors="replace"))
        stdout_preview = safe_stdout[:_FILE_ERROR_DIAGNOSTIC_LIMIT]
        stdout_truncated = len(safe_stdout) > _FILE_ERROR_DIAGNOSTIC_LIMIT
        stderr_preview = safe_stderr[:_FILE_ERROR_DIAGNOSTIC_LIMIT]
        stderr_truncated = len(safe_stderr) > _FILE_ERROR_DIAGNOSTIC_LIMIT
        return (
            f"worker file operation {operation} failed "
            f"(exit_code={exit_code}, stdout_bytes={len(stdout)}, stderr_bytes={len(stderr)}, "
            f"stdout_truncated={str(stdout_truncated).lower()}, stdout_preview={stdout_preview!r}, "
            f"stderr_truncated={str(stderr_truncated).lower()}, stderr_preview={stderr_preview!r})"
        )

    @staticmethod
    def _redact_diagnostic(message: str) -> str:
        """Remove common credential forms before error text enters controller logs."""
        message = re.sub(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", message,
        )
        message = re.sub(
            r"(?i)\b(api[_-]?key|token|password|secret|credential)\b(\s*[:=]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
            r"\1\2[REDACTED]", message,
        )
        message = re.sub(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16})\b",
            "[REDACTED_TOKEN]", message,
        )
        return message

    async def list_files(self, worker_id: str, path: str = ".") -> list[str]:
        if path != ".":
            self._path(path)
        name = self._workers[worker_id]
        code, out, err = await self._docker("exec", name, "python", "-I", "-S", "-c", _LIST_PROGRAM, path)
        if code:
            self._raise_file_error("list_files", code, out, err)
        return json.loads(out)

    async def seal(self, worker_id: str) -> None:
        """Make initial worker files immutable to its non-root process."""
        name = self._workers[worker_id]
        code, out, err = await self._docker("exec", "--user=0:0", name, "python", "-I", "-S", "-c", _SEAL_PROGRAM)
        if code:
            state = await self._failed_worker_state(name)
            raise IsolationError(
                "could not seal worker: " + self._operation_diagnostic("seal", code, out, err)
                + f"; container_state={state}"
            )

    async def _failed_worker_state(self, name: str) -> str:
        """Best-effort bounded evidence before create() removes a failed worker."""
        try:
            code, out, err = await self._docker(
                "inspect", name, "--format", "{{json .State}}", timeout=10, output_limit=4096,
            )
            if code:
                return self._operation_diagnostic("inspect_failed_worker", code, out, err)
            state = json.loads(out)
            if not isinstance(state, dict):
                raise ValueError("container state is not an object")
            selected = {key: state[key] for key in (
                "Status", "Running", "Paused", "Restarting", "OOMKilled", "Dead", "ExitCode", "Error",
            ) if key in state}
            safe = self._redact_diagnostic(json.dumps(selected, sort_keys=True))
            return safe[:_FILE_ERROR_DIAGNOSTIC_LIMIT]
        except Exception as error:
            return self._redact_diagnostic(f"unavailable: {type(error).__name__}: {error}")[
                :_FILE_ERROR_DIAGNOSTIC_LIMIT
            ]

    async def destroy(self, worker_id: str) -> None:
        name = self._workers.get(worker_id)
        if name is not None:
            code, out, err = await self._docker("rm", "-f", name, timeout=30)
            # Exact-name absence is safe after an interrupted startup or another
            # cleanup call. Other errors leave the identity available to reconcile.
            already_absent = code == 1 and err.decode(errors="replace").strip() == (
                f"Error response from daemon: No such container: {name}"
            )
            if code and not already_absent:
                raise IsolationError(
                    "could not remove worker: " + self._operation_diagnostic("destroy", code, out, err)
                )
            if self._workers.get(worker_id) == name:
                del self._workers[worker_id]

    async def metadata(self) -> dict[str, object]:
        await self._check_runtime()
        _, docker_version, _ = await self._docker("version", "--format", "{{.Server.Version}}")
        _, image_id, _ = await self._docker("image", "inspect", self.image, "--format", "{{.Id}}")
        proc = await asyncio.create_subprocess_exec("runsc", "--version", stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        fingerprint = (
            stdout.decode(errors="replace").strip(), docker_version.decode().strip(),
            image_id.decode().strip(), self.memory, self.cpus, self.pids_limit,
            self.output_limit, self.max_concurrent_docker_operations,
        )
        if self._qualification_key != fingerprint or not (
            self._qualification and self._qualification.get("verified")
        ):
            self._qualification = await self._qualification_for(fingerprint)
            self._qualification_key = fingerprint if self._qualification.get("verified") else None
        outer = await self._outer_boundary()
        workers: dict[str, str] = {}
        for worker_id, name in self._workers.items():
            code, out, _ = await self._docker("inspect", name, "--format", "{{.HostConfig.Runtime}}")
            if code or out.strip() != b"runsc":
                raise IsolationError(f"worker {worker_id} is not using runsc")
            workers[worker_id] = name
        return {
            "runtime": "runsc", "runsc_version": stdout.decode(errors="replace").strip(),
            "docker_version": docker_version.decode().strip(),
            "image": self.image, "image_id": image_id.decode().strip(), "workers": workers,
            "network": "none", "workspace": "private tmpfs", "memory": self.memory,
            "cpus": self.cpus, "pids_limit": self.pids_limit,
            "max_concurrent_docker_operations": self.max_concurrent_docker_operations,
            "docker_operation_limit_scope": "per GVisorIsolation instance; Docker subprocess lifetimes",
            "docker_timeout_scope": "after operation-slot acquisition; caller deadlines include queueing",
            "verified": bool(self._qualification["verified"] and outer["verified"]),
            "qualification": self._qualification,
            "outer_boundary": outer,
        }

    async def _qualification_for(self, fingerprint: tuple[object, ...]) -> dict[str, object]:
        """Share an in-flight probe within this event loop and cache passes only."""
        cls = type(self)
        with cls._qualified_cache_lock:
            cached = cls._qualified_cache.get(fingerprint)
            if cached is not None:
                cls._qualified_cache.move_to_end(fingerprint)
                return cached

        loop = asyncio.get_running_loop()
        task_key = (loop, fingerprint)
        task = cls._qualification_tasks.get(task_key)
        if task is None:
            async def run_probe() -> dict[str, object]:
                try:
                    result = await self.probe_boundaries()
                    if result.get("verified") is True:
                        with cls._qualified_cache_lock:
                            cls._qualified_cache[fingerprint] = result
                            cls._qualified_cache.move_to_end(fingerprint)
                            while len(cls._qualified_cache) > cls._qualified_cache_limit:
                                cls._qualified_cache.popitem(last=False)
                    return result
                finally:
                    current = asyncio.current_task()
                    if cls._qualification_tasks.get(task_key) is current:
                        del cls._qualification_tasks[task_key]

            task = loop.create_task(run_probe())
            cls._qualification_tasks[task_key] = task
        return await asyncio.shield(task)

    async def _outer_boundary(self) -> dict[str, object]:
        """Verify that the controller runs in the isolated QEMU guest."""
        checks: dict[str, bool] = {}
        process = await asyncio.create_subprocess_exec(
            "systemd-detect-virt", "--vm", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        virt, _ = await process.communicate()
        checks["qemu_vm"] = process.returncode == 0 and virt.strip() in (b"kvm", b"qemu")
        with open("/proc/mounts", encoding="utf-8") as mounts_file:
            mounts = mounts_file.read()
        checks["no_host_filesystem_share"] = (
            not any(f" {kind} " in mounts for kind in ("drvfs", "9p", "virtiofs"))
            and not os.path.exists("/mnt/c/Windows")
            and not os.path.exists("/mnt/d")
        )

        def direct_blocked() -> bool:
            try:
                with socket.create_connection(("1.1.1.1", 443), timeout=2):
                    return False
            except OSError:
                return True

        def host_blocked() -> bool:
            try:
                with socket.create_connection(("10.0.2.2", 2222), timeout=2):
                    return False
            except OSError:
                return True

        def proxy_status(host: str) -> bytes:
            with socket.create_connection(("10.0.2.100", 3128), timeout=5) as connection:
                connection.settimeout(5)
                connection.sendall(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
                return connection.recv(1024)

        checks["direct_internet_blocked"] = await asyncio.to_thread(direct_blocked)
        checks["direct_host_blocked"] = await asyncio.to_thread(host_blocked)
        proxy_responses: dict[str, str] = {}
        for name, host, expected in (
            ("private_proxy_denied", "127.0.0.1", b"403 Forbidden"),
            ("other_public_proxy_denied", "example.com", b"403 Forbidden"),
            ("provider_proxy_allowed", "chatgpt.com", b"200 Connection Established"),
        ):
            try:
                response = await asyncio.to_thread(proxy_status, host)
                proxy_responses[name] = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")[:200]
                checks[name] = expected in response
            except OSError as error:
                checks[name] = False
                proxy_responses[name] = type(error).__name__
        return {"verified": all(checks.values()), "checks": checks, "proxy_responses": proxy_responses}

    async def probe_boundaries(self) -> dict[str, object]:
        """Exercise real runsc workers. A failed check forbids live model use."""
        prefix = f"probe_{uuid.uuid4().hex[:10]}"
        a, b, sealed, flood, pid_worker = (
            f"{prefix}_{suffix}" for suffix in ("a", "b", "sealed", "flood", "pid")
        )
        checks: dict[str, bool] = {}
        failures: dict[str, str] = {}
        probe_diagnostics: dict[str, dict[str, object]] = {}
        canaries = {
            "credential.txt": b"DUMMY_CREDENTIAL_MARKER_FOR_ISOLATION_PROBE\n",
            "evaluator.txt": b"DUMMY_EVALUATOR_MARKER_FOR_ISOLATION_PROBE\n",
            "controller.txt": b"DUMMY_CONTROLLER_MARKER_FOR_ISOLATION_PROBE\n",
            "events.txt": b"DUMMY_LOG_MARKER_FOR_ISOLATION_PROBE\n",
        }
        canary_hashes = {name: hashlib.sha256(content).hexdigest()
                         for name, content in canaries.items()}
        try:
            checks["protected_canaries_present"] = all(
                self._check_canary(name, content) for name, content in canaries.items()
            )
        except OSError as exc:
            checks["protected_canaries_present"] = False
            failures["protected_canaries_present"] = f"{type(exc).__name__}: {exc}"

        async def check(name: str, operation) -> None:
            try:
                checks[name] = bool(await operation())
                if not checks[name]:
                    failures[name] = "unexpected result"
            except Exception as exc:
                checks[name] = False
                failures[name] = f"{type(exc).__name__}: {exc}"

        async def denied_path() -> bool:
            try:
                await self.read_file(a, "../protected/credential.txt")
            except PathAccessError:
                return True
            return False

        async def denied_symlink() -> bool:
            result = await self.execute(a, ["ln", "-s", "/proc/1/root/etc/passwd", "escape-link"])
            if result.exit_code:
                return False
            try:
                await self.read_file(a, "escape-link")
            except PathAccessError:
                return True
            return False

        async def denied_fifo() -> bool:
            result = await self.execute(a, ["mkfifo", "blocked-fifo"])
            if result.exit_code:
                return False
            for operation in (
                self.read_file(a, "blocked-fifo"),
                self.write_file(a, "blocked-fifo", "blocked"),
            ):
                try:
                    await operation
                except PathAccessError:
                    continue
                return False
            return True

        async def authorized_work() -> bool:
            result = await self.execute(
                a, ["python", "-c", "from pathlib import Path; p=Path('work.txt'); p.write_text(p.read_text()+' done'); print(p.read_text())"]
            )
            return result.exit_code == 0 and result.stdout.strip() == "allowed done"

        async def shadowed_helpers() -> bool:
            await self.write_file(a, "json.py", "raise RuntimeError('UNTRUSTED_JSON_RAN')\n")
            await self.write_file(a, "sitecustomize.py", "raise RuntimeError('UNTRUSTED_SITE_RAN')\n")
            try:
                content = await self.read_file(a, "work.txt")
                names = await self.list_files(a)
                return content == b"allowed done" and "work.txt" in names
            finally:
                cleanup = await self.execute(a, ["rm", "-f", "json.py", "sitecustomize.py"])
                if cleanup.exit_code:
                    raise IsolationError("could not remove import shadow probes")

        async def denied_other_worker() -> bool:
            result = await self.execute(a, ["cat", "/work/peer_only.txt"])
            return result.exit_code != 0 and await self.read_file(b, "peer_only.txt") == b"PEER_DUMMY_MARKER"

        async def denied_protected() -> bool:
            for name in canaries:
                result = await self.execute(
                    a, ["python", "-c", f"open('/var/lib/swarm-auth-bench/protected/{name}').read()"]
                )
                if result.exit_code == 0 or "DUMMY_" in result.stdout:
                    return False
            return True

        async def denied_network() -> bool:
            result = await self.execute(a, [
                "python", "-c",
                "import socket; s=socket.socket(); s.settimeout(2); s.connect(('192.0.2.1', 443))",
            ])
            return result.exit_code != 0

        async def denied_host_and_docker() -> bool:
            result = await self.execute(a, [
                "python", "-c",
                "import os; print(int(os.path.exists('/mnt/c/Windows')), int(os.path.exists('/var/run/docker.sock')), int(os.path.exists('/opt/swarm-auth-bench')))"
            ])
            return result.exit_code == 0 and result.stdout.strip() == "0 0 0"

        async def process_boundary() -> bool:
            result = await self.execute(a, [
                "python", "-c",
                "from pathlib import Path; p=Path('/proc/1/environ').read_bytes(); print(int(b'CODEX_HOME=' in p or b'OPENAI_API_KEY=' in p or b'DOCKER_HOST=' in p))",
            ])
            return result.exit_code == 0 and result.stdout.strip() == "0"

        async def read_only_root() -> bool:
            result = await self.execute(a, ["sh", "-c", "echo escaped > /usr/local/bin/escaped"])
            return result.exit_code != 0

        async def process_timeout() -> bool:
            result = await self.execute(a, ["sleep", "8"], timeout=0.5)
            return result.timed_out and result.duration_s < 5

        async def descendant_timeout() -> bool:
            result = await self.execute(
                a, ["sh", "-c", "(sleep 2; echo leaked > /work/descendant.txt) & sleep 8"],
                timeout=0.5,
            )
            await asyncio.sleep(2.2)
            return result.timed_out and "descendant.txt" not in await self.list_files(a)

        async def pid_limit() -> bool:
            program = (
                "import errno, os, signal, time\n"
                "children=[]\n"
                "blocked_errno=0\n"
                "try:\n"
                " for i in range(100):\n"
                "  try: pid=os.fork()\n"
                "  except OSError as error: blocked_errno=error.errno; break\n"
                "  if pid == 0:\n"
                "   time.sleep(60)\n"
                "   os._exit(0)\n"
                "  children.append(pid)\n"
                "finally:\n"
                " print(len(children), blocked_errno, flush=True)\n"
                " for child in children:\n"
                "  try: os.kill(child, signal.SIGTERM)\n"
                "  except ProcessLookupError: pass\n"
                " for child in children:\n"
                "  try: os.waitpid(child, 0)\n"
                "  except ChildProcessError: pass\n"
            )
            await self.create(pid_worker)
            host_info = await self._worker_host_info(pid_worker)
            resource_path = host_info.get("resource_path") if host_info else None
            container_id = host_info.get("container_id") if host_info else None
            if not isinstance(resource_path, Path):
                resource_path = None
            if not isinstance(container_id, str):
                container_id = None
            scope_matches_container = self._resource_scope_matches_container(
                resource_path, container_id
            )
            samples: list[dict[str, object]] = []
            if resource_path is not None:
                initial = self._read_cgroup_resources(resource_path)
                if initial is not None:
                    samples.append(initial)

            stop_sampling = asyncio.Event()

            async def sample_resources() -> None:
                while resource_path is not None and not stop_sampling.is_set():
                    sample = self._read_cgroup_resources(resource_path)
                    if sample is not None:
                        samples.append(sample)
                    await asyncio.sleep(0.025)

            sampler = asyncio.create_task(sample_resources())
            try:
                result = await self.execute(pid_worker, ["python", "-c", program], timeout=30)
            finally:
                stop_sampling.set()
                await sampler

            if resource_path is not None:
                last = self._read_cgroup_resources(resource_path)
                if last is not None:
                    samples.append(last)
            state = await self._worker_state(pid_worker)
            resource_evidence = self._pid_resource_evidence(
                samples,
                resource_path,
                self.pids_limit,
                container_id=container_id,
                scope_matches_container=scope_matches_container,
            )
            kernel_rejection = None
            if isinstance(container_id, str) and isinstance(resource_path, Path):
                cgroup_path = "/" + resource_path.relative_to(Path("/sys/fs/cgroup")).as_posix()
                kernel_rejection = await self._kernel_pid_rejection(
                    container_id, cgroup_path
                )
            resource_evidence["kernel_pids_rejection"] = kernel_rejection

            neighbor = await self.execute(b, [
                "python", "-c",
                "from pathlib import Path; p=Path('pid-neighbor.txt'); p.write_text('alive'); print(p.read_text())",
            ])
            neighbor_ok = neighbor.exit_code == 0 and neighbor.stdout.strip() == "alive"
            try:
                canaries_unchanged = all(
                    self._check_canary(name, content) for name, content in canaries.items()
                )
                post_canary_hashes = {
                    name: hashlib.sha256(
                        Path(f"/var/lib/swarm-auth-bench/protected/{name}").read_bytes()
                    ).hexdigest()
                    for name in canaries
                }
                canaries_unchanged = canaries_unchanged and post_canary_hashes == canary_hashes
            except OSError:
                canaries_unchanged = False
                post_canary_hashes = {}
            outcome, cap_enforced = self._classify_pid_limit_result(
                result,
                self.pids_limit,
                resource_evidence,
                state,
                neighbor_ok=neighbor_ok,
                canaries_unchanged=canaries_unchanged,
            )

            probe_diagnostics["pid_limit"] = {
                **self._execution_diagnostic(result),
                "outcome": outcome,
                "worker_state": state,
                "worker_state_observed_before_cleanup": state,
                "resource_evidence": resource_evidence,
                "neighbor_authorized_work": {
                    **self._execution_diagnostic(neighbor),
                    "passed": neighbor_ok,
                },
                "protected_canaries_unchanged": canaries_unchanged,
                "protected_canary_sha256_after": post_canary_hashes,
            }
            return cap_enforced and neighbor_ok and canaries_unchanged

        async def storage_limit() -> bool:
            program = (
                "from pathlib import Path\n"
                "import errno\n"
                "chunk=b'x'*1048576\n"
                "reason=''\n"
                "for i in range(100):\n"
                " try: Path(f'quota-{i}').write_bytes(chunk)\n"
                " except OSError as e:\n"
                "  reason=e.errno\n"
                "  break\n"
                "print(reason)\n"
                "for p in Path('.').glob('quota-*'): p.unlink()\n"
            )
            result = await self.execute(a, ["python", "-c", program], timeout=20)
            probe_diagnostics["storage_limit"] = self._execution_diagnostic(result)
            return result.exit_code == 0 and result.stdout.strip() == "28"

        async def limits() -> bool:
            name = self._workers[a]
            code, out, _ = await self._docker(
                "inspect", name, "--format", "{{json .HostConfig}}"
            )
            if code:
                return False
            config = json.loads(out)
            return (
                config.get("Runtime") == "runsc"
                and config.get("NetworkMode") == "none"
                and config.get("ReadonlyRootfs") is True
                and config.get("PidsLimit") == self.pids_limit
                and config.get("Memory", 0) > 0
                and config.get("MemorySwap") == config.get("Memory")
                and config.get("CapDrop") == ["ALL"]
            )

        try:
            await self.create(a, {"work.txt": "allowed"})
            await self.create(b, {"peer_only.txt": "PEER_DUMMY_MARKER"})
            await check("authorized_work", authorized_work)
            await check("helper_import_shadowing", shadowed_helpers)
            await check("path_traversal", denied_path)
            await check("symlink", denied_symlink)
            await check("fifo", denied_fifo)
            await check("other_worker", denied_other_worker)
            await check("protected_dummy", denied_protected)
            await check("external_network", denied_network)
            await check("host_and_docker_paths", denied_host_and_docker)
            await check("process_environment", process_boundary)
            await check("read_only_root", read_only_root)
            await check("process_timeout", process_timeout)
            await check("descendant_timeout", descendant_timeout)
            await check("storage_limit", storage_limit)
            await check("runtime_and_limits", limits)

            await self.create(sealed, {"fixed.txt": "original"}, readonlywork=True)
            result = await self.execute(sealed, ["sh", "-c", "echo changed > fixed.txt"])
            checks["sealed_files"] = result.exit_code != 0 and await self.read_file(sealed, "fixed.txt") == b"original"
            if not checks["sealed_files"]:
                failures["sealed_files"] = "sealed workspace changed"

            await self.create(flood)
            result = await self.execute(flood, ["python", "-c", "print('x' * 2000000)"])
            checks["output_limit"] = result.output_limited
            if not checks["output_limit"]:
                failures["output_limit"] = "flood did not reach output cap"

            await self.destroy(a)
            await self.create(a)
            checks["fresh_workspace"] = await self.list_files(a) == []
            if not checks["fresh_workspace"]:
                failures["fresh_workspace"] = "workspace survived worker destruction"
            await self.destroy(a)
            await self.destroy(sealed)
            await self.destroy(flood)

            # Exhaustion can stop a runsc worker. Keep the disposable PID
            # worker separate and this probe last so it cannot corrupt other
            # worker evidence.
            await check("pid_limit", pid_limit)
        except Exception as exc:
            failures["probe_setup"] = f"{type(exc).__name__}: {exc}"
        finally:
            for worker_id in (a, b, sealed, flood, pid_worker):
                try:
                    await self.destroy(worker_id)
                except Exception as exc:
                    failures[f"cleanup_{worker_id}"] = f"{type(exc).__name__}: {exc}"
                    cleanup_ok = False
                else:
                    cleanup_ok = True
                if worker_id == pid_worker and "pid_limit" in probe_diagnostics:
                    probe_diagnostics["pid_limit"]["worker_cleanup"] = {
                        "succeeded": cleanup_ok,
                        "error": failures.get(f"cleanup_{worker_id}"),
                    }
                    checks["pid_worker_cleanup"] = cleanup_ok

        return {"verified": bool(checks) and all(checks.values()) and not failures,
                "checks": checks, "failures": failures,
                "probe_diagnostics": probe_diagnostics,
                "protected_canary_sha256": canary_hashes}

    @staticmethod
    def _execution_diagnostic(result: ExecutionResult) -> dict[str, object]:
        """Keep bounded output and timing for the two resource-boundary probes."""
        output_limit = 512
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout[:output_limit],
            "stdout_truncated": len(result.stdout) > output_limit,
            "stderr": result.stderr[:output_limit],
            "stderr_truncated": len(result.stderr) > output_limit,
            "timed_out": result.timed_out,
            "output_limited": result.output_limited,
            "duration_s": round(result.duration_s, 3),
        }

    async def _worker_state(self, worker_id: str) -> dict[str, object] | None:
        name = self._workers.get(worker_id)
        if name is None:
            return None
        code, out, _ = await self._docker("inspect", name, "--format", "{{json .State}}")
        if code:
            return None
        try:
            state = json.loads(out)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return {
            "Status": state.get("Status"),
            "Running": state.get("Running"),
            "OOMKilled": state.get("OOMKilled"),
            "ExitCode": state.get("ExitCode"),
        }

    async def _worker_host_info(self, worker_id: str) -> dict[str, object] | None:
        name = self._workers.get(worker_id)
        if name is None:
            return None
        code, out, _ = await self._docker(
            "inspect", name, "--format", "{{.Id}} {{json .State}}"
        )
        if code:
            return None
        try:
            container_id, state_json = out.decode().split(" ", 1)
            state = json.loads(state_json)
            pid = int(state["Pid"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return None
        return {
            "container_id": container_id,
            "resource_path": self._worker_cgroup_path(pid),
        }

    @staticmethod
    def _resource_scope_matches_container(path: Path | None, container_id: str | None) -> bool:
        """Tie host cgroup evidence to the exact Docker worker inspected above."""
        if (
            path is None
            or not isinstance(container_id, str)
            or re.fullmatch(r"[a-f0-9]{64}", container_id) is None
        ):
            return False
        parts = [part for part in path.as_posix().replace("\\", "/").split("/") if part]
        return (
            len(parts) == 5
            and parts[:3] == ["sys", "fs", "cgroup"]
            and parts[-2:] == ["system.slice", f"docker-{container_id}.scope"]
        )

    @staticmethod
    def _worker_cgroup_path(pid: int) -> Path | None:
        root = Path("/sys/fs/cgroup").resolve()
        try:
            records = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for record in records:
            parts = record.split(":", 2)
            if len(parts) != 3 or parts[0] != "0" or parts[1]:
                continue
            try:
                candidate = (root / parts[2].lstrip("/")).resolve(strict=True)
                candidate.relative_to(root)
            except (OSError, ValueError):
                return None
            return candidate if candidate.is_dir() else None
        return None

    @staticmethod
    def _read_cgroup_resources(path: Path) -> dict[str, object] | None:
        try:
            pids_max_value = path.joinpath("pids.max").read_text(encoding="utf-8").strip()
            pids_max = None if pids_max_value == "max" else int(pids_max_value)
            pids_current = int(path.joinpath("pids.current").read_text(encoding="utf-8").strip())
            pids_events = {
                key: int(value)
                for key, value in (
                    line.split() for line in path.joinpath("pids.events").read_text(encoding="utf-8").splitlines()
                )
            }
            memory_current = int(path.joinpath("memory.current").read_text(encoding="utf-8").strip())
            memory_events = {
                key: int(value)
                for key, value in (
                    line.split() for line in path.joinpath("memory.events").read_text(encoding="utf-8").splitlines()
                )
            }
        except (OSError, ValueError):
            return None
        return {
            "pids_max": pids_max,
            "pids_current": pids_current,
            "pids_events": pids_events,
            "memory_current": memory_current,
            "memory_events": memory_events,
        }

    @staticmethod
    def _pid_resource_evidence(
        samples: list[dict[str, object]],
        path: Path | None,
        pids_limit: int,
        *,
        container_id: str | None,
        scope_matches_container: bool,
    ) -> dict[str, object]:
        if not samples:
            return {
                "available": False,
                "path": str(path) if path else None,
                "container_id": container_id,
                "resource_scope_matches_container": scope_matches_container,
            }
        before = samples[0]
        before_pids_events = before["pids_events"]
        before_memory_events = before["memory_events"]
        assert isinstance(before_pids_events, dict) and isinstance(before_memory_events, dict)
        max_pid_events = max(
            (sample["pids_events"].get("max", -1) for sample in samples
             if isinstance(sample.get("pids_events"), dict)),
            default=-1,
        )
        max_memory_events = {
            key: max(
                (sample["memory_events"].get(key, -1) for sample in samples
                 if isinstance(sample.get("memory_events"), dict)),
                default=-1,
            )
            for key in ("oom", "oom_kill", "oom_group_kill")
        }
        pids_before = int(before_pids_events.get("max", -1))
        memory_before = {key: int(before_memory_events.get(key, -1)) for key in max_memory_events}
        pids_max = before.get("pids_max")
        return {
            "available": True,
            "path": str(path) if path else None,
            "container_id": container_id,
            "resource_scope_matches_container": scope_matches_container,
            "pids_max": pids_max,
            "configured_pids_limit": pids_limit,
            "pids_current_before": before.get("pids_current"),
            "pids_current_peak": max(int(sample.get("pids_current", -1)) for sample in samples),
            "pids_events_max_before": pids_before,
            "pids_events_max_observed": max_pid_events,
            "pids_denial_witness": max_pid_events > pids_before >= 0,
            "memory_current_before": before.get("memory_current"),
            "memory_events_before": memory_before,
            "memory_events_observed": max_memory_events,
            "oom_events_unchanged": all(
                memory_before[key] >= 0 and max_memory_events[key] == memory_before[key]
                for key in max_memory_events
            ),
        }

    async def _kernel_pid_rejection(self, container_id: str, cgroup_path: str) -> str | None:
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                "sudo", "-n", "journalctl", "-k", "--since", "5 minutes ago",
                "--no-pager", "--lines", "64", "-o", "cat", "--grep", container_id,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await asyncio.gather(process.wait(), return_exceptions=True)
            return None
        if process.returncode:
            return None
        for line in stdout.decode(errors="replace").splitlines():
            if "fork rejected by pids controller" in line and cgroup_path in line:
                return line[:512]
        return None

    @staticmethod
    def _classify_pid_limit_result(
        result: ExecutionResult,
        pids_limit: int,
        evidence: dict[str, object],
        worker_state: dict[str, object] | None,
        *,
        neighbor_ok: bool,
        canaries_unchanged: bool,
    ) -> tuple[str, bool]:
        memory_before = evidence.get("memory_events_before")
        memory_observed = evidence.get("memory_events_observed")
        no_oom = (
            evidence.get("oom_events_unchanged") is True
            and isinstance(memory_before, dict)
            and isinstance(memory_observed, dict)
            and all(memory_before.get(key, -1) == memory_observed.get(key, -2)
                    for key in ("oom", "oom_kill", "oom_group_kill"))
        )
        resource_path = evidence.get("path")
        container_id = evidence.get("container_id")
        scope_matches_container = (
            evidence.get("resource_scope_matches_container") is True
            and isinstance(resource_path, str)
            and GVisorIsolation._resource_scope_matches_container(
                Path(resource_path), container_id if isinstance(container_id, str) else None
            )
        )
        kernel_rejection = evidence.get("kernel_pids_rejection")
        normalized_resource_path = (
            resource_path.replace("\\", "/") if isinstance(resource_path, str) else ""
        )
        cgroup_root = "/sys/fs/cgroup/"
        expected_kernel_scope = (
            "/" + normalized_resource_path.removeprefix(cgroup_root)
            if normalized_resource_path.startswith(cgroup_root)
            else None
        )
        kernel_denial_witness = (
            isinstance(kernel_rejection, str)
            and "fork rejected by pids controller" in kernel_rejection
            and isinstance(container_id, str)
            and container_id in kernel_rejection
            and expected_kernel_scope is not None
            and expected_kernel_scope in kernel_rejection
        )
        denial_witness = evidence.get("pids_denial_witness") is True or kernel_denial_witness
        trusted_cap = (
            evidence.get("pids_max") == pids_limit
            and scope_matches_container
            and no_oom
            and denial_witness
        )
        if (
            not trusted_cap
            or worker_state is None
            or worker_state.get("OOMKilled") is not False
        ):
            return "unexplained_failure", False

        if result.exit_code == 0 and not result.timed_out and not result.output_limited:
            try:
                child_count, blocked_errno = map(int, result.stdout.split())
            except ValueError:
                pass
            else:
                if (
                    worker_state.get("Running") is True
                    and child_count < pids_limit
                    and blocked_errno == errno.EAGAIN
                ):
                    return "guest_EAGAIN", neighbor_ok and canaries_unchanged

        if (
            result.exit_code == 128
            and "WaitPID" in result.stderr
            and "EOF" in result.stderr
        ):
            contained = neighbor_ok and canaries_unchanged
            if worker_state.get("Running") is True:
                return "contained_exec_failure", contained
            if worker_state.get("Running") is False:
                return "contained_worker_termination", contained
        return "unexplained_failure", False

    @staticmethod
    def _check_canary(name: str, expected: bytes) -> bool:
        path = f"/var/lib/swarm-auth-bench/protected/{name}"
        details = os.stat(path)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != 0 or details.st_mode & stat.S_IROTH:
            return False
        with open(path, "rb") as canary_file:
            return canary_file.read() == expected
