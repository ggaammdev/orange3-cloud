"""Google's Cloud Storage API, as the Google Cloud provider uses it.

:class:`StorageApi` lists the calls; :class:`GoogleStorageApi` makes them as
plain REST requests (JSON API) with the signed-in credentials, like
:mod:`~orangecontrib.cloud.client.providers.gcp_compute`, so tests can replace
it with a fake bucket.

Uploads use Google's resumable uploads: :meth:`StorageApi.start_upload`
returns a session URL, chunks are sent with :meth:`StorageApi.upload_chunk`,
and :meth:`StorageApi.upload_offset` asks how much Google has after an
interruption. A session stays valid for about a week.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from orangecontrib.cloud.client.providers.base import ProviderError
from orangecontrib.cloud.client.providers.gcp_compute import NotFoundError
from orangecontrib.cloud.client.providers.uploads import TransientError as _Transient

__all__ = [
    "GoogleStorageApi",
    "NotFoundError",
    "StorageApi",
    "TransientError",
    "UploadExpiredError",
]


class TransientError(_Transient):
    """The connection broke or Google was briefly unavailable: try again."""


class UploadExpiredError(ProviderError):
    """The resumable upload session is gone: start the upload again."""


class StorageApi:
    """The Cloud Storage calls the provider makes, with resources as dicts in
    the API's JSON form. Each raises :class:`ProviderError`
    (:class:`NotFoundError` for a missing bucket or object,
    :class:`TransientError` when trying again may help)."""

    def list_buckets(self, project: str, prefix: str) -> list[dict]:
        """The project's buckets whose names start with ``prefix``."""
        raise NotImplementedError

    def create_bucket(self, project: str, body: dict) -> dict:
        """Create a bucket from a ``buckets.insert`` request body."""
        raise NotImplementedError

    def get_policy(self, bucket: str) -> dict:
        """The bucket's IAM policy."""
        raise NotImplementedError

    def set_policy(self, bucket: str, policy: dict) -> None:
        """Replace the bucket's IAM policy."""
        raise NotImplementedError

    def get_object(self, bucket: str, name: str) -> dict:
        """The object's metadata."""
        raise NotImplementedError

    def list_objects(self, bucket: str, prefix: str) -> list[dict]:
        """The objects whose names start with ``prefix``."""
        raise NotImplementedError

    def delete_object(self, bucket: str, name: str) -> None:
        """Delete an object."""
        raise NotImplementedError

    def read_object(self, bucket: str, name: str, length: int) -> bytes:
        """The object's first ``length`` bytes (or all, if shorter)."""
        raise NotImplementedError

    def start_upload(self, bucket: str, name: str, size: int) -> str:
        """Start a resumable upload of ``size`` bytes; returns its session URL."""
        raise NotImplementedError

    def upload_chunk(self, session_url: str, data: bytes, offset: int, size: int) -> int | dict:
        """Send ``data`` from byte ``offset``: the bytes Google now has, or
        the object's metadata when the upload is complete."""
        raise NotImplementedError

    def upload_offset(self, session_url: str, size: int) -> int | dict:
        """How many bytes Google has (or the object if it's complete)."""
        raise NotImplementedError


class GoogleStorageApi(StorageApi):
    """:class:`StorageApi` with REST calls, using the signed-in credentials
    (:func:`~orangecontrib.cloud.client.providers.gcp_account.load_credentials`)."""

    STORAGE = "https://storage.googleapis.com/storage/v1"
    UPLOAD = "https://storage.googleapis.com/upload/storage/v1"

    def __init__(self, credentials: Any, timeout: float = 120):
        from google.auth.transport.requests import AuthorizedSession

        self.session = AuthorizedSession(credentials)
        self.timeout = timeout

    def _send(self, method: str, url: str, what: str, **kwargs: Any) -> Any:
        """The response; connection problems raise :class:`TransientError`."""
        from google.auth import exceptions as auth
        from requests import RequestException

        from orangecontrib.cloud.client.providers.gcp_account import NotSignedInError

        try:
            return self.session.request(
                method, url, timeout=self.timeout, allow_redirects=False, **kwargs
            )
        except (auth.RefreshError, auth.DefaultCredentialsError) as exc:
            raise NotSignedInError(
                'Your Google sign-in has expired or was revoked. Click "Sign in with Google".'
            ) from exc
        except (auth.TransportError, RequestException) as exc:
            raise TransientError(
                f"Couldn't reach Google Cloud to {what}. Check the connection."
            ) from exc

    @staticmethod
    def _error(response: Any, what: str, project: str) -> ProviderError:
        from orangecontrib.cloud.client.providers.gcp_setup import _http_error

        if response.status_code == 404:
            return NotFoundError(f"Couldn't {what}: not found.")
        if response.status_code == 429 or response.status_code >= 500:
            return TransientError(f"Google Cloud couldn't {what} just now; trying again.")
        return _http_error(response, what, project)

    def _call(self, method: str, path: str, what: str, project: str = "", **kwargs: Any) -> Any:
        response = self._send(method, f"{self.STORAGE}/{path}", what, **kwargs)
        if not response.ok:
            raise self._error(response, what, project)
        return response.json() if response.content else {}

    def _pages(self, path: str, what: str, params: dict) -> list[dict]:
        items: list[dict] = []
        while True:
            reply = self._call("GET", path, what, params=params)
            items.extend(reply.get("items", []))
            if not reply.get("nextPageToken"):
                return items
            params = {**params, "pageToken": reply["nextPageToken"]}

    def list_buckets(self, project: str, prefix: str) -> list[dict]:
        return self._pages("b", "list the storage buckets", {"project": project, "prefix": prefix})

    def create_bucket(self, project: str, body: dict) -> dict:
        return self._call(
            "POST", "b", "create the storage bucket", project, params={"project": project},
            json=body,
        )  # fmt: skip

    def get_policy(self, bucket: str) -> dict:
        return self._call("GET", f"b/{bucket}/iam", "read the bucket's access")

    def set_policy(self, bucket: str, policy: dict) -> None:
        self._call("PUT", f"b/{bucket}/iam", "give the machine access to the bucket", json=policy)

    def get_object(self, bucket: str, name: str) -> dict:
        return self._call("GET", f"b/{bucket}/o/{quote(name, safe='')}", "read the file")

    def list_objects(self, bucket: str, prefix: str) -> list[dict]:
        return self._pages(f"b/{bucket}/o", "list the uploaded files", {"prefix": prefix})

    def delete_object(self, bucket: str, name: str) -> None:
        self._call("DELETE", f"b/{bucket}/o/{quote(name, safe='')}", "delete the file")

    def read_object(self, bucket: str, name: str, length: int) -> bytes:
        what = "read the file"
        response = self._send(
            "GET",
            f"{self.STORAGE}/b/{bucket}/o/{quote(name, safe='')}",
            what,
            params={"alt": "media"},
            headers={"Range": f"bytes=0-{max(length, 1) - 1}"},
        )
        if not response.ok:
            raise self._error(response, what, "")
        return response.content[:length]

    def start_upload(self, bucket: str, name: str, size: int) -> str:
        what = "start the upload"
        response = self._send(
            "POST",
            f"{self.UPLOAD}/b/{bucket}/o",
            what,
            params={"uploadType": "resumable", "name": name},
            headers={
                "X-Upload-Content-Type": "application/octet-stream",
                "X-Upload-Content-Length": str(size),
            },
            json={"name": name, "contentType": "application/octet-stream"},
        )
        if not response.ok or "Location" not in response.headers:
            raise self._error(response, what, "")
        return response.headers["Location"]

    def _upload_state(self, response: Any, what: str) -> int | dict:
        if response.status_code == 308:
            # "Range: bytes=0-N" when Google has N+1 bytes; no header: none.
            stored = response.headers.get("Range", "")
            return int(stored.rsplit("-", 1)[1]) + 1 if "-" in stored else 0
        if response.status_code in (200, 201):
            return response.json()
        if response.status_code in (404, 410):
            raise UploadExpiredError("The upload session expired; starting the upload again.")
        raise self._error(response, what, "")

    def upload_chunk(self, session_url: str, data: bytes, offset: int, size: int) -> int | dict:
        what = "upload the file"
        last = offset + len(data) - 1
        content_range = f"bytes {offset}-{last}/{size}" if data else f"bytes */{size}"
        response = self._send(
            "PUT", session_url, what, data=data, headers={"Content-Range": content_range}
        )
        return self._upload_state(response, what)

    def upload_offset(self, session_url: str, size: int) -> int | dict:
        what = "check the upload"
        response = self._send(
            "PUT", session_url, what, data=b"", headers={"Content-Range": f"bytes */{size}"}
        )
        return self._upload_state(response, what)
