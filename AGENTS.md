# AGENTS.md

Notes for AI coding agents, and for people, working on this repository. [README.md](README.md) says what the project is for users. [docs/dev.md](docs/dev.md) has the full development guide.

## What this is

`orange3-cloud` is an add-on for [Orange Data Mining](https://orangedatamining.com). Users build workflows in Orange on their own computer as usual, and the heavy computation runs on a machine in their own cloud account: a Google Cloud project, an AWS account or an Azure subscription. The same machine can also run locally in Docker ("local mode"), which is how most development and tests work.

The code has two sides in one Python package:

- **The add-on** runs inside Orange on the user's computer: Cloud widgets, the Cloud settings dialog, and a client that starts machines and talks to them.
- **The machine** (`orangecontrib/cloud/server/`) is an HTTP API server that runs headless on the cloud machine. It keeps tables in memory and runs jobs on a local Dask cluster.

How they fit together:

- They talk over a versioned HTTP API (v1), defined in `protocol/` and documented in [docs/api-v1.md](docs/api-v1.md). Requests carry a bearer token, over TLS with a self-signed certificate that the add-on pins.
- At boot, the machine installs the user's exact Python and package versions (`docker/bootstrap.py`). So Orange objects such as learners, models and domains can travel between the two sides as pickles.
- Data stays on the machine. The laptop holds handles to it, such as `RemoteTable` and `CloudModel`, plus small samples.

## Layout

```
orangecontrib/cloud/
    protocol/       API v1, shared by both sides: wire.py (version, headers), errors.py, machine.py, tables.py, jobs.py,
                    memory.py (estimates), endpoints.py (ENDPOINTS), the pickler (pickles.py: dumps/loads, dump/load)
                    and the add-on's own recipes (recipes.py)
    env.py          the user's Python and package versions (EnvironmentSpec)
    settings.py     the add-on's settings, in Orange's settings store, with each provider's own (ProviderSettings)
    client/         HTTP client, CloudSession, machine providers (providers/: the interface in base.py, registry.py,
                    sizes.py, prices.py (each cloud's <cloud>_prices.json), setup_checks.py (the setup checks' results),
                    cli_tools.py (the clouds' command-line tools), cloud_vm.py (this computer's address, boot progress),
                    cloud_init.py and machine_images.py (machines that get their settings as files), uploads.py
                    (what the clouds' storage of uploaded files shares), local Docker, Google Cloud (gcp*.py),
                    AWS (aws*.py), Azure (azure*.py))
    data/           RemoteTable, CloudModel, CloudDistances, memory estimates
    gui/            Cloud settings dialog, each provider's page (provider_page.py; account_page.py, the base of the
                    clouds' pages: gcp_page.py, aws_page.py, azure_page.py), size picker, relay.py (results from
                    background threads), stop_machine.py (Stop: when it can, its question, the machine's cost)
                    and quit_question.py (the question when Orange quits with a cloud machine running)
    widgets/        Cloud widgets; base.py is their base class, viewer.py the viewers', help/ their help pages,
                    dev/ holds test widgets
    server/         the API server on the machine: app, jobs (worker.py: the operations' side), table store, file loader (loader.py, with csv_file.py and parquet_file.py) and sources, object downloads, ops/ (a module per job kind, listed in registration.py)
docker/             the machine image (Dockerfile), bootstrap.py and boot_platforms.py (a small Platform class per
                    place it runs)
scripts/            dev setup, local machine, demo data, benchmark, cloud test accounts, dev registry setup, the
                    clouds' bundled prices (update-<cloud>-prices.py) and the release's checks (release.py)
examples/           demo workflows (.ows)
tests/              smoke tests
docs/               dev.md (setup and how things work), api-v1.md, setup-gcp.md, setup-aws.md and
                    setup-azure.md (users' Google Cloud, AWS and Azure setup), gcp-checklist.md,
                    aws-checklist.md and azure-checklist.md (manual tests on each cloud), supported-widgets.md
                    (Orange's widgets on cloud data), images/ (the docs' screenshots), demo-taxi.md (a year
                    of NYC taxi trips) and demo-higgs.md (the 11M-row demo)
.github/            workflows/ (CI, image, weekly check, release), ISSUE_TEMPLATE/ (bug report, feature request),
                    dependabot.yml
CHANGELOG.md        a section per release (## X.Y.Z), which the release workflow requires and uses as the notes
CONTRIBUTING.md, SECURITY.md   how to contribute, and how to report a vulnerability privately
```

## Commands

Run these from the repository root. [docs/dev.md](docs/dev.md) explains each one.

```sh
scripts/dev-setup.sh                           # create .venv with Orange and the add-on (editable)
scripts/run-orange.sh                          # start Orange from .venv
.venv/bin/ruff check .                         # style check, function size, swallowed errors
.venv/bin/ruff format --check .                # formatting (ruff format . fixes it)
.venv/bin/lint-imports                         # import rules (architecture rules 1-5)
QT_QPA_PLATFORM=offscreen .venv/bin/pytest     # tests (add -m slow for the slow ones)
scripts/local-machine.sh up|status|logs|down   # the machine in Docker, at https://localhost:8443
```

CI runs the style check, the formatting check, the import rules and the tests on Ubuntu with Python 3.10, the oldest version the add-on supports. Code in `orangecontrib/` must run on Python 3.10. `docker/bootstrap.py` and `docker/boot_platforms.py` run on the image's own Python 3.13. A weekly check runs the tests with the newest Orange on the newest Python it supports, the image workflow scans the image with Trivy, and Dependabot proposes updates. The release workflow (`release.yml`) runs when a version tag `vX.Y.Z` is pushed: the same checks, then, after the maintainer approves the `release` environment's deployment, it publishes the version to the public image, PyPI and GitHub releases. Started by hand, it is a trial that only checks and builds.

## Architecture rules

These keep changes in one place: a new API version, a second cloud provider or another widget should touch one module, not many.

1. `protocol/` (the API models, the pickler and the recipes) imports nothing from the rest of the add-on, and no Qt. It is the contract both sides share.
2. `server/` runs headless. It imports only `server/`, `protocol/` and its own libraries. It never imports Qt, Orange's widget code (`Orange.widgets`, `orangewidget`), or the add-on's `client/`, `data/`, `gui/`, `widgets/` or `settings.py`.
3. The add-on's layers, top to bottom: `widgets/` → `gui/` and `data/` (independent of each other) → `client/` → `protocol/`, `env.py` and `settings.py`. A module imports only from its own layer or lower ones.
4. Network code lives only in `client/`. The clouds' code (their libraries `google`, `boto3`, `botocore` and `azure`, and their command-line tools such as `gcloud`) stays in `client/providers/`, behind the `ClusterProvider` interface: another cloud is new modules there and its page in `gui/`. The one exception is the server's storage access through `fsspec`/`gcsfs`.
5. Qt appears only in `widgets/` and `gui/`, plus `client/session.py`, the bridge that turns the client's work into Qt signals, and `settings.py`, which reads Orange's settings store (`QSettings`, Qt's core, no GUI) and imports Qt only when it opens it.
6. Functions stay small: complexity at most 12 and at most 6 arguments. Modules stay under about 800 lines; split a long module before adding much to it.
7. Never swallow errors. Catch broad exceptions only at boundaries (the job runner, API handlers, widget callbacks), report them there as a protocol error code or a widget error message, and give the reason in a `# noqa: BLE001` comment. No bare `except`, and no `except …: pass`.
8. Public functions and classes have type hints and a one-line docstring.

Ruff checks rules 6 and 7, and `lint-imports` checks rules 1 to 5 with the contracts in `pyproject.toml`.

## Conventions

- **Don't change Orange.** Never edit Orange's files or monkey-patch its classes at runtime, and don't rely on unreleased Orange changes. Everything lives in this add-on.
- **API changes:** `protocol/` and [docs/api-v1.md](docs/api-v1.md) change together. An incompatible change needs a new API version. The add-on refuses to work with a machine whose API or `orange3-cloud` version differs from its own.
- **Cloud widgets** subclass `OWCloudBaseWidget` in `widgets/base.py`, and each has a help page in `widgets/help/`. See "Writing a Cloud widget" in [docs/dev.md](docs/dev.md).
- **Modules, not `__init__.py`:** a package's `__init__.py` holds only its docstring, and code is imported from the module that defines it (no re-exports). The one exception is `widgets/__init__.py`, the category metadata that Orange reads. A test checks it.
- **Pickles** between the add-on and the machine go through `dumps` and `loads` in `protocol/pickles.py` (`dump` and `load` for files), never plain `pickle`: they handle Orange objects that plain `pickle` can't, and the classes of Orange's widget modules that recipes and steps use (Feature Constructor's expressions, Create Class's rules, Preprocess's "Remove numeric features"), whose modules need Qt (the machine has none; `server/expressions.py` and `server/preprocessors.py` have stand-ins). Importing `protocol/` imports no Qt: the pickler imports a widget module only to unpickle one of its classes, and `class_by_name` only the modules of `BY_NAME_CLASSES`.
- **Code that follows Orange's** (because Orange's widgets can't be subclassed or need Qt) names the Orange functions and classes it follows in `tests/test_orange_copies.py`, with a fingerprint of each; the weekly check against the newest Orange fails when one changes.
- **No credentials or connection settings in `.ows` workflow files.**
- **No private details in git:** Google Cloud project IDs, AWS account IDs, Azure subscription IDs, bucket and storage account names, IP addresses, account details or tokens. Local values go in the git-ignored `.orange3-cloud-dev.env`, and CI reads them from GitHub Actions secrets (masked in the logs, which are public).
- **Tests stay small:** smoke tests that prove what changed, and no test matrices.
- **Keep the docs current:** update [docs/dev.md](docs/dev.md) when setup, commands or behaviour change, the README, [docs/setup-gcp.md](docs/setup-gcp.md), [docs/setup-aws.md](docs/setup-aws.md), [docs/setup-azure.md](docs/setup-azure.md) and the widgets' help pages when what users see changes, and this file when the layout, commands or rules change.
- **License:** GPL-3.0-or-later, the same as Orange. New dependencies must be compatible with it.
