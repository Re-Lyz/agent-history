"""Presentation overrides for registered launch entries, outside the checkout."""
import json
import agent_history as ah
from setup_backend import atomic_json


def load():
    try:
        value = json.loads((ah.DATA / 'launch-entries.json').read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('launch-entries.json 应为对象。')
        for key, entry in value.items():
            if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not entry['name'].strip():
                raise ValueError('启动入口显示名称无效：' + str(key))
            if any(type(entry.get(field)) is not bool for field in ('enabled', 'favorite', 'default')):
                raise ValueError('启动入口开关必须为布尔值：' + str(key))
            if type(entry.get('order')) is not int:
                raise ValueError('启动入口顺序必须为整数：' + str(key))
        return value
    except FileNotFoundError:
        return {}


def save(key, item=None):
    values = load()
    if item is None:
        values.pop(key, None)
    else:
        values[key] = {name: item[name] for name in ('name', 'enabled', 'favorite', 'default', 'order')}
        if item['default']:
            for other, value in values.items():
                if other != key:
                    value['default'] = False
    atomic_json(ah.DATA / 'launch-entries.json', values)


def item(key, name):
    return {**dict(name=name, enabled=True, favorite=False, default=False, order=0), **load().get(key, {})}
