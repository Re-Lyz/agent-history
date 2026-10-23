# Changelog

## Unreleased

- Page collected history in SQLite, read preview tails on demand across bounded native forks, and seek directly to full tool records. Avoid duplicate project-list scans, narrow exact physical-version search with FTS, cancel obsolete reads, display cached rows during startup, and cache completed launch reports with guarded state updates. See [performance measurements](docs/performance.md).

- Refresh only related native paths after single-current-conversation exit; avoid whole-directory walks for exact-path indexing and skip fork scans when no files need retirement. Publish completion after indexing succeeds, retaining a recoverable reservation on failure.

- Reuse connection resolution within each bulk sync and group listing; batch physical-version membership lookups and resolve each listed identity once, refreshing configuration on the next operation.

- Avoid repeated whole-library scans after targeted sync and completed runner reports; briefly retry transient native writer locks after confirmed successful exit, preserving protection for interrupted runs and content conflicts.

- Split the main viewer into focused controllers and group history storage, native adapters, and launch lifecycle modules; preserve legacy import identity, installed script paths, UTF-8 CLI errors, and runner exit codes.

- Unified read-only diagnostics, redacted export, immutable app-configuration backups, verified restore and reviewed installation update / uninstall actions.
- Native-session-aware launch status, cancellation of unclaimed idle launches, and explicit sync of observed sessions switched away from a still-running Codex terminal. Late runners and old cleanup cannot replace a newer reservation.
- Extract installation, terminal adapters, history queries and reader widgets into history_app while preserving existing root imports and command entry points.

- Single-current-conversation handoffs: reserve one managed writer, promote the used native target without a return copy, and retire covered old transcripts / resume entries through recoverable cleanup journals. Existing jobs retain their legacy policy.
- Explicit conversation consolidation with database backups, full branch text, native resume verification and reversible folding of included versions.
- Lazy tool-detail widgets, incremental rendering for all conversations, reuse of unchanged previews and coalesced launch-plan refreshes.
- Selectable, readonly right-column text with keyboard / context-menu copying; remove dark tool-toggle backgrounds and borders.
- New conversation dialog with selectable builtin commands / custom profiles, working directories, command preview, cancellation and retry.
- New-session checks allow uninitialized Codex homes; independent terminal reports collect saved history after exit.

## 0.1.0-beta.1 — 2026-10-10

First public beta, under the MIT license. Windows / Ubuntu GitHub-hosted regression, packaging, isolated installation and native Codex checks passed. Release: `v0.1.0-beta.1`.

- Local Codex / Claude history indexing, full-text search and optional MCP readers.
- Desktop viewer with Markdown, code highlighting, search navigation, collapsible tool details, independently scrolling columns and sortable history.
- Logical conversation titles, source selection, configurable launch profiles and launch diagnostics.
- Native Codex resume across registered homes; Claude-to-Codex text import and temporary cross-Agent handoffs with durable return versions.
- Recoverable deletion with active-session protection, revision snapshots and maintenance tools.
- Python-based Windows / Linux installation, optional integrations, ownership-aware uninstall and checksummed source ZIP.
- MIT license, public documentation, synthetic screenshot, content auditing and isolated first-use verification.

Requires Python 3.11+, Tk and SQLite FTS5 with the trigram tokenizer. Desktop dependencies are installed separately. Windows and Ubuntu 20.04 / WSLg / xterm were tested locally with Codex CLI 0.159.2; actual Claude CLI interactive resume, other Linux desktop terminals, native Wayland and real systemd scheduling are not yet verified. See [beta release notes](docs/beta-release.md).
