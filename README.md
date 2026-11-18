# OpenAgent Bridge

> An orchestration bridge that coordinates AI coding agents with structured
> task management, verification, and human-in-the-loop approval workflows.

## Status

| Phase | Description | Status |
|-------|-------------|--------|
| **0** | Specification & architecture | ✅ Complete |
| **1** | Core models, config, CLI scaffold | 🚧 In progress |
| **2** | Task store, runner, verification | ⬜ Planned |
| **3** | Autonomy loop & watcher | ⬜ Planned |
| **4** | Dashboard & integrations | ⬜ Planned |

## Features (Phase 1)

- **Data models** — full task lifecycle with 14-state machine, run records,
  verification results, evaluation reports, structured events, and approval
  gates.
- **Configuration** — YAML-based config with sensible defaults, environment
  variable override (`OPENAGENT_CONFIG`), and `~` expansion.
- **CLI scaffold** — `openagent` entry point (commands coming in Phase 2).

## Requirements

- Python ≥ 3.11
- [psutil](https://pypi.org/project/psutil/) (runtime)
- [PyYAML](https://pypi.org/project/PyYAML/) (for config loading)

## Installation

```bash
# Clone the repo
git clone <repo-url> && cd OpenAgentBridge

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

## Configuration

```bash
# Copy the example config and edit it
cp config.example.yaml ~/.openagent/config.yaml

# Or point to a custom location
export OPENAGENT_CONFIG=/path/to/my/config.yaml
```

See [`config.example.yaml`](config.example.yaml) for all available options with
inline documentation.

## Environment Variables

```bash
cp .env.example .env
# Then fill in your API keys
```

## CLI Usage

```bash
# Show version
openagent --version

# Show help
openagent --help

# (Phase 2) Submit a task
# openagent submit --goal "Add unit tests for parser module"

# (Phase 2) List tasks
# openagent list

# (Phase 2) Show task details
# openagent show <task-id>

# (Phase 2) Approve a pending action
# openagent approve <approval-id>
```

## Project Structure

```
OpenAgentBridge/
├── pyproject.toml                  # Build & project metadata
├── config.example.yaml             # Annotated example config
├── .env.example                    # API key placeholders
├── README.md
├── src/
│   └── openagent_bridge/
│       ├── __init__.py             # Package init, __version__
│       ├── models.py               # Dataclasses & enums
│       ├── config.py               # YAML config loader
│       └── cli.py                  # CLI entry point (Phase 2)
└── tests/
    └── ...
```

## License

MIT
