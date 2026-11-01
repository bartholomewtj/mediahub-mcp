# Contributing

## Run the tests

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio pyyaml     # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest -q                                              # Windows: .venv\Scripts\python
```

Tests that need Docker, PowerShell or bash skip when the tool is missing. CI runs the suite on Linux and
Windows and checks the compose files.

## Rules

- Name no tracker or indexer anywhere (code, docs, examples, tests). Users add their own in Prowlarr.
- Never print, log or return a secret. Setup and scripts report key names and lengths, never values.
- The safety model and the destructive caps (bounded mode, daily limits, confirmation code, 7-day undo,
  `127.0.0.1` binding) do not change without discussion in an issue first.
- Text written by strangers (release names, error text) stays fenced; it never goes into an alert or a tool
  parameter.
- Keep bash blocks in the playbooks quote-safe (no single quotes, so they survive a `bash -lc '...'`
  wrapper), and give every step both a bash and a PowerShell version.
- Add or update tests with the change.

## How changes land

This repo is published from the maintainer's working copy at each release. Pull requests are reviewed
here; accepted changes are applied upstream with your authorship (Co-authored-by) and arrive in the next
release, then the pull request is closed with a link.
