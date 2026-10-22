"""Compatibility entry point; implementation lives in history_app.storage.revisions."""
import importlib
import sys

implementation = importlib.import_module("history_app.storage.revisions")
sys.modules[__name__] = implementation
