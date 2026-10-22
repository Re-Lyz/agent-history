"""Compatibility entry point; implementation lives in history_app.adapters.codex."""
import importlib
import sys

implementation = importlib.import_module("history_app.adapters.codex")
sys.modules[__name__] = implementation
