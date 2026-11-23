"""The user's Google Cloud account: the ``gcloud`` tool, signing in, and the
projects, regions and zones the user can choose.

The add-on signs in with the ``gcloud`` tool's application-default
credentials (ADC): "Sign in with Google" runs
``gcloud auth application-default login`` (:func:`sign_in`, through the
provider's ``sign_in`` and the session), and :func:`load_credentials`
reads the result with :func:`google.auth.default`. The add-on never sees or
stores the user's password or tokens.

Every function here may be slow (network, or a login shell to find
``gcloud``), so the settings dialog calls them off the GUI thread. They raise
:class:`~orangecontrib.cloud.client.providers.base.ProviderError` (or a
subclass) with a message for the user. Google's client libraries are
imported only when needed.
"""

from __future__ import annotations

import configparser
import contextlib
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.client.providers.cli_tools import find_tool, run_sign_in

__all__ = [
    "CLOUD_PLATFORM_SCOPE",
    "DEFAULT_REGION",
    "DEFAULT_ZONE",
    "GCLOUD_INSTALL_URL",
    "ApiDisabledError",
    "GcloudDefaults",
    "NotSignedInError",
    "Project",
    "Region",
    "account_email",
    "available_machine_types",
    "find_gcloud",
    "gcloud_defaults",
    "list_projects",
    "list_regions",
    "load_credentials",
    "pick_region",
    "pick_zone",
    "sign_in",
    "sign_in_command",
]

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GCLOUD_INSTALL_URL = "https://cloud.google.com/sdk/docs/install"

#: Used when the user's ``gcloud`` configuration has no region or zone.
DEFAULT_REGION = "us-central1"
DEFAULT_ZONE = "us-central1-a"


class NotSignedInError(ProviderError):
    """No application-default credentials, or they no longer work."""


class ApiDisabledError(ProviderError):
    """The API that a call needs isn't enabled in the project."""


# ----------------------------------------------------------------------
# The gcloud tool


def _gcloud_names() -> tuple[str, ...]:
    return ("gcloud.cmd", "gcloud") if sys.platform == "win32" else ("gcloud",)


def _known_gcloud_paths() -> list[Path]:
    """Where the Google Cloud SDK installers and package managers put gcloud."""
    home = Path.home()
    name = _gcloud_names()[0]
    paths = [
        home / "google-cloud-sdk" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/opt/homebrew/share/google-cloud-sdk/bin") / name,
        Path("/usr/local/bin") / name,
        Path("/usr/local/share/google-cloud-sdk/bin") / name,
        Path("/usr/lib/google-cloud-sdk/bin") / name,
        Path("/usr/bin") / name,
        Path("/snap/bin") / name,
    ]
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            paths.append(Path(base) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / name)
    return paths


def find_gcloud(*, login_shell: bool = True) -> str | None:
    """The path of the ``gcloud`` tool, or ``None`` if it isn't installed.

    Looks on ``PATH``, in the usual install locations and, unless
    ``login_shell`` is false, on the PATH of the user's login shell.
    """
    return find_tool(_gcloud_names(), _known_gcloud_paths(), login_shell=login_shell)


def sign_in_command(gcloud: str) -> list[str]:
    """The command that signs the user in: it opens Google's sign-in page in
    the browser and saves the application-default credentials."""
    return [gcloud, "auth", "application-default", "login"]


def sign_in(gcloud: str, cancel: threading.Event | None = None, *, poll: float = 0.2) -> str:
    """Run :func:`sign_in_command` and wait until it ends; return its output.

    The user signs in in the browser, which can take minutes, so call it off
    the GUI thread. Setting ``cancel`` kills the process (and what it
    started) and raises :class:`SignInCancelledError`. Raises
    :class:`ProviderError` if gcloud can't start or doesn't finish.
    """
    return run_sign_in(
        sign_in_command(gcloud),
        cancel,
        tool="gcloud",
        install_hint="Is the Google Cloud CLI installed?",
        poll=poll,
    )


@dataclass(frozen=True)
class GcloudDefaults:
    """Project, region and zone from the user's active ``gcloud``
    configuration (empty if not set)."""

    project: str = ""
    region: str = ""
    zone: str = ""


def _gcloud_config_dir() -> Path:
    if os.environ.get("CLOUDSDK_CONFIG"):
        return Path(os.environ["CLOUDSDK_CONFIG"])
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "gcloud"
    return Path.home() / ".config" / "gcloud"


def gcloud_defaults() -> GcloudDefaults:
    """Read the active ``gcloud`` configuration's project, region and zone
    from its files (no need to run gcloud), with gcloud's environment
    overrides (``CLOUDSDK_CORE_PROJECT``, ``CLOUDSDK_COMPUTE_REGION``,
    ``CLOUDSDK_COMPUTE_ZONE``)."""
    folder = _gcloud_config_dir()
    name = os.environ.get("CLOUDSDK_ACTIVE_CONFIG_NAME", "")
    if not name:
        try:
            name = (folder / "active_config").read_text().strip()
        except OSError:
            name = ""
    config = configparser.ConfigParser(interpolation=None)
    with contextlib.suppress(configparser.Error):
        config.read(folder / "configurations" / f"config_{name or 'default'}")

    def get(section: str, key: str, env: str) -> str:
        value = os.environ.get(env) or config.get(section, key, fallback="")
        return value.strip()

    zone = get("compute", "zone", "CLOUDSDK_COMPUTE_ZONE")
    region = get("compute", "region", "CLOUDSDK_COMPUTE_REGION")
    if not region and zone:
        region = zone.rsplit("-", 1)[0]
    return GcloudDefaults(get("core", "project", "CLOUDSDK_CORE_PROJECT"), region, zone)


# ----------------------------------------------------------------------
# Credentials


def load_credentials() -> tuple[Any, str]:
    """The application-default credentials and their default project ID
    (``""`` if none).

    The credentials have no quota project. ``gcloud auth application-default
    login`` sets one, often a project unrelated to the add-on, and Google then
    refuses calls to APIs that aren't enabled in it (for example listing
    projects with the Resource Manager API). Without it, API usage is
    accounted to the project being worked on, as with the ``gcloud`` tool.
    """
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError as exc:  # pragma: no cover - a dependency of the add-on
        raise ProviderError(
            "Google's sign-in library (google-auth) isn't installed. Reinstall orange3-cloud."
        ) from exc
    try:
        credentials, project = google.auth.default(default_scopes=[CLOUD_PLATFORM_SCOPE])
    except DefaultCredentialsError as exc:
        raise NotSignedInError(
            'Not signed in to Google Cloud. Click "Sign in with Google".'
        ) from exc
    if hasattr(credentials, "with_quota_project"):
        with contextlib.suppress(NotImplementedError):
            credentials = credentials.with_quota_project(None)
    return credentials, project or ""


def account_email(credentials: Any) -> str:
    """The signed-in account's email address, if the credentials tell it
    (``""`` otherwise). Call after the credentials were used (refreshed)."""
    email = getattr(credentials, "service_account_email", None)
    if isinstance(email, str) and "@" in email:
        return email
    token = getattr(credentials, "id_token", None)
    if not token:
        return ""
    try:
        from google.auth import jwt

        claims = jwt.decode(token, verify=False)
    except (ImportError, ValueError):  # not a token we can read: no email to show
        return ""
    email = claims.get("email")
    return email if isinstance(email, str) else ""


def _api_error(exc: Exception, what: str, project: str = "") -> ProviderError:
    """A Google API error as a :class:`ProviderError` for the user."""
    from google.api_core import exceptions as api
    from google.auth import exceptions as auth

    if isinstance(exc, (auth.RefreshError, auth.DefaultCredentialsError)):
        return NotSignedInError(
            'Your Google sign-in has expired or was revoked. Click "Sign in with Google".'
        )
    if isinstance(exc, (auth.TransportError, api.ServiceUnavailable)):
        return ProviderError(f"Couldn't reach Google Cloud to {what}. Check the connection.")
    if isinstance(exc, api.GoogleAPICallError):
        reasons = {
            getattr(detail, "reason", None) for detail in (getattr(exc, "details", None) or [])
        }
        message = getattr(exc, "message", "") or str(exc)
        if "SERVICE_DISABLED" in reasons or "has not been used in project" in message:
            where = f" in project {project}" if project else ""
            return ApiDisabledError(f"Couldn't {what}: the API isn't enabled{where}. {message}")
        if isinstance(exc, (api.PermissionDenied, api.Forbidden)):
            where = f" in project {project}" if project else ""
            return ProviderError(f"Couldn't {what}: no permission{where}. {message}")
        if isinstance(exc, api.NotFound) and project:
            return ProviderError(f"Couldn't {what}: project {project} wasn't found.")
        return ProviderError(f"Couldn't {what}: {message}")
    return ProviderError(f"Couldn't {what}: {exc}")


# ----------------------------------------------------------------------
# Projects, regions and zones


@dataclass(frozen=True)
class Project:
    """A Google Cloud project the user can use."""

    project_id: str
    name: str = ""

    @property
    def label(self) -> str:
        """The project's name and ID, as the user sees them."""
        if self.name and self.name != self.project_id:
            return f"{self.name} ({self.project_id})"
        return self.project_id


@dataclass(frozen=True)
class Region:
    """A Google Cloud region and its zones."""

    name: str
    #: Zones in the region that are up, sorted.
    zones: tuple[str, ...] = field(default_factory=tuple)


def list_projects(credentials: Any) -> list[Project]:
    """The active projects the user can see (Resource Manager API), sorted
    by label."""
    from google.cloud import resourcemanager_v3

    try:
        client = resourcemanager_v3.ProjectsClient(credentials=credentials, transport="rest")
        state = resourcemanager_v3.Project.State.ACTIVE
        projects = [
            Project(p.project_id, p.display_name)
            for p in client.search_projects(request={})
            if p.state == state
        ]
    except Exception as exc:
        raise _api_error(exc, "list your projects") from exc
    return sorted(projects, key=lambda p: p.label.lower())


def list_regions(credentials: Any, project: str) -> list[Region]:
    """The project's regions with their zones that are up (Compute Engine
    API, one call), sorted by name."""
    from google.cloud import compute_v1

    try:
        client = compute_v1.ZonesClient(credentials=credentials)
        zones = list(client.list(project=project))
    except Exception as exc:
        raise _api_error(exc, "list the regions", project) from exc
    by_region: dict[str, list[str]] = {}
    for zone in zones:
        if zone.status != "UP":
            continue
        region = zone.region.rsplit("/", 1)[-1] or zone.name.rsplit("-", 1)[0]
        by_region.setdefault(region, []).append(zone.name)
    return [Region(name, tuple(sorted(by_region[name]))) for name in sorted(by_region)]


def available_machine_types(credentials: Any, project: str, zone: str) -> set[str]:
    """The add-on's machine types (:data:`~orangecontrib.cloud.client.
    providers.gcp_vm.MACHINE_TYPES`) that ``zone`` offers (Compute Engine
    API, one call)."""
    from google.cloud import compute_v1

    from orangecontrib.cloud.client.providers.gcp_vm import MACHINE_TYPES

    names = {t.name for t in MACHINE_TYPES}
    try:
        client = compute_v1.MachineTypesClient(credentials=credentials)
        listed = client.list(
            request={
                "project": project,
                "zone": zone,
                "filter": f'name eq "{"|".join(sorted(names))}"',
            }
        )
        return {m.name for m in listed} & names
    except Exception as exc:
        raise _api_error(exc, f"list the machine types of {zone}", project) from exc


def pick_region(regions: list[Region], *preferred: str) -> str:
    """The first of ``preferred`` that is one of ``regions``, else
    :data:`DEFAULT_REGION`, else the first region (``""`` if none)."""
    names = [r.name for r in regions]
    for name in (*preferred, DEFAULT_REGION):
        if name and name in names:
            return name
    return names[0] if names else ""


def pick_zone(zones: tuple[str, ...] | list[str], region: str, *preferred: str) -> str:
    """The first of ``preferred`` that is in ``zones``, else ``<region>-a``,
    else the first zone (``""`` if none)."""
    for name in (*preferred, f"{region}-a", DEFAULT_ZONE):
        if name and name in zones:
            return name
    return zones[0] if zones else ""
