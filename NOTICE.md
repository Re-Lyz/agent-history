# License and third-party notices

Required Notice: Copyright 2026 Re-Lyz and Agent History contributors

Agent History is distributed under the unmodified [PolyForm Noncommercial License 1.0.0](LICENSE), SPDX identifier `PolyForm-Noncommercial-1.0.0`. Noncommercial use, modification and distribution are permitted under that license; keep LICENSE and this required notice with copies. Commercial use requires a separate permission from the rights holders. This is source-available software with a noncommercial restriction, not OSI-approved open source. The English LICENSE is authoritative.

The license was obtained from the [official plain-text source](https://polyformproject.org/licenses/noncommercial/1.0.0.txt). Third-party components retain their own licenses; this project license does not relicense them.

| Component | Use | License | Distributed in source ZIP? |
|---|---|---|---|
| Python / standard library | Runtime, SQLite interface, Tkinter | Python Software Foundation and bundled component licenses | No; installed separately |
| Tcl/Tk | Native desktop widgets | Tcl/Tk license | No; supplied by Python/system |
| SQLite | History database / FTS5 | Public domain | No; supplied by Python/system |
| markdown-it-py (including upstream markdown-it notices) | Markdown parser | MIT | No; installed by pip |
| mdurl | Parser dependency | MIT | No; installed by pip |
| Pygments | Syntax highlighting | BSD-2-Clause | No; installed by pip |
| Codex CLI / Claude Code CLI | User-selected external Agent | Respective vendor terms | No; user installs and authenticates separately |

The release ZIP contains project source, documentation, templates and synthetic tests only. The screenshot is captured from this project's real UI using generated history; its generation script additionally requires Pillow, which is a development-only dependency, not part of the product or release. No fonts, interpreter, CLI executables or dependency wheels are bundled. A future binary distribution must include notices for every component it actually bundles. See [UI dependencies](docs/ui-dependencies.md) for versions tested locally.
