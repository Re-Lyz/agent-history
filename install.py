"""Compatible installation entry point; implementation lives in history_app."""
import sys
from history_app import installer

if __name__ == '__main__':
    installer.main()
else:
    # Preserve the existing import/patch contract for integrations and callers.
    sys.modules[__name__] = installer
