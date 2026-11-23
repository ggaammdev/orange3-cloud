"""Reading the size and the first bytes of an ``http(s)://`` file.

Cloud File's memory estimate (:mod:`~orangecontrib.cloud.data.estimate`) uses
it for files the machine will download from the web.
"""

from __future__ import annotations

__all__ = ["read_url_head"]


def read_url_head(url: str, nbytes: int, timeout: float = 15.0) -> tuple[int | None, bytes]:
    """The size of the file at ``url`` and at most its first ``nbytes`` bytes.

    It makes one request: a ``GET`` for a range of bytes, or a ``HEAD`` when
    ``nbytes`` is 0. The size is ``None`` if the server doesn't say it.
    """
    import httpx

    raw = bytearray()
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        headers = {"Range": f"bytes=0-{nbytes - 1}"} if nbytes else {}
        method = "GET" if nbytes else "HEAD"
        with client.stream(method, url, headers=headers) as response:
            response.raise_for_status()
            size: int | None = None
            content_range = response.headers.get("content-range", "")
            if response.status_code == 206 and "/" in content_range:
                total = content_range.rsplit("/", 1)[1]
                size = int(total) if total.isdigit() else None
            elif response.headers.get("content-length", "").isdigit():
                size = int(response.headers["content-length"])
            if nbytes:
                for chunk in response.iter_bytes():
                    raw += chunk
                    if len(raw) >= nbytes:
                        break
    return size, bytes(raw[:nbytes])
