#!/usr/bin/env python3
"""Cross-platform setup. Works on Linux, macOS and Windows.

    python3 bootstrap.py

setup.sh does the same on a Unix shell; this exists because Windows has no
bash, and because macOS lacks some GNU flags the shell script relied on.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
WINDOWS = platform.system() == "Windows"
PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")


def step(n: int, text: str) -> None:
    print(f"\n\033[1m{n}/5  {text}\033[0m" if not WINDOWS else f"\n{n}/5  {text}")


def run(*args: str) -> None:
    subprocess.run(list(args), check=True, cwd=str(ROOT))


def main() -> int:
    if sys.version_info < (3, 11):
        print(f"Python 3.11+ required, found {platform.python_version()}")
        return 1

    step(1, "Virtual environment")
    if not PY.exists():
        # system_site_packages is needed on Linux so pywebview can see the GTK
        # bindings, which are not installable from PyPI. Harmless elsewhere.
        venv.EnvBuilder(with_pip=True,
                        system_site_packages=(platform.system() == "Linux")).create(VENV)
    run(str(PY), "-m", "pip", "install", "--quiet", "--upgrade",
        "pip", "setuptools", "wheel")

    step(2, "Dependencies")
    run(str(PY), "-m", "pip", "install", "--quiet", "-r", str(ROOT / "requirements.txt"))

    step(3, "Browser engine (~150MB, one time)")
    run(str(PY), "-m", "playwright", "install", "chromium")

    step(4, "API key")
    env = ROOT / ".env"
    if env.exists():
        print("  .env already exists, leaving it alone.")
    else:
        env.write_text((ROOT / ".env.example").read_text())
        print("  Created .env — add your Anthropic API key before judging.")
        print("  Get one at https://console.anthropic.com/")

    step(5, "Database")
    run(str(PY), "-c", "from tracker import db; db.connect().close()")
    run(str(PY), "-m", "tracker", "accounts", "import")

    exe = ".venv\\Scripts\\python -m tracker" if WINDOWS else "./run"
    print(f"""
Setup complete. Next:

  {exe} login          sign in to a THROWAWAY X account (not your own)
  {exe} collect --all  first collection pass
  {exe} daily          the whole pipeline
  {exe} app            desktop app

Edit seeds.txt to change who is tracked, then: {exe} accounts import
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
