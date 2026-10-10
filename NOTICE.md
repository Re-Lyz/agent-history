# License and third-party notices

Copyright (c) 2026 Re-Lyz and Agent History contributors

Agent History is distributed under the [MIT License](LICENSE), SPDX identifier `MIT`. Use, modification, distribution, sublicensing and commercial use are permitted. Keep the copyright and permission notice in copies or substantial portions of the software. The English LICENSE is authoritative.

The standard license text is documented by the [Open Source Initiative](https://opensource.org/license/mit). Third-party components retain their own licenses; this project license does not relicense them.

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
