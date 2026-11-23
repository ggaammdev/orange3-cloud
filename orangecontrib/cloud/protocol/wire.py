"""How API v1 travels over HTTP: its version and path prefix, the header that
marks pickles, the content types, and the multipart part that holds a
request's JSON model."""

from __future__ import annotations

__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "JSON_CONTENT_TYPE",
    "MULTIPART_CONTENT_TYPE",
    "PAYLOAD_HEADER",
    "PAYLOAD_PICKLE",
    "PICKLE_CONTENT_TYPE",
    "SPEC_PART",
]

#: Major version of the API. It changes only with incompatible changes, and
#: the path prefix changes with it.
API_VERSION = 1

#: Path prefix of every endpoint.
API_PREFIX = "/v1"

#: Header that marks a body (or a multipart part) as a pickled Python object.
#: The receiver unpickles only bodies that carry it.
PAYLOAD_HEADER = "X-Orange-Cloud-Payload"

#: Value of :data:`PAYLOAD_HEADER` for pickles.
PAYLOAD_PICKLE = "pickle"

#: Content type of pickles and of raw file uploads.
PICKLE_CONTENT_TYPE = "application/octet-stream"

JSON_CONTENT_TYPE = "application/json"
MULTIPART_CONTENT_TYPE = "multipart/form-data"

#: Name of the multipart part that holds the request's JSON model.
SPEC_PART = "spec"
