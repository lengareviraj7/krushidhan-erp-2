#!/usr/bin/env python3
"""
1-Click Application Launcher for Offline Agri-Input Shop ERP.
Usage: python run.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
import uvicorn
from src.config import get_jwt_secret, load_runtime_dotenv
from src.db.connection import get_db_manager

ALLOWED_DESKTOP_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def resolve_bind_address(app_mode: str, host: str, port: int) -> tuple[str, int]:
    """Validate bind host/port and strictly enforce loopback-only binding in desktop mode."""
    cleaned_host = (host or "127.0.0.1").strip()
    if app_mode == "desktop" and cleaned_host.lower() not in ALLOWED_DESKTOP_HOSTS:
        raise RuntimeError(
            f"Refusing to bind desktop server to non-loopback host '{cleaned_host}'. "
            "Desktop mode only permits localhost / 127.0.0.1 / ::1."
        )
    if not (1 <= int(port) <= 65535):
        raise ValueError(f"Invalid server port {port}. Must be between 1 and 65535.")
    return cleaned_host, int(port)


def open_browser(url: str = "http://127.0.0.1:8008"):
    """Wait for server to start, then open the browser window."""
    time.sleep(1.2)
    print(f"🚀 Opening Agri-Shop ERP at {url}")
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"Could not auto-open browser: {e}")


def main():
    print("=" * 60)
    print("🌾 KRISHI AGRI-INPUT SHOP ERP (100% Offline)")
    print("=" * 60)

    try:
        load_runtime_dotenv()
        get_jwt_secret()

        # Initialize SQLite database respecting APP_MODE / DB_PATH
        print("⚙️ Initializing local database...")
        db = get_db_manager()
        db.initialize_database(include_seed=True)
        print(f"✓ Database ready at: {db.db_path} (APP_MODE={db.app_mode})")

        host, port = resolve_bind_address(
            db.app_mode,
            os.environ.get("HOST", "127.0.0.1"),
            int(os.environ.get("PORT", "8008")),
        )
    except Exception as exc:
        print(f"❌ FATAL STARTUP ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    url = f"http://{host}:{port}"

    if not db.is_desktop:
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()

    print(f"🚀 Starting local ERP server on {url} (Press Ctrl+C to quit)...")
    uvicorn.run("src.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
