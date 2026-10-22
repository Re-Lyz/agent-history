"""Compatibility entry point; implementation lives in history_app.storage.merge."""
import importlib
import sys

implementation = importlib.import_module("history_app.storage.merge")
if __name__ == '__main__':
    implementation.main()
else:
    sys.modules[__name__] = implementation
