"""Actual user-manager timer execution, failure reporting and uninstall.

Run only as a disposable Linux user with a live systemd user manager.
No Agent, authentication, real history or model request is involved.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


def systemctl(*args, check=True):
    return subprocess.run(['systemctl', '--user', *args], check=check,
                          capture_output=True, text=True, timeout=30)


def verify():
    if sys.platform != 'linux' or os.environ.get('AGENT_HISTORY_DISPOSABLE_USER') != '1':
        raise SystemExit('Requires an explicitly disposable Linux user and live user manager.')
    systemctl('show-environment')
    user = Path.home()
    with tempfile.TemporaryDirectory(prefix='agent-history-systemd-', dir=user) as folder:
        root = Path(folder)
        prefix = root / '安装 中文 空格'
        data = root / '历史 中文 空格'
        shared = root / '配置 中文 空格'
        data.mkdir(); shared.mkdir()
        (shared / 'connections.json').write_text('{"connections":{}}', encoding='utf-8')
        (data / 'sources.json').write_text('{"version":1,"codex":[],"claude":[]}', encoding='utf-8')
        marker = data / 'preserve.txt'
        marker.write_text('synthetic preserved data', encoding='utf-8')
        spec = install.plan(prefix, user, sys.executable, shared_root=shared,
                            data_dir=data, collect_task=True)
        unit = 'agenthistory-validation-' + uuid.uuid4().hex
        spec['tasks'][0]['name'] = unit
        unit_folder = user / '.config/systemd/user'
        dropin = unit_folder / (unit + '.timer.d')
        dropin.mkdir(parents=True)
        override = dropin / 'validation.conf'
        override.write_text('[Timer]\nOnBootSec=\nOnActiveSec=1s\nAccuracySec=100ms\n', encoding='utf-8')
        try:
            install.install(spec)
            install.install(spec)
            deadline = time.monotonic() + 45
            database = data / 'history.db'
            while time.monotonic() < deadline:
                if database.exists() and systemctl('show', unit + '.service', '-p',
                        'ExecMainStatus', '--value').stdout.strip() == '0':
                    break
                time.sleep(.2)
            else:
                raise AssertionError('Timer did not execute collection successfully.')
            systemctl('stop', unit + '.timer')
            systemctl('start', unit + '.service')
            # The real service must report a failure and then recover on retry.
            saved = data / 'history.saved.db'
            database.rename(saved)
            database.write_bytes(b'synthetic invalid database')
            failed = systemctl('start', unit + '.service', check=False)
            assert failed.returncode != 0
            assert systemctl('show', unit + '.service', '-p', 'Result', '--value').stdout.strip() == 'exit-code'
            database.unlink()
            saved.rename(database)
            systemctl('reset-failed', unit + '.service')
            systemctl('start', unit + '.service')
            assert systemctl('show', unit + '.service', '-p', 'ExecMainStatus', '--value').stdout.strip() == '0'
            override.unlink(); dropin.rmdir()
            result = install.uninstall(prefix)
            assert not result['kept'], result
            assert marker.read_text('utf-8') == 'synthetic preserved data'
            assert database.exists()
            assert not (unit_folder / (unit + '.timer')).exists()
            assert not (unit_folder / (unit + '.service')).exists()
            assert systemctl('is-active', unit + '.timer', check=False).returncode != 0
            print(json.dumps(dict(real_systemd=True, timer_fired=True, failure_observed=True,
                retry_succeeded=True, repeat_install=True, uninstall=True, data_preserved=True)))
        finally:
            systemctl('disable', '--now', unit + '.timer', check=False)
            systemctl('stop', unit + '.service', check=False)
            for path in (override, unit_folder / (unit + '.timer'), unit_folder / (unit + '.service')):
                path.unlink(missing_ok=True)
            if dropin.exists():
                dropin.rmdir()
            systemctl('daemon-reload')
            systemctl('reset-failed', unit + '.service', check=False)


if __name__ == '__main__':
    verify()
