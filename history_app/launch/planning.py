"""Conversation launch planning and preparation; usable without Tk or a model turn."""
import contextlib
import base64
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import agent_history as ah
import launch_profiles as profiles
import launch_runner
import connections
import entry_settings
from history_app.reader_data import message_events, positioned_records, file_message_page
import terminal_platform
from history_app.paths import SOURCE

SHORTCUTS = {'openai1': 'codex1', 'openai2': 'codex2', 'openai3': 'codex3', 'deepseek': 'codexds'}
CLAUDE = 'claude'


def file_messages(row, cancel=None):
    """Read the selected physical transcript, including bounded native fork ancestors."""
    if row['tool'] == 'codex':
        home = dict(ah.codex_homes()).get(row['source'])
        if home is None:
            raise ValueError('来源连接已不存在。')
        chain = ah.codex_import.ancestry(home, row['path'])
    else:
        chain = [(Path(row['path']), None, None)]
    messages = []
    for path, byte_limit, ordinal_limit in chain:
        stat = Path(path).stat()
        for offset, ordinal, record in positioned_records(path, byte_limit, ordinal_limit, cancel):
            messages.extend(message_events(row['tool'], path, record, (stat.st_size, stat.st_mtime_ns),
                                           offset=offset, ordinal=ordinal))
    return messages


def physical_row(row):
    """Keep metadata aligned with the file selected in the list."""
    row = dict(row)
    path = Path(row['path'])
    if not path.is_file():
        raise ValueError('所选会话文件已不存在，请刷新历史。')
    row['updated'] = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat()
    if row['tool'] == 'codex':
        meta = ah.codex_import.first_meta(path)
        if (meta.get('id') or meta.get('session_id')) != row['id']:
            raise ValueError('所选文件的会话 ID 已变化，请刷新历史。')
        info = next(event[1] for event in ah.parse_codex(dict(type='session_meta', payload=meta)) if event[0] == 'meta')
        row['cwd'] = info.get('cwd') or ''
        row['kind'] = 'import' if row['kind'] == 'import' else info['kind']
        row['started'] = info.get('started') or row.get('started')
    else:
        for record in ah.codex_import.records(path):
            info = next((event[1] for event in ah.parse_claude(record)
                         if event[0] == 'meta' and event[1].get('id')), None)
            if info:
                if info['id'] != row['id']:
                    raise ValueError('所选文件的会话 ID 已变化，请刷新历史。')
                row['cwd'] = info.get('cwd') or ''
                break
    if row.get('indexed_path') and Path(row['indexed_path']).resolve() != path.resolve():
        row['title'] = row['id']
        for record in ah.codex_import.records(path):
            message = next((event for event in ah.PARSERS[row['tool']](record)
                            if event[0] == 'msg' and event[1] == 'user'), None)
            if message:
                row['title'] = message[2].splitlines()[0][:120]
                break
    return row


def transcript_stamp(row):
    try:
        if row['tool'] == 'codex':
            home = dict(ah.codex_homes()).get(row['source'])
            if home is None:
                raise ValueError('来源连接已不存在。')
            return ah.codex_import.source_stamp(ah.codex_import.ancestry(home, row['path']))
        stat = Path(row['path']).stat()
        return (stat.st_size, stat.st_mtime_ns)
    except OSError as error:
        raise ValueError('所选会话文件已变化或不可读，请刷新历史。') from error


# ---------------------------------------------------------------- data (no tkinter)

def launch_targets():
    """Launchable commands: registered Codex connections (not history-only) and Claude."""
    try:
        registry = json.loads((ah.ROOT / 'connections.json').read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        registry = {'connections': {}}
    names = [n for n, e in registry['connections'].items() if not e.get('history_only')]
    configured, _ = profiles.load()
    configured = sorted(configured.items(), key=lambda item: (
        not item[1]['default'], not item[1]['favorite'], item[1]['order'], item[1]['name'].casefold()))
    defaults = [key for key, profile in configured if profile['enabled'] and profile['default']]
    builtins = names + [CLAUDE]
    options = entry_settings.load()
    builtins = [key for key in builtins if options.get(key, {}).get('enabled', True)]
    builtins.sort(key=lambda key: (not options.get(key, {}).get('default', False),
                                 not options.get(key, {}).get('favorite', False),
                                 options.get(key, {}).get('order', 0)))
    native_defaults = [key for key in builtins if options.get(key, {}).get('default')]
    return native_defaults + defaults + [key for key in builtins if key not in native_defaults] + \
        [key for key, profile in configured if profile['enabled'] and key not in defaults]


def command_label(target):
    if target.startswith('profile:'):
        profile = profiles.get(target)
        actual = profile.get('program') or command_label(profile.get('connection', CLAUDE))
        return profile['name'] + ' [' + profile['id'] + '] (' + actual + ')'
    entry = connections.load(ah.ROOT)['connections'].get(target, {})
    label = target if target == CLAUDE else entry.get('name') or SHORTCUTS.get(target, f'codexc {target}')
    return entry_settings.load().get(target, {}).get('name') or label


def original_of(db, row):
    """Follow an imported copy back to the Codex conversation it was made from."""
    if row['kind'] != 'import' or not row.get('imported_from'):
        return row
    origin = db.execute('SELECT * FROM sessions WHERE key=?', (row['imported_from'],)).fetchone()
    return dict(origin) if origin else row


def builtin_plan(db, row, target, cwd):
    """Return dict(kind, args/session, preview) describing how `target` continues `row`, or raise ValueError."""
    cwd = str(cwd or '').strip()
    if not cwd or not Path(cwd).is_dir():
        raise ValueError('工作目录不存在。')
    row = physical_row(row)
    if target != CLAUDE:
        registry = json.loads((ah.ROOT / 'connections.json').read_text(encoding='utf-8-sig'))
        entry = registry['connections'].get(target)
        if not entry or entry.get('history_only'):
            raise ValueError('目标必须是可启动的已注册 Codex 连接。')
    if target == CLAUDE:
        if row['tool'] == 'claude':
            # Claude finds a session through the project folder named after its directory.
            if ah.claude_project_dir(cwd) != Path(row['path']).parent:
                raise ValueError('Claude 会话只能在原目录恢复：' + (row['cwd'] or ''))
            return dict(kind='claude', session=row['id'], preview=f'cd "{cwd}"\nclaude --resume {row["id"]}')
        if row['kind'] in ('guardian', 'subagent'):
            raise ValueError('审批/子代理会话只能在原连接恢复。')
        return dict(kind='claude-import', session=row['id'], description='切换 Agent，结束后保留最新主会话并清理旧文件',
                    preview=f'cd "{cwd}"\n# 转换选中文件的文字与工具摘要；图片、隐藏推理不迁移\n'
                            f'# 来源：{row["source"]} · {row["id"]}\nclaude --resume <目标会话ID>')
    if row['tool'] == 'codex' and row['source'] == target:
        home = dict(ah.codex_homes()).get(target)
        native = ah.codex_import.source_row(home, row['id']) if home else {}
        if native.get('rollout_path') and not ah.codex_import.same_path(native['rollout_path'], row['path']):
            raise ValueError('原生恢复记录指向另一个文件，无法保证恢复当前预览；请先核对该连接的记录。')
        files = db.execute('SELECT COUNT(DISTINCT path) FROM files WHERE source=? AND session_key=?',
                           (target, row['key'])).fetchone()[0]
        if not native and files > 1:
            raise ValueError('该连接有多个同 ID 文件且缺少原生恢复记录，无法确定恢复版本。')
        args = [target, '-Session', row['id']]          # native resume, copies included
    else:
        if row['tool'] == 'claude':
            homes = dict(ah.codex_homes())
            if target not in homes:
                raise ValueError('目标连接已不存在。')
            selected = dict(row, cwd=cwd)
            imported = ah.codex_import.import_session(db, selected, None, target, homes[target],
                                                     dry_run=True, prefer_native=False, fresh=True)
            return dict(kind='claude-codex-import', target=target, conversion=imported.get('conversion'),
                        description='切换到 Codex，结束后保留最新主会话并清理旧文件',
                        preview=f'cd "{cwd}"\n# 只迁移文字与工具摘要；不重放工具，图片与附件不迁移\n'
                                '# 来源：' + row['id'] + '\n' + command_label(target) +
                                ' -Session <目标会话ID> -NoSync')
        if row['kind'] in ('guardian', 'subagent'):
            raise ValueError('审批/子代理会话只能在原连接恢复。')
        homes = dict(ah.codex_homes())
        if target not in homes or row['source'] not in homes:
            raise ValueError('来源或目标连接已不存在。')
        imported = ah.codex_import.import_session(db, row, homes[row['source']], target, homes[target],
                                                 dry_run=True, prefer_native=False, explicit_handoff=True, fresh=True)
        args = [target, '-From', row['source'], '-Session', row['id']]
        return dict(kind='codex-import', args=args, description='切换连接，结束后保留最新主会话并清理旧文件',
                    preview=f'cd "{cwd}"\n# 创建接续会话；结束并验证后保留此会话，清理已接替的旧文件\n'
                            '# 目标会话 ID 在准备后确定；以下为等价启动命令\n'
                            f'{command_label(target)} ' + ' '.join(args[1:]) + ' -NoSync')
    return dict(kind='codex', args=args, description='在原连接恢复选中版本',
                preview=f'cd "{cwd}"\n' + ' '.join([command_label(target)] + args[1:]) + ' -NoSync')


def claude_copy(db, session, cwd):
    """Reuse the newest untouched Claude copy for this cwd, refresh it, or make a new one."""
    latest = db.execute('SELECT * FROM imports WHERE src_key=? ORDER BY created DESC LIMIT 1',
                        (session['key'],)).fetchone()
    sid = None
    if latest and Path(latest['dst_path']).exists() and \
            Path(latest['dst_path']).parent == ah.claude_project_dir(cwd):
        if ah.continued_in_claude(latest['dst_path']):
            sid = None                                  # the user's own branch: leave it, make a new copy
        else:
            sid = latest['dst_key'].split(':', 1)[1]
    return ah.import_claude(db, session, cwd=cwd, sid=sid)['session']


def terminal_command(cwd, inner, *, custom_environment=False):
    """Open a new terminal in cwd running the PowerShell argument list `inner`."""
    if os.name != 'nt':
        return terminal_platform.linux_terminal() + inner, cwd
    shell = ['powershell.exe', '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass'] + inner
    wt = None if custom_environment else shutil.which('wt.exe')
    if wt:
        # wt treats ';' as a command separator; escape it inside arguments.
        return [wt, 'new-tab', '-d', cwd] + [a.replace(';', r'\;') for a in shell], None
    return shell, cwd


def start_terminal(cwd, inner, env=None):
    if env and env.get('AGENT_HISTORY_JOB_ID'):
        identity = env['AGENT_HISTORY_JOB_ID']
        payload = base64.b64encode(json.dumps(dict(job=identity, inner=inner), ensure_ascii=False).encode('utf-8')).decode('ascii')
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe':
            python = python.with_name('python.exe')
        if os.name != 'nt':
            inner = [str(python), str((SOURCE / 'launch_job_runner.py')), payload]
        else:
            script = '& ' + ' '.join(ps_quote(value) for value in [str(python),
                str((SOURCE / 'launch_job_runner.py')), payload])
            inner = ['-EncodedCommand', base64.b64encode(script.encode('utf-16le')).decode('ascii')]
    cmd, popen_cwd = terminal_command(cwd, inner, custom_environment=bool(env))
    environment = {**os.environ, **env} if env else None
    if environment is not None and 'CODEX_HOME' in env:
        environment.pop('CODEX_SQLITE_HOME', None)
    subprocess.Popen(cmd, cwd=popen_cwd or cwd,
                     env=environment,
                     creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def program_command(program, args):
    """PowerShell receives one quoted literal per argument, never interpolated shell text."""
    launch_runner.command(program, args)  # Validate batch restrictions before opening a terminal.
    if os.name != 'nt':
        return launch_runner.command(program, args)
    payload = base64.b64encode(json.dumps(dict(program=program, args=args), ensure_ascii=False).encode('utf-8')).decode('ascii')
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe' and python.with_name('python.exe').is_file():
        python = python.with_name('python.exe')
    script = '& ' + ' '.join(ps_quote(value) for value in [str(python),
                          str((SOURCE / 'launch_runner.py')), payload])
    return ['-EncodedCommand', base64.b64encode(script.encode('utf-16le')).decode('ascii')]


def codex_invocation(target, sid, extra):
    if os.name != 'nt' or connections.load(ah.ROOT)['connections'].get(target, {}).get('direct'):
        entry = connections.load(ah.ROOT)['connections'][target]
        return launch_runner.invocation(entry.get('program') or 'codex',
                                        entry.get('launch_args', []) + extra + (['resume', sid] if sid else []))
    script = '& ' + ps_quote(str(Path.home() / 'codex-switch.ps1')) + ' ' + ps_quote(target)
    if sid is not None:
        script += ' -Session ' + ps_quote(sid)
    script += ' -NoSync'
    if extra:
        script += ' -CodexArgs @(' + ','.join(ps_quote(value) for value in extra) + ')'
    return script


def profile_readiness(target, *, new_session=False):
    return check_profile(profiles.get(target), new_session=new_session)


def check_profile(profile, *, new_session=False):
    profile = profiles.validate(profile)
    issues = []
    issues.extend(terminal_platform.issues())
    program = profile.get('program')
    if program and not (shutil.which(program) or Path(program).is_file()):
        issues.append('未找到程序／脚本：' + program)
    elif program and os.name != 'nt':
        try:
            launch_runner.command(program, [])
        except ValueError as error:
            issues.append(str(error))
    if profile['type'] == 'codex':
        import ui_support
        issues.extend(ui_support.launch_readiness(profile['connection'], program=program, new_session=new_session)['issues'])
    elif profile['type'] == 'claude' and not program and not shutil.which('claude'):
        issues.append('未找到 claude 程序。')
    if profile['cwd_policy'] == 'fixed' and not Path(profile['cwd']).expanduser().is_dir():
        issues.append('固定工作目录不存在。')
    try:
        profiles.environment(profile)
    except ValueError as error:
        issues.append(str(error))
    return dict(ready=not issues, issues=issues, message='可启动' if not issues else '\n'.join(issues))


def launch_plan(db, row, target, cwd):
    if not target.startswith('profile:'):
        plan = builtin_plan(db, row, target, cwd)
        if target != CLAUDE and (connections.load(ah.ROOT)['connections'][target].get('direct') or os.name != 'nt'):
            sid = row['id'] if plan['kind'] == 'codex' else '<目标会话ID>'
            entry = connections.load(ah.ROOT)['connections'][target]
            home = str(dict(ah.codex_homes())[target])
            prefix = 'cd ' + (ps_quote(cwd) if os.name == 'nt' else terminal_platform.shlex.quote(cwd)) + '\n'
            prefix += '\n'.join(line for line in plan['preview'].splitlines() if line.startswith('#'))
            prefix += '\n' if not prefix.endswith('\n') else ''
            environment = '$env:CODEX_HOME=' + ps_quote(home) + '\n' if os.name == 'nt' else \
                          'CODEX_HOME=' + terminal_platform.shlex.quote(home) + ' '
            plan['preview'] = prefix + environment + codex_invocation(target, sid, [])
        return plan
    profile = profiles.get(target)
    row = physical_row(row)
    cwd = profiles.working_directory(profile, row, cwd)
    environment = profiles.environment(profile)
    if profile['type'] == 'custom':
        if row['tool'] not in profile['resume_tools'] or not profile['resume_args']:
            raise ValueError('此自定义配置仅启动新会话，或未声明支持所选历史的恢复方式。')
        if row['tool'] == 'claude' and ah.claude_project_dir(cwd) != Path(row['path']).parent:
            raise ValueError('自定义命令恢复 Claude 历史时需使用原工作目录。')
        plan = dict(kind='custom', session=row['id'], description='用自定义命令恢复选中会话 ID')
    else:
        native_target = profile['connection'] if profile['type'] == 'codex' else CLAUDE
        plan = builtin_plan(db, row, native_target, cwd)
        plan['native_target'] = native_target
    plan.update(profile=profile, target=target, cwd=cwd, environment=environment)
    # The preview shows placeholders until import preparation supplies the actual target id.
    if profile.get('program'):
        sid = plan.get('session') or (row['id'] if plan['kind'] in ('codex', 'custom') else '<目标会话ID>')
        values = dict(session_id=sid, source_session_id=row['id'],
                      source=row['source'], connection=profile.get('connection', ''), cwd=cwd)
        resume = profile['resume_args'] or (['resume', '{session_id}'] if profile['type'] == 'codex' else ['--resume', '{session_id}'])
        args = profiles.expand(profile['args'] + resume, values)
        check_values = dict(values, session_id='00000000-0000-0000-0000-000000000000' if sid == '<目标会话ID>' else sid)
        launch_runner.command(profile['program'], profiles.expand(profile['args'] + resume, check_values))
        prefix = plan['preview'].rsplit('\n', 1)[0] if 'preview' in plan else 'cd ' + ps_quote(cwd)
        plan['preview'] = prefix + '\n' + launch_runner.invocation(profile['program'], args)
    elif profile['type'] == 'claude':
        sid = plan.get('session', '<目标会话ID>')
        values = dict(session_id=sid, source_session_id=row['id'], source=row['source'], connection='', cwd=cwd)
        args = profiles.expand(profile['args'], values) + ['--resume', sid]
        launch_runner.command('claude', profiles.expand(profile['args'], dict(values,
                              session_id='00000000-0000-0000-0000-000000000000' if sid == '<目标会话ID>' else sid)) +
                              ['--resume', '00000000-0000-0000-0000-000000000000' if sid == '<目标会话ID>' else sid])
        prefix = plan['preview'].rsplit('\n', 1)[0]
        plan['preview'] = prefix + '\n' + launch_runner.invocation('claude', args)
    elif profile['type'] == 'codex':
        sid = row['id'] if plan['kind'] == 'codex' else '<目标会话ID>'
        values = dict(session_id=sid, source_session_id=row['id'], source=row['source'], connection=profile['connection'], cwd=cwd)
        extra = profiles.expand(profile['args'], values)
        prefix = plan['preview'].rsplit('\n', 1)[0]
        plan['preview'] = prefix + '\n' + codex_invocation(profile['connection'], sid, extra)
    if profile['env']:
        plan['preview'] += '\n# 配置环境变量：' + ', '.join(profile['env']) + '（值不显示）'
    return plan


def prepared_command(plan, sid, row):
    profile = plan.get('profile')
    if profile and profile.get('program'):
        values = dict(session_id=sid, source_session_id=row['id'], source=row['source'],
                      connection=profile.get('connection', ''), cwd=plan['cwd'])
        resume = profile['resume_args'] or (['resume', '{session_id}'] if profile['type'] == 'codex' else ['--resume', '{session_id}'])
        inner = program_command(profile['program'], profiles.expand(profile['args'] + resume, values))
    elif plan['kind'] in ('codex', 'codex-import', 'claude-codex-import'):
        target = plan.get('native_target') or plan.get('target') or plan['args'][0]
        extra = profiles.expand(profile['args'], dict(session_id=sid, source_session_id=row['id'],
                                source=row['source'], connection=target, cwd=plan['cwd'])) if profile else []
        inner = ['-File', str(Path.home() / 'codex-switch.ps1'), target, '-Session', sid, '-NoSync']
        if extra:
            # Call the script directly so -CodexArgs receives a real PowerShell array.
            script = codex_invocation(target, sid, extra)
            inner = ['-EncodedCommand', base64.b64encode(script.encode('utf-16le')).decode('ascii')]
        if connections.load(ah.ROOT)['connections'][target].get('direct') or os.name != 'nt':
            entry = connections.load(ah.ROOT)['connections'][target]
            inner = program_command(entry.get('program') or 'codex', entry.get('launch_args', []) + extra + ['resume', sid])
    else:
        values = dict(session_id=sid, source_session_id=row['id'], source=row['source'], connection='', cwd=plan['cwd'])
        args = profiles.expand(profile['args'], values) if profile else []
        inner = program_command('claude', args + ['--resume', sid])
    environment = dict(plan.get('environment') or {})
    if profile and profile['type'] == 'codex' and profile.get('program'):
        environment['CODEX_HOME'] = str(dict(ah.codex_homes())[profile['connection']])
    elif plan['kind'] in ('codex', 'codex-import', 'claude-codex-import') and \
            (connections.load(ah.ROOT)['connections'][target].get('direct') or os.name != 'nt'):
        environment['CODEX_HOME'] = str(dict(ah.codex_homes())[target])
    return dict(cwd=plan['cwd'], inner=inner, env=environment, session=sid, kind=plan['kind'])


def prepare_launch(db, row, target, cwd, expected_stamp=None, *, policy='single'):
    """Caller holds the collector lock; prepare one selected version, then return its command."""
    import setup_backend
    if not setup_backend.source_enabled(row, setup_backend.load_sources()):
        raise ValueError('此历史来源已停用，请先重新接入。')
    stamp = transcript_stamp(row)
    if expected_stamp is not None and stamp != expected_stamp:
        raise ValueError('来源会话已变化，请刷新后再启动。')
    plan = launch_plan(db, row, target, cwd)
    plan.setdefault('cwd', str(Path(cwd).resolve()))
    plan.setdefault('target', target)
    kind = plan['kind']
    import conversation_store as store
    store.ensure(db)
    sync_report = store.sync_all(db)
    source = store.enroll(db, physical_row(row))
    if policy == 'single':
        import single_conversation
        single_conversation.available(db, source)
    if row.get('logical') or policy == 'single':
        sync_report = store.sync_all(db, source['conversation'], force=True)
        failed_versions = {failure['version'] for failure in sync_report['failed']}
        member_ids = {item['id'] for item in db.execute(
            'SELECT id FROM conversation_versions WHERE conversation=?', (source['conversation'],))}
        if failed_versions & member_ids:
            raise ValueError('这条对话的部分平台版本读取失败，请检查历史来源并刷新后再继续。')
        if row.get('logical') and policy != 'single':
            heads = store.tips(db, source['conversation'])
            if len(heads) > 1:
                raise ValueError('这条对话有多个续聊分支，请在版本选择中明确选择一个分支。')
            if heads and heads[0]['id'] != source['id']:
                raise ValueError('另一平台已有新增内容，请刷新并查看最新版本后继续。')
    if policy == 'single':
        single_conversation.latest(db, source)
    cross = kind in ('codex-import', 'claude-codex-import', 'claude-import')
    native_target = plan.get('native_target') or target
    identity = None
    if cross:
        import handoff_lifecycle
        identity, target_row = handoff_lifecycle.prepare(db, source, native_target, plan['cwd'], policy=policy)
        sid = target_row['id']
    else:
        sid = plan.get('session') or row['id']
        store.activate(db, source)
    if transcript_stamp(row) != stamp:
        if identity:
            import launch_jobs
            launch_jobs.update(identity, state='failed', error='准备期间来源变化，临时副本保留。')
        raise ValueError('准备期间来源会话变化，尚未打开终端；请刷新后重试。')
    prepared = prepared_command(plan, sid, row)
    import launch_jobs
    if identity:
        launch_jobs.update(identity, session=prepared['session'], kind=prepared['kind'], target=target, handoff_state='prepared')
    else:
        identity = launch_jobs.create(row, prepared, target)
    if policy == 'single':
        # Reserve the actual target version so the independent runner can reject
        # stale/double launches even after the UI has closed.
        selected = store.lookup(db, target_row) if cross else source
        try:
            single_conversation.reserve(db, selected, identity)
        except (OSError, ValueError, TimeoutError) as error:
            launch_jobs.update(identity, state='failed', error=str(error), sync_state='failed')
            if cross:
                handoff_lifecycle.update(db, identity, state='failed', error=str(error))
            raise
    prepared['env'].update(AGENT_HISTORY_JOB_ID=identity, AGENT_HISTORY_HOME=str(ah.DATA),
                           AGENT_HISTORY_SHARED_ROOT=str(ah.ROOT))
    prepared['job'] = identity
    return prepared


def test_launch(db, row, target, cwd):
    """Read-only adapter/argv checks; no Agent process, authentication or model request."""
    import ui_support
    readiness = ui_support.launch_readiness(target)
    plan = launch_plan(db, row, target, cwd)
    if not readiness['ready']:
        raise ValueError(readiness['message'])
    return dict(ready=True, preview=plan['preview'], message='启动检查通过；未启动 Agent 或发送模型请求。')


def new_profile(target):
    """Use the same configuration for builtin and user-defined new sessions."""
    if target.startswith('profile:'):
        return profiles.get(target)
    if not entry_settings.load().get(target, {}).get('enabled', True):
        raise ValueError('启动入口已停用。')
    if target != CLAUDE:
        entry = connections.load(ah.ROOT)['connections'].get(target)
        if not entry or entry.get('history_only'):
            raise ValueError('目标必须是可启动的已注册 Codex 连接。')
    return profiles.validate(dict(id='builtin', type='claude' if target == CLAUDE else 'codex',
                                  connection='' if target == CLAUDE else target))


def new_readiness(target):
    return check_profile(new_profile(target), new_session=True)


def new_plan(target, cwd):
    """Construct a new-session command, including custom profiles without resume support."""
    profile = new_profile(target)
    cwd = profiles.working_directory(profile, dict(cwd=cwd), cwd)
    values = dict(session_id='', source_session_id='', source='', connection=profile.get('connection', ''), cwd=cwd)
    args = profiles.expand(profile['args'], values)
    program = profile.get('program') or ('claude' if profile['type'] == 'claude' else 'powershell.exe')
    extra = []
    if profile['type'] == 'codex' and not profile.get('program'):
        program, extra = str(Path.home() / 'codex-switch.ps1'), args
        args = [profile['connection'], '-NoSync']
        entry = connections.load(ah.ROOT)['connections'][profile['connection']]
        if entry.get('direct') or os.name != 'nt':
            program, args, extra = entry.get('program') or 'codex', entry.get('launch_args', []) + extra, []
    quote = ps_quote if os.name == 'nt' else terminal_platform.shlex.quote
    preview = 'cd ' + quote(cwd) + '\n' + launch_runner.invocation(program, args)
    if profile['type'] == 'codex' and not profile.get('program') and Path(program).suffix == '.ps1':
        preview = 'cd ' + ps_quote(cwd) + '\n' + codex_invocation(profile['connection'], None, extra)
    launch_runner.command(program, args)
    if profile['type'] == 'codex' and (profile.get('program') or os.name != 'nt' or
            connections.load(ah.ROOT)['connections'][profile['connection']].get('direct')):
        home = str(dict(ah.codex_homes())[profile['connection']])
        preview = 'cd ' + quote(cwd) + '\n' + ('$env:CODEX_HOME=' + quote(home) + '\n' if os.name == 'nt'
                  else 'CODEX_HOME=' + quote(home) + ' ') + launch_runner.invocation(program, args)
    return dict(cwd=cwd, program=program, args=args, codex_args=extra, tool=profile['type'],
                preview=preview, env_names=list(profile['env']))


def prepare_new(target, cwd, expected_plan=None):
    plan = new_plan(target, cwd)
    if expected_plan is not None and plan != expected_plan:
        raise ValueError('启动配置或工作目录已变化，请重新查看预览后启动。')
    profile = new_profile(target)
    environment = profiles.environment(profile)
    if profile['type'] == 'codex' and (profile.get('program') or os.name != 'nt' or
            connections.load(ah.ROOT)['connections'][profile['connection']].get('direct')):
        environment['CODEX_HOME'] = str(dict(ah.codex_homes())[profile['connection']])
    inner = program_command(plan['program'], plan['args'])
    if plan['codex_args']:
        script = codex_invocation(profile['connection'], None, plan['codex_args'])
        inner = ['-EncodedCommand', base64.b64encode(script.encode('utf-16le')).decode('ascii')]
    roots = []
    if profile['type'] == 'codex':
        roots = [str(dict(ah.codex_homes())[profile['connection']])]
    elif profile['type'] == 'claude':
        roots = [str(home / 'projects') for home in ah.claude_homes()]
    prepared = dict(cwd=plan['cwd'], inner=inner, env=environment, history_roots=roots,
                    session=None, kind='new', tool=profile['type'])
    import launch_jobs
    identity = launch_jobs.create(None, prepared, target)
    environment.update(AGENT_HISTORY_JOB_ID=identity, AGENT_HISTORY_HOME=str(ah.DATA),
                       AGENT_HISTORY_SHARED_ROOT=str(ah.ROOT))
    prepared['job'] = identity
    return prepared
