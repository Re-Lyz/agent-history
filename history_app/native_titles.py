"""Synchronize explicit user names through Codex, never through SQL writes."""
from datetime import datetime, timezone
from pathlib import Path

import agent_history as ah
import codex_import as native
from native_codex import CodexRPC
from setup_backend import load_sources, source_enabled


def update(row, value, journal, save):
    item = dict(source=row['source'], session=row['id'], path=row['path'], state='pending')
    journal['native'].append(item)
    save()
    try:
        homes = dict(ah.codex_homes())
        home = homes.get(row['source'])
        if home is None or not source_enabled(row, load_sources()) or not Path(row['path']).is_file():
            raise ValueError('来源已停用或会话文件不存在，原生名称保留。')
        native.config(home)  # Reject unsupported SQLite-home overrides.
        metadata = native.source_row(home, row['id'])
        if not metadata or not native.same_path(metadata['rollout_path'], row['path']):
            raise ValueError('原生索引未指向所选文件，原生名称保留。')
        with native.writer_lock(home, row['id']) as free:
            if not free:
                item.update(state='busy', error='会话正在使用，退出后重新修改名称可重试同步。')
                return
            # Validate the native metadata ID against the actual selected file.
            meta = native.first_meta(row['path'])
            if (meta.get('id') or meta.get('session_id')) != row['id']:
                raise ValueError('原生文件身份已变化，请刷新后重试。')
            with CodexRPC(home) as rpc:
                thread = rpc.call('thread/read', dict(threadId=row['id']))['thread']
                if thread['id'] != row['id'] or not native.same_path(thread.get('path') or '', row['path']):
                    raise ValueError('Codex 返回的会话与所选文件不一致。')
                # Codex rejects empty names. Reset to the original prompt preview.
                desired = value.strip() or (thread.get('preview') or metadata.get('first_user_message') or row['id'])[:200]
                item.update(previous_name=thread.get('name'), requested_name=desired,
                            home=str(home), state='prepared')
                save()  # Durable old name before the first native write.
                rpc.call('thread/name/set', dict(threadId=row['id'], name=desired))
                actual = rpc.call('thread/read', dict(threadId=row['id']))['thread']
                if actual.get('name') != desired:
                    raise ValueError('原生名称已提交，但读回不一致；请重试同步。')
                item.update(state='synced')
    except Exception as error:
        item.update(state='failed', error=str(error))
    finally:
        item['updated_at'] = datetime.now(timezone.utc).isoformat()
        save()
