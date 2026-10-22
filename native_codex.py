"""Compatibility entry point; implementation lives in history_app.adapters.codex_rpc."""
import importlib
import sys

implementation = importlib.import_module("history_app.adapters.codex_rpc")
sys.modules[__name__] = implementation
