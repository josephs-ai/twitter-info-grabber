"""Entry point for the packaged build.

A frozen executable has no `python -m tracker`, but the app shells out to its
own CLI for every pipeline stage and the scheduler needs a command to run. So
the one executable answers to both: bare launch opens the window, any argument
goes to the CLI. `paths.self_command()` is the other half of this contract.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # PyInstaller + anything that spawns a process needs this first, or the
    # child re-runs the whole program instead of the target function.
    multiprocessing.freeze_support()

    if len(sys.argv) > 1:
        from tracker.cli import main as cli_main
        return cli_main()

    from tracker.app import main as app_main
    return app_main()


if __name__ == "__main__":
    sys.exit(main())
