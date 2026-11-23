"""Which machine image a machine on a cloud other than Google's runs (AWS,
Azure), and the token it pulls a private development image with.

A released add-on runs the public image, which needs no credentials. A
development version runs the private development image in the Artifact
Registry of the developer's Google Cloud project (the one chosen on the
Google Cloud page; see ``docs/dev.md``, "Development images"). Machines on
other clouds can't use Google's service accounts, so they pull it with a
short-lived token of the service account :data:`IMAGE_READER`, which may
only read that repository; the developer's Google sign-in makes the token
(:func:`registry_token`).
"""

from __future__ import annotations

import os

from orangecontrib.cloud.client.providers.base import PUBLIC_IMAGE, ProviderError, is_release
from orangecontrib.cloud.client.providers.gcp_vm import DEV_REPOSITORY
from orangecontrib.cloud.settings import CloudSettings

__all__ = ["IMAGE_READER", "TOKEN_LIFETIME", "dev_image", "machine_image", "registry_token"]

#: The service account whose tokens pull development images on other clouds.
IMAGE_READER = "orange3-cloud-image-reader"
#: Seconds a registry token lasts (the machine pulls the image at boot).
TOKEN_LIFETIME = 3600


def machine_image(version: str, override_var: str) -> str:
    """The machine image for add-on ``version``: the environment variable
    ``override_var`` if set (e.g. ``ORANGE3_CLOUD_AWS_IMAGE``), the public
    image of a release, else :func:`dev_image`."""
    override = os.environ.get(override_var, "").strip()
    if override:
        return override
    if is_release(version):
        return f"{PUBLIC_IMAGE}:{version}"
    return dev_image(override_var)


def dev_image(override_var: str = "") -> str:
    """The development image ``:dev`` in the Artifact Registry of the Google
    Cloud project and region chosen on the Google Cloud page."""
    google = CloudSettings.load().of("gcp")
    region = google.zone.rsplit("-", 1)[0] if google.zone else google.region
    if not google.account or not region:
        instead = f", or set {override_var}" if override_var else ""
        raise ProviderError(
            "A development version of orange3-cloud runs the development image from the "
            "private registry in your Google Cloud project. Choose that project and its region "
            f"on the Google Cloud page of the Cloud settings{instead}."
        )
    return f"{region}-docker.pkg.dev/{google.account}/{DEV_REPOSITORY}/machine:dev"


def registry_token(image: str) -> str | None:
    """A short-lived token that pulls ``image`` from Google's Artifact
    Registry (``<region>-docker.pkg.dev/<project>/…``), made with your
    Google sign-in as :data:`IMAGE_READER` of that project; ``None`` for
    images elsewhere (e.g. the public image)."""
    host, _, path = image.partition("/")
    if not host.endswith("-docker.pkg.dev") or "/" not in path:
        return None
    project = path.split("/", 1)[0]
    from google.auth import exceptions, impersonated_credentials
    from google.auth.transport.requests import Request

    from orangecontrib.cloud.client.providers.gcp_account import (
        CLOUD_PLATFORM_SCOPE,
        load_credentials,
    )

    credentials, _ = load_credentials()
    reader = impersonated_credentials.Credentials(
        source_credentials=credentials,
        target_principal=f"{IMAGE_READER}@{project}.iam.gserviceaccount.com",
        target_scopes=[CLOUD_PLATFORM_SCOPE],
        lifetime=TOKEN_LIFETIME,
    )
    try:
        reader.refresh(Request())
    except exceptions.TransportError as exc:
        raise ProviderError(
            "Couldn't reach Google Cloud for a token to pull the development image."
        ) from exc
    except exceptions.GoogleAuthError as exc:
        raise ProviderError(
            f"Couldn't get a token to pull the development image {image} as {IMAGE_READER} "
            f"({exc}). Sign in to Google Cloud, and run scripts/setup-dev-registry.sh."
        ) from exc
    return reader.token
