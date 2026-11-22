# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller Spec for Krushidhan ERP Standalone Desktop Sidecar.

Produces a self-contained single-file executable (`krushidhan-sidecar` / `krushidhan-sidecar.exe`)
for Tauri 2 externalBin integration.

Usage:
    pyinstaller --clean krushidhan-sidecar.spec

Optional debug build with visible console window:
    KRUSHIDHAN_SIDECAR_CONSOLE=1 pyinstaller --clean krushidhan-sidecar.spec
"""
from __future__ import annotations

import os
import sys
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT_DIR = os.path.abspath(SPECPATH)
ENTRYPOINT = os.path.join(ROOT_DIR, "src", "desktop_sidecar.py")

# Default console=False so Windows Tauri launches do not flash a cmd window
# (inherited stdout/stdin pipes from Tauri remain connected under Win32 GUI subsystem).
ENABLE_CONSOLE = os.environ.get("KRUSHIDHAN_SIDECAR_CONSOLE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Strictly bundle ONLY the 7 mandatory read-only runtime assets under exact src/... paths.
# Never include data/, backups/, seed.sql, .env, .jwt_secret, tests/, or scripts/.
DATAS = [
    (os.path.join(ROOT_DIR, "src", "db", "schema.sql"), os.path.join("src", "db")),
    (os.path.join(ROOT_DIR, "src", "db", "seed_clean.sql"), os.path.join("src", "db")),
    (
        os.path.join(ROOT_DIR, "src", "printing", "fonts", "NotoSansDevanagari-Regular.ttf"),
        os.path.join("src", "printing", "fonts"),
    ),
    (
        os.path.join(ROOT_DIR, "src", "printing", "fonts", "NotoSansDevanagari-Bold.ttf"),
        os.path.join("src", "printing", "fonts"),
    ),
    (
        os.path.join(ROOT_DIR, "src", "web", "templates", "index.html"),
        os.path.join("src", "web", "templates"),
    ),
    (
        os.path.join(ROOT_DIR, "src", "web", "static", "css", "app.css"),
        os.path.join("src", "web", "static", "css"),
    ),
    (
        os.path.join(ROOT_DIR, "src", "web", "static", "js", "app.js"),
        os.path.join("src", "web", "static", "js"),
    ),
]

HIDDEN_IMPORTS = sorted(
    set(
        collect_submodules("src")
        + [
            # Uvicorn dynamic runtime components
            "uvicorn",
            "uvicorn.logging",
            "uvicorn.loops",
            "uvicorn.loops.auto",
            "uvicorn.loops.asyncio",
            "uvicorn.protocols",
            "uvicorn.protocols.http",
            "uvicorn.protocols.http.auto",
            "uvicorn.protocols.http.h11_impl",
            "uvicorn.protocols.websockets",
            "uvicorn.protocols.websockets.auto",
            "uvicorn.lifespan",
            "uvicorn.lifespan.on",
            "uvicorn.lifespan.off",
            "h11",
            "anyio._backends._asyncio",
            # FastAPI / Starlette / Pydantic / Jinja / Multipart
            "fastapi",
            "starlette.middleware",
            "starlette.middleware.base",
            "starlette.staticfiles",
            "starlette.templating",
            "pydantic",
            "pydantic_core",
            "annotated_types",
            "typing_extensions",
            "jinja2",
            "markupsafe",
            "markupsafe._speedups",
            "python_multipart",
            "multipart",
            "multipart.multipart",
            # Security, Encrypted Backups & SQLite
            "bcrypt",
            "bcrypt._bcrypt",
            "cryptography",
            "cryptography.exceptions",
            "cryptography.hazmat.backends",
            "cryptography.hazmat.backends.openssl",
            "cryptography.hazmat.bindings._rust",
            "cryptography.hazmat.primitives.ciphers.aead",
            "cryptography.hazmat.primitives.hashes",
            "cryptography.hazmat.primitives.kdf.hkdf",
            "cffi",
            "_cffi_backend",
            "sqlite3",
            # OS-native Recovery Key storage (Windows uses stdlib ctypes DPAPI; macOS uses explicit Keychain backend)
            "ctypes",
            *(
                ["ctypes.wintypes", "msvcrt"]
                if sys.platform == "win32"
                else []
            ),
            *(
                [
                    "keyring",
                    "keyring.backend",
                    "keyring.errors",
                    "keyring.backends",
                    "keyring.backends.macOS",
                    "keyring.backends.macOS.api",
                    "jaraco.classes",
                    "jaraco.context",
                    "jaraco.functools",
                    "more_itertools",
                ]
                if sys.platform == "darwin"
                else []
            ),
            # Excel OOXML & XXE defense (openpyxl + defusedxml)
            "defusedxml",
            "defusedxml.ElementTree",
            "defusedxml.common",
            "defusedxml.cElementTree",
            "defusedxml.expatbuilder",
            "defusedxml.minidom",
            "defusedxml.pulldom",
            "defusedxml.sax",
            "defusedxml.xmlrpc",
            "openpyxl",
            "openpyxl.xml",
            "openpyxl.xml.functions",
            "openpyxl.xml.constants",
            "et_xmlfile",
            # PDF Invoice generation
            "reportlab",
            "reportlab.pdfbase",
            "reportlab.pdfbase.ttfonts",
            "reportlab.platypus",
            "PIL",
            "PIL._imaging",
            # Stdlib email MIME modules used by Starlette/HTTP/ReportLab
            "email.mime.multipart",
            "email.mime.text",
            "email.mime.application",
        ]
    )
)

EXCLUDES = [
    "pytest",
    "_pytest",
    "pluggy",
    "iniconfig",
    "httpx",
    "httpcore",
    "pymongo",
    "bson",
    "gridfs",
    "dns",
    "lxml",
    "tkinter",
    "tests",
]

BINARIES = collect_dynamic_libs("cryptography")

a = Analysis(
    [ENTRYPOINT],
    pathex=[ROOT_DIR],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# Defense-in-depth: strip any accidental test/data/backup/secret resources if pulled transitively.
_FORBIDDEN_PREFIXES = ("data/", "backups/", "tests/", "scripts/")
_FORBIDDEN_EXACT = {".env", ".jwt_secret", "src/db/seed.sql"}


def _is_safe_toc_entry(dest_name: str) -> bool:
    norm = dest_name.replace("\\", "/").lstrip("./")
    if norm in _FORBIDDEN_EXACT:
        return False
    if any(norm.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES):
        return False
    if norm.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".jwt_secret", ".kdb.enc", ".dpapi")):
        return False
    return True


a.datas = [entry for entry in a.datas if _is_safe_toc_entry(entry[0])]

pyz = PYZ(a.pure)

_WIN_ICON_CANDIDATE = os.path.join(ROOT_DIR, "src-tauri", "icons", "icon.ico")
_EXE_ICON = _WIN_ICON_CANDIDATE if (sys.platform == "win32" and os.path.isfile(_WIN_ICON_CANDIDATE)) else None

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="krushidhan-sidecar",
    icon=_EXE_ICON,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=ENABLE_CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
