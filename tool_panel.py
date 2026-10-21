"""Compatibility import; implementation lives in history_app.ui.tool_panel."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module('history_app.ui.tool_panel')
