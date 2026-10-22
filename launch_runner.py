"""Compatibility entry point; implementation lives in history_app.launch.runner."""
import importlib
import sys

implementation = importlib.import_module("history_app.launch.runner")
if __name__ == '__main__':
    sys.exit(implementation.main())
else:
    sys.modules[__name__] = implementation
