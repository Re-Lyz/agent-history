"""Validate configuration values without reading Agent credentials or executing commands."""


def valid(name, value):
    if not isinstance(value, dict):
        return False
    if name == 'connections':
        return isinstance(value.get('connections'), dict) and all(
            isinstance(item, dict) and isinstance(item.get('home'), str) and item['home']
            for item in value['connections'].values())
    if name == 'sources':
        return value.get('version') == 1 and all(isinstance(value.get(key), list) and
            all(isinstance(item, str) for item in value[key]) for key in ('codex', 'claude'))
    if name == 'launch-profiles':
        import launch_profiles
        if value.get('version') != 1 or not isinstance(value.get('profiles'), list):
            return False
        try:
            profiles = [launch_profiles.validate(item) for item in value['profiles']]
            return len({item['id'] for item in profiles}) == len(profiles) and \
                sum(item['default'] for item in profiles) <= 1
        except (ValueError, TypeError):
            return False
    if name == 'launch-entries':
        return all(isinstance(item, dict) and isinstance(item.get('name'), str) and item['name'].strip() and
            all(type(item.get(key)) is bool for key in ('enabled', 'favorite', 'default')) and
            type(item.get('order')) is int for item in value.values())
    return name == 'ui-preferences' and value.get('version', 1) == 1
