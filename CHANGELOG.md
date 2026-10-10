# Changelog

## 0.1.0-beta.1 — 2026-10-10

First public beta candidate. Publication and GitHub-hosted CI are pending.

- Local Codex / Claude history indexing, full-text search and optional MCP readers.
- Desktop viewer with Markdown, code highlighting, search navigation, collapsible tool details, independently scrolling columns and sortable history.
- Logical conversation titles, source selection, configurable launch profiles and launch diagnostics.
- Native Codex resume across registered homes; Claude-to-Codex text import and temporary cross-Agent handoffs with durable return versions.
- Recoverable deletion with active-session protection, revision snapshots and maintenance tools.
- Python-based Windows / Linux installation, optional integrations, ownership-aware uninstall and checksummed source ZIP.
- PolyForm Noncommercial license, public documentation, synthetic screenshot, content auditing and isolated first-use verification.

Requires Python 3.11+, Tk and SQLite FTS5 with the trigram tokenizer. Desktop dependencies are installed separately. Windows and Ubuntu 20.04 / WSLg / xterm were tested locally with Codex CLI 0.159.2; actual Claude CLI interactive resume, other Linux desktop terminals, native Wayland and real systemd scheduling are not yet verified. See [beta release notes](docs/beta-release.md).
