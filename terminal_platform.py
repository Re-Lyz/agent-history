"""Compatibility import; implementation lives in history_app.platform.terminal."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module('history_app.platform.terminal')
