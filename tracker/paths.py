"""Where the code is, and where your data is — which are not the same place.

Running from a clone, they are: the database sits next to the source and that
is convenient. A packaged build cannot work that way. A macOS `.app` bundle is
signed and effectively read-only, `C:\\Program Files` needs admin rights, and
in both cases the executable can be replaced by an update without taking your
corpus with it. Data belongs in the per-user location each OS already has.

Two roots, then:

  code_dir()  read-only assets shipped with the program — the schema, the UI,
              the seed list. Inside the bundle when frozen.
  data_dir()  everything the program writes — database, session profile, .env,
              digests, logs. Per-user when frozen, the project folder otherwise.

Source checkouts keep the old behaviour exactly, so nobody's existing install
moves under them.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

APP_NAME = "AI Signal"
DIR_NAME = "ai-signal"

FROZEN = getattr(sys, "frozen", False)


def code_dir() -> Path:
    """Assets that ship with the program."""
    if FROZEN:
        # PyInstaller unpacks bundled data here; falls back to the executable's
        # own directory for a onedir build.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Everything the program writes. Honours an explicit override first."""
    override = os.environ.get("AI_SIGNAL_HOME")
    if override:
        path = Path(override).expanduser()
    elif not FROZEN:
        path = Path(__file__).resolve().parent.parent
    elif platform.system() == "Darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    elif platform.system() == "Windows":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        path = Path(base) / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        path = Path(base) / DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def self_command() -> list[str]:
    """How to invoke this program's CLI as a subprocess.

    From source that is `python -m tracker`. A frozen build has no `-m`, so the
    executable dispatches on argv instead and the prefix is just itself. Both
    the in-app stage buttons and the scheduled entry go through here, so
    neither can drift from how the program is actually launched.
    """
    if FROZEN:
        return [sys.executable]
    return [sys.executable, "-m", "tracker"]
