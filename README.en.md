# Agent History

[中文](README.md) · **0.1.0-beta.1** · MIT licensed

[Download the beta source ZIP](https://github.com/Re-Lyz/agent-history/releases/tag/v0.1.0-beta.1) · [Windows / Ubuntu CI](https://github.com/Re-Lyz/agent-history/actions/runs/38031469719)

A local desktop viewer and history index for Codex and Claude Code. Search conversations, read Markdown, select an Agent command and working directory, and resume in an independent terminal. Supports a single Agent as well as multiple Codex homes / login connections. Agent installation and authentication remain with the user's CLI.

![Real desktop UI with generated conversation data](docs/images/demo.png)

## Install

Requires Python 3.11+, Tk, and SQLite FTS5 with the trigram tokenizer. Use a Python build that includes Tcl/Tk on Windows; install your distribution's Python, venv and Tk packages on Linux. From a clone or extracted source ZIP:

```text
git clone https://github.com/Re-Lyz/agent-history.git
cd agent-history
python -m venv .venv
```

Activate using `.\.venv\Scripts\Activate.ps1` on Windows PowerShell or `source .venv/bin/activate` on Linux. Alternatively, call the virtual environment's Python directly.

```text
python -m pip install -r requirements-ui.txt
python install.py --dry-run
python install.py
```

Open **Agent History** from the Start / application menu. The first-use wizard selects existing Codex / Claude homes or custom directories. No personal aliases such as `codex1` are required. Before resuming, install and authenticate your chosen Agent CLI, then check its program, working directory and command preview in the right column. The interface currently uses Chinese labels; this file is an English entry point, not an English UI.

To start a new conversation, click **新增对话**, select a builtin command or custom launch profile and a working directory, then click **打开终端并新建** after reviewing the command. No existing history is required. Profiles keep their directory policy and model settings; authentication happens in the terminal. Saved history from connected sources is collected after the command exits, or through Refresh History while it runs. Closing the viewer leaves the terminal running.

Basic installation registers only the viewer. MCP, hooks, background jobs and desktop shortcuts are optional. Installation defaults to `~/.local/share/agent-history`; `--prefix`, `--python` and `--shared-root` override locations. Launchers refer to the source directory, so keep it after installation. The ZIP is source, not a standalone executable. For direct source execution, `python history_ui.py` reads source configuration from the checkout's parent by default; use `AGENT_HISTORY_SHARED_ROOT` to override. See the [first-use guide](docs/first-use.md) (Chinese).

## Features and limits

- Search, project filtering, sortable columns, Markdown and highlighted code, message navigation and collapsible / scrolling tool details.
- Logical conversation names and configurable launch profiles, programs, argument arrays, favorites and default launch entries.
- Native Codex resume across homes, text-based Claude-to-Codex conversion, temporary cross-Agent handoffs with durable return versions.
- Recoverable deletion with active-session / writer-lock protection; optional stdlib MCP history readers.

Locally verified on Windows / Python 3.12 and Ubuntu 20.04 / WSL2 / WSLg / Python 3.12.2 / xterm, using Codex CLI 0.159.2. [GitHub-hosted Windows / Ubuntu 22.04 CI](https://github.com/Re-Lyz/agent-history/actions/runs/38031469719) also passed regressions, release packaging, temporary-user installation and all four zero-model-request native Codex checks. Actual Claude CLI interactive resume, other Linux desktop terminals, native Wayland and real systemd scheduling remain unverified. macOS is not supported yet. Temporary user environments were exercised; this is not evidence of fresh OS installation testing.

Native formats may change when Codex updates: run the isolated checks before upgrading production usage. Cross-Agent conversion transfers text and tool summaries, not images, attachments, hidden reasoning or execution state; it does not replay tools. External files still need accessible paths, and encrypted records may not work across providers. Tool full text is available on demand; full-text indexing retains tool summaries only. [Release notes and verification](docs/beta-release.md) document the boundaries.

## Data, updates and uninstall

Source configuration defaults to the installation's `config/`; private runtime data lives under `~/.agent-history/`, including the index, settings, job reports, trash, revisions, backups and logs. Agent homes stay in place. `AGENT_HISTORY_SHARED_ROOT` overrides source configuration; `AGENT_HISTORY_HOME` overrides runtime data. Never commit these private directories or upload full transcripts / authentication files.

After updating source, reopen the UI and restart long-running MCP processes. Reinstall if paths changed. To uninstall, first preview, then execute:

```text
python install.py --prefix INSTALL_DIRECTORY --uninstall --dry-run
python install.py --prefix INSTALL_DIRECTORY --uninstall
```

Uninstall removes unchanged registrations owned by the installer and preserves histories, configuration, backups and user edits. [Contributing](CONTRIBUTING.md), [test instructions](tests/README.md), [security reporting](SECURITY.md) and [changelog](CHANGELOG.md) provide development and feedback guidance.

## License

[MIT](LICENSE) permits use, modification, distribution, sublicensing and commercial use. Keep the copyright and permission notice in copies or substantial portions of the software. Third-party components retain their own licenses; see [NOTICE](NOTICE.md).
