"""Compatibility entry point; implementation lives in history_app.launch.planning."""
import importlib
import sys

implementation = importlib.import_module("history_app.launch.planning")
sys.modules[__name__] = implementation
