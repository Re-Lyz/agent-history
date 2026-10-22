"""Compatibility entry point; implementation lives in history_app.ui.application."""
import importlib
import sys

implementation = importlib.import_module("history_app.ui.application")
if __name__ == '__main__':
    implementation.main()
else:
    sys.modules[__name__] = implementation
