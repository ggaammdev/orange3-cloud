# Contributing

Thank you for helping with orange3-cloud. Report bugs and ask for features in the [issues](https://github.com/ggaammdev/orange3-cloud/issues); their forms ask for what we need. Report security problems privately, as [SECURITY.md](SECURITY.md) says, not in an issue.

## Guides

- [AGENTS.md](AGENTS.md): what the project is, its layout, the commands, the architecture rules and the conventions, for people and for coding agents.
- [docs/dev.md](docs/dev.md): the development setup, local mode, the tests, the cloud test accounts and how everything works.

## Set up

You need [uv](https://docs.astral.sh/uv/) and git, and Docker Desktop or OrbStack for local mode. From the repository root:

```sh
scripts/dev-setup.sh    # .venv with Orange and the add-on (editable)
scripts/run-orange.sh   # Orange from .venv
```

Most work needs no cloud account: the machine runs in Docker on your computer (`scripts/local-machine.sh up`), and the tests run it in-process.

## Before you open a pull request

Run the checks that CI runs:

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/lint-imports
QT_QPA_PLATFORM=offscreen .venv/bin/pytest
```

- **Keep pull requests small and focused:** one fix or feature each, with a description of what changes for users.
- **Keep tests small:** a few smoke tests that prove what changed, no test matrices.
- **Update the docs** when what users see changes: the README, the setup guides in `docs/` and the widgets' help pages; and `docs/dev.md` and `AGENTS.md` when setup, commands, layout or rules change.
- **Follow the architecture rules** in AGENTS.md. `lint-imports` and `ruff` check most of them.
- **Don't change Orange:** everything lives in this add-on.
- **Nothing private in git:** no project, account or subscription IDs, bucket names, IP addresses or tokens, in code, tests, docs, workflows or screenshots.

## License

orange3-cloud is licensed under GPL-3.0-or-later, the same as Orange. By contributing, you agree that your contributions are licensed under the same license. New dependencies must be compatible with it.
