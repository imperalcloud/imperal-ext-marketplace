"""Marketplace · entry point.

Loaded by ICNLI OS Kernel via spec_from_file_location.exec_module.
Module purge enables hot-reload; sys.path insert mirrors the loader.

Handlers split (2026-05-27, federal handlers-<300 LOC):
  * handlers.py            — search_marketplace, get_app_details
  * handlers_installed.py  — list_my_installed, recommend_for_intent
  * handlers_lifecycle.py  — install_app, uninstall_app
"""
from __future__ import annotations

import os
import sys

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

_MODULES = (
    "app", "api", "models",
    "handlers", "handlers_installed", "handlers_lifecycle",
    "skeleton",
)
for _m in [k for k in sys.modules if k in _MODULES]:
    del sys.modules[_m]

from app import ext, chat                # noqa: E402, F401

import handlers                           # noqa: E402, F401
import handlers_installed                 # noqa: E402, F401
import handlers_lifecycle                 # noqa: E402, F401
import skeleton                           # noqa: E402, F401
