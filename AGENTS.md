# Maintenance

- Use GPT / Codex for implementation and review while the owner's Claude override remains in effect. Do not switch accounts by copying login files.
- Keep this a small Python standard-library integration. Preserve the separation between source code, generated machine configuration, and running services.
- Installation generates files only. Updating code preserves instance configuration and connection credentials. Do not activate services or edit the user's Paseo/SSH configuration as a side effect of tests.
- App-server stdout is protocol traffic: diagnostics belong on stderr. Preserve unrelated frames, input order, child exit status, and signal forwarding.
- Keep image conversion scoped to the documented app-server messages. Changes to other CLI paths need their own explicit behavior and evidence.
- Never commit installation directories, tokens, account logins, real screenshots, conversation logs, or machine-specific configuration.
- Use real temporary files and subprocesses for regression tests. Fix test fixtures rather than adding production branches solely for tests.
- Run relevant tests with `python3 -m unittest discover -s tests -v`; actual SSH, service, and model checks are separate from this offline suite.
- Update README and setup instructions when generated files, configuration formats, or migration steps change. Distinguish automated checks from real environment verification.
