"""Compatibility entry point; implementation lives in history_app.launch.jobs."""
import importlib
import sys

implementation = importlib.import_module("history_app.launch.jobs")
sys.modules[__name__] = implementation
