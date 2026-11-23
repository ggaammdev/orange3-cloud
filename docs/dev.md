# Development setup

How to set up a development environment for orange3-cloud and run Orange from it.

Supported development platform: macOS on Apple Silicon. Linux and Windows should work, but they aren't tested.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) 0.12 or newer. It also installs the Python version the project uses, so you don't need a system Python.
- git.
- Docker Desktop or OrbStack, for local mode (see "Local machine" below).
- For work on a cloud: its command-line tool (`gcloud`, the AWS CLI 2.32 or newer, or the Azure CLI) and a test account (see "Cloud test accounts" below).

## Set up the environment

From the repository root:

```sh
scripts/dev-setup.sh
```

This creates `.venv` in the repository root with a uv-managed Python. It installs Orange, the Qt binding that Orange's README recommends (PyQt6 with PyQt6-WebEngine), and this add-on in editable mode with its `server` and `dev` extras, the same as:

```sh
uv pip install --python .venv/bin/python -e ".[dev,server]"
```

The add-on adds a "Cloud" section to Orange's toolbox. You can safely run it again: it keeps an existing `.venv` with the right Python version and only installs what is missing.

Environment overrides:

| Variable | Default | Meaning |
|---|---|---|
| `PYTHON_VERSION` | `3.12` | Python version for `.venv`. If it changes, `.venv` is recreated. |
| `ORANGE_VERSION` | `3.41.0` | `orange3` version to install. |

Python 3.12 is the version Orange's own install instructions use. `orange3` 3.41.0 requires Python 3.11 or newer and also publishes wheels for 3.13 and 3.14.

## Run Orange

```sh
scripts/run-orange.sh
```

This starts the Orange canvas (`python -m Orange.canvas`) from `.venv`. Any extra arguments go to Orange. For example, this skips the splash screen and welcome dialog and logs more:

```sh
scripts/run-orange.sh --no-splash --no-welcome -l 2
```

## Installed versions

These versions were installed on 2026-11-19 on macOS 26.6 (Apple Silicon):

| Package | Version |
|---|---|
| Python | 3.12.13 (uv-managed) |
| orange3 | 3.41.0 |
| orange-canvas-core | 0.2.10 |
| orange-widget-base | 4.29.0 |
| PyQt6 | 6.12.1 (Qt 6.12.0) |
| PyQt6-WebEngine | 6.140.1 |
| AnyQt | 0.2.2 |
| numpy | 2.5.5 |
| scipy | 1.18.1 |
| scikit-learn | 1.9.1 |
| pandas | 3.1.1 |

`scripts/dev-setup.sh` prints the main versions when it finishes. To list everything installed, run `uv pip list --python .venv/bin/python`.

## Local machine

The cloud machine runs from one generic image (`docker/Dockerfile`). In local mode the same image runs in Docker on your computer, at `https://localhost:8443`:

```sh
scripts/local-machine.sh up       # build the image, start the machine, wait until it answers
scripts/local-machine.sh status   # is it running, and with which versions
scripts/local-machine.sh logs -f  # follow its output
scripts/local-machine.sh down     # stop it (keeps the download cache)
scripts/local-machine.sh clean    # stop it and delete the download cache
```

`up` builds the image for your computer's architecture, then writes a new random token, a self-signed certificate for `localhost` (valid for 30 days) and the EnvironmentSpec of `.venv` into `~/.orange3-cloud/local-machine/` (`ORANGE3_CLOUD_LOCAL_DIR` changes the folder). It starts the machine with `docker/docker-compose.yml`, waits until `/v1/info` answers, and prints the main versions and the paths of the token and the certificate (the token itself isn't printed). To call the API by hand:

```sh
D=~/.orange3-cloud/local-machine
curl --cacert $D/cert.pem -H "Authorization: Bearer $(cat $D/token)" https://localhost:8443/v1/info
```

When the container starts, `docker/bootstrap.py` turns the generic image into a copy of your environment:

1. It installs the exact Python version of the EnvironmentSpec with `uv python install`, and creates a fresh virtual environment.
2. It installs the **required** packages at your exact versions. If that fails, the machine stops with an error.
3. It installs `orange3-cloud[server]` at your version, from the first of: the repository mounted at `/src` (local mode), a wheel bundled in a development image if its version matches, or the package index. In local mode the machine therefore always runs the code in your working copy; restart it after changing the server code.
4. It installs the **best-effort** packages, and lists the ones that fail in `/v1/info` under `install_failures`.
5. It starts the API server with HTTPS on port 8443.

uv's download cache, the Python installs and the virtual environment are kept in the Docker volume `orange3-cloud-cache`, so only the first start downloads everything. On an Apple Silicon Mac the first start takes a few minutes, mainly to download the packages and to compile the few that have no wheel for Linux on arm64 (the image has a C/C++ compiler for that); later starts take under a minute.

The bootstrap reads its settings from environment variables in local mode, from the instance metadata on Google Cloud, and from files that cloud-init wrote on AWS and Azure: the token, the TLS certificate and key, the EnvironmentSpec, the idle timeout and the maximum lifetime. The docstring of `docker/bootstrap.py` lists the names. Each place the machine runs is a small `Platform` class in `docker/boot_platforms.py`, next to it (`Environment`, `GoogleCloud`, `AmazonEC2`, `AzureVM`): where the settings come from (`read`), how to tell it runs there (`detect`, for `ORANGE3_CLOUD_CONFIG=auto`), and how the machine stops at the end (`power_off`). Another cloud adds one to `PLATFORMS`. `ORANGE3_CLOUD_IDLE_TIMEOUT` and `ORANGE3_CLOUD_MAX_LIFETIME` (seconds) are passed through by `local-machine.sh`.

The machine stops by itself after the idle timeout (default 30 minutes without requests or jobs; `GET /v1/info` doesn't count) and at the end of the maximum lifetime (default 12 hours); `0` turns a limit off. Jobs keep it running only while Orange is in touch: after an idle timeout without any request, `GET /v1/info` included, it cancels its queued and running jobs (see "When Orange closes or crashes"), and the idle shutdown follows an idle timeout later. Locally the container then exits (compose doesn't restart it); on a cloud the VM powers off (Google Cloud, AWS) or deallocates itself (Azure), so it stops costing compute. The container's exit code says why: `0` after `POST /v1/shutdown`, `3` after the idle timeout, `4` after the maximum lifetime, `143` after `docker stop`. To try it:

```sh
ORANGE3_CLOUD_IDLE_TIMEOUT=20 scripts/local-machine.sh up
docker wait orange3-cloud-local-machine-1   # prints 3 about 20 s later
```

To build the image by hand, from the repository root (`--platform linux/amd64` builds the image the cloud machines use):

```sh
docker buildx build -f docker/Dockerfile -t orange3-cloud-machine:local --load .
```

The machine installs the Python version of your environment. When uv has no build of that exact version (for example 3.10.10 or 3.11.2), it installs the newest release of the same minor version instead; `GET /v1/info` reports both (`python_version_requested`, `python_version`), and `scripts/local-machine.sh up` and the Cloud settings dialog show the difference.

The build context is only `docker/bootstrap.py` and `docker/boot_platforms.py` (see `.dockerignore`). CI (`.github/workflows/image.yml`) builds the amd64 image on pull requests that change `docker/`, `orangecontrib/` or `pyproject.toml` and on every push to `main`, with the add-on wheel of that commit inside. It starts the image without a source folder, so the machine installs the bundled wheel, and checks the versions in `/v1/info` and the idle shutdown. It also scans the image with Trivy and fails on critical problems that have a fix available. The base image is pinned by digest, so Dependabot proposes each new Debian base.

## Development images

Cloud machines can't mount your working copy, so they run a **development image**: the machine image with the add-on wheel of one commit inside (`/opt/orange3-cloud/wheels`). The bootstrap installs that wheel when its version matches yours. To build one by hand:

```sh
uv build --wheel --out-dir /tmp/wheels
docker buildx build -f docker/Dockerfile --platform linux/amd64 \
    --build-context wheels=/tmp/wheels -t orange3-cloud-machine:dev --load .
```

On every push to `main`, CI publishes the tested image to a **private** Artifact Registry repository in the Google Cloud test project:

```
<region>-docker.pkg.dev/<project>/orange3-cloud/machine:dev          # the latest push to main
<region>-docker.pkg.dev/<project>/orange3-cloud/machine:dev-<sha>    # one commit (first 7 characters of its hash)
```

Development images contain unreleased code, so they never go to a public registry. Only amd64 is built, because the cloud machines are x86-64.

CI signs in to Google Cloud without a stored key (Workload Identity Federation): GitHub's OIDC token is accepted only for pushes to `main` of this repository, and lets the workflow act as a service account that can push to that one repository and do nothing else. `scripts/setup-dev-registry.sh` sets this up once in the test project and can safely be run again. It reads the project, region and `gcloud` configuration from `.orange3-cloud-dev.env`, creates the Artifact Registry repository `orange3-cloud`, the service account, the workload identity pool and provider, and stores these GitHub Actions secrets and variable, which the workflow reads (the repository itself contains none of these values). The project's ID and number are secrets, so that the workflow's logs, which anyone can read in a public repository, show them masked:

| Name | Kind | Value |
|---|---|---|
| `GCP_PROJECT` | secret | the test project's ID |
| `GCP_WIF_PROVIDER` | secret | the full name of the workload identity provider (it contains the project's number, which the workflow masks too) |
| `GCP_DEV_IMAGES_SA` | secret | the service account's email |
| `GCP_REGION` | variable | the registry's region |

To pull a development image yourself, sign in with your own Google account (an owner or Artifact Registry reader of the project). A short-lived token keeps your Docker configuration unchanged:

```sh
source .orange3-cloud-dev.env
REGISTRY=$ORANGE3_CLOUD_GCP_REGION-docker.pkg.dev
gcloud --configuration=$ORANGE3_CLOUD_GCLOUD_CONFIG auth print-access-token \
    | docker login -u oauth2accesstoken --password-stdin https://$REGISTRY
docker pull --platform linux/amd64 $REGISTRY/$ORANGE3_CLOUD_GCP_PROJECT/orange3-cloud/machine:dev
docker logout https://$REGISTRY
```

(`gcloud auth configure-docker $REGISTRY` instead sets up a permanent credential helper.) Without credentials the registry refuses the pull.

Machines on AWS and Azure can't use Google's service accounts. `scripts/setup-dev-registry.sh` also creates the service account `orange3-cloud-image-reader`, which may only read the repository, and lets you (the signed-in developer) make its short-lived tokens. Development machines on those clouds pull with such a token:

```sh
gcloud --configuration=$ORANGE3_CLOUD_GCLOUD_CONFIG auth print-access-token \
    --impersonate-service-account=orange3-cloud-image-reader@$ORANGE3_CLOUD_GCP_PROJECT.iam.gserviceaccount.com
```

## Releasing

The release workflow (`.github/workflows/release.yml`) publishes a version. Start it by hand, on `main`:

```sh
gh workflow run release.yml -f version=X.Y.Z -f publish=true
```

It runs the CI checks (it calls `ci.yml`), builds the sdist and wheel and checks them (`scripts/release.py check-dist`: one of each, of that version, with none of the files that git ignores as internal; and `twine check`), and builds the machine image for `linux/amd64` and `linux/arm64`. Then it publishes, in this order:

1. the image to `ghcr.io/ggaammdev/orange3-cloud-machine`, tagged `X.Y.Z` and `latest`. Release images have no wheel inside: the machine installs `orange3-cloud[server]==X.Y.Z` from PyPI at boot;
2. the sdist and wheel to PyPI, with Trusted Publishing: PyPI trusts this repository's `release.yml`, so no token is stored anywhere;
3. the tag `vX.Y.Z` and the GitHub release, with the version's section of `CHANGELOG.md`, the sdist and the wheel.

The image goes first because it can be pushed again, while a version on PyPI can never be replaced. If a publishing job fails, fix the cause and re-run the failed jobs.

With `publish=true`, the version must be `X.Y.Z` and the same as `pyproject.toml`'s (commit it first), `CHANGELOG.md` must have a section for it (`## X.Y.Z` or `## [X.Y.Z]`, optionally followed by a date), and the tag must not exist yet. After a release, set `main` to the next development version (such as `1.0.1.dev0`), so that checkouts use the development images again.

**Working on a release version.** While a checkout's version is a release (such as `1.0.0`, from the commit that sets it until the next development version), the add-on on a cloud runs the public image of that version: before the release it doesn't exist, and after it, it has the released code rather than your changes. Local mode from a source checkout isn't affected: it builds its image and mounts the checkout. On a cloud, point the add-on at a development image, which has the wheel of its commit inside, with `ORANGE3_CLOUD_GCP_IMAGE`, `ORANGE3_CLOUD_AWS_IMAGE` or `ORANGE3_CLOUD_AZURE_IMAGE`, for example the `:dev-<sha>` tag that CI pushed for the commit you work on (see "Development images"; on AWS and Azure the add-on makes the token to pull it as for a development version). An installed add-on in local mode uses `ORANGE3_CLOUD_LOCAL_IMAGE`. The tests that start real machines need the same variables.

PyPI shows the README without the repository's files, so the workflow makes the README's relative links and images absolute, pointing to the repository at the tag (`scripts/release.py prepare`).

A package's first push to ghcr.io makes it private, whatever the repository's visibility. After the first release, make it public on GitHub (the package's settings, "Danger Zone", "Change visibility"), or released add-ons can't pull it; the workflow warns while it is private.

**A trial** publishes nothing: leave `publish` off (the default). The workflow runs the checks, builds the sdist and wheel, keeps them as the run's artifact `dist`, and builds the image for both architectures without pushing it. Any PEP 440 version works, for example a release candidate, which the workflow writes into `pyproject.toml` for its build:

```sh
gh workflow run release.yml -f version=1.0.0rc1
gh run download <run-id> -n dist -D /tmp/rc1
```

An add-on whose version is a release runs the public image of that version, and for a release candidate such as `1.0.0rc1` that image doesn't exist (only `.dev` and local versions are development versions). To try a trial's wheel on a cloud, build a development image with it (see "Development images"), push it to the private registry with its own tag, for example `dev-1.0.0rc1`, and point the add-on at it with `ORANGE3_CLOUD_GCP_IMAGE`, `ORANGE3_CLOUD_AWS_IMAGE` or `ORANGE3_CLOUD_AZURE_IMAGE`.

## Cloud test accounts

Development and tests on the clouds use separate test accounts. Their private details (IDs, regions) go in `.orange3-cloud-dev.env`, which git ignores. Each setup script checks the current state first, changes only what is missing, and leaves the other scripts' lines in that file alone (`scripts/env-file.sh`), so you can safely run it again.

| Cloud | Script | What it sets up |
|---|---|---|
| Google Cloud | `scripts/setup-gcp-test-project.sh` | a test project with billing, a budget with alerts, the needed APIs, and the `gcloud` configuration `orange3-cloud` |
| AWS | `scripts/setup-aws-test-account.sh --email ADDRESS` | for the AWS CLI profile `orange3-cloud`: a monthly budget with email alerts, and a request for the EC2 vCPU quota (32 by default) |
| Azure | `scripts/setup-azure-test-subscription.sh --email ADDRESS` | for the Azure CLI's subscription: the resource providers, a monthly budget with email alerts, requests for the vCPU quotas of the Dsv6 and Esv6 families and the region (32 by default), and a check that the region offers the machine sizes |

Sign in to AWS first, as an IAM user with administrator rights rather than the account's root user. The AWS CLI's browser sign-in uses your console session:

```sh
aws login --profile orange3-cloud --region us-east-1
```

For Azure, `az login` signs in with your browser. New subscriptions start with no quota for older machine families (for example Dsv5), and Azure refuses to raise those at once, which is why the sizes are v6.

## Cloud session from Python

Orange's side uses the machine through `CloudSession` (`orangecontrib/cloud/client/session.py`), one shared object that holds the machine provider, the running machine and the API client. It also works from a plain Python shell:

```python
from orangecontrib.cloud.client.session import CloudSession

session = CloudSession.instance()  # default provider: local Docker
session.start_machine(wait=True)  # build the image, start, wait for /v1/info
print(session.info())  # GET /v1/info
session.client  # the CloudClient, for jobs and tables
session.stop_machine(wait=True)  # stop and remove the machine
```

Without `wait=True`, these calls run on a background thread and return a `concurrent.futures.Future`. The session reports through Qt signals, which arrive in the GUI thread: `statusChanged(state)` (`disconnected`, `starting`, `connecting`, `connected`, `reconnecting`, `stopping`, `error`), `messageChanged(text)`, `connected()`, `disconnected()`, `error(text)`, `infoChanged(info)` and `signInFinished(error)` (see "Google Cloud sign-in" and "AWS sign-in and setup checker"). While connected in Orange, a heartbeat reads `/v1/info` every 30 seconds, whether or not a dialog is open (a `QTimer` of the session, started when it connects). It notices when the machine has stopped (for example after its idle timeout), says why, deletes what is left of it, and reconnects after network failures. A Google Cloud VM's console, where the server writes why it stopped, is cleared when the VM powers off, so the session takes the reason from the last `/v1/info`: the idle timeout or the maximum lifetime was due by now, or, with jobs running, Orange hadn't been in touch for twice the idle timeout (after a long sleep, for example).

The local provider (`LocalDockerProvider`) runs the container `orange3-cloud-local` at `https://127.0.0.1:8443`. When the add-on runs from a source checkout, the provider builds `orange3-cloud-machine:local` from `docker/` before each start (cached, so it takes about a second) and mounts the checkout at `/src`. An installed add-on uses `ORANGE3_CLOUD_LOCAL_IMAGE` if set; otherwise a release uses its public image `ghcr.io/ggaammdev/orange3-cloud-machine:<version>` (pulled on the first start, "Downloading the machine image"), and a development version `orange3-cloud-machine:local`, which must exist. It uses the same cache volume as `scripts/local-machine.sh` and gets a new token and certificate on every start. Both publish port 8443, so only one of them can run at a time, and each says so when the other is running.

The handle of the running machine, including its token and certificate, is kept in `machine.json` in the add-on's private folder: `~/Library/Application Support/Orange/orange3-cloud/` on macOS, or `ORANGE3_CLOUD_STATE_DIR` if set. The folder has mode `0700` and the files `0600`. The local machine's key is kept in `local-machine/` in the same folder. None of this goes into workflows. After Orange restarts, `CloudSession.instance()` reconnects to that machine if it's still running (`session.reattach(wait=True)` does the same from a shell). A `machine.json` that can't be read doesn't stop the add-on: `HandleStore.load()` returns nothing and keeps the reason in `load_error`, and the Cloud settings dialog names the file and says that the machine it pointed to may still run (on Google Cloud it is then listed as another machine to delete). The next machine's record replaces the file.

The test that starts the real local machine is skipped unless you ask for it:

```sh
ORANGE3_CLOUD_DOCKER_TESTS=1 .venv/bin/pytest tests/test_session.py
```

### When Orange closes or crashes

- **Quitting Orange** (File > Quit, or closing its window) cancels the running jobs. The canvas closes the workflow's widgets, and each Cloud widget's `onDeleteWidget` waits for its thread, which cancels its job on the machine (`DELETE /v1/jobs/{id}`), before Orange exits. It waits at most `CANCEL_WAIT` (3 s, `widgets/base.py`), so a machine that can't be reached doesn't hold Orange up: the cancel is sent once with a short timeout (`CANCEL_TIMEOUT`), and once a task is cancelled its failed calls aren't repeated (`CloudClient.giving_up_when`), so its thread ends as soon as its call fails. `tests/test_widget_base.py` checks this through a proxy that drops everything, as a broken network does. Then `CloudSession.shutdown` (on `aboutToQuit`) stops the session's background work. The machine keeps running, is reattached when Orange opens again, and stops after its idle timeout. `tests/test_orange_quit.py` checks this with Orange's own main window in another process: after File > Quit, the job on the machine is `cancelled`.
- **If Orange crashes, is force-quit, loses the network or can't reach the machine as it quits,** nothing cancels the jobs. The machine keeps the time of the add-on's last request (`ServerState.last_contact`; any request counts, `GET /v1/info` too, and while Orange runs the heartbeat and the widgets' job polls come every 30 seconds or more often). When jobs are queued or running and nothing has come for the idle timeout, the server's watchdog cancels them (`cancel_unattended_jobs` in `server/app.py`), with the message "The machine cancelled the job: Orange hadn't been in touch for 30 minutes." in each job's error, and logs it. The jobs counted as activity until then, so the idle shutdown follows an idle timeout later, unless Orange gets in touch again. Without this, such a job would keep the machine running until its maximum lifetime. With the idle timeout off, jobs are never cancelled this way. The test above also kills Orange while its job runs and checks that the machine cancels the job.
- **A laptop that sleeps** with Orange open is silent too, which is why the limit is the idle timeout and not something shorter: a job started before a short sleep keeps running and is collected on waking. After a sleep longer than the idle timeout, the widget's next poll gets the job `cancelled`, and the widget shows the message with "Run it again." (`Error.job_cancelled`). After a sleep longer than twice the idle timeout the machine has stopped too, and the session says so ("The machine stopped: Orange hadn't been in touch for longer than its idle timeout.").

## Machine providers

Everything that differs between the places a machine runs is behind the `ClusterProvider` interface (`client/providers/base.py`), so another cloud is new modules in `client/providers/`, its page in the dialog and its docs, with no changes elsewhere:

- **Registry** (`client/providers/registry.py`): `registered_providers()` lists the built-in providers (`BUILTIN`: `local`, `gcp`, `aws`, `azure`) and any added with `register_provider(id, factory, label)`, in the dialog's order; `get_provider(id)` makes one (the id is stored in each machine's handle).
- **Sizes:** `machine_sizes` (from the least memory to the most) and `default_size`. Cloud File's suggestion, Cloud Distances' and Cloud MDS's memory checks and the size picker use the sizes of the machine's provider, or of the one chosen in the settings (`OWCloudBaseWidget.size_choices()`); the functions that pick one are in `client/providers/sizes.py`.
- **Prices:** `size_price(machine_type, region, disk_gb)` and `prices_date()`, from the provider's bundled `<id>_prices.json`, read by `prices.load_price_file(id)` (its date, currency, source and prices per region). `machine_price_per_hour(handle)` is the price of a running machine, recorded at start. For a machine that stopped by itself, `stopped_at(handle)` says when, from the cloud's records, and `stopped_price_per_hour(handle)` what it still costs until it is deleted (see "Machine sizes, prices and costs"); both default to `None` (unknown).
- **Storage:** `stores_uploads` and `stage_file` (local files go to the provider's storage), `storage_schemes` (e.g. `gs://`), `machine_location(location, handle)`, which turns a location in the provider's storage, or an uploaded file, into one the machine can read (Google: unchanged; a provider whose machine can't read its storage gives a short-lived signed `https://` link, made anew at each load), and `read_stored_head` for Cloud File's memory estimate. The machine itself reads the schemes in `protocol.tables.URI_SCHEMES`, and its error message names them.
- **Settings:** `CloudSettings.of(id)`, the provider's `ProviderSettings` (account, region, zone, machine size, disk). Settings saved before there were several providers (`gcp_project`, …) are read as Google Cloud's and replaced at the next save.
- **Page:** the dialog shows `PAGE` from `gui/<id>_page.py` (a `ProviderPage`, e.g. `gui/gcp_page.py`), or one given to `register_page`, else `SizesPage`, which has only the size picker. A page's `ready` tells whether Start may run (Google Cloud: the setup check passed). `finds_leftovers` makes the dialog look for the provider's machines that the session doesn't use.
- **Account pages:** the pages of clouds with an account to sign in to subclass `AccountPage` (`gui/account_page.py`): background calls on the page's own thread (their results come back through `gui/relay.py`, so a page deleted meanwhile is left alone), signing in through the session, a message line, and the setup check (a cloud's checker returns a list of `Check` from `client/providers/setup_checks.py`; `setup_key()` says what it depends on, `setup_job()` runs it). `client/providers/cli_tools.py` finds a cloud's command-line tool and runs its browser sign-in.
- **Imports:** a provider's modules import their cloud's libraries (`google`, `boto3`/`botocore`, `azure`) inside the functions that use them, not at the top. So Orange starts without them, and only the libraries of the cloud in use load, when its page is shown or its machine starts: Google's take about 2 s to import, Azure's 0.7 s and AWS's 0.2 s. `test_start_up_imports_no_cloud_library` in `tests/test_package.py` checks that the widgets, the settings and the registry's providers import none of them.

`tests/test_providers.py` registers a provider that exists only in the test, and checks that its sizes and prices appear in the dialog, that Cloud File suggests one of its sizes and loads from its storage through `machine_location`, and that the settings are kept per provider.

## Cloud settings dialog

The Cloud settings dialog (`orangecontrib/cloud/gui/settings_dialog.py`) is where users choose where the machine runs (one of the registered providers: Local (Docker), Google Cloud, AWS or Azure; see "Machine providers"), set the idle timeout and the maximum lifetime, start and stop the machine, and compare the versions on their computer with those on the machine. `show_settings_dialog(parent)` opens the one shared, non-modal dialog. Starting and stopping run on the session's background thread, so Orange keeps responding. The dialog's own background check for machines left behind, the pages' background calls and Cloud File's "Uploaded files…" dialog (`gui/uploaded_files.py`) hand their results back through `receiver()` in `gui/relay.py`: a long-lived object in the GUI thread calls the dialog or page, which the thread references only weakly, if it still exists. A dialog deleted while a cloud answers slowly is left alone (emitting a deleted dialog's signal from the thread crashed Python).

To try it without Orange's canvas, run the developer test widget (it isn't in the toolbox):

```sh
.venv/bin/python -m orangecontrib.cloud.widgets.dev.owcloudsettingstest
```

The choices are saved in Orange's settings file (`~/.config/biolab.si/Orange.ini`, group `orange3-cloud`, keys `provider`, `idle_timeout` and `max_lifetime` in seconds, and for each provider `providers/<id>/account`, `region`, `zone`, `machine_type` and `disk_gb`; for Google Cloud the account is the project), never in workflows; `CloudSession.start_machine()` uses them unless it gets other values. The file holds no secrets. `ORANGE3_CLOUD_SETTINGS_FILE` points to another `.ini` file instead; the tests use it, so they don't touch your settings.

The dialog's "Notify me about the hosted version" button opens the page in `HOSTED_INTEREST_URL` (`orangecontrib/cloud/settings.py`), or the URL in the key `hosted_interest_url` of the same group, which overrides it. Only `http`/`https` URLs count. With neither set (as now) the button is disabled and its tooltip says there is no sign-up page yet. The add-on sends no usage statistics.

### Google Cloud sign-in

The dialog's Google Cloud page (`gui/gcp_page.py`, built on `client/providers/gcp_account.py`) does nothing until it is shown. Then, on a background thread, it:

- finds the `gcloud` tool: on `PATH`, in the usual install folders, and on the PATH of your login shell (Orange started from the Dock doesn't get your shell's PATH). If it's missing, the page links to the install instructions.
- reads the application-default credentials with `google.auth.default()`. "Sign in with Google" runs `gcloud auth application-default login`, which opens Google's sign-in page in the browser, and then checks again. The page doesn't run it itself: it calls `CloudSession.sign_in("gcp")`, which runs the provider's `sign_in` (`OwnGCPProvider.sign_in`, then `gcp_account.sign_in`) on a thread of its own and reports with the signal `signInFinished(error)`. "Cancel sign-in" (`cancel_sign_in()`) kills gcloud and what it started.
- lists your projects (Resource Manager API) and the chosen project's regions and zones that are up (Compute Engine API, one call).

The default project is the saved one, else your active `gcloud` configuration's project; the default region and zone are the saved ones, else the configuration's `compute/region` and `compute/zone`, else `us-central1` / `us-central1-a`.

The credentials are used **without a quota project**. `gcloud auth application-default login` sets one, often an unrelated project, and Google then refuses to list projects if the Resource Manager API isn't enabled in it.

`tests/test_gcp_account.py` replaces Google's clients and `gcloud` with fakes, so the tests need no Google account. `test_real_account` lists your projects and regions with your own sign-in, read-only, when `ORANGE3_CLOUD_GCP_PROJECT` is set (for example after `set -a; source .orange3-cloud-dev.env; set +a`).

### Setup checker

Below the project, region and zone, the page shows the setup check (`client/providers/gcp_setup.py`). It runs in the background when the project or region changes, and on "Check again". Each check shows pass or fail, what was found, how to fix it and a link to the Google Cloud console:

- signed in, and a project selected,
- billing enabled (Cloud Billing API, `billingInfo`),
- the Compute Engine and Cloud Storage APIs enabled (Service Usage API). If any is off, "Enable APIs…" asks first, then enables them (`services:batchEnable`) and checks again,
- the CPU quota of the machine type's family in the region (`CPUS` for E2, `N2_CPUS`, `N2D_CPUS`, `C3_CPUS`) and the global `CPUS_ALL_REGIONS` quota, each with enough free vCPUs for the chosen machine type (Compute Engine API),
- the permissions for machines, firewall rules and buckets (`testIamPermissions` on the project; the list is `gcp_setup.PERMISSIONS`). Bucket object permissions aren't checked on the project: owners and editors get them on each bucket.

The machine type is the chosen size (`providers/gcp/machine_type`; default `n2-standard-16`, Medium). When everything passes, the page shows one line, with the checks in its tooltip; `dialog.pages["gcp"].setup_ok` tells whether all passed, and Start waits for it (the page's `ready`).

The calls are plain REST requests with the signed-in credentials (`GoogleSetupApi`), so `tests/test_gcp_setup.py` replaces them with a fake project, ready or not. `test_real_project_is_ready` checks your test project, read-only, when `ORANGE3_CLOUD_GCP_PROJECT` is set.

### Google Cloud machines

With Google Cloud chosen, Start needs the setup check to pass. It then starts the machine in the chosen project and zone (`OwnGCPProvider` in `client/providers/gcp.py`; the VM and firewall requests and the cloud-init are built in `gcp_vm.py` and sent as Compute Engine REST calls with your sign-in by `gcp_compute.py`):

- **Firewall:** the rule `orange3-cloud-https` on the `default` network allows TCP 8443 to the network tag `orange3-cloud` only from your computer's public IPv4 address (`/32`). The address comes from `https://api.ipify.org` (or `https://checkip.amazonaws.com`); `ORANGE3_CLOUD_PUBLIC_IP` overrides it. The session updates the rule when the address changes (before reattaching, and while reconnecting).
- **VM:** `orange3-cloud-<machine id>`, the chosen machine type (`providers/gcp/machine_type`; `CloudSession.start_machine("e2-standard-4")` overrides it), Container-Optimized OS (`cos-stable`), a `pd-balanced` boot disk of the chosen size (`providers/gcp/disk_gb`, default 100 GB) deleted with the VM, an ephemeral external IP, the network tag `orange3-cloud` and the labels `orange3-cloud=true` and `orange3-cloud-session=<machine id>`. It runs as the project's default Compute Engine service account with the read-only Cloud Storage scope.
- **Container:** cloud-init (`user-data`) adds a systemd unit that opens port 8443 in the VM's own firewall, pulls the machine image and runs it with the host's network. Its output goes to the serial console, which the dialog shows as progress while the machine starts. When the container exits (idle timeout, maximum lifetime, a failed boot), the VM powers off.
- **Image:** a released add-on uses the public `ghcr.io/ggaammdev/orange3-cloud-machine:<version>`. A development version uses the private development image `<region>-docker.pkg.dev/<project>/orange3-cloud/machine:dev` in the chosen project and region (see "Development images"; `scripts/setup-dev-registry.sh` lets the default Compute Engine service account pull it). `ORANGE3_CLOUD_GCP_IMAGE` overrides both, for example with a `:dev-<sha>` tag.
- **Secrets:** a new token and a self-signed certificate and key go into the VM's metadata, where the bootstrap reads them. The certificate is issued for the name `<vm name>.orange3-cloud`, because the IP address is known only after the VM exists; the add-on checks the certificate against that name and trusts nothing else. The handle (address, token, certificate, project and zone) is kept in `machine.json` as for local machines, so Orange reattaches after a restart.

Stop deletes the VM with its disk, and the firewall rule when no other `orange3-cloud` machine is left in the project. If Orange quits while a machine starts, the machine is left behind: the dialog lists such machines (labelled `orange3-cloud=true` but not the session's) with a button that deletes them after asking. To check by hand:

```sh
source .orange3-cloud-dev.env
gcloud --configuration=$ORANGE3_CLOUD_GCLOUD_CONFIG --project=$ORANGE3_CLOUD_GCP_PROJECT \
    compute instances list --filter=labels.orange3-cloud=true
```

`tests/test_gcp_provider.py` replaces Compute Engine with a fake project whose VM runs the API server in-process. `test_real_machine` starts a Small machine (`e2-standard-4`) in the test project, checks `/v1/info`, the firewall rule and reattaching, and deletes everything; it runs only when asked:

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_GCP_VM_TESTS=1 .venv/bin/pytest tests/test_gcp_provider.py -k real -s
```

### Machine sizes, prices and costs

The Google Cloud page has a **Machine size** list and a **Boot disk** size (50 to 2000 GB). The list has the add-on's sizes that the chosen zone offers (one Compute Engine `machineTypes.list` call per zone, `gcp_account.available_machine_types`), each with its hourly price in the chosen region. Choosing a size saves it (`providers/gcp/machine_type`) and runs the setup check again (the quotas depend on it). The sizes are `MACHINE_TYPES` in `client/providers/gcp_vm.py`.

- **Prices** come from the bundled `client/providers/gcp_prices.json` (with its date, read by `prices.load_price_file("gcp")`), not from a call at run time: per region, the on-demand list price of a vCPU and a GiB of memory of each family (E2, N2, N2D, C3) and of a GiB-month of `pd-balanced` disk, plus an external IPv4 address. A machine's price is its vCPUs and memory, its disk and its address (`gcp_prices.PriceList.per_hour`); discounts, network traffic and bucket storage aren't included. Refresh the file with your Google sign-in (it reads the Cloud Billing Catalog API, about 35,000 SKUs):

  ```sh
  .venv/bin/python scripts/update-gcp-prices.py
  ```

- **Size suggestion:** Cloud File tells the picker what the chosen file probably needs (its memory estimate and disk size, `gui/machine_size.size_hints()`). The picker marks the smallest offered size that fits ("fits your data"), offers "Use suggested size", and warns when the chosen size or disk is too small.
- **Costs:** a new machine's price is stored in its handle. A job costs its runtime on the machine times that price; the Cloud widgets show it in their status line ("Cost about $0.0012 at $0.15/h."). The dialog shows the machine's price and its cost so far (from when it was created; the jobs' share in brackets). The session (its `SessionCosts`, `client/costs.py`) records each job, and each machine when it deletes it, in `costs.jsonl` (one JSON object per line, with the machine's `provider`) in the add-on's private folder; `CostLog.totals()` adds up the machines' costs per provider. Local machines are free and show no cost.
- **A machine's whole session** (`costs.session_entry`) is recorded when the add-on deletes the machine: on Stop, when the heartbeat finds that it stopped by itself, when Orange opens again and finds it stopped (it stopped while Orange was closed), and for leftovers deleted with "Delete them…" in Cloud settings. Its running time, from its creation until it stopped (`seconds`), counts at the full price (`price_per_hour`: VM, disk and address), and its stopped time, until it was deleted (`stopped_seconds`), at the stopped price (`stopped_price_per_hour`): the disk on Google Cloud and AWS, which release a stopped machine's ephemeral address, and the disk and the Standard public IP address on Azure (`<cloud>_prices.stopped_per_hour`). `stopped_at` is when it stopped (empty after Stop). The provider reads that time from the cloud (`stopped_at(handle)`), just before deleting the machine:
  - **Google Cloud:** the instance's `lastStopTimestamp`.
  - **AWS:** after a stop request, the time in the instance's `StateTransitionReason` ("User initiated (2026-11-23 10:15:00 GMT)"). After the machine powered itself off, EC2 writes only "User initiated", so the time comes from the instance's `CPUUtilization` metric in EC2's free basic monitoring (CloudWatch `GetMetricData`, 5-minute values; `AmazonEC2FullAccess` allows it): the end of its last 5-minute period, so at most 5 minutes late. CloudWatch publishes the values a few minutes late, so when Orange opens again within minutes of the stop, the last one may be missing and the time up to 5 minutes early.
  - **Azure:** once the VM is deallocated, the time of the `ProvisioningState` status in its instance view, which the deallocation sets. A VM that only powered off stays allocated and costs compute, so it counts as running until it is deleted.
  - When the heartbeat noticed the stop, never before the last time the session saw the machine answer, and the current time if the cloud doesn't say.
- **If the cloud doesn't say when the machine stopped** (for example AWS without CloudWatch access, or a leftover that was still running), the entry is `approximate`: the machine counts as running until it was deleted, but no longer than its maximum lifetime after it first answered plus 5 minutes to power off (`costs.latest_stop`; the session keeps the maximum lifetime in the handle). An unknown stopped price makes it `approximate` too: the stopped time then counts at the full price. Leftovers are recorded at the price of their size, region and disk (AWS doesn't list the disk size: the default 100 GB); a leftover whose price is unknown, and a machine deleted outside Orange, aren't recorded.

`tests/test_costs.py` checks the prices, the picker's suggestion and the recorded costs, among them a machine found stopped when Orange opens again, a leftover, and the fallback without a stop time; each provider's tests read a stop time from a reply recorded on its cloud (`test_stop_time_and_stopped_price`). `TestRealCost` (with `ORANGE3_CLOUD_GCP_VM_TESTS=1`) lists the sizes of the test project's zone, starts a Small machine from the dialog, runs Cloud File and Cloud Test & Score on the demo data, checks the cost in the status line and the dialog, and stops the machine.

### Storage bucket and uploads

On Google Cloud, Cloud File doesn't send a local file to the machine: it uploads it to the project's storage bucket, and the machine loads it from there with its service account (`ClusterProvider.stage_file`; `client/providers/gcp_storage.py`, with the Cloud Storage REST calls in `gcp_storage_api.py`). The file stays in the bucket for later sessions, so it's uploaded only once.

- **Bucket:** `orange3-cloud-<project>-<6 random hex digits>` (bucket names are global), in the machine's region, labelled `orange3-cloud=true`, with uniform access, public access prevention and no soft delete (deleted files are gone at once and cost nothing). The first local file asks before the bucket is created. The default Compute Engine service account, which the machine runs as, gets `roles/storage.objectViewer` on the bucket.
- **Files:** `uploads/<key>/<file name>`, where the key comes from the file's path, size and modification time. A file already in the bucket with the same size isn't uploaded again.
- **Resumable:** 16 MB chunks through Google's resumable uploads. A broken connection is retried (for about a minute), each time from where Google's copy ends. If the upload still stops, or Orange quits, or the user cancels, the upload session is kept in the add-on's private folder (`gcs-uploads.json`), and loading the same file again continues where it stopped (sessions last about a week).
- **Deleting:** "Uploaded files…" in Cloud File lists the bucket's files and deletes the checked ones after asking. Deleting a file doesn't affect tables already loaded from it.
- **Memory estimate of `gs://` files:** made when you click Load, from the file's size and its first bytes, read with your Google sign-in (`OwnGCPProvider.read_stored_head`).

`tests/test_gcp_storage.py` replaces Cloud Storage with a fake. `test_real_bucket` uploads a small file to the test project's bucket (creating it if there is none), estimates it and deletes it; it runs with the same `ORANGE3_CLOUD_GCP_VM_TESTS=1` as above.

### AWS sign-in and setup checker

The dialog's AWS page (`gui/aws_page.py`, provider `aws` in `client/providers/aws.py`) works like the Google Cloud page: Start waits until the setup check passes.

- **The AWS CLI** (`client/providers/aws_account.py`): found like `gcloud`. If it's missing, the page links to the install instructions.
- **Profile:** the profiles in the AWS CLI's `~/.aws/config` and `~/.aws/credentials` (`AWS_CONFIG_FILE` and `AWS_SHARED_CREDENTIALS_FILE` if set). The default is the saved one, else `AWS_PROFILE`, else `default`. The profile is the AWS settings' `account`.
- **Sign in to AWS** runs `aws login --profile <profile>` (with `--region` when the profile has none, since the CLI would ask for one on the console), or `aws sso login --profile <profile>` for a profile of IAM Identity Center, through `CloudSession.sign_in("aws")`. The credentials are read with `boto3` for that profile; profiles of `aws login` need `botocore[crt]`, which is why the add-on depends on `boto3[crt]`.
- **Region:** the regions enabled for the account (EC2 `DescribeRegions`). The default is the saved one, else the profile's region, else `us-east-1`.
- **Sizes:** `client/providers/aws_vm.py` (default `m7i.4xlarge`, Medium), those the region offers, with prices (see "AWS sizes, prices and costs").

The setup check (`client/providers/aws_setup.py`) runs like Google's and checks, with a fix and a link for each: the sign-in (STS `GetCallerIdentity`), that the region is enabled, the region's default VPC with a default subnet, the permissions, and the vCPU quota `L-1216C47A` ("Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances") against the vCPUs of the region's running instances that count against it, naming an open request to raise it. The permissions are tried rather than read from policies: EC2 calls with `DryRun` (`RunInstances` without and with tags, `DescribeInstances`, and for security groups `Describe`, `Create`, `AuthorizeIngress`, `RevokeIngress` and `Delete` on the default VPC's `default` group, which is never changed), the image's SSM parameter, and S3 `ListBuckets`. Stopping and terminating instances and reading their console can't be tried without an instance (EC2 checks that it exists first), and S3 has no dry run, so `ec2:StopInstances`, `ec2:TerminateInstances`, `ec2:GetConsoleOutput`, `s3:CreateBucket` and `s3:PutObject` are asked of IAM's policy simulator (`SimulatePrincipalPolicy` for the signed-in user, or for a role session's role, found with `GetRole` for its path; the root user may do everything). Without the right to use the simulator, the check passes and lists them as not checked. The calls go through `SetupApi` (`BotoSetupApi` with `boto3`).

`tests/test_aws_account.py` and `tests/test_aws_setup.py` replace AWS and `aws` with fakes, so the tests need no AWS account. `test_real_account` and `test_real_account_is_ready` read your test account (read-only and dry-run calls, nothing is created) when `ORANGE3_CLOUD_AWS_PROFILE` is set:

```sh
set -a; source .orange3-cloud-dev.env; set +a
.venv/bin/pytest tests/test_aws_account.py tests/test_aws_setup.py -k real
```

### AWS machines

With AWS chosen, Start starts the machine as an EC2 instance in the chosen region, with the chosen profile's credentials (`AWSProvider` in `client/providers/aws.py`; the requests are built in `aws_vm.py` and sent with `boto3` by `aws_ec2.py`):

- **Security group:** `orange3-cloud-https` in the region's default VPC, tagged `orange3-cloud=true`, allows TCP 8443 only from your computer's public IPv4 address (`/32`, found as for Google Cloud; `ORANGE3_CLOUD_PUBLIC_IP` overrides it). The session updates it when the address changes, and removes other rules.
- **Instance:** the chosen size (`providers/aws/machine_type`), the ECS-optimized Amazon Linux 2023 image (it has Docker; its ID is the public SSM parameter `/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id`), a `gp3` boot volume of the chosen size (`providers/aws/disk_gb`, default 100 GB) deleted with the instance, a public IPv4 address in a default subnet, instance metadata only through IMDSv2 (hop limit 1), and the tags `Name=orange3-cloud-<machine id>`, `orange3-cloud=true` and `orange3-cloud-session=<machine id>` on the instance and its volume. Its shutdown behaviour is "stop": when the machine powers itself off, the instance stops and compute charges end.
- **User data** (`client/providers/cloud_init.py`): `#cloud-config` followed by JSON, compressed with gzip, because EC2 allows only 16 KB and a typical environment's settings are about that (about 4.5 KB compressed). Cloud-init writes each setting (`token`, `tls-cert`, `tls-key`, `env-spec`, `idle-timeout`, `max-lifetime`, `machine`) to a file in `/run/orange3-cloud/settings` (in memory, root only) and adds a systemd unit. The unit gives the files to the image's user (uid 1000), signs Docker in to the registry for a development image and pulls the image (both retried: right after boot the network may not reach the registry yet), and runs it with the host's network and the folder mounted read-only at `/run/orange3-cloud-settings`, where the bootstrap's `aws` platform reads them. When the container exits for any reason (idle timeout, maximum lifetime, a failed boot after its 10-minute wait), the unit powers the VM off.
- **Image** (`client/providers/machine_images.py`): a released add-on uses the public `ghcr.io/ggaammdev/orange3-cloud-machine:<version>`, with no credentials. A development version uses the development image `:dev` in the Artifact Registry of the project and region chosen on the Google Cloud page (see "Development images"), and the add-on makes a 1-hour token of `orange3-cloud-image-reader` with your Google sign-in, which the unit gives to `docker login`. `ORANGE3_CLOUD_AWS_IMAGE` overrides both, for example with a `:dev-<sha>` tag.
- **Progress:** while the machine starts, the dialog shows the bootstrap's progress from the instance's console (`GetConsoleOutput`, about a minute late).

Stop terminates the instance (its volume goes with it), waits until it is gone, and deletes the security group when no other `orange3-cloud` instance is left in the region. A machine that stops by itself (idle timeout, maximum lifetime) powers off, and the instance is `stopped` with the reason `Client.InstanceInitiatedShutdown`: no compute or public IPv4 charges, but its volume stays. With Orange open, the heartbeat notices within about two minutes and terminates it (from `stopping` or `stopped`); `status` says "The machine stopped." without a reason, so the session explains it from the last `/v1/info` as on Google Cloud. With Orange closed, the instance stays `stopped` until Orange opens again, when `reattach` finds it stopped and deletes it. The dialog lists machines left behind (tagged `orange3-cloud=true` but not the session's) with a button that deletes them. To check by hand:

```sh
set -a; source .orange3-cloud-dev.env; set +a
aws ec2 describe-instances --profile $ORANGE3_CLOUD_AWS_PROFILE --region $ORANGE3_CLOUD_AWS_REGION \
    --filters Name=tag:orange3-cloud,Values=true --query 'Reservations[].Instances[].[InstanceId,State.Name]'
aws ec2 describe-security-groups --profile $ORANGE3_CLOUD_AWS_PROFILE --region $ORANGE3_CLOUD_AWS_REGION \
    --filters Name=group-name,Values=orange3-cloud-https --query 'SecurityGroups[].GroupId'
```

`tests/test_aws_provider.py` replaces EC2 and SSM with a fake account that checks every request against botocore's model of the API and whose instances run the API server in-process. `test_real_machine` starts a Small machine (`m7i.xlarge`) in the test account, checks `/v1/info`, the security group and reattaching, and checks after Stop that the instance is terminated and its volume and the security group are gone; it runs only when asked (a development version also needs `ORANGE3_CLOUD_AWS_IMAGE`, or the Google Cloud project from the env file, for its image):

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_AWS_VM_TESTS=1 .venv/bin/pytest tests/test_aws_provider.py -k real -s
```

### S3 storage and uploads

On AWS, Cloud File uploads a local file to an S3 bucket in the account (`AWSProvider.stage_file`; `client/providers/aws_storage.py`, `S3Storage`), as on Google Cloud. The machine has no AWS access of its own: for each load, the add-on signs a link to the file with your credentials (`machine_location`), and the machine downloads the file through it.

- **Bucket:** `orange3-cloud-<12 random hex digits>`. Bucket names are public and global, so the name has no account ID. The first local file asks before the bucket is created, in the machine's region, tagged `orange3-cloud=true` (in the `CreateBucket` request itself, which needs `s3:TagResource`), with all four "block public access" settings, ACLs off (`BucketOwnerEnforced`) and a lifecycle rule that deletes the parts of uploads left unfinished for 7 days. The add-on finds its bucket by name and tag, preferring one in the machine's region; buckets without the tag are never used.
- **Files:** the same names as on Google Cloud (`uploads/<key>/<file name>`, `client/providers/uploads.py`). A file already in the bucket with the same size isn't uploaded again.
- **In parts:** a multipart upload in 16 MiB parts (more for files over 160 GB, as S3 allows 10,000 parts), each with a CRC32 checksum that S3 checks; a file of one part goes in one request. A broken connection is retried for about a minute, then the upload stops with "Load again to continue where it stopped". The upload ID is kept in the add-on's private folder (`s3-uploads.json`), so loading the same file again sends only the parts S3 doesn't have (`ListParts`), also after Orange restarts.
- **Signed links:** `generate_presigned_url` for `GetObject`, Signature Version 4 on the bucket region's own endpoint (`aws_account.client` configures S3 so; the default links go to the global endpoint, which redirects for buckets in other regions and breaks the signature). Before signing, a `HeadObject` with your credentials checks that you may read the file, so a missing or forbidden file gets a clear error. A link is asked for an hour, but one signed with temporary credentials (`aws login`, IAM Identity Center) works only as long as they do, 5 to 15 minutes. So Cloud File asks the machine to copy the file to its disk with one request first (`copy_first` in `POST /v1/tables`, see `docs/api-v1.md`); a download that has started isn't cut off. A link to a file in another region is signed for that region (`HeadBucket` tells it).
- **`s3://` URLs:** any file your profile may read loads the same way, through a signed link; its memory estimate reads the file's size and first bytes with your credentials (`read_stored_head`).
- **Deleting:** "Uploaded files…" lists the bucket's files and deletes the checked ones after asking, as on Google Cloud.

`tests/test_aws_storage.py` replaces S3 with a fake that checks every request against botocore's model and serves the signed links over HTTP, refusing `HEAD` as S3 does for a link signed for `GET`. Its Cloud File test uploads a file in several parts and loads it on the in-process machine, then loads it as an `s3://` URL. `test_real_bucket` uploads a small file to the test account's bucket (creating it if there is none), estimates it, reads it through a signed link and deletes it:

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_AWS_VM_TESTS=1 .venv/bin/pytest tests/test_aws_storage.py -k real
```

### AWS sizes, prices and costs

The AWS page has the same **Machine size** list and **Boot disk** size as the Google Cloud page (`gui/machine_size.py`), with AWS's sizes (`MACHINE_TYPES` in `client/providers/aws_vm.py`). The list has the sizes the chosen region offers (one EC2 `DescribeInstanceTypeOfferings` call per profile and region, `aws_account.offered_instance_types`; the others are hidden), each with its hourly price there. Choosing a size saves it (`providers/aws/machine_type`) and runs the setup check again, whose vCPU quota check is for that size.

- **Prices** come from the bundled `client/providers/aws_prices.json` (with its date, read by `prices.load_price_file("aws")`): per region, the on-demand price of each of the add-on's instance types (Linux, shared tenancy), of a GB-month of `gp3` volume storage and of an hour of an in-use public IPv4 address. A machine's price is its instance, its boot volume and its address (`aws_prices.per_hour`); discounts, free tiers, network traffic and S3 storage aren't included. Refresh the file with a signed-in AWS CLI profile (it reads the AWS Price List Query API, a few hundred products; any account may, and it costs nothing):

  ```sh
  .venv/bin/python scripts/update-aws-prices.py --profile <profile>
  ```

- **Size suggestion and costs** work as on Google Cloud: a new machine's handle keeps its price (`price_per_hour`), the widgets' status line and the dialog show the costs, and `costs.jsonl` records them with the provider `aws`.

`tests/test_costs.py` checks the bundled AWS prices, the provider's prices, and that a large file in Cloud File makes the picker suggest an AWS size that fits; `tests/test_aws_account.py` checks the dialog's sizes, prices, hidden sizes and the quota check per size. `TestRealAwsCost` (with `ORANGE3_CLOUD_AWS_VM_TESTS=1`, and for a development version `ORANGE3_CLOUD_AWS_IMAGE`) does on AWS what `TestRealCost` does on Google Cloud:

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_AWS_VM_TESTS=1 .venv/bin/pytest tests/test_costs.py -k RealAws -s
```

### Azure sign-in and setup checker

The dialog's Azure page (`gui/azure_page.py`, provider `azure` in `client/providers/azure.py`) works like the AWS page. Start is enabled once the setup check passes (see "Azure machines"). [setup-azure.md](setup-azure.md) is the users' guide to setting up a subscription for it.

- **The Azure CLI** (`client/providers/azure_account.py`): found like `gcloud`. If it's missing, the page links to the install instructions. The add-on runs `az` with `PYTHONWARNINGS=ignore`, because the CLI's own Python may print warnings.
- **Sign in to Azure** runs `az login` through `CloudSession.sign_in("azure")`, with `AZURE_CORE_LOGIN_EXPERIENCE_V2=off`, so the CLI doesn't ask on the console which subscription to use.
- **Subscription:** those of the CLI's sign-in (`az account list`, which reads the CLI's files). The default is the saved one, else the CLI's default subscription. Its ID is the Azure settings' `account`. The credentials are `azure.identity`'s `AzureCliCredential` for the subscription's tenant, which runs `az account get-access-token` for each token; `CachedCredential` keeps a token until five minutes before it expires, so a setup check asks `az` once. `AzureCliCredential` finds `az` only on `PATH`, so the add-on appends the folder of the `az` it found when it isn't there (Orange started from the Dock).
- **Region:** the subscription's physical regions (`Subscriptions.list_locations`), shown as "East US (eastus)". The default is the saved one, else the CLI's default location (`az config set defaults.location=…`, read from `~/.azure/config` or `$AZURE_CONFIG_DIR/config`), else `eastus`.
- **Sizes:** `client/providers/azure_vm.py` (default `Standard_D16s_v6`, Medium), those the region offers to the subscription, with prices (see "Azure sizes, prices and costs").

The setup check (`client/providers/azure_setup.py`) checks, with a fix and a link for each: the sign-in (a token for Azure Resource Manager), that the subscription exists and is enabled, that the resource providers `Microsoft.Compute`, `Microsoft.Network` and `Microsoft.Storage` are registered (the page's **Register…** registers the missing ones after you confirm, and waits up to two minutes), the permissions to create and delete what a machine needs in the resource group `orange3-cloud` (and the storage account for uploaded files, its container and its user delegation keys), the permission to assign roles there (`Microsoft.Authorization/roleAssignments/write` and `delete`: each machine gets the built-in role "Desktop Virtualization Power On Off Contributor" on its own VM, so that it can deallocate itself, and you get "Storage Blob Data Contributor" on the storage account; Owner, User Access Administrator and Role Based Access Control Administrator have it, Contributor doesn't), and the vCPU quotas of the size's family (for example "Standard Dsv6 Family vCPUs") and "Total Regional vCPUs" in the region, naming an open request to raise them (Microsoft.Quota). The permissions are read, not tried: Resource Manager's `Microsoft.Authorization/permissions` lists what you may do on the resource group, or on the subscription while the group doesn't exist yet, and the checker matches the needed actions against it, wildcards and exclusions included. The independent checks run at once; a check takes about 3 seconds. The calls go through `SetupApi` (`AzureSetupApi` with the Azure libraries: `azure-mgmt-resource`, `azure-mgmt-resource-subscriptions` and `azure-mgmt-compute`).

`tests/test_azure_account.py` and `tests/test_azure_setup.py` replace Azure and `az` with fakes, so the tests need no Azure subscription. `test_real_subscription` and `test_real_subscription_is_ready` read your test subscription (read-only calls, nothing is created) when `ORANGE3_CLOUD_AZURE_SUBSCRIPTION` is set:

```sh
set -a; source .orange3-cloud-dev.env; set +a
.venv/bin/pytest tests/test_azure_account.py tests/test_azure_setup.py -k real
```

### Azure machines

With Azure chosen, Start starts the machine as a VM in the chosen subscription and region, with the Azure CLI's credentials (`AzureProvider` in `client/providers/azure.py`). The request bodies are built in `azure_vm.py` (the shared pieces below in `azure_network.py`), and `azure_arm.py` sends them as Resource Manager REST requests through an Azure SDK client's pipeline (`send_request`: the CLI's credentials, timeouts and retries), so the add-on needs no Azure network or authorization library.

- **Shared pieces,** created once in the chosen region and tagged `orange3-cloud=true`: the resource group `orange3-cloud` (everything the add-on creates on Azure is in it), the virtual network `orange3-cloud` (`10.42.0.0/16`, one subnet `default`), and the network security group `orange3-cloud-https`, whose one rule allows TCP 8443 only from your computer's public IPv4 address (`/32`, found as for Google Cloud; `ORANGE3_CLOUD_PUBLIC_IP` overrides it). The session updates the rule when the address changes (a `PUT` of the group, which replaces any other rule). If you choose another region, the network and the group move there once no machine of the add-on is left; while one runs, Start says where.
- **VM** (`orange3-cloud-<machine id>`): the chosen size (`providers/azure/machine_type`), Canonical's Ubuntu 26.04 LTS image (`ubuntu-26_04-lts`/`server`, Gen2, NVMe, Trusted Launch), a Premium SSD OS disk of the chosen size (`providers/azure/disk_gb`, default 100 GB), a network interface with the security group and a Standard static public IP address, the tags `orange3-cloud=true` and `orange3-cloud-session=<machine id>`, boot diagnostics, and a system-assigned managed identity. The disk, the interface and the address have the delete option `Delete`, so they go with the VM. Azure requires an administrator and an SSH key for a Linux VM: the add-on makes a new key and throws its private half away, and port 22 stays closed.
- **Its role:** right after creating the VM, the add-on gives the VM's identity the built-in role "Desktop Virtualization Power On Off Contributor" on the VM itself (a role assignment named by a GUID made from the VM and the machine id), so the machine may deallocate its own VM and nothing else. That needs your permission to assign roles in the resource group (the setup check's "Permission to give machines their role"). It is retried for a while if Azure doesn't know the new identity yet.
- **Custom data** (`client/providers/cloud_init.py`, as on AWS, base64 of the gzip, at most 64 KB): cloud-init installs Docker (`docker.io`, about 25 seconds), writes the settings files and the systemd unit, which runs the image for the bootstrap's `azure` platform. When the container exits for any reason, the unit's `ExecStopPost` runs `/usr/local/sbin/orange3-cloud-deallocate`: it gets a token for the VM's identity from the instance metadata service and asks Resource Manager to deallocate the VM, and powers off only if that fails. A VM that only powers off stays allocated and keeps costing compute. Without the unit (`ORANGE3_CLOUD_POWER_OFF` not `host`), the bootstrap's `azure` platform deallocates the VM the same way.
- **Image:** as on AWS; `ORANGE3_CLOUD_AZURE_IMAGE` overrides it.
- **Progress:** while the machine starts, the dialog shows the bootstrap's progress from the VM's serial log (boot diagnostics, read every 15 seconds). Azure writes the log a minute or two after the VM is created and then lags, so a quick start may show none.

Stop asks Azure to deallocate the VM, then deletes its role assignment and the VM (its disk, interface and address go with it), waits until it is gone, and deletes the network security group when no other machine of the add-on is left. The resource group and the virtual network stay for the next machine. A machine that stops by itself (idle timeout, maximum lifetime) deallocates its VM: no compute charges, but its disk and its static public address are still billed until the VM is deleted. With Orange open, the heartbeat notices and deletes it; with Orange closed, the next `reattach` does. The dialog lists machines left behind (VMs tagged `orange3-cloud=true` in the resource group but not the session's) with a button that deletes them. To check by hand:

```sh
set -a; source .orange3-cloud-dev.env; set +a
export PYTHONWARNINGS=ignore
az vm list --subscription $ORANGE3_CLOUD_AZURE_SUBSCRIPTION -g orange3-cloud -d --query '[].[name,powerState]' -o tsv
az resource list --subscription $ORANGE3_CLOUD_AZURE_SUBSCRIPTION -g orange3-cloud --query '[].[type,name]' -o tsv
```

`tests/test_azure_provider.py` replaces Resource Manager with a fake subscription that answers the provider's requests, reads each VM's body through the Azure SDK's own model, and runs the API server in-process for each VM. `test_real_machine` starts a Small machine (`Standard_D4s_v6`) in the test subscription, checks `/v1/info`, the rule, the role and reattaching, and checks after Stop that the VM, its disk, network interface, public address and role, and the network security group are gone, each read directly (only the resource group, the virtual network and the storage account for uploads stay). `test_real_machine_deallocates_itself` closes the session after Start, waits until the VM deallocates itself after a 2-minute idle timeout, checks that it stays deallocated with its disk and address, and that the next session deletes it and records its cost (running until it was deallocated, then the disk and address). They run only when asked (a development version also needs `ORANGE3_CLOUD_AZURE_IMAGE`, or the Google Cloud project from the env file, for its image):

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_AZURE_VM_TESTS=1 .venv/bin/pytest tests/test_azure_provider.py -k real -s
```

### Azure Blob storage and uploads

On Azure, Cloud File uploads a local file to a storage account in the subscription (`AzureProvider.stage_file`; `client/providers/azure_storage.py`, `AzureStorage`), as on the other clouds. The machine has no Azure access of its own: for each load, the add-on makes a read-only link to the file, signed with your sign-in (`machine_location`), and the machine downloads the file through it. The account and its container are managed through Resource Manager (`Arm`, as for machines); the files through the Azure Storage library (`azure-storage-blob`, MIT), with the Azure CLI's credentials (`Arm.credential`).

- **Storage account:** `o3cloud<12 random hex digits>` (account names are global and public, and only lower-case letters and digits, so the name has no subscription ID), in the resource group `orange3-cloud` and the machine's region, tagged `orange3-cloud=true`: StorageV2, Standard_LRS, Hot, TLS 1.2, anonymous access off (`allowBlobPublicAccess`) and access with account keys off (`allowSharedKeyAccess`), so only Microsoft Entra sign-ins and the links they sign work. Its container `orange3-cloud` is private. The first local file asks before the account is created. The add-on finds its account by name prefix and tag in the resource group, preferring one in the machine's region; other accounts are never used for uploads.
- **Your role on it:** signed in with the Azure CLI, you need the data role "Storage Blob Data Contributor" on the account to read and write its files, even as the subscription's Owner (Owner manages accounts but has no data actions). The add-on assigns it to you (the object ID from your Resource Manager token's `oid` claim) when it creates the account, and, when an existing account doesn't give you the data actions (Resource Manager's `Microsoft.Authorization/permissions` on the account, `dataActions`), Cloud File asks before assigning it. Azure takes a minute or two to apply a new role: until then the first upload's requests are refused (`AuthorizationPermissionMismatch`), and it tries again every 10 seconds for up to 5 minutes, each time with one short request without the Azure library's retries (`ROLE_TRY_OPTIONS`), while Cloud File's status says that it waits (`stage_file`'s `on_status`, which also says when a bucket or storage account is being created). Assigning needs `Microsoft.Authorization/roleAssignments/write`, which the setup check already requires.
- **Files:** the same names as on the other clouds (`uploads/<key>/<file name>`, `client/providers/uploads.py`). A file already in the container with the same size isn't uploaded again.
- **In blocks:** a block blob in 16 MiB blocks (more for files over about 800 GB, as a blob has at most 50,000 blocks), each sent with its MD5 checksum, which Azure checks, and committed at the end; a file of one block goes in one request. A broken connection is retried for about two minutes (seven tries with growing pauses, and the library itself tries each request three times), then the upload stops with "Load again to continue where it stopped". The block IDs start with a random prefix kept in the add-on's private folder (`azure-uploads.json`), so loading the same file again sends only the blocks Azure doesn't have yet (`Get Block List`), also after Orange restarts. Azure deletes uncommitted blocks after a week.
- **Signed links:** a user delegation SAS: the add-on gets a user delegation key from the account with your sign-in (kept for a day, so links need no new key), and signs a link for one blob, read only, HTTPS only, for an hour, at each load. Before signing, it reads the blob's properties with your sign-in, so a missing or forbidden file gets a clear error. Cloud File asks the machine to copy the file to its disk with one request first (`copy_first`). The machine keeps and shows the link without its query.
- **`az://` URLs:** `az://<storage account>/<container>/<path>`. The account is in the URL because a workflow has to work without other settings (fsspec's `adlfs` and Polars read `az://<container>/<path>` with the account given separately, so the forms differ). Any file your sign-in may read (the role "Storage Blob Data Reader" or more on its account or container) loads the same way, through a signed link; its memory estimate reads the file's size and first bytes with your sign-in (`read_stored_head`). For a file whose address in the Azure portal is `https://<account>.blob.core.windows.net/<container>/<path>`, write `az://<account>/<container>/<path>`.
- **Deleting:** "Uploaded files…" lists the container's files and deletes the checked ones after asking, as on the other clouds.

`tests/test_azure_storage.py` replaces Resource Manager with the fake subscription of `test_azure_provider.py` (a new account appears after a few reads; your permissions come from your role assignments) and Blob Storage with a fake that checks each call against the Azure Storage library's own method and serves the signed links over HTTP. Its Cloud File test uploads a file in several blocks and loads it on the in-process machine, then loads it as an `az://` URL. `test_real_storage` uploads a small file to the test subscription's account (creating it, with your role, if there is none), estimates it, reads it through a signed link and deletes it:

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_AZURE_VM_TESTS=1 .venv/bin/pytest tests/test_azure_storage.py -k real -s
```

### Azure sizes, prices and costs

The Azure page has the same **Machine size** list and **Boot disk** size as the other clouds' pages (`gui/machine_size.py`), with Azure's sizes (`MACHINE_TYPES` in `client/providers/azure_vm.py`). The list has the sizes the chosen region offers to the subscription, each with its hourly price there: `azure_account.offered_sizes` reads the region's Compute resource SKUs (one Resource Manager call, `Microsoft.Compute/skus` with a location filter, a few MB) and leaves out the sizes that aren't there or whose region is restricted for the subscription (`NotAvailableForSubscription`; restricted zones don't matter, as the machines aren't in a zone). The add-on keeps a region's list for an hour, and the page asks once per subscription and region; **Reload** asks again. While the list is unknown, or if it can't be read, all sizes are shown. Choosing a size saves it (`providers/azure/machine_type`) and runs the setup check again, whose family and regional vCPU quota checks are for that size.

- **Prices** come from the bundled `client/providers/azure_prices.json` (with its date, read by `prices.load_price_file("azure")`): per region, the pay-as-you-go price of each of the add-on's VM sizes (Linux, not Spot), of a month of each Premium SSD tier the add-on's disk sizes fall in (P6 to P40, LRS) and of an hour of a Standard static public IPv4 address. Premium SSD is billed per tier, by the smallest tier that holds the disk (`azure_prices.disk_tier`: 100 GB is a P10 of 128 GiB). A machine's price is its VM, its OS disk and its address (`azure_prices.per_hour`); discounts, network traffic and Blob Storage aren't included. The disk and the address are also billed while a VM that stopped by itself is deallocated, until it is deleted. Refresh the file from Azure's public Retail Prices API (no sign-in; three queries, a few thousand price items):

  ```sh
  .venv/bin/python scripts/update-azure-prices.py
  ```

- **Size suggestion and costs** work as on Google Cloud: a new machine's handle keeps its price (`price_per_hour`), the widgets' status line and the dialog show the costs, and `costs.jsonl` records them with the provider `azure`.

`tests/test_costs.py` checks the bundled Azure prices, the disk tiers, the provider's prices, and that a large file in Cloud File makes the picker suggest an Azure size that fits; `tests/test_azure_account.py` checks the offered sizes (from sample SKUs, and kept), and the dialog's sizes, prices, hidden sizes and the quota checks per size. `TestRealAzureCost` (with `ORANGE3_CLOUD_AZURE_VM_TESTS=1`, and for a development version `ORANGE3_CLOUD_AZURE_IMAGE`) does on Azure what `TestRealCost` does on Google Cloud:

```sh
set -a; source .orange3-cloud-dev.env; set +a
ORANGE3_CLOUD_AZURE_VM_TESTS=1 .venv/bin/pytest tests/test_costs.py -k RealAzure -s
```

## Writing a Cloud widget

Cloud widgets subclass `OWCloudBaseWidget` (`orangecontrib/cloud/widgets/base.py`). It adds a "Cloud" box with the connection indicator, a "Cloud settings…" button and a status line ("Computed with Orange … and scikit-learn … on <machine> in … s."), and:

- `self.run_cloud_job(kind, inputs=None, params=None, objects=None, on_done=None)` runs a job off the GUI thread, with the widget's progress bar and status text, and gives its `JobResult` to `on_done` or `on_cloud_result(result)` in the GUI thread. It returns `False` and shows "Not connected…" when there's no machine. For several calls in one task, use `self.run_cloud(fn)`, where `fn(task)` gets a `CloudTask` (`task.client`, `task.run_job(...)`, `task.set_progress`, `task.set_status`, `task.is_cancelled()`).
- Call `self.cancel()` in input handlers: a new input makes the running job obsolete. A new run and closing the widget cancel too. The job on the machine is cancelled as well.
- Errors become the widget's messages: `Error.not_connected`, `Error.connection_lost`, `Error.cloud_error`, `Error.out_of_memory` and `Error.job_cancelled` (the machine cancelled the job, for example because Orange hadn't been in touch for its idle timeout; the widget's own cancelling shows nothing). The machine's traceback is under "Show details" and in `self.cloud_error_details`.
- If you override `onDeleteWidget`, call `super().onDeleteWidget()`.

### Cloud tables

Cloud tables reach widgets as a `RemoteTable` (`orangecontrib/cloud/data/remote_table.py`): an Orange `Table` with the cloud table's domain and one placeholder row in which every value is missing, so nothing on the laptop can compute from the data. `table.ref` holds the table ID on the machine, the real row count (`ref.n_rows`, `None` after a row filter) and the steps that Orange's data-changing widgets recorded (`table.steps`: `("transform", Domain)`, `("filter", Filter)`, and `("preprocess", recipe)` from Cloud Preprocess, Cloud Discretize, Cloud Purge Domain, Cloud Categories and Cloud Create Class). Selections and other row subsets give plain local tables, and so does writing values into the table (`Table.unlocked`, as Randomize does).

Distributions and contingencies of a cloud table (the `Storage` hooks `_compute_distributions` and `_compute_contingency`, which `Orange.statistics` calls) raise `PlaceholderStatisticsError` with a message that names the Cloud widget to use, because computed from the placeholder row they would choose columns from no data: Rank's counting scores (information gain, gain ratio, Gini, FCBF), Discretize's equal frequency and entropy-MDL cut points, and Purge Domain's check for constant numeric columns. Orange shows the error in its error report. Orange code that reads the placeholder's values directly (ANOVA, χ², ReliefF, univariate regression, equal-width discretization, ...) can't be stopped this way; `docs/supported-widgets.md` warns about it. Basic statistics (`_compute_basic_stats`) aren't stopped: Orange's Data Table uses them, and the machine already refuses Normalize's steps with a clearer message.

Which of Orange's own widgets work on cloud tables is in [supported-widgets.md](supported-widgets.md). `tests/test_supported_widgets.py` checks it: each listed widget gets a cloud table offscreen, and every replayed output is compared with the same widget's output on the full table. The default run checks the replayed widgets; `pytest -m slow -s tests/test_supported_widgets.py` checks all of them and prints what each did. When you change what a widget does on cloud data, update both.

In a Cloud widget, get the table ID to compute on with `resolve(data, task.client, machine_id)` (in the worker thread). It raises a `RemoteTableError`, which the base class shows as an error, for local data ("This is local data, not a cloud table."), and for a table whose machine is gone ("This cloud table is no longer available. Run Cloud File again."). `is_cloud_table(data)` checks the input without calling the machine.

With recorded steps, `resolve` asks the machine to replay them on all rows (`POST /v1/tables/{id}/derive`, `server/ops/derive.py`) and returns the new table's ID. The machine keeps the result per table and steps, so a second `resolve` with the same steps costs one quick request. Branches of a workflow share their first steps (the taxi demo's Feature Constructor and Select Rows come before a second Select Rows in one branch and Select Columns in two others), so `CloudClient.derive` also sends the keys of the first 1, 2, … steps (`prefix_keys`), and the machine runs `derive` jobs one at a time, each starting from the table it derived with the longest of its first steps; a job keeps a table between its steps when a waiting job starts with the same steps and then goes on with other ones. Without this, each branch replayed Feature Constructor on all rows, all at once, which needed several times the memory of the table. It refuses steps that learned their settings from the placeholder row (Normalize in Continuize or Preprocess, imputation, discretization, ...) and Feature Constructor categorical columns whose values aren't all in the column's list; the client raises `CloudUnsupportedStepError` with a message that names the column and says what to do. See `derive` in [api-v1.md](api-v1.md).

Pickles between the add-on and the machine always go through `dumps` and `loads` in `protocol/pickles.py` (`dump` and `load` for files, such as the results of the forked processes in `server/forked.py`). Importing `protocol/` imports no Qt; the pickler imports one of Orange's widget modules only to unpickle a class of it, and `class_by_name` only those of `BY_NAME_CLASSES`. They handle Select Rows' conditions, which plain `pickle` can't, and Feature Constructor's expressions, Create Class's rules and Preprocess's "Remove numeric features", whose modules need Qt: the machine has no Qt, so it uses stand-ins from `server/expressions.py` and `server/preprocessors.py`. Other classes of Orange's widget modules unpickle there as `Unavailable` objects, so the machine can refuse the step that uses them with a message that names the column. With `refs`, objects that both sides already have travel as references (`("ref", name, position)`); the job runner resolves them to the columns of the job's input tables.

Every Cloud widget has a help page, which Orange opens with F1 or the widget's "?" button: `orangecontrib/cloud/widgets/help/<name>.html`, with its screenshots in `help/images/`. Orange finds the pages through the `orange.canvas.help` entry point (`html-index` in `pyproject.toml`, pointing to `WIDGET_HELP_PATH` in `widgets/__init__.py`): it reads `help/index.html` and links each widget to the page whose link text in the `widgets` section is the widget's name. A new widget needs a page and a line in that list; `tests/test_package.py` checks that every Cloud widget has one and that its images exist. The screenshots are taken from the widgets with real results on the local machine (`QWidget.grab()`, offscreen), saved as PNG with 256 colours to keep them small. Screenshots never show a real Google Cloud project, account or IP address.

The developer test widget runs a `sleep` job through the base class (start the machine with "Cloud settings…" first):

```sh
.venv/bin/python -m orangecontrib.cloud.widgets.dev.owcloudsleeptest
```

### Cloud Preprocess

**Cloud Preprocess** (`widgets/owcloudpreprocess.py`) fits a recipe from Orange's own Preprocess widget (its "Preprocessor" output; build the recipe without data) on all rows of a cloud table, on the machine: normalizing, imputing, discretizing, PCA, feature selection and the rest of Preprocess's steps, with Orange's own code. Without a recipe the data passes through unchanged; local data is refused.

`preprocessed(data, recipe, client, machine_id, wait)` in `data/remote_table.py` adds `("preprocess", recipe)` to the steps, resolves them (a `derive` job), and returns a `RemoteTable` with the fitted table's domain as the machine sent it (`ref.fitted`). Its row count is the fitted table's.

Steps recorded after it (Select Columns, Continuize, ...) use the fitted columns. Some of them compare equal only to themselves (PCA's components, for example), so a copy that travelled to the laptop and back isn't the machine's column any more, and the checks would refuse it. So `RemoteTable.resolve` splits the steps after each `preprocess` step (`CloudRef.segments()`): the first request ends with the recipe, which the machine already fitted and kept, and the next one starts from the fitted table and sends the fitted columns as references to that table's columns (`CloudClient.derive(..., columns=)`), not as copies. A step that learned from the placeholder after it (Orange's Normalize on the output, say) is still refused.

### Cloud Rank

**Cloud Rank** (`widgets/owcloudrank.py`) is Orange's Rank for a cloud table: the same scoring methods (information gain, gain ratio, Gini, ANOVA, χ², ReliefF and FCBF for a categorical target, univariate regression and RReliefF for a numeric one), learners on its Scorer input (random forest, logistic regression, ...), Orange's own table of scores and its selection (none, all, manual, the best N). The machine scores on all rows in a `rank` job (`server/ops/rank.py`) that calls each method and scorer as Orange's Rank does, with its fallbacks, so the scores are Orange's. Scores are kept per method and scorer: checking another method scores only that one. Reduced Data is the cloud table with the chosen columns, a recorded step that the machine replays; Scores is Orange's scores table (local, one row per feature); Features the chosen columns.

The scorers copy and discretize the data; measured on 1M rows (12 columns), they need up to 10 times the memory of the features and target (14 times for a random forest's scores), so the job first checks 12 times against the worker's free memory and refuses with `payload_too_large`. ReliefF and RReliefF take 50 sampled rows as in Orange and search all rows for their neighbours, so their time grows linearly with the rows. On 1M rows × 12 columns (in-process), each method took 0.2–0.8 s, ReliefF 4.3 s and a random forest of 10 trees 12 s.

### Cloud Discretize and Cloud Purge Domain

Both send a recipe that the machine fits on all rows, through `preprocessed()` as Cloud Preprocess does, so their output is the cloud table with a `("preprocess", recipe)` step that replays.

**Cloud Discretize** (`widgets/owclouddiscretize.py`) is Orange's Discretize for a cloud table: the same list of numeric and time columns (features, target and metas), the same methods and arguments, a general preset and settings per column (`var_hints`, as Orange's), built from the parts of Orange's widget module (its list view and models, `Methods`, `VarHint`); the widget's own code follows Orange's, which can't be subclassed. Orange has no recipe for settings per column, so the widget sends `DiscretizeColumns` (`protocol/recipes.py`, shared by both sides): it discretizes each column with Orange's own discretizers (`Orange.preprocess.discretize`) as Orange's widget calls them, with its method numbers and messages. After the fit, the list shows the cut points of the fitted columns, as Orange's list does for the whole table; columns whose method depends only on the settings (keep, remove, invalid widths or cut points, entropy-MDL without a categorical target) show Orange's notes before that. When every column is kept, the table passes through without a job.

**Cloud Purge Domain** (`widgets/owcloudpurgedomain.py`) has Orange's Purge Domain's options and settings and sends Orange's own `Remove` with them. Its counts of sorted, reduced and removed columns come from comparing the input's columns with the fitted ones (`purge_counts`), the same numbers Orange's `Remove` reports.

The machine checks before fitting any recipe that a table as large as the input's arrays still fits in the worker's memory (`check_fit_memory` in `server/ops/derive.py`; the text in meta columns is shared, so it doesn't count), and refuses with `payload_too_large` otherwise. Measured on 1M rows with 15 features (in-process), discretizing all of them took 0.2–0.5 s per method (entropy-MDL 2.3 s) and Purge Domain 0.4 s, and the result was one copy of the table. `DiscretizeColumns` reports each column and can be cancelled between columns.

### Cloud Categories and Cloud Create Class

Both do on all rows what Orange's Edit Domain and Create Class decide from the data they see, and the placeholder row has none. They show counts from the `values` statistics (`value_counts` in `protocol/recipes.py`, used by `server/ops/stats.py`): a column's values (a categorical column's own, or the sorted distinct values of a numeric or text column, listed up to 100,000), the rows with each, and the order in which they first appear. Both send a recipe from `protocol/recipes.py` through `preprocessed()`, which the machine fits on all rows, so their output is the cloud table with a `("preprocess", recipe)` step. The fitted columns are the machine's own, so the checks on the steps after it accept them, while Edit Domain's own "as categorical" (a `MappingTransform` made on the laptop) and Create Class stay refused.

**Cloud Categories** (`widgets/owcloudcategories.py`) takes a categorical, numeric (not time) or text column, lists its values with their counts and what each becomes, and sends `ColumnCategories`. It repeats what Edit Domain does on the whole table: "as categorical" gives the sorted distinct values as categories, named as the installed Orange names them (Orange 3.41 uses the column's number format, older versions `str`), with Edit Domain's `MappingTransform`; merging gives Edit Domain's `Lookup`, with the modes of its merge dialog. Like the dialog's `Counter`, the modes by count leave out values that no row has, and among equal counts keep the values that appear first. A column with more than 100,000 values isn't made categorical (an error); one that the settings don't change passes through without a job.

**Cloud Create Class** (`widgets/owcloudcreateclass.py`) has Create Class's settings and rules, and its code follows Orange's widget, which can't be subclassed. Create Class keeps a rule only while the rows it sees aren't all matched by the rules above it (`zip(rules, match_counts)`), so on the placeholder only the first rule. `CreateClass` counts the matches as Create Class does, from the counts of the column's values on all rows (on the machine when fitting, and here for the counts next to the rules), keeps the same rules, and makes the class column with Create Class's own classes, which `protocol.pickles.class_by_name` gives: Orange's on the laptop, and on the machine the stand-ins in `server/expressions.py`, which pickle back as Orange's.

Measured on 1M rows (local Docker machine): the `values` statistics of a categorical, a numeric (220,000 values) and a text column (1M values) took 0.1–0.15 s; merging a categorical column 0.1 s, a numeric column as categorical 0.9 s, Create Class on a categorical column 0.1 s and on a text column with 1M values 1.2 s (2.6 s with regular expressions).

### Cloud File and Cloud Data Table

**Cloud File** (`widgets/owcloudfile.py`) loads a CSV or Parquet file (optionally `.gz`) into the machine's memory and outputs a `RemoteTable`. The file is on this computer (uploaded straight to a local machine, or to the provider's storage: the project's storage bucket on Google Cloud, an S3 bucket on AWS, a storage account on Azure; see "Storage bucket and uploads", "S3 storage and uploads" and "Azure Blob storage and uploads"), at an `https://` URL or in the provider's storage, `gs://` on Google Cloud, `s3://` on AWS or `az://<account>/<container>/<path>` on Azure (the machine downloads it; on AWS and Azure through a signed link that it copies to its disk first). The URL check accepts `https://`, `http://` and the schemes of the machine's provider (`storage_schemes`; the URL field's examples come from `storage_examples()`). Before loading, it estimates the memory the file needs from the file's size and its first rows (`data/estimate.py`): rows × columns × 8 bytes, more for text columns with many values, `.gz` assumed 4× compressed, plus 3–5× working memory. If that's more than the machine has, it warns, suggests the smallest size that fits (`client/providers/sizes.py`) among the sizes of the machine's provider, or of the one chosen in the Cloud settings when the machine has none (a local machine), and a boot disk, and loads only after "Load anyway". Estimates for `https://` and `gs://` files are made when Load is clicked. An `https://` download may take as long as it needs; only a connection that sends nothing for 5 minutes fails (`remote_options` in `server/sources.py`: aiohttp, which fsspec uses, otherwise stops every request after 5 minutes). An `https://` CSV file is read as it arrives; an `https://` Parquet file, which is read with many requests, is first copied to the machine's disk with one request (`copy_to_disk`), as is any file the request asks to copy first.

**Several files as one table:** a URL with a numbered pattern, e.g. `…/yellow_tripdata_2025-{01..12}.parquet`, or a list in braces, `…/{north,south}.csv`, names several files with the same columns (`widgets/file_sources.py`: `expand_pattern`, at most `protocol.tables.MAX_FILES`). Cloud File expands it, estimates each file and adds the estimates up (`combine_estimates`), turns each file into a location the machine reads (`machine_location`, so each gets its own signed link on AWS and Azure), and sends the list as `uris` in `POST /v1/tables`, with the pattern's file name as the table's name. A pattern rather than a list of URLs, because it fits in the one URL field and in the workflow's one URL setting, it stays short for many files, and the files of a series (months, parts) are numbered anyway. On the machine, the loader (`server/loader.py`) reads the files one after the other into the same columns, with a reader per format (`server/csv_file.py`, `server/parquet_file.py`): the first file decides the columns; later files' columns are matched by name (or by the same name in other letter case), their types may differ in width or unit, columns missing from a file are missing values in its rows, extra columns are skipped, and either gives one warning per column. `docs/api-v1.md` has the rules and the errors.

Files with Orange's three header rows (names, types, flags: `.tab` files, and CSV files saved by Orange) are read on the machine as Orange's File widget reads them (`server/orange_header.py`, used by the CSV reader in `server/csv_file.py`), and the estimate doesn't count those rows. A CSV file whose first line holds only numbers has no header row, as in Orange: its columns are named `Feature 1`, `Feature 2`, … (HIGGS is such a file). Column roles and types are edited in Orange's own domain editor (renaming is off). Apply loads the file again with the new roles and types as `ColumnSpec` overrides; an uploaded file stays on the machine (or in the bucket), so it isn't uploaded again, and the previous table is deleted first. The workflow saves the source, the column roles and the table's ID; when the workflow opens while the same machine still runs, the table is reused instead of loaded again.

**Cloud Data Table** (`widgets/owclouddatatable.py`) shows a page of rows at a time (`GET /v1/tables/{id}/rows`), with the exact row and column counts. Clicking a column header sorts all rows on the machine.

To try them without Orange's canvas (start the machine with "Cloud settings…" first):

```sh
.venv/bin/python -m orangecontrib.cloud.widgets.owcloudfile
```

### Cloud Learner

**Cloud Learner** (`widgets/owcloudlearner.py`) trains any Orange learner (from Orange's learner widgets, with attached preprocessors) on a cloud table. It sends the pickled learner and the table's ID as a `fit` job (`server/ops/learn.py`); the model stays on the machine and goes out as a `CloudModel` (`data/cloud_model.py`: the model's object ID, name, the training data's domain, learner, rows, machine and download size), which only Cloud widgets accept. scikit-learn learners that use one core by default (Orange's random forests) use all the machine's cores; the model is the same. Training again deletes the replaced model on the machine.

"Download model" downloads the real Orange model (`GET /v1/objects/{id}`) to the second output, "Model (local)", for Orange's own widgets such as Tree Viewer. The widget shows the model's size first and asks before downloading more than 100 MB: some models contain the training data (k-nearest neighbours, and Orange's trees, which keep the training rows for Tree Viewer's selections). On 1M rows with 4 columns, a random forest of 10 trees is about 425 MB, a tree about 69 MB and k-nearest neighbours about 60 MB.

### Cloud Test & Score

**Cloud Test & Score** (`widgets/owcloudtestandscore.py`) cross-validates learners (2 to 20 folds, stratified or not) or tests them on a second cloud table (Test Data). It sends the pickled learners and the tables' IDs as an `evaluate` job (`server/ops/evaluate.py`). The folds are the same as in Orange's Test & Score (`CrossValidation(k, stratified, random_state=42)`), so the scores are the same as Orange's on the same rows.

On the machine, every learner × fold is a part, its own Dask task in the worker that holds the table (`server/ops/parts.py`), so the parts share the table without copying it. scikit-learn learners train in the worker's threads. Learners that aren't scikit-learn's, such as Orange's own tree, hold Python's GIL while they train, so threads wouldn't speed them up: on Linux (the machine) their parts run in processes forked from the worker, which share the table copy-on-write (`server/forked.py`). On other systems at most two of them run at a time, in threads.

Parts run side by side as far as the worker's threads and memory allow. For a learner in threads, its first part runs alone with all cores while the machine measures the memory it needs; its other parts count with that measurement plus 10%. A forked process's own memory is measured while it runs, so those parts start together, counted at twice the table's size until one of them uses more. If the processes together use more than the memory left, the one started last is stopped and runs again later with fewer at a time (results don't change); a part that runs out of memory on its own fails the job with `out_of_memory`, as before. Each part's learner gets the remaining cores (`n_jobs` of scikit-learn learners whose default is one core or all of them; BLAS and OpenMP are limited the same way), so the parts don't compete for the CPUs. Cancelling the job stops forked parts at once; parts in threads finish first. The widget shows how many parts ran at a time; the job's values `parallel`, `threads_per_part`, `part_memory_bytes`, `processes` and `parts_run_again` say how, and the param `max_parallel` limits the parts at a time (for comparisons).

Measured on Google Cloud, Medium (`n2-standard-16`), 1M rows with 30 numeric columns: 5-fold cross-validation of Orange's tree (unlimited depth) took 120 s with the 5 parts in forked processes, against 560 s one part at a time (`max_parallel` 1), with the same predictions. On 20M rows, Random Forest ran 2 parts at a time (each counted at 23 GB) and took 1,341 s, at a peak of 44 of 63 GiB.

Why it's safe to fork the worker, a process with many threads: the worker forks only once per job, before the job's parts start, a single-threaded "zygote" that forks one child per part. The children use one OpenMP thread, write their result to a file, and end without running any of the worker's clean-up. They die with the worker, are the first the system's out-of-memory killer stops (so the worker keeps its tables), and are killed together when the job ends.

The scores table (Orange's own, with its column menu) is computed on the machine on all rows with `Orange.evaluation.scoring`: CA, AUC, F1, Precision, Recall and the others Orange offers for classification, MSE, RMSE, MAE, R² and the others for regression. Scores for each target class are computed in the same job, so changing the target class doesn't run it again. A learner that fails is marked "(error)" and left out of the output, as in Orange.

The Evaluation Results (Orange's `Results`, a prediction for every row and learner) are assembled and kept on the machine. When they're 100 MB or less, they're downloaded right away and go to the output for Orange's ROC Analysis, Confusion Matrix and similar widgets (the data stays on the machine, so `results.data` is a local table with only the target column, which Confusion Matrix needs); larger results wait for "Download results", which shows their size and asks first. On 1M rows two learners' results are about 64 MB.

Downloads of stored objects (results, models) show their progress and can be cancelled. The machine pickles the object once into a file and streams that file (`server/objects.py`). It used to send the pickle as one response body, which broke large downloads over slow connections: uvicorn hands the whole body to the TLS transport at once, closes the connection after its keep-alive timeout (5 s) as if it were idle, and asyncio drops what is still unsent after its TLS shutdown timeout (30 s), so the download stopped after about 35 s. The file is served with `Range` support, so a download that breaks off for another reason continues where it stopped instead of starting again (`CloudClient.get_object`).

### Cloud Predictions

**Cloud Predictions** (`widgets/owcloudpredictions.py`) predicts a cloud table (Data) with cloud models (Predictors, from Cloud Learner). It sends the table's and the models' IDs as a `predict` job (`server/ops/predict.py`). Each model predicts every row, on the data without its target, as in Orange's Predictions, in blocks of rows so that memory stays bounded on big tables.

The output, Predictions, is a new cloud table that stays on the machine: the data plus, for each model, a prediction column and, for a categorical target, a probability column for each class value ("tree (yes)"), added as meta attributes like the output of Orange's Predictions. It shares the data's columns on the machine instead of copying them. View it with Cloud Data Table. Predicting again deletes the replaced table on the machine.

When the data has the target the models predict, the scores table shows their scores on all rows with a known target, computed on the machine with `Orange.evaluation.scoring` as Orange's Predictions computes them (for a target class, the scorers' target, not one against the rest as in Test & Score). Models that predict another target get no scores and a warning; a model that fails is marked "(error)" and left out of the output.

On the local machine (8 CPUs), predicting 1M rows with logistic regression and a random forest of 10 trees took 19 s (the forest 12 s); the output table was about 420 MB, mostly the meta columns, which Orange keeps as Python objects when the data has a text column.

### Cloud k-Means

**Cloud k-Means** (`widgets/owcloudkmeans.py`) clusters a cloud table with the options of Orange's k-Means widget: a fixed number of clusters or a range, normalizing, initialization, re-runs and maximum iterations. It sends a `kmeans` job (`server/ops/kmeans.py`), which prepares the data and runs `Orange.clustering.KMeans` exactly as Orange's widget does (same preprocessing, same seed), so the clusters are the same.

Silhouettes are always computed on all rows on the machine, never on a sample:

- up to a row limit (the widget's "Exact silhouette up to" setting, default 100,000): the exact silhouette, the numbers of scikit-learn's `silhouette_samples` that Orange's widget uses. The distances between rows don't depend on k, so the machine computes them once, in chunks, for every k in the range;
- above it: the simplified silhouette (each row's distance to its own cluster's centre against the nearest other centre), on all rows. Its numbers differ from the exact silhouette, so the scores table and the output column are named "Simplified silhouette", and the widget says so.

(Orange's own widget computes the exact silhouette up to 5,000 rows; above that its score per k comes from a 5,000-row sample and the silhouette column is empty.)

For a range of k, the scores table shows the mean silhouette per k and selects the best; selecting another k sends that clustering without a new job. The Data output is a cloud table on the machine (the data plus "Cluster" and "Silhouette" meta columns, scaled as in Orange: `arctan(s) / pi + 0.5`); the Centroids output is a small local table, as in Orange. Unlike Orange's, the cluster column has no `compute_value` (Orange's holds the model): after the round trip to the machine it must stay equal to itself, so that steps recorded on the output (Select Columns, Select Rows, ...) replay. Clustering again deletes the replaced tables on the machine.

The exact silhouette grows with the square of the rows: on the local machine (8 CPUs) one pass over 100,000 rows takes about 30-50 s, for all k together.

### Cloud t-SNE

**Cloud t-SNE** (`widgets/owcloudtsne.py`) embeds a cloud table in two dimensions with the options of Orange's t-SNE widget: normalizing, PCA preprocessing and its number of components, initialization (PCA or spectral), distance metric, perplexity, "Preserve global structure" and exaggeration. As in Orange, new data starts the embedding and changed options wait for "Start" (which becomes "Stop" while it runs). It sends a `tsne` job (`server/ops/tsne.py`), which takes the steps of Orange's widget with `Orange.projection.TSNE` and the widget's seed: t-SNE's preprocessing, normalizing, PCA, the nearest neighbours, the initial positions, then 750 iterations in steps of 25, reporting progress (and the KL divergence) after each step. The widget doesn't draw the map: its output, the data plus "t-SNE-x" and "t-SNE-y" meta columns, stays on the machine; view it with Cloud Scatter Plot (choose t-SNE-x and t-SNE-y as the axes). As for Cloud k-Means, the new columns have no `compute_value`, so steps recorded on the output replay. Running again deletes the replaced table on the machine.

openTSNE uses all the worker's threads (Orange's widget uses one). Up to about 1,000 rows openTSNE finds the exact nearest neighbours and the embedding is the same as Orange's on the same platform; above that it uses approximate neighbours, which depend on the number of threads, so the map is an equally good one but not the same numbers. The numbers also differ slightly between macOS and Linux builds of openTSNE (t-SNE magnifies tiny floating-point differences), so compare the cloud machine with Orange on Linux.

Before the job starts, the widget estimates the runtime from the rows, the perplexity and the machine's CPUs (`rough_runtime`) and shows a warning when it's a minute or more. It's rough: about 1.4 ms per row on 8 CPUs at perplexity 30. On the local Docker machine (8 CPUs) 100,000 rows took 147 s: 16 s for the neighbours, 130 s for the iterations.

### Cloud Distances

**Cloud Distances** (`widgets/owclouddistances.py`) computes the distances between the rows or the columns of a cloud table with the options of Orange's Distances widget: its metrics (Euclidean and Manhattan also normalized), and the same settings, so they mean the same. It sends a `distances` job (`server/ops/distances.py`), which checks and fixes the data as Orange's widget does (the same errors and warnings) and calls the same `Orange.distance` metric, so the matrix is Orange's. Orange's widget stops at 20,000 rows; here the limit is the machine's memory. The output is a `CloudDistances` (`data/cloud_distances.py`): a reference to the `DistMatrix` kept on the machine, with its size, metric, axis, source table and, between columns, the column names. Cloud widgets that take distances pass its object ID to their jobs; Orange's own widgets can't take it. Computing again deletes the replaced matrix on the machine.

A matrix of n rows takes n × n × 8 bytes (50,000 rows: 18.6 GB), and computing it takes more: from 1.2 times that (Manhattan) to 5.1 times (Spearman), measured on Orange's code (`DISTANCE_METRICS` in `protocol/memory.py`). Before the job, the widget compares that, plus copies of the data, with 90% of the machine's free memory (`/v1/info`) and refuses with a message that names the smallest machine size that fits (and tells the size picker in Cloud settings). The job checks again on the machine, against what the worker may still use, so a direct API call can't make the worker run out of memory.

On the local Docker machine (8 CPUs, 7.7 GiB), Euclidean (normalized) distances between 15,000 rows took 9.8 s; 50,000 rows are refused. On Google Cloud's "Medium, high memory" (`n2-highmem-16`: 16 CPUs, 128 GB), 50,000 rows took 51 s (Euclidean, normalized) and 25 s (Manhattan); the matrix keeps 18.6 GB of the machine's memory until it is replaced or deleted.

### Cloud Hierarchical Clustering

**Cloud Hierarchical Clustering** (`widgets/owcloudhierarchicalclustering.py`) clusters the distances that Cloud Distances keeps on the machine, with the options of Orange's Hierarchical Clustering widget: the linkage (single, average, weighted, complete or Ward) and a cut, by the number of top clusters ("Top N") or by a share of the tree's height ("Height ratio"). Orange's manual selection in the dendrogram isn't offered. It sends an `hclust` job (`server/ops/hclust.py`), which does what Orange's widget does: SciPy's `linkage` on the matrix's upper triangle, Orange's tree (`tree_from_linkage`) and its cuts (`top_clusters`, or the clusters below the height), with the clusters numbered C1, C2, ... in the dendrogram's order. So the clusters are Orange's, and the same as SciPy's `fcluster` with `maxclust` or `distance`.

Between rows, the output is a cloud table: the table the distances were computed from, with a "Cluster" meta column that, as for Cloud k-Means, has no `compute_value`, so steps recorded on it replay. (Orange's widget outputs the matrix's own copy of the data, in which Distances may have moved categorical columns to the metas and imputed missing values.) Between columns, the output is a small local table with each column's name and cluster. The widget draws the top of the dendrogram (its top 50 clusters, or as many as the cut has, up to 200) with the cut's clusters coloured, and lists the clusters' sizes. The machine keeps the linkage (a small object), so changing only the cut doesn't compute it again; running again deletes the replaced table, and new distances or another linkage delete the old linkage.

The linkage needs the matrix's upper triangle, n (n - 1) / 2 × 8 bytes (half the matrix: 9.3 GB for 50,000 rows), and SciPy works on a copy of it except for single linkage (measured: 1.13 times the triangle at the peak, 0.13 for single). The job checks 2.2 times the triangle (1.2 for single) against what the worker may still use, besides the matrix itself, and refuses with a clear message before computing.

On Google Cloud's "Medium, high memory" (`n2-highmem-16`: 16 CPUs, 128 GB), with the Euclidean (normalized) distances between 50,000 rows (56 s) on the machine, SciPy's linkage took 174-182 s for Ward, average, complete and weighted linkage and 26 s for single linkage (SciPy computes every linkage on one CPU); each job took about 50 s more, mostly to check the matrix and copy its upper triangle. Cutting again with the kept linkage took 2 s.

### Cloud MDS

**Cloud MDS** (`widgets/owcloudmds.py`) places a cloud table's rows, or the items of the distances that Cloud Distances keeps on the machine, in two dimensions, with the options of Orange's MDS widget: the initial positions (PCA, which is Torgerson's classical MDS, or random) and the maximum number of iterations (the setting `max_iter`, default 300). It sends an `mds` job (`server/ops/mds.py`), which does what Orange's widget does: from a table, Euclidean distances after `Orange.projection.MDS`'s preprocessors (continuize, impute); then `Orange.projection.MDS` (scikit-learn's SMACOF) in steps of 25 iterations (the widget's default refresh rate), each starting from the last positions, until the maximum number of iterations or until the stress stops improving by the widget's rule. So the positions are those of Orange's widget (the tests compare them, and Kruskal's stress, on 600 rows). The steps give the progress and the chances to stop. Random positions are seeded (Orange's are not). Orange's widget always starts new data from PCA and re-initializes with its PCA, Randomize and Jitter buttons; Cloud MDS uses the chosen initialization for each run and has no Jitter or Resume, since it doesn't draw the map.

The output is a cloud table: the data (with distances, the table they were computed from) plus "mds-x" and "mds-y" meta columns without `compute_value`, so steps recorded on it replay; view it with Cloud Scatter Plot. With both inputs, the distances are embedded and the table must have as many rows. Between columns, the output is a small local table of the column names ("labels") and their positions, as in Orange. As in Orange, new inputs start the optimization and changed options wait for Start; before a run that may take more than a minute (`rough_runtime`: max iterations × n² × 6.5 ns, whatever the number of CPUs: SMACOF works mostly on one CPU) the widget warns.

SMACOF works with about 5.5 matrices of n × n × 8 bytes besides the distances (measured peak). The job checks 6 such matrices (7 and copies of the data when it computes the distances from a table; `mds_memory_bytes` in `protocol/memory.py`) against what the worker may still use and refuses with a clear message before computing; the widget checks the machine's free memory first and names a machine size that fits. 20,000 rows need about 18 GB (21 GB from a table), more than the local Docker machine has.

On Google Cloud's "Medium, high memory" (`n2-highmem-16`: 16 CPUs, 128 GB), MDS of 20,000 rows from a cloud table took 569 s (distances 7 s, PCA initialization 3 s, 12 steps of SMACOF 554 s; Kruskal stress 0.22), and from Cloud Distances' Euclidean (normalized) matrix with random initial positions 786 s (stress 0.33). The steps ran to the maximum of 300 iterations by the widget's rule (each stops early by scikit-learn's own), and SMACOF works mostly on one CPU, so a bigger machine doesn't make it faster.

### Cloud viewers

**Cloud Feature Statistics**, **Cloud Distributions**, **Cloud Box Plot** and **Cloud Scatter Plot** (`widgets/owcloud*.py`) show a cloud table with numbers that the machine computes on all its rows (`POST /v1/tables/{id}/stats`, `server/ops/stats.py`), never from the placeholder row. Each kind of statistics computes what Orange's own widget computes, the same way: `basic` (Feature Statistics: missing, min, max, mean, median, mode, dispersion), `distribution` (Distributions: counts per value or per bin, with the binnings Orange's widget offers, split by a categorical column, and the fitted curves), `quantiles` (Box Plot: count, extremes, mean, variance and midpoint quartiles per group), `histogram2d` (a scatter plot's density) and `values` (a column's values and the rows with each, for Cloud Categories and Cloud Create Class). Columns are indices into the Orange domain. The worker keeps the last 64 answers per table and request.

They subclass `OWCloudViewer` (`widgets/viewer.py`): `set_cloud_data(data)` takes the input (and shows the error for local data), and `fetch_stats({name: StatsRequest}, on_done, rows_up_to=0)` resolves the table in the worker thread, asks for the statistics and gives a `ViewerResult` (`info`, `stats`, and `rows` when the table has at most `rows_up_to` rows) to `on_done`. The status line starts with "From all N rows."

- **Cloud Feature Statistics** uses Orange's own Feature Statistics model and view: the model sets up its columns from the placeholder, and the machine's numbers replace its arrays. So sorting and the "Statistics" output are Orange's; "Reduced Data" sends the selected columns as a recorded step.
- **Cloud Distributions** draws the bars for the chosen bin width (the slider offers Orange's binnings, computed from all rows), split by a categorical column, stacked, as probabilities or cumulative, and the fitted curve (normal, beta, gamma, Rayleigh, Pareto, exponential or kernel density) fitted on the machine. Its "Histogram Data" output has the form of Orange's.
- **Cloud Box Plot** draws one box per group for a numeric column, or the counts per group for a categorical one, and writes Orange's test (Student's t, ANOVA or χ²), computed from those numbers.
- **Cloud Scatter Plot** draws every point when the table has at most 20,000 rows (fetched whole with `GET /rows`), and otherwise the density on a grid (`histogram2d`, 100×100 by default); with a colour column, each cell takes the colour of its most common value.

On 1,000,000 rows (in-process machine and the local Docker machine) each viewer's first answer took under a second: Feature Statistics 0.9 s, Distributions 0.1–0.2 s, Box Plot 0.1 s, Scatter Plot 0.2–0.3 s. Fitting a beta curve to 1M rows takes about 10 s (gamma 4 s, the others under 0.5 s), so stats requests wait up to 10 minutes.

## Demo workflow

`examples/local-demo.ows` shows the whole stack on the local machine:

- Cloud File → Cloud Test & Score ← Random Forest and Logistic Regression → ROC Analysis: 5-fold cross-validation of both learners on all rows, on the machine; the Evaluation Results come back for Orange's own ROC Analysis.
- Cloud File → Cloud Learner ← Logistic Regression → Cloud Predictions → Cloud Data Table: the model is trained and kept on the machine, predicts every row there, and Cloud Data Table shows pages of the predictions.

Its data isn't in the repository. Make it first; it's a synthetic table of 1,000,000 rows (eight numeric columns `x1`–`x8`, a categorical `group` and the target `outcome`), about 24 MB gzipped, written next to the workflow as `examples/demo-data.csv.gz` (git-ignored):

```sh
.venv/bin/python scripts/make-demo-data.py              # --rows and --out change the size and place
scripts/run-orange.sh examples/local-demo.ows
```

Then click "Cloud settings…" in any Cloud widget and start the local machine. Cloud File loads the file as soon as the machine is connected (a relative path in Cloud File is relative to the workflow's folder), and everything after it runs by itself. The workflow saves only the file's path and its column roles (`outcome` is the target), no connection settings.

On the local machine (8 CPUs, 7.7 GiB), with the machine already running, the whole workflow takes a little over a minute: Cloud File uploads and loads the file in about 4 s, Cloud Test & Score cross-validates both learners in about 45 s (AUC about 0.83 for the random forest and 0.77 for logistic regression), Cloud Learner trains in about 1 s and Cloud Predictions predicts and scores all rows in about 4 s. The Evaluation Results of two learners on 1M rows are about 64 MB, below the 100 MB that needs a confirmed download, so ROC Analysis gets them without asking.

`tests/test_demo.py` checks the workflow file and runs the same operations through the API on a small table.

On Google Cloud, with a Small machine (`e2-standard-4`, 4 vCPUs) in `us-central1`, the workflow takes about two and a half minutes after the machine is connected (Cloud Test & Score about 1 min 40 s), and starting the machine takes 75–90 s. [gcp-checklist.md](gcp-checklist.md) has the manual test steps on Google Cloud, and `TestRealDemo` in `tests/test_demo.py` (with `ORANGE3_CLOUD_GCP_VM_TESTS=1`) runs the workflow there, waits for the idle shutdown and checks that nothing is left.

On AWS, with a Small machine (`m7i.xlarge`, 4 vCPUs) in `us-east-1`, starting the machine takes about a minute, and the workflow about two minutes after the machine is connected: Cloud Test & Score computes for about a minute, and its 64 MB of evaluation results download in 20–40 s on a connection that reads `us-east-1` at 2–3 MB/s. Orange's canvas updates one widget at a time, so Cloud Learner waits for that download. In one run of three the download took 2.5 minutes instead, as once on Google Cloud; it didn't happen again. [aws-checklist.md](aws-checklist.md) has the manual test steps on AWS, and `TestRealAwsDemo` in `tests/test_demo.py` (with `ORANGE3_CLOUD_AWS_VM_TESTS=1`) runs the workflow there and waits for the idle shutdown, then checks that a machine stopped while Orange was closed stays `stopped` until Orange opens again and deletes it, and that nothing is left.

On Azure, with a Small machine (`Standard_D4s_v6`, 4 vCPUs) in `eastus`, starting the machine takes about a minute and a half (cloud-init installs Docker first), and the workflow a little over two minutes after the machine is connected: Cloud File uploads the file to the storage account and loads it in under 30 s, Cloud Test & Score computes for about a minute, and its evaluation results download in under a minute. After the idle timeout the machine deallocates its own VM, and the session notices within a minute and deletes it. [azure-checklist.md](azure-checklist.md) has the manual test steps on Azure, and `TestRealAzureDemo` in `tests/test_demo.py` (with `ORANGE3_CLOUD_AZURE_VM_TESTS=1`) runs the workflow there, waits for the idle shutdown, checks that the VM deallocated itself before the session deleted it, and that nothing of the machine is left; `test_real_machine_deallocates_itself` in `tests/test_azure_provider.py` checks a machine that stops while Orange is closed.

`examples/higgs-demo.ows` is the larger demo: Cloud File makes the machine download the HIGGS dataset (11 million rows, a 2.6 GB `.csv.gz`) from its URL, and Cloud Test & Score cross-validates Random Forest and Logistic Regression on all rows. It needs a cloud machine (Medium or larger); [demo-higgs.md](demo-higgs.md) has the steps and the timings against a laptop, and `TestRealHiggs` in `tests/test_demo.py` (with `ORANGE3_CLOUD_GCP_HIGGS=1`) runs it on Google Cloud in about 22 minutes.

`examples/taxi-demo.ows` is the demo on data most people know: every yellow taxi trip in New York City in 2025 (48.7 million), which Cloud File reads from the TLC's twelve monthly Parquet files with one URL pattern. Orange's own Feature Constructor, Select Rows and Select Columns change the data in four branches, and Cloud Distributions, Cloud Scatter Plot, Cloud Box Plot, Cloud k-Means and Cloud Test & Score look at it and model it. `examples/taxi-demo-january.ows`, for a Small machine, is the same workflow with January's file as Cloud File's URL, and the workflow's column settings kept for it (Cloud File keeps them only for the URL they were made for); `test_january_workflow_is_the_same_on_one_month` checks that their widgets, links and settings are the same apart from that, so change both together. [demo-taxi.md](demo-taxi.md) has the steps, the timings against a laptop and screenshots; `TestTaxiWorkflow` in `tests/test_taxi_demo.py` runs the workflow on two months of made-up trips on the in-process machine, and `TestRealTaxi` (with `ORANGE3_CLOUD_TAXI=gcp`, `aws` or `azure`) runs it on a cloud machine: the whole year on a Medium machine on Google Cloud, January on a Small machine on AWS and Azure.

## Run the API server directly

The server that runs on the cloud machine can also run straight from `.venv`, which is handy for trying the API:

```sh
export ORANGE3_CLOUD_TOKEN=$(openssl rand -hex 32)
.venv/bin/python -m orangecontrib.cloud.server --port 8443
curl -H "Authorization: Bearer $ORANGE3_CLOUD_TOKEN" http://127.0.0.1:8443/v1/info
```

The token comes from the `ORANGE3_CLOUD_TOKEN` environment variable, or from `--token`, which other users of the computer can see in the process list. `--tls-cert cert.pem --tls-key key.pem` serves HTTPS. Jobs run on a local Dask cluster that the server starts: one worker process with a thread per CPU and a memory limit of the machine's memory minus a small reserve; `--worker-threads 2 --worker-memory 2GB` makes it smaller. `POST /v1/shutdown` stops the server, and so do the idle timeout and the maximum lifetime (`--idle-timeout`, `--max-lifetime`, in seconds; `0` turns them off). Run `python -m orangecontrib.cloud.server --help` for all options.

## Tests

The tests are a few smoke tests in `tests/`. The style check is [Ruff](https://docs.astral.sh/ruff/), and [Import Linter](https://import-linter.readthedocs.io/) checks which modules may import which (the architecture rules in [AGENTS.md](../AGENTS.md)). Both are configured in `pyproject.toml` and installed with the `dev` extra, like pytest. From the repository root:

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .     # .venv/bin/ruff format . fixes it
.venv/bin/lint-imports
QT_QPA_PLATFORM=offscreen .venv/bin/pytest
```

Ruff also checks the size of functions (complexity at most 12, at most 6 arguments) and that errors aren't swallowed. A broad `except Exception` is allowed only at a boundary that reports the error (the job runner, API handlers, widget callbacks), marked `# noqa: BLE001 - <why>`. The import contracts name their few exceptions (`ignore_imports`), each with its reason.

`QT_QPA_PLATFORM=offscreen` lets the tests that create Orange widgets run without opening windows. On macOS you can leave it out.

Slow tests, such as loading a table of a million rows and checking its memory use, are marked `slow` and skipped by default (CI skips them too). Run them with:

```sh
.venv/bin/pytest -m slow -s
```

CI (`.github/workflows/ci.yml`) runs the same four commands on Ubuntu on every push and pull request, with Python 3.10, the oldest version the add-on supports, so code that needs a newer Python fails there. Python 3.10 also gets an older Orange (the newest that supports it, 3.39.0 in November 2026), so CI covers that too. To reproduce CI locally, use a throwaway venv outside the repository:

```sh
uv venv --python 3.10 /tmp/venv310
VIRTUAL_ENV=/tmp/venv310 uv pip install orange3 PyQt6 -e ".[dev,server]"
/tmp/venv310/bin/ruff check . && /tmp/venv310/bin/ruff format --check . && /tmp/venv310/bin/lint-imports && QT_QPA_PLATFORM=offscreen /tmp/venv310/bin/pytest
```

Qt on Ubuntu also needs a few system libraries even when it runs offscreen: `libegl1`, `libgl1`, `libxkbcommon0`, `libfontconfig1` and `libdbus-1-3`.

A weekly check (`.github/workflows/weekly.yml`, Mondays, or by hand with `gh workflow run weekly.yml`) runs the tests with the newest Orange release on the newest Python it has wheels for. When it fails, it opens an issue "Breaks with Orange X.Y", or comments on it if that issue is already open.

Some of the add-on's code follows Orange's own code, because Orange's widgets can't be subclassed or need Qt: the Cloud widgets made from Orange's (Cloud Discretize, Cloud Categories, Cloud Create Class, Cloud Rank, Cloud Purge Domain), the recipes in `protocol/recipes.py`, the `rank` job and the machine's stand-ins for classes of Orange's widget modules. `tests/test_orange_copies.py` names the Orange functions and classes each copy follows, with a fingerprint of their source in the Orange version the copies follow (3.41; older versions aren't compared). When a newer Orange changes one, the test fails in the weekly check and names the copy: update it, then record the new fingerprints with `python tests/test_orange_copies.py`. Dependabot (`.github/dependabot.yml`) proposes updates to the GitHub Actions, the Python dependencies and the machine image's base every week.

## Baseline benchmark

`scripts/baseline.py` measures where Orange on one computer reaches its limits. It generates synthetic CSV files (30 numeric columns and a class) of 1, 5 and 20 million rows in a temporary folder, and runs two operations with Orange's own library on each: loading the file the way the File widget does, and Random Forest with 5-fold cross-validation. Each operation runs in a child process, which records its time and peak memory and is stopped after 30 minutes or when memory runs out. An operation that fails at one size isn't tried at bigger sizes, and the generated files are deleted at the end.

```sh
.venv/bin/python scripts/baseline.py --out results.json
.venv/bin/python scripts/baseline.py --sizes 10k --timeout 60   # quick check
```

Run `scripts/baseline.py --help` for all options. The 20-million-row file needs about 5 GB of free disk space.

`--backend cloud` runs the same operations on a cloud machine through the add-on. The script starts the machine at the first operation and deletes it at the end. `load` loads the file with the machine's loader, and `rf_cv` evaluates the same learner on the loaded table with an `evaluate` job, whose folds run in parallel. The cloud files have plain CSV headers, only the column names, and the class column is set when loading. The time is the job's run time on the machine. The peak memory is the most memory in use on the whole machine during the job, sampled from `GET /v1/info`. The cost is the run time multiplied by the machine's hourly price. On Google Cloud the files go to the project's bucket first. With `--data-dir`, the generated files are kept, so a second run reuses them and their uploads. `--delete-uploads` deletes the uploads at the end.

```sh
# Google Cloud: the project and zone from the environment, else from the Cloud settings
ORANGE3_CLOUD_GCP_PROJECT=... ORANGE3_CLOUD_GCP_ZONE=... .venv/bin/python scripts/baseline.py \
    --backend cloud --provider gcp --machine-type n2-standard-16 --data-dir ~/baseline-data
.venv/bin/python scripts/baseline.py --backend cloud --provider local --sizes 20k   # local Docker
```
