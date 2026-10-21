"""Explicit runtime locations shared by maintenance services and their UI."""
from dataclasses import dataclass
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RuntimePaths:
    shared: Path
    data: Path
    prefix: Path
    user: Path
    source: Path = SOURCE

    @classmethod
    def current(cls, prefix=None):
        import agent_history as ah
        user = Path.home()
        return cls(ah.ROOT.resolve(), ah.DATA.resolve(),
                   Path(prefix).expanduser().resolve() if prefix else user / '.local/share/agent-history', user)

    def configuration(self):
        return {'connections': self.shared / 'connections.json',
                **{name: self.data / (name + '.json') for name in
                   ('sources', 'launch-profiles', 'launch-entries', 'ui-preferences')}}

    @property
    def backups(self):
        return self.data / 'maintenance' / 'config-backups'
