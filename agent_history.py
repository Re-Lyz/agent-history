"""Compatibility entry point; implementation lives in history_app.storage.history."""
import importlib
import sys

implementation = importlib.import_module("history_app.storage.history")
if __name__ == '__main__':
    if sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        implementation.main()
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
else:
    sys.modules[__name__] = implementation
