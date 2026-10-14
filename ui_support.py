"""Viewer preferences, read-only launch checks and cleanup-report metadata."""
import csv
import contextlib
import datetime as dt
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid

import agent_history as ah
import codex_import as ci
import connections

WINDOWS_LAUNCHER = os.name == 'nt'


def error_log_path():
    return ah.DATA / 'ui-errors.log'


def log_error(context, error):
    """Keep local tracebacks, without dumping settings, environment or transcripts."""
    try:
        ah.DATA.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(error_log_path(), maxBytes=1_000_000, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger = logging.Logger('agent-history-ui')
        logger.addHandler(handler)
        try:
            logger.error(context, exc_info=(type(error), error, error.__traceback__))
        finally:
            handler.close()
    except OSError:
        pass  # Logging must not prevent opening/closing the viewer.


def project_key(cwd):
    return os.path.normcase(os.path.abspath(cwd)) if cwd else ''


def clean_preferences(raw):
    """Validate only UI fields; ignore malformed values and unknown fields."""
    out = dict(version=1, geometry='1440x860+40+40', maximized=False,
               panes=[0.40, 0.75], filters={}, project_targets={}, default_target='',
               sort=dict(column='time', descending=True))
    if not isinstance(raw, dict) or raw.get('version', 1) != 1:
        return out
    geometry = raw.get('geometry')
    if isinstance(geometry, str) and re.fullmatch(r'\d{2,5}x\d{2,5}[+-]\d{1,6}[+-]\d{1,6}', geometry):
        out['geometry'] = geometry
    if isinstance(raw.get('maximized'), bool):
        out['maximized'] = raw['maximized']
    panes = raw.get('panes')
    if isinstance(panes, list) and len(panes) == 2 and all(type(x) in (int, float) for x in panes):
        if 0.1 <= panes[0] < panes[1] <= 0.95:
            out['panes'] = panes
    filters = raw.get('filters')
    if isinstance(filters, dict):
        for name in ('query', 'source', 'project'):
            if isinstance(filters.get(name), str):
                out['filters'][name] = filters[name][:4096]
        for name in ('imports', 'internal', 'review'):
            if isinstance(filters.get(name), bool):
                out['filters'][name] = filters[name]
    targets = raw.get('project_targets')
    if isinstance(targets, dict):
        out['project_targets'] = {project_key(k): v for k, v in targets.items()
                                  if isinstance(k, str) and k and isinstance(v, str) and v}
    if isinstance(raw.get('default_target'), str):
        out['default_target'] = raw['default_target']
    if raw.get('preview_mode') in ('markdown', 'raw'):
        out['preview_mode'] = raw['preview_mode']
    order = raw.get('sort')
    if isinstance(order, dict) and order.get('column') in ('time', 'source', 'turns', 'title') and \
            isinstance(order.get('descending'), bool):
        out['sort'] = dict(column=order['column'], descending=order['descending'])
    return out


def sort_history(rows, column='time', descending=True):
    """Sort all matches before paging; missing/invalid values stay last either way."""
    if column not in ('time', 'source', 'turns', 'title') or not isinstance(descending, bool):
        raise ValueError('Invalid history sort column or direction.')
    def value(row):
        raw = row.get('modified' if column == 'time' else column)
        if raw is None or raw == '':
            return None
        if column in ('time', 'turns'):
            try:
                number = float(raw)
                return number if math.isfinite(number) else None
            except (ValueError, TypeError):
                return None
        return str(raw).casefold()
    stable = sorted(rows, key=lambda r: (str(r.get('source') or '').casefold(),
                                        str(r.get('id') or ''), str(r.get('path') or '')))
    known = [r for r in stable if value(r) is not None]
    missing = [r for r in stable if value(r) is None]
    return sorted(known, key=value, reverse=descending) + missing


def load_preferences():
    try:
        return clean_preferences(json.loads((ah.DATA / 'ui-preferences.json').read_text(encoding='utf-8')))
    except FileNotFoundError:
        return clean_preferences({})
    except (OSError, ValueError) as error:
        log_error('Loading viewer preferences', error)
        return clean_preferences({})


def trash_item_kind(item, imported_keys=()):
    if item.get('kind') == 'import' or item.get('key') in imported_keys:
        return '副本'
    return {'main': '原会话', 'guardian': '审批', 'subagent': '子代理'}.get(item.get('kind'), '类型未记录')


def trash_summary(manifest, imported_keys=(), now=None):
    """Summarize old and new manifests without treating unknown types as originals."""
    now = now or dt.datetime.now(dt.timezone.utc)
    seconds = (dt.datetime.fromisoformat(manifest['expires_at']) - now).total_seconds()
    remaining = '已到期' if seconds <= 0 else ('不到 1 天' if seconds < 86400 else f'{math.ceil(seconds / 86400)} 天')
    items = manifest['items']
    kinds = [trash_item_kind(item, imported_keys) for item in items]
    copies, unknown = kinds.count('副本'), kinds.count('类型未记录')
    counts = f'{len(items)} 项 · {copies} 副本' + (f' · {unknown} 类型未记录' if unknown else '')
    return dict(remaining=remaining, soon=seconds < 3 * 86400,
                sources='、'.join(sorted({item['source'] for item in items})), counts=counts)


def save_preferences(raw):
    path = ah.DATA / 'ui-preferences.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            json.dump(clean_preferences(raw), stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def fit_geometry(value, screen_width, screen_height):
    """Bring a saved window back onto the current screen after a display change."""
    match = re.fullmatch(r'(\d+)x(\d+)([+-]\d+)([+-]\d+)', value or '')
    width, height, x, y = map(int, match.groups()) if match else (1440, 860, 40, 40)
    available_w, available_h = max(320, screen_width - 40), max(240, screen_height - 80)
    width = min(max(width, min(1100, available_w)), available_w)
    height = min(max(height, min(640, available_h)), available_h)
    # Tk negative offsets are relative to the right/bottom edges.
    if x < 0:
        x = screen_width - width + x
    if y < 0:
        y = screen_height - height + y
    x = min(max(0, x), max(0, screen_width - width))
    y = min(max(0, y), max(0, screen_height - height - 40))
    return f'{width}x{height}+{x}+{y}'


def preferred_target(row, targets, preferences):
    """Project preference wins; missing commands fall back to the original connection."""
    project = preferences.get('project_targets', {}).get(project_key(row.get('cwd')))
    default = row['source'] if row['source'] in targets else ('claude' if row['tool'] == 'claude' else '')
    import launch_profiles
    configured, _ = launch_profiles.load()
    configured_default = next((key for key, profile in configured.items() if profile['default'] and profile['enabled']), None)
    import entry_settings
    configured_default = next((key for key, value in entry_settings.load().items()
                               if value.get('default') and value.get('enabled', True)), configured_default)
    for candidate in (project, preferences.get('default_target'), configured_default, default, *targets):
        if candidate in targets:
            return candidate
    return ''


def launch_readiness(target, user_home=None, program=None, *, new_session=False):
    """Inspect executable/config paths only; never authenticate or start a model turn."""
    if target.startswith('profile:'):
        import launch_backend
        return launch_backend.profile_readiness(target, new_session=new_session)
    issues = []
    user_home = Path(user_home) if user_home is not None else Path.home()
    import terminal_platform
    issues.extend(terminal_platform.issues())
    command = program or ('claude' if target == 'claude' else 'codex')
    if target != 'claude' and program is None:
        with contextlib.suppress(OSError, ValueError, KeyError):
            command = connections.load(ah.ROOT)['connections'][target].get('program') or command
    executable = shutil.which(command)
    if not executable and Path(command).is_file():
        executable = command
    if not executable:
        issues.append(f'未找到 {command} 程序，请安装后重试。')
    elif os.name != 'nt' and (not os.access(executable, os.X_OK) or Path(executable).suffix.lower() in ('.cmd','.bat','.ps1','.exe')):
        issues.append('Linux 程序需要执行权限且必须是原生可执行文件或脚本：' + str(command))
    if target != 'claude':
        try:
            registry = json.loads((ah.ROOT / 'connections.json').read_text(encoding='utf-8-sig'))
            entry = registry['connections'].get(target)
            if not entry or entry.get('history_only'):
                raise ValueError('该连接未注册或仅用于查看历史。')
            home = (ah.ROOT / entry['home']).resolve()
            direct = entry.get('direct', False) or os.name != 'nt'
            if not direct and not (user_home / 'codex-switch.ps1').is_file():
                issues.append('缺少 codex-switch.ps1，请重新运行项目安装入口。')
            profile, common = (home / 'config.toml', None) if direct else (home / 'profile.toml', ah.ROOT / 'common.toml')
            for path in (profile, common):
                if path is not None and not path.is_file() and not (new_session and direct):
                    issues.append(f'缺少配置文件：{path}')
            if profile.is_file() and (common is None or common.is_file()):
                config = tomllib.loads(profile.read_text(encoding='utf-8-sig') + '\n\n' +
                                       (common.read_text(encoding='utf-8-sig') if common else ''))

                def incomplete(values):
                    return any((key in ('model', 'base_url') and isinstance(value, str) and 'CHANGE_ME_' in value)
                               or (isinstance(value, dict) and incomplete(value)) for key, value in values.items())

                if incomplete(config):
                    issues.append('连接配置仍包含待填写的 model／base_url。')
            if not new_session:
                try:
                    ci.state_path(home)
                except ValueError:
                    issues.append('连接尚未初始化，请先启动该连接并完成登录。')
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            issues.append(f'无法读取连接配置：{error}')
    return dict(ready=not issues, issues=issues,
                message='\n'.join(issues) if issues else '环境检查通过；登录状态和恢复结果请在终端确认。')


def review_snapshot():
    """Load the newest complete audit and exclude candidates whose files changed."""
    result = dict(candidates={}, created='', report='', outdated=0)
    reports = sorted((ah.DATA / 'audits').glob('*/review-candidates.csv'),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    for report in reports:
        if not (report.parent / 'review.md').is_file():
            continue  # audit writes this last; a report still being built is not ready
        try:
            summary = json.loads((report.parent / 'summary.json').read_text(encoding='utf-8'))
            inventory = json.loads((report.parent / 'inventory.json').read_text(encoding='utf-8'))
            file_times = {row['path']: row['mtime'] for row in inventory['files']}
            created = summary['created']
            dt.datetime.fromisoformat(created)  # reject malformed metadata
            candidates, outdated = {}, 0
            with report.open(encoding='utf-8-sig', newline='') as stream:
                for row in csv.DictReader(stream):
                    if row.get('recommendation') != '可审阅候选':
                        continue
                    path = Path(row['path'])
                    try:
                        stat = path.stat()
                        unchanged = stat.st_size == int(row['size']) and abs(stat.st_mtime - file_times[str(path)]) < 0.00001
                    except (OSError, ValueError, KeyError):
                        unchanged = False
                    if not unchanged:
                        outdated += 1
                        continue
                    if json.loads(row.get('blockers') or '[]'):
                        continue
                    candidates[str(path)] = '；'.join(json.loads(row.get('review_reasons') or '[]'))
            return dict(candidates=candidates, created=created, report=str(report.parent), outdated=outdated)
        except (OSError, ValueError, KeyError, TypeError) as error:
            log_error('Reading cleanup report', error)
    return result


def rebuild_review():
    """Run the existing read-only audit in a worker process; keep UI stdout untouched."""
    output = ah.DATA / 'audits' / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-ui-' + uuid.uuid4().hex[:8])
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    env = os.environ.copy()
    env.update(AGENT_HISTORY_HOME=str(ah.DATA.resolve()), AGENT_HISTORY_SHARED_ROOT=str(ah.ROOT.resolve()),
               PYTHONIOENCODING='utf-8')
    completed = subprocess.run([str(executable), str(Path(__file__).with_name('audit_history.py')),
                                '--refresh', '--output', str(output)], env=env,
                               capture_output=True, text=True, encoding='utf-8', errors='replace',
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if completed.returncode:
        raise ValueError('重新分析失败：' + (completed.stderr.strip() or '请查看错误日志。')[-2000:])
    return review_snapshot()
