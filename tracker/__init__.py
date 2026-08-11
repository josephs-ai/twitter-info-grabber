"""AI Signal Tracker.

.env is loaded here, at package import, because settings in it are read at
module import time — embed.py picks its backend from AI_SIGNAL_EMBED as it
loads. Doing it any later means the setting is read after the decision it was
meant to influence.
"""

from . import paths as _paths

_paths.load_env()
