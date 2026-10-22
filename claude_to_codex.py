"""Compatibility entry point; implementation lives in history_app.adapters.claude."""
import importlib
import sys

implementation = importlib.import_module("history_app.adapters.claude")
sys.modules[__name__] = implementation
