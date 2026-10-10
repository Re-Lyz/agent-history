"""User launch profiles stored outside the checkout. No command execution on load/check."""
import argparse
import json
import os
from pathlib import Path
import re
import string
import tempfile

import agent_history as ah

FIELDS = {'id', 'name', 'type', 'connection', 'program', 'args', 'resume_args',
          'resume_tools', 'cwd_policy', 'cwd', 'env', 'enabled', 'favorite', 'order', 'default'}
VARIABLES = {'session_id', 'source_session_id', 'source', 'connection', 'cwd'}
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


def config_path():
    return ah.DATA / 'launch-profiles.json'


def text(value, field, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or '\0' in value or '\n' in value or '\r' in value:
        raise ValueError(f'{field}: expected a nonempty single-line string.')
    return value


def arguments(value, field):
    if not isinstance(value, list):
        raise ValueError(f'{field}: expected an argument array, not shell text.')
    for item in value:
        text(item, field, empty=True)
        try:
            for _, name, spec, conversion in string.Formatter().parse(item):
                if name is not None and (name not in VARIABLES or spec or conversion):
                    raise ValueError(f'{field}: unsupported placeholder {{{name}}}.')
        except ValueError as error:
            raise ValueError(f'{field}: {error}') from error
    return list(value)


def validate(raw):
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError('Profile must be an object containing only documented fields.')
    result = dict(raw)
    identity = text(raw.get('id'), 'id')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', identity):
        raise ValueError('id: use letters, numbers, underscores or hyphens.')
    result['name'] = text(raw.get('name', identity), 'name')
    kind = raw.get('type')
    if kind not in ('codex', 'claude', 'custom'):
        raise ValueError('type: expected codex, claude or custom.')
    if kind == 'codex':
        result['connection'] = text(raw.get('connection'), 'connection')
    elif raw.get('connection'):
        raise ValueError('connection: only Codex profiles associate with a registered connection.')
    if 'program' in raw:
        result['program'] = text(raw['program'], 'program')
    elif kind == 'custom':
        raise ValueError('program: required for a custom profile.')
    result['args'] = arguments(raw.get('args', []), 'args')
    result['resume_args'] = arguments(raw.get('resume_args', []), 'resume_args')
    if result['resume_args'] and not any(name == 'session_id' for arg in result['resume_args']
            for _, name, _, _ in string.Formatter().parse(arg)):
        raise ValueError('resume_args: must include {session_id}.')
    if kind in ('codex', 'claude') and not raw.get('program') and result['resume_args']:
        raise ValueError('resume_args: the built-in launcher supplies its own resume arguments; specify program to override.')
    tools = raw.get('resume_tools', [])
    if not isinstance(tools, list) or any(tool not in ('codex', 'claude') for tool in tools):
        raise ValueError('resume_tools: expected an array of codex/claude.')
    result['resume_tools'] = list(dict.fromkeys(tools))
    if kind != 'custom' and tools:
        raise ValueError('resume_tools: only custom profiles declare source tool support.')
    if kind == 'custom' and bool(tools) != bool(result['resume_args']):
        raise ValueError('Custom resume requires both resume_tools and resume_args; omit both for new sessions only.')
    policy = raw.get('cwd_policy', 'chosen')
    if policy not in ('chosen', 'session', 'fixed'):
        raise ValueError('cwd_policy: expected chosen, session or fixed.')
    result['cwd_policy'] = policy
    if policy == 'fixed':
        result['cwd'] = text(raw.get('cwd'), 'cwd')
        if not Path(result['cwd']).expanduser().is_absolute():
            raise ValueError('cwd: fixed directory must be absolute.')
    env = raw.get('env', {})
    if not isinstance(env, dict):
        raise ValueError('env: expected an object.')
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise ValueError('env: invalid environment variable name.')
        if isinstance(value, dict) and set(value) == {'from_env'}:
            if not isinstance(value['from_env'], str) or not ENV_NAME.fullmatch(value['from_env']):
                raise ValueError(f'env.{name}: invalid from_env reference.')
        elif not isinstance(value, str) or '\0' in value:
            raise ValueError(f'env.{name}: expected a string or {{"from_env": "NAME"}}.')
        if kind == 'codex' and name.upper() in ('CODEX_HOME', 'CODEX_SQLITE_HOME'):
            raise ValueError(f'env.{name}: the selected connection controls Codex storage.')
    result['env'] = env
    result['enabled'] = raw.get('enabled', True)
    if not isinstance(result['enabled'], bool):
        raise ValueError('enabled: expected a boolean.')
    for field in ('favorite', 'default'):
        result[field] = raw.get(field, False)
        if not isinstance(result[field], bool):
            raise ValueError(field + ': expected a boolean.')
    result['order'] = raw.get('order', 0)
    if type(result['order']) is not int:
        raise ValueError('order: expected an integer.')
    return result


def load():
    """Return valid profiles and field errors; broken entries do not hide other launchers."""
    try:
        data = json.loads(config_path().read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError) as error:
        return {}, [f'launch-profiles.json: {error}']
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('profiles'), list):
        return {}, ['launch-profiles.json: expected version 1 and a profiles array.']
    profiles, errors = {}, []
    for index, raw in enumerate(data['profiles']):
        try:
            profile = validate(raw)
            key = 'profile:' + profile['id']
            if key in profiles:
                raise ValueError('id: duplicate profile.')
            profiles[key] = profile
        except ValueError as error:
            errors.append(f'profiles[{index}]: {error}')
    return profiles, errors


def get(key):
    profiles, _ = load()
    if key not in profiles or not profiles[key]['enabled']:
        raise ValueError('启动配置不存在、无效或已禁用：' + key)
    return profiles[key]


def save(items):
    validated = [validate(item) for item in items]
    if len({item['id'] for item in validated}) != len(validated):
        raise ValueError('Duplicate profile ids.')
    if sum(item['default'] for item in validated) > 1:
        raise ValueError('default: only one profile can be the default.')
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            json.dump(dict(version=1, profiles=validated), stream, indent=2, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def working_directory(profile, row, chosen):
    policy = profile['cwd_policy']
    value = profile.get('cwd') if policy == 'fixed' else row.get('cwd') if policy == 'session' else chosen
    directory = Path(value).expanduser() if value else None
    if directory is None or not directory.is_dir():
        raise ValueError('工作目录不存在（策略：' + policy + '）。')
    return str(directory.resolve())


def environment(profile, environ=None):
    environ = os.environ if environ is None else environ
    values = {}
    for name, value in profile['env'].items():
        if isinstance(value, dict):
            reference = value['from_env']
            if reference not in environ:
                raise ValueError('缺少环境变量：' + reference)
            value = environ[reference]
        values[name] = value
    return values


def expand(args, values):
    return [arg.format_map(values) for arg in args]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    put = sub.add_parser('put', help='add/replace a profile from a JSON object file')
    put.add_argument('file', type=Path)
    remove = sub.add_parser('remove')
    remove.add_argument('id')
    check = sub.add_parser('check', help='validate readiness without starting a process or model')
    check.add_argument('id')
    new = sub.add_parser('new-plan', help='preview a new-session command without executing it')
    new.add_argument('id')
    new.add_argument('--cwd', required=True)
    start = sub.add_parser('start-new', help='open an independent terminal with a new-session profile')
    start.add_argument('id')
    start.add_argument('--cwd', required=True)
    for name in ('plan', 'prepare'):
        resume = sub.add_parser(name, help='preview/prepare selected history without opening a terminal')
        resume.add_argument('id')
        resume.add_argument('--session', required=True)
        resume.add_argument('--source', help='explicit Codex source connection, or claude')
        resume.add_argument('--cwd')
    args = parser.parse_args()
    items, errors = load()
    if args.command == 'list':
        print(json.dumps(dict(path=str(config_path()), errors=errors, profiles=[
            {k: v for k, v in item.items() if k != 'env'} | {'env_names': list(item['env'])}
            for item in items.values()]), ensure_ascii=False, indent=2))
    elif args.command in ('put', 'remove'):
        if errors:
            raise ValueError('Repair configuration errors before editing: ' + '; '.join(errors))
        if args.command == 'put':
            profile = validate(json.loads(args.file.read_text(encoding='utf-8-sig')))
            items['profile:' + profile['id']] = profile
        else:
            if items.pop('profile:' + args.id.removeprefix('profile:'), None) is None:
                raise ValueError('Profile not found.')
        save(list(items.values()))
        print(str(config_path()))
    else:
        import launch_backend as backend
        key = 'profile:' + args.id.removeprefix('profile:')
        if args.command in ('plan', 'prepare'):
            import trash
            with trash.locked_db() as db:
                row = ah.resolve(db, 'claude:' + args.session.removeprefix('claude:')) if args.source == 'claude' else \
                    ah.resolve_codex(db, args.source, args.session) if args.source else ah.resolve(db, args.session)
                row = backend.physical_row(row)
                chosen = args.cwd or row['cwd']
                result = backend.launch_plan(db, row, key, chosen) if args.command == 'plan' else \
                    backend.prepare_launch(db, row, key, chosen)
                result = {name: value for name, value in result.items() if name not in ('profile', 'environment', 'env')}
                result['env_names'] = list(get(key)['env'])
        elif args.command == 'start-new':
            ready = backend.profile_readiness(key)
            if not ready['ready']:
                raise ValueError(ready['message'])
            plan = backend.prepare_new(key, args.cwd)
            backend.start_terminal(plan['cwd'], plan['inner'], env=plan['env'])
            result = dict(status='terminal-opened')
        else:
            result = backend.profile_readiness(key) if args.command == 'check' else backend.new_plan(key, args.cwd)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
