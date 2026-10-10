"""Capture the actual viewer with generated histories; never load user data.

Windows desktop and Pillow are required. No Agent process is started. All
runtime files are temporary; project/cwd labels are intentionally fictional.
"""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def capture(output):
    from PIL import ImageGrab
    import tkinter as tk
    from tkinter import ttk
    with tempfile.TemporaryDirectory(prefix='agent-history-demo-') as folder:
        base=Path(folder).resolve()
        shared,data,home=(base/name for name in ('config','data','codex'))
        shared.mkdir(); data.mkdir(); (home/'sessions').mkdir(parents=True)
        (shared/'connections.json').write_text(json.dumps(dict(connections=dict(
            demo=dict(home=str(home),direct=True,name='Codex · 演示',sync_history=False)))),encoding='utf-8')
        (home/'config.toml').write_text('model="demo-model"\n',encoding='utf-8')
        (data/'sources.json').write_text(json.dumps(dict(version=1,codex=['demo'],claude=[])),encoding='utf-8')
        (data/'ui-preferences.json').write_text(json.dumps(dict(geometry='1440x860+40+40',
            preview_mode='markdown',panes=[0.26,0.73])),encoding='utf-8')
        (data/'launch-profiles.json').write_text(json.dumps(dict(version=1,profiles=[dict(
            id='demo_agent',name='Codex · 演示入口',type='codex',connection='demo',
            program='C:/demo/bin/codex.exe',favorite=True,default=True)])),encoding='utf-8')
        stamp=dt.datetime.now(dt.timezone.utc).isoformat()
        messages=[('user','请整理一下我们的项目发布计划。'),('assistant',
            '# Beta 发布计划\n\n先完成安装、阅读与接续这三个流程。\n\n'
            '| 项目 | 状态 |\n|---|---|\n| Markdown 预览 | 已完成 |\n| 启动配置 | 已完成 |\n| 发布验证 | 进行中 |\n\n'
            '## 第一次使用\n\n1. 选择历史来源\n2. 搜索并查看对话\n3. 选择 Agent 与工作目录\n\n'
            '> 这里的内容全部是生成的演示数据。\n\n'
            '```python\nprint("Hello, Agent History!")\n```\n\n'
            '工具调用可以展开查看参数与输出。')]
        records=[dict(type='session_meta',timestamp=stamp,payload=dict(
            id='00000000-0000-4000-8000-000000000001',cwd='C:/demo/project',source='cli',timestamp=stamp))]
        for role,text in messages:
            records.append(dict(type='response_item',timestamp=stamp,payload=dict(type='message',role=role,
                content=[dict(type='input_text' if role=='user' else 'output_text',text=text)])))
        records.extend([dict(type='response_item',timestamp=stamp,payload=dict(type='function_call',
            name='list_files',call_id='demo-call',arguments=json.dumps(dict(directory='C:/demo/project')))),
            dict(type='response_item',timestamp=stamp,payload=dict(type='function_call_output',call_id='demo-call',
                output='README.md\nLICENSE\nVERSION\n'))])
        (home/'sessions/demo.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
        with patch.dict(os.environ,AGENT_HISTORY_SHARED_ROOT=str(shared),AGENT_HISTORY_HOME=str(data)):
            import agent_history as ah
            import history_ui as ui
            root=tk.Tk()
            def widgets():
                result=[]; pending=[root]
                while pending:
                    widget=pending.pop(); result.append(widget); pending.extend(widget.winfo_children())
                return result
            def wait(predicate):
                end=time.monotonic()+15
                while time.monotonic()<end:
                    root.update()
                    if predicate(): return
                    time.sleep(0.02)
                raise TimeoutError('Demo UI did not become ready')
            def mainloop(window):
                root.lift(); root.attributes('-topmost',True)
                tree=next(w for w in widgets() if isinstance(w,ttk.Treeview) and 'check' in w.cget('columns'))
                wait(lambda: bool(tree.get_children()))
                tree.selection_set(tree.get_children()[0]); tree.event_generate('<<TreeviewSelect>>')
                wait(lambda: any(isinstance(w,tk.Text) and 'Beta 发布计划' in w.get('1.0','end') for w in widgets()))
                deadline=time.monotonic()+1
                while time.monotonic()<deadline: root.update(); time.sleep(0.02)
                for widget in widgets():
                    if isinstance(widget,tk.Text): widget.yview_moveto(0)
                root.update()
                # Preview contains a temporary runtime path. Publish only the
                # history/Markdown portion; crop excludes private launch argv.
                pane=next(w for w in widgets() if isinstance(w,ttk.Panedwindow))
                right=pane.sashpos(1)+pane.winfo_rootx()
                bounds=(root.winfo_rootx(),root.winfo_rooty(),right,root.winfo_rooty()+root.winfo_height())
                output.parent.mkdir(parents=True,exist_ok=True)
                ImageGrab.grab(bbox=bounds).save(output)
                root.tk.call(root.protocol('WM_DELETE_WINDOW'))
            with patch.object(tk,'Tk',return_value=root), patch.object(type(root),'mainloop',mainloop), \
                    patch.object(ah,'ROOT',shared),patch.object(ah,'DATA',data),patch.object(ah,'CLAUDE_HOMES',[]), \
                    patch.object(ui,'start_terminal',side_effect=AssertionError('Demo must not launch an Agent')):
                ui.run_ui()
            print('Saved generated-history screenshot: '+str(output))


if __name__=='__main__':
    capture(Path(__file__).resolve().parents[1]/'docs/images/demo.png')
