"""Compatibility entry point; implementation lives in history_app.launch.handoff."""
import importlib
import sys

implementation = importlib.import_module("history_app.launch.handoff")
sys.modules[__name__] = implementation
