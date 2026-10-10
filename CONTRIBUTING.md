# Contributing

Please read [LICENSE](LICENSE) before contributing. Contributions are offered under the project's MIT license; retain third-party notices. Discuss a different licensing requirement before submitting code.

Use Python 3.11+ with Tk and SQLite FTS5/trigram. Clone into any directory, optionally create a virtual environment, and install `requirements-ui.txt`. From the repository root:

```text
python -m unittest discover -s tests -t . -v
python tests/release_audit.py --output dist/content-audit.json
```

On headless Linux, use `xvfb-run -a python tests/linux_check.py` for desktop tests. See [test instructions](tests/README.md) for optional, synthetic, zero-model-request native Codex checks. Use temporary Agent homes and generated histories; never test destructive changes on personal data. Regenerate the documented screenshot with `tests/make_demo_screenshot.py` only when its real UI changes.

Keep runtime files outside the checkout. Check new files against the `.gitignore` allowlist. Run checks appropriate to your changes; explain behavior, validation and limitations in a pull request. For bugs, include OS, Python and CLI versions, minimal reproduction and redacted error details. Do not upload authentication files, full transcripts, history databases or private paths. Security reports follow [SECURITY.md](SECURITY.md).
