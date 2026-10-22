"""Compatibility entry point; implementation lives in history_app.launch.reservations."""
import importlib
import sys

implementation = importlib.import_module("history_app.launch.reservations")
sys.modules[__name__] = implementation
