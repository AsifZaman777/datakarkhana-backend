#!/usr/bin/env python3
"""
Standalone Backend Builder for DataKarkhana Desktop
Packages FastAPI + Uvicorn + Selenium into a standalone executable using PyInstaller.
When compiled, customer machines require ZERO Python installation.
"""

import sys
import subprocess
import shutil
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
DESKTOP_RESOURCES = PROJECT_ROOT / "desktop" / "resources" / "datakarkhana-backend"

def run(cmd, **kwargs):
    print(f"[BUILD] Running: {cmd}")
    subprocess.check_call(cmd, shell=True, **kwargs)

def main():
    print("=======================================================")
    print("   DataKarkhana Standalone Backend PyInstaller Packager")
    print("=======================================================")

    # 1. Check/install PyInstaller
    try:
        import PyInstaller
    except ImportError:
        print("[BUILD] Installing PyInstaller...")
        run(f'"{sys.executable}" -m pip install pyinstaller')

    # 2. Build backend executable
    dist_dir = BACKEND_DIR / "dist"
    build_dir = BACKEND_DIR / "build"
    spec_file = BACKEND_DIR / "datakarkhana-backend.spec"

    print("[BUILD] Compiling main.py into standalone backend executable...")
    pyinstaller_cmd = [
        f'"{sys.executable}"',
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name", "datakarkhana-backend",
        "--hidden-import", "uvicorn",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespans",
        "--hidden-import", "uvicorn.lifespans.on",
        "--hidden-import", "selenium",
        "--hidden-import", "fastapi",
        "--hidden-import", "pydantic",
        "--collect-all", "uvicorn",
        "--collect-all", "selenium",
        f'"{BACKEND_DIR / "main.py"}"'
    ]

    run(" ".join(pyinstaller_cmd), cwd=str(BACKEND_DIR))

    # 3. Copy output into desktop/resources if directory exists
    output_folder = dist_dir / "datakarkhana-backend"
    if output_folder.exists():
        print(f"[BUILD] Compiled successfully to {output_folder}")
        DESKTOP_RESOURCES.parent.mkdir(parents=True, exist_ok=True)
        if DESKTOP_RESOURCES.exists():
            shutil.rmtree(DESKTOP_RESOURCES)
        shutil.copytree(output_folder, DESKTOP_RESOURCES)
        print(f"[BUILD] Copied standalone backend into: {DESKTOP_RESOURCES}")
        print("\n🎉 Done! DataKarkhana Desktop will now run the standalone binary without requiring Python.")
    else:
        print("[BUILD] Warning: Output folder not found.")

if __name__ == "__main__":
    main()
