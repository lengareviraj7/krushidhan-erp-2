"""
Standalone Desktop Sidecar Entrypoint for Krushidhan ERP (PyInstaller + Tauri 2).

Startup sequence:
1. Capture raw stdout for the single readiness JSON line and route Python/Uvicorn
   logs strictly to %LOCALAPPDATA%\\com.krushidhan.erp\\logs\\sidecar.log.
2. Force APP_MODE="desktop" (zero .env loading, zero MongoDB sync, zero dev seeds).
3. Validate mandatory bundled read-only assets and defusedxml parser hardening.
4. Acquire OS-level single-instance file lock before opening SQLite or sockets.
5. Initialize/migrate clean SQLite database and per-install .jwt_secret.
6. Bind ephemeral loopback TCP socket (127.0.0.1:0), generate in-memory per-launch
   token, enable desktop HTTP security middleware, start single-worker Uvicorn.
7. Emit ONE compact readiness JSON line on stdout once server & lifespan are live.
8. Watch stdin for parent closure/EOF and drain DB + checkpoint WAL on exit.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import multiprocessing
import os
import secrets
import signal
import socket
import sys
import threading
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, TextIO

import uvicorn
from uvicorn.server import HANDLED_SIGNALS

from src.config import (
    APP_VERSION,
    get_desktop_lock_path,
    get_jwt_secret,
    is_cloud_sync_allowed,
    is_frozen_bundle,
    load_runtime_dotenv,
    validate_bundled_assets,
)
from src.db.connection import DatabaseManager, get_db_manager, reset_db_manager
from src.desktop_runtime import (
    DesktopLockError,
    SingleInstanceLock,
    configure_desktop_security,
    register_redacted_secret,
    reset_desktop_security,
    setup_desktop_logging,
    teardown_desktop_logging,
)
from src.services.excel_security import ensure_defusedxml_active

SIDECAR_PROTOCOL_VERSION = 1
GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 5.0
DB_DRAIN_TIMEOUT_SECONDS = 3.0

logger = logging.getLogger("agri_erp.sidecar")


class _NullStream:
    """Safe no-op text stream when running windowed without console stdio handles."""

    encoding = "utf-8"

    def write(self, data: str) -> int:
        return len(data) if data else 0

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


class _LoggerStream:
    """Redirects accidental stdout writes to the sidecar file logger so handshake stdout stays pure."""

    encoding = "utf-8"

    def __init__(self, target_logger: logging.Logger):
        self._logger = target_logger
        self._buffer = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._buffer += str(data)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            cleaned = line.rstrip("\r")
            if cleaned:
                self._logger.info("%s", cleaned)
        return len(data)

    def flush(self) -> None:
        if self._buffer:
            cleaned = self._buffer.rstrip("\r\n")
            self._buffer = ""
            if cleaned:
                self._logger.info("%s", cleaned)

    def isatty(self) -> bool:
        return False


def build_readiness_payload(
    *,
    port: int,
    launch_token: str,
    pid: Optional[int] = None,
    app_version: str = APP_VERSION,
) -> Dict[str, Any]:
    """Construct the exact single-line stdout JSON readiness handshake payload."""
    return {
        "protocol_version": SIDECAR_PROTOCOL_VERSION,
        "status": "ready",
        "port": int(port),
        "launch_token": str(launch_token),
        "pid": int(pid if pid is not None else os.getpid()),
        "app_version": str(app_version),
    }


def bind_ephemeral_loopback_socket(host: str = "127.0.0.1") -> tuple[socket.socket, int]:
    """
    Bind a loopback TCP socket on port 0 safely without permissive SO_REUSEADDR.
    Uses SO_EXCLUSIVEADDRUSE on Windows to guard against local port hijacking.
    """
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(f"Desktop sidecar only permits loopback IPv4 binding, got '{host}'.")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, 0))
        sock.listen(128)
        sock.setblocking(False)
        bound_port = int(sock.getsockname()[1])
        return sock, bound_port
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


class SidecarUvicornServer(uvicorn.Server):
    """Uvicorn server subclass that fires on_ready strictly after lifespan & socket listener startup."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        on_ready: Callable[[], None],
        shutdown_event: threading.Event,
    ):
        super().__init__(config)
        self._on_ready = on_ready
        self._shutdown_event = shutdown_event
        self._ready_emitted = False

    def handle_exit(self, sig: int, frame: Any = None) -> None:
        self._shutdown_event.set()
        super().handle_exit(sig, frame)

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Catch termination signals so Uvicorn returns cleanly to run DB drain/WAL checkpoint and exit 0."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original_handlers = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)

    async def startup(self, sockets: Optional[list[socket.socket]] = None) -> None:
        await super().startup(sockets=sockets)
        if self._shutdown_event.is_set():
            self.should_exit = True
            return
        if self.started and not self.should_exit and not self._ready_emitted:
            self._ready_emitted = True
            self._on_ready()

    async def _serve(self, sockets: Optional[list[socket.socket]] = None) -> None:
        await super()._serve(sockets=sockets)
        # Uvicorn returns early from _serve without calling shutdown() when
        # should_exit becomes True during startup(). Cleanly shut down lifespan
        # so background ASGI lifespan tasks never cancel noisily on exit.
        if self.should_exit and self.started and not self.force_exit:
            lifespan = getattr(self, "lifespan", None)
            if (
                lifespan is not None
                and lifespan.startup_event.is_set()
                and not lifespan.shutdown_event.is_set()
                and not lifespan.error_occured
            ):
                await self.shutdown(sockets=sockets)


def _emit_readiness_line(stdout_stream: Optional[TextIO], payload: Dict[str, Any]) -> None:
    """Write exactly one compact JSON line to the captured sidecar stdout pipe and flush immediately."""
    if stdout_stream is None:
        return
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    stdout_stream.write(line)
    stdout_stream.flush()


def _start_stdin_eof_watcher(
    stdin_stream: Optional[TextIO],
    shutdown_event: threading.Event,
    on_eof: Callable[[], None],
) -> Optional[threading.Thread]:
    """
    Monitor parent stdin in a daemon thread so parent crash or pipe closure cleanly triggers shutdown.
    """
    if stdin_stream is None:
        logger.warning("Sidecar started without a stdin handle; relying on OS signals for shutdown.")
        return None

    def _watch() -> None:
        try:
            raw_buf = getattr(stdin_stream, "buffer", None)
            while not shutdown_event.is_set():
                chunk = raw_buf.read(1) if raw_buf is not None else stdin_stream.read(1)
                if not chunk:
                    logger.info("Parent stdin EOF detected; initiating graceful sidecar shutdown.")
                    shutdown_event.set()
                    on_eof()
                    return
        except (OSError, ValueError) as exc:
            if not shutdown_event.is_set():
                logger.info("Parent stdin pipe closed (%s); initiating graceful sidecar shutdown.", exc)
                shutdown_event.set()
                on_eof()

    thread = threading.Thread(target=_watch, name="sidecar-stdin-watcher", daemon=True)
    thread.start()
    return thread


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the Krushidhan ERP standalone desktop HTTP sidecar."""
    del argv  # Sidecar accepts no secret/config flags via CLI argv.
    multiprocessing.freeze_support()

    raw_stdout: Optional[TextIO] = sys.stdout
    raw_stdin: Optional[TextIO] = sys.stdin
    orig_stdout = sys.stdout
    orig_stderr = sys.stderr

    if sys.stderr is None:
        sys.stderr = _NullStream()  # type: ignore[assignment]

    os.environ["APP_MODE"] = "desktop"
    launch_token = secrets.token_urlsafe(32)
    register_redacted_secret(launch_token)

    try:
        log_file = setup_desktop_logging(app_mode="desktop")
    except Exception as exc:
        err_msg = f"FATAL: Unable to initialize sidecar logging: {exc}\n"
        if sys.stderr is not None:
            sys.stderr.write(err_msg)
            sys.stderr.flush()
        return 1

    # Divert stdout after capturing raw_stdout so stray prints go to log, not the JSON pipe.
    sys.stdout = _LoggerStream(logging.getLogger("agri_erp.stdout"))  # type: ignore[assignment]

    lock: Optional[SingleInstanceLock] = None
    sock: Optional[socket.socket] = None
    db: Optional[DatabaseManager] = None
    shutdown_event = threading.Event()
    server_holder: Dict[str, Optional[SidecarUvicornServer]] = {"server": None}

    try:
        logger.info(
            "Starting Krushidhan ERP sidecar v%s (protocol=%s, frozen=%s, log_file=%s)",
            APP_VERSION,
            SIDECAR_PROTOCOL_VERSION,
            is_frozen_bundle(),
            log_file,
        )

        # 1. Verify bundled assets & defusedxml hardening before opening locks/DB
        validate_bundled_assets()
        ensure_defusedxml_active()

        # 2. Acquire OS-level single-instance lock scoped to active app data directory
        lock = SingleInstanceLock(get_desktop_lock_path())
        lock.acquire()
        logger.info("Acquired single-instance lock at %s", lock.lock_path)

        # 3. Ensure offline desktop config, .jwt_secret, and clean DB initialization
        load_runtime_dotenv(app_mode="desktop")
        jwt_secret = get_jwt_secret(app_mode="desktop")
        register_redacted_secret(jwt_secret)

        if is_cloud_sync_allowed(app_mode="desktop"):
            raise RuntimeError("Cloud sync must remain disabled in desktop mode.")

        reset_db_manager()
        db = get_db_manager(app_mode="desktop")
        db.initialize_database(include_seed=True, clean_install=True, run_migrations=True)
        schema_ver = db.get_schema_version()
        logger.info("Desktop SQLite ready (clean_install=%s, schema_version=%s)", db.clean_install_mode, schema_ver)

        # 4. Bind loopback ephemeral port & enable in-memory HTTP security guard
        sock, bound_port = bind_ephemeral_loopback_socket("127.0.0.1")
        configure_desktop_security(
            enabled=True,
            launch_token=launch_token,
            bound_host="127.0.0.1",
            bound_port=bound_port,
        )

        # Import FastAPI app object directly (eliminates string-import fragility in frozen builds)
        from src.app import app as fastapi_app

        def _trigger_shutdown() -> None:
            srv = server_holder.get("server")
            if srv is not None:
                srv.should_exit = True

        def _emit_ready() -> None:
            payload = build_readiness_payload(
                port=bound_port,
                launch_token=launch_token,
                pid=os.getpid(),
                app_version=APP_VERSION,
            )
            _emit_readiness_line(raw_stdout, payload)
            logger.info("Sidecar ready on http://127.0.0.1:%d (pid=%d).", bound_port, os.getpid())

        uv_config = uvicorn.Config(
            app=fastapi_app,
            host="127.0.0.1",
            port=bound_port,
            workers=1,
            reload=False,
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="on",
            log_config=None,
            use_colors=False,
            access_log=True,
            server_header=False,
            timeout_graceful_shutdown=int(GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS),
        )
        server = SidecarUvicornServer(
            uv_config,
            on_ready=_emit_ready,
            shutdown_event=shutdown_event,
        )
        server_holder["server"] = server

        _start_stdin_eof_watcher(raw_stdin, shutdown_event, _trigger_shutdown)

        # Run Uvicorn blocking loop cleanly against the pre-bound socket
        server.run(sockets=[sock])
        logger.info("HTTP server loop stopped cleanly.")
        return 0

    except DesktopLockError as exc:
        logger.error("Single-instance conflict prevented sidecar startup: %s", exc)
        if orig_stderr is not None:
            orig_stderr.write(f"ERROR: {exc}\n")
            orig_stderr.flush()
        return 2
    except Exception as exc:
        logger.exception("Fatal desktop sidecar error: %s", exc)
        if orig_stderr is not None:
            orig_stderr.write(f"FATAL: {exc}\n")
            orig_stderr.flush()
        return 1
    finally:
        shutdown_event.set()
        reset_desktop_security()
        if db is not None:
            try:
                remaining = db.wait_for_connections_drain(timeout=DB_DRAIN_TIMEOUT_SECONDS)
                if remaining > 0:
                    logger.warning(
                        "Timed out waiting for %d active SQLite connection(s) before shutdown checkpoint.",
                        remaining,
                    )
                db.checkpoint_wal("TRUNCATE")
            except Exception as exc:
                logger.warning("Cleanup WAL checkpoint warning during shutdown: %s", exc)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if lock is not None:
            was_locked = lock.is_locked
            lock.release()
            if was_locked:
                logger.info("Released single-instance lock cleanly.")
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        teardown_desktop_logging()


if __name__ == "__main__":
    raise SystemExit(main())
