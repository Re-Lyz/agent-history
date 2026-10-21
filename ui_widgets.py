"""Compatibility import; implementation lives in history_app.ui.widgets."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module('history_app.ui.widgets')
