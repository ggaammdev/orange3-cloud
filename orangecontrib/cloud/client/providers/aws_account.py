"""The user's AWS account: the AWS CLI, its profiles, signing in, the
regions the user can choose and the machine sizes each offers.

The add-on signs in with the AWS CLI's profiles (``~/.aws/config``): the
sign-in button runs the CLI's browser sign-in for the chosen profile,
``aws login`` (with the user's AWS console sign-in), or ``aws sso login``
for a profile of IAM Identity Center (:func:`sign_in`, through the
provider's ``sign_in`` and the session). :func:`load_session` then reads
that profile's credentials with ``boto3``; profiles of ``aws login`` need
``botocore[crt]``. The add-on never sees or stores the user's password or
keys.

Every function here may be slow (network, or a login shell to find ``aws``),
so the settings dialog calls them off the GUI thread. They raise
:class:`~orangecontrib.cloud.client.providers.base.ProviderError` (or
:class:`NotSignedInError`) with a message for the user. ``boto3`` is
imported only when needed.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.client.providers.cli_tools import find_tool, run_sign_in

__all__ = [
    "AWS_CLI_INSTALL_URL",
    "DEFAULT_PROFILE",
    "DEFAULT_REGION",
    "SIGN_IN_BUTTON",
    "DENIED_CODES",
    "AwsProfile",
    "Identity",
    "NotSignedInError",
    "aws_error",
    "client",
    "default_profile",
    "error_code",
    "find_aws",
    "identity",
    "list_profiles",
    "list_regions",
    "load_session",
    "offered_instance_types",
    "pick_profile",
    "pick_region",
    "sign_in",
    "sign_in_command",
]

AWS_CLI_INSTALL_URL = (
    "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
)

#: Used when the chosen profile has no region.
DEFAULT_REGION = "us-east-1"
#: The AWS CLI's profile when none is named.
DEFAULT_PROFILE = "default"
#: The sign-in button's text, which messages refer to.
SIGN_IN_BUTTON = "Sign in to AWS"

#: Settings that make a profile one of IAM Identity Center (``aws sso login``).
_SSO_KEYS = ("sso_session", "sso_start_url", "sso_account_id", "sso_role_name")
#: Error codes of credentials AWS no longer accepts.
_EXPIRED_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "InvalidToken",
    "UnrecognizedClientException",
    "AuthFailure",
}
#: Error codes of calls the user may not make.
DENIED_CODES = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedAccess"}
)


class NotSignedInError(ProviderError):
    """The profile has no credentials, or they no longer work."""


# ----------------------------------------------------------------------
# The AWS CLI


def _known_aws_paths() -> list[Path]:
    """Where the AWS CLI's installers and package managers put ``aws``."""
    if sys.platform == "win32":
        bases = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)")]
        return [Path(b) / "Amazon" / "AWSCLIV2" / "aws.exe" for b in bases if b]
    return [
        Path("/opt/homebrew/bin/aws"),
        Path("/usr/local/bin/aws"),
        Path("/usr/local/aws-cli/aws"),
        Path("/usr/bin/aws"),
        Path("/snap/bin/aws"),
        Path.home() / ".local" / "bin" / "aws",
    ]


def find_aws(*, login_shell: bool = True) -> str | None:
    """The path of the ``aws`` tool, or ``None`` if it isn't installed.

    Looks on ``PATH``, in the usual install locations and, unless
    ``login_shell`` is false, on the PATH of the user's login shell.
    """
    names = ("aws.exe", "aws") if sys.platform == "win32" else ("aws",)
    return find_tool(names, _known_aws_paths(), login_shell=login_shell)


@dataclass(frozen=True)
class AwsProfile:
    """A profile of the AWS CLI's configuration."""

    name: str
    #: Its region ("": not set).
    region: str = ""
    #: It signs in with IAM Identity Center (``aws sso login``).
    sso: bool = False


def list_profiles() -> list[AwsProfile]:
    """The profiles in the AWS CLI's configuration and credentials files
    (``AWS_CONFIG_FILE`` and ``AWS_SHARED_CREDENTIALS_FILE`` if set), sorted
    by name. No network."""
    import botocore.session
    from botocore.exceptions import BotoCoreError

    try:
        profiles = botocore.session.Session().full_config.get("profiles", {})
    except BotoCoreError as exc:  # e.g. a configuration file that can't be parsed
        raise ProviderError(f"Couldn't read the AWS CLI's configuration: {exc}") from exc
    return [
        AwsProfile(
            name,
            str(settings.get("region") or "").strip(),
            any(key in settings for key in _SSO_KEYS),
        )
        for name, settings in sorted(profiles.items())
    ]


def default_profile() -> str:
    """The profile the AWS CLI uses when none is named: ``AWS_PROFILE`` if
    set, else ``default``."""
    return os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE") or DEFAULT_PROFILE


def pick_profile(profiles: list[AwsProfile], *preferred: str) -> str:
    """The first of ``preferred`` that is one of ``profiles``, else the
    default profile, else the first profile; :data:`DEFAULT_PROFILE` if there
    are none (signing in creates it)."""
    names = [p.name for p in profiles]
    for name in (*preferred, default_profile()):
        if name and name in names:
            return name
    return names[0] if names else DEFAULT_PROFILE


def sign_in_command(aws: str, profile: AwsProfile, region: str = "") -> list[str]:
    """The command that signs the user in to ``profile``: it opens AWS's
    sign-in page in the browser and saves the profile's credentials.

    ``aws login`` asks for a region on the console when the profile has
    none, so it then gets ``region`` (default :data:`DEFAULT_REGION`).
    """
    if profile.sso:
        return [aws, "sso", "login", "--profile", profile.name]
    command = [aws, "login", "--profile", profile.name]
    if not profile.region:
        command += ["--region", region or DEFAULT_REGION]
    return command


def sign_in(
    aws: str,
    profile: AwsProfile,
    region: str = "",
    cancel: threading.Event | None = None,
    *,
    poll: float = 0.2,
) -> str:
    """Run :func:`sign_in_command` and wait until it ends; return its output.

    The user signs in in the browser, which can take minutes, so call it off
    the GUI thread. Setting ``cancel`` kills the process and raises
    :class:`~orangecontrib.cloud.client.providers.base.SignInCancelledError`.
    Raises :class:`ProviderError` if ``aws`` can't start or doesn't finish.
    """
    return run_sign_in(
        sign_in_command(aws, profile, region),
        cancel,
        tool="aws",
        install_hint="Is the AWS CLI installed?",
        poll=poll,
    )


# ----------------------------------------------------------------------
# Credentials


def load_session(profile: str) -> Any:
    """A ``boto3`` session with the credentials of the AWS CLI's ``profile``
    (they are read when a call needs them)."""
    import boto3
    from botocore.exceptions import BotoCoreError, ProfileNotFound

    try:
        return boto3.Session(profile_name=profile)
    except ProfileNotFound as exc:
        raise NotSignedInError(
            f'There is no AWS profile "{profile}" yet. Click “{SIGN_IN_BUTTON}” to create it.'
        ) from exc
    except BotoCoreError as exc:
        raise aws_error(exc, "read your AWS profile", profile) from exc


def client(session: Any, service: str, region: str) -> Any:
    """A ``boto3`` client of ``service`` in ``region``, with short timeouts
    and a few retries. S3's signs with Signature Version 4 and uses the
    region's own endpoint, also in signed links (``boto3``'s default links
    would go to the global endpoint, which redirects for buckets in other
    regions and breaks the signature)."""
    from botocore.config import Config

    config = Config(
        connect_timeout=10,
        read_timeout=30,
        retries={"max_attempts": 3, "mode": "standard"},
    )
    if service == "s3":
        config = config.merge(
            Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"},
            )
        )
    return session.client(service, region_name=region, config=config)


def error_code(exc: Exception) -> str:
    """The AWS error code of a ``botocore`` ``ClientError`` (``""`` for
    other errors)."""
    response = getattr(exc, "response", None)
    return response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""


def aws_error(exc: Exception, what: str, profile: str = "") -> ProviderError:
    """An error of ``boto3`` as a :class:`ProviderError` for the user;
    :class:`NotSignedInError` if the credentials are missing or expired."""
    from botocore import exceptions as boto

    who = f' "{profile}"' if profile else ""
    if isinstance(exc, boto.NoCredentialsError):
        return NotSignedInError(
            f"Not signed in to AWS with the profile{who}. Click “{SIGN_IN_BUTTON}”."
        )
    expired = (
        boto.PartialCredentialsError,
        boto.CredentialRetrievalError,
        boto.LoginError,
        boto.SSOError,
        boto.TokenRetrievalError,
    )
    code = error_code(exc)
    if isinstance(exc, expired) or code in _EXPIRED_CODES:
        return NotSignedInError(
            f"The AWS sign-in of the profile{who} has expired or doesn't work ({exc}). "
            f"Click “{SIGN_IN_BUTTON}”."
        )
    if isinstance(exc, boto.MissingDependencyException):
        return ProviderError(f"{exc} Reinstall orange3-cloud.")
    if isinstance(exc, (boto.ConnectionError, boto.HTTPClientError)):
        return ProviderError(f"Couldn't reach AWS to {what}. Check the connection.")
    if isinstance(exc, boto.ClientError):
        message = exc.response.get("Error", {}).get("Message", "") or str(exc)
        if code in DENIED_CODES:
            return ProviderError(f"Couldn't {what}: no permission. {message}")
        return ProviderError(f"Couldn't {what}: {message}")
    return ProviderError(f"Couldn't {what}: {exc}")


@dataclass(frozen=True)
class Identity:
    """Who the credentials belong to."""

    account: str  #: the AWS account's ID
    arn: str  #: e.g. ``arn:aws:iam::<account>:user/<name>``

    @property
    def name(self) -> str:
        """The user's name, the role's session, or "root user"."""
        kind, _, rest = self.arn.split(":", 5)[-1].partition("/")
        if kind == "root" or not rest:
            return "root user" if kind == "root" else kind
        if kind == "assumed-role":
            role, _, session = rest.partition("/")
            return f"{session} ({role})" if session else role
        return rest.rsplit("/", 1)[-1]

    @property
    def label(self) -> str:
        """The name and the account, as the user sees them."""
        return f"{self.name} in account {self.account}"


def identity(session: Any) -> Identity:
    """Who the session's credentials belong to (STS ``GetCallerIdentity``,
    which needs no permission); raises :class:`NotSignedInError` if they
    are missing or expired."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        sts = client(session, "sts", session.region_name or DEFAULT_REGION)
        reply = sts.get_caller_identity()
    except (BotoCoreError, ClientError) as exc:
        raise aws_error(exc, "check your AWS sign-in", session.profile_name) from exc
    return Identity(reply.get("Account", ""), reply.get("Arn", ""))


# ----------------------------------------------------------------------
# Regions


def list_regions(session: Any) -> list[str]:
    """The regions enabled for the account (EC2 ``DescribeRegions``), sorted."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        ec2 = client(session, "ec2", session.region_name or DEFAULT_REGION)
        regions = ec2.describe_regions(AllRegions=False)["Regions"]
    except (BotoCoreError, ClientError) as exc:
        raise aws_error(exc, "list the regions", session.profile_name) from exc
    return sorted(r["RegionName"] for r in regions)


def offered_instance_types(session: Any, region: str) -> set[str]:
    """The add-on's instance types (:data:`~orangecontrib.cloud.client.
    providers.aws_vm.MACHINE_TYPES`) that ``region`` offers (EC2
    ``DescribeInstanceTypeOfferings``, one call)."""
    from botocore.exceptions import BotoCoreError, ClientError

    from orangecontrib.cloud.client.providers.aws_vm import MACHINE_TYPES

    names = sorted(t.name for t in MACHINE_TYPES)
    try:
        ec2 = client(session, "ec2", region)
        pages = ec2.get_paginator("describe_instance_type_offerings").paginate(
            LocationType="region", Filters=[{"Name": "instance-type", "Values": names}]
        )
        offered = {o["InstanceType"] for page in pages for o in page["InstanceTypeOfferings"]}
    except (BotoCoreError, ClientError) as exc:
        raise aws_error(exc, f"list the machine sizes of {region}", session.profile_name) from exc
    return offered & set(names)


def pick_region(regions: list[str], *preferred: str) -> str:
    """The first of ``preferred`` that is one of ``regions``, else
    :data:`DEFAULT_REGION`, else the first region (``""`` if none)."""
    for name in (*preferred, DEFAULT_REGION):
        if name and name in regions:
            return name
    return regions[0] if regions else ""
