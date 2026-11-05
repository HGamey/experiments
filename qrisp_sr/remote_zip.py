"""Small, read-only HTTP Range adapter for inspecting public ZIP archives.

Only requested byte ranges are transferred. The implementation deliberately
rejects servers that silently ignore Range, so a metadata read cannot turn into
an accidental multi-gigabyte download.
"""
import re
import time
import urllib.error
import urllib.request


_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class HTTPRangeFile:
    def __init__(self, url, timeout=45, max_read_bytes=128 * 1024 * 1024):
        self.url = url
        self.timeout = timeout
        self.max_read_bytes = max_read_bytes
        self.pos = 0
        self.bytes_read = 0
        self.requests = 0
        self.attempts = 0
        self.closed = False
        status, headers, body = self._request(0, 0)
        if status != 206:
            raise RuntimeError(f"range probe returned HTTP {status}, expected 206")
        match = _CONTENT_RANGE.match(headers.get("Content-Range", ""))
        if not match or body != b"\x50":
            # ZIP files start with a local-file signature. Requiring its first
            # byte also rejects HTML login/error pages with a misleading 206.
            raise RuntimeError("range probe did not return a ZIP first byte")
        self.size = int(match.group(3))
        if self.size < 22:
            raise RuntimeError(f"remote object is too small for ZIP: {self.size}")

    def tell(self):
        return self.pos

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, offset, whence=0):
        if whence == 0:
            target = offset
        elif whence == 1:
            target = self.pos + offset
        elif whence == 2:
            target = self.size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if target < 0:
            raise ValueError(f"negative seek: {target}")
        self.pos = target
        return self.pos

    def _request(self, start, end):
        if start < 0 or end < start:
            raise ValueError(f"invalid range: {start}-{end}")
        if hasattr(self, "size") and end >= self.size:
            raise ValueError(f"range past end of {self.size}-byte object: {start}-{end}")
        last_error = None
        for attempt in range(4):
            request = urllib.request.Request(
                self.url,
                headers={
                    "User-Agent": "QRISP-range-reader/1",
                    "Range": f"bytes={start}-{end}",
                },
            )
            self.attempts += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    status = response.status
                    headers = response.headers
                self.requests += 1
                self.bytes_read += len(body)
                return status, headers, body
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise
                last_error = exc
            except (TimeoutError, ConnectionError, urllib.error.URLError) as exc:
                last_error = exc
            if attempt < 3:
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"range GET failed after 4 attempts: {last_error}")

    def read_at(self, start, length):
        if length < 0 or length > self.max_read_bytes:
            raise ValueError(f"invalid read length: {length}")
        if start < 0 or start > self.size:
            raise ValueError(f"invalid read start: {start}")
        end = min(self.size, start + length)
        wanted = end - start
        if wanted == 0:
            return b""
        status, headers, body = self._request(start, end - 1)
        if status != 206:
            raise RuntimeError(f"range read returned HTTP {status}, expected 206")
        match = _CONTENT_RANGE.match(headers.get("Content-Range", ""))
        if not match or int(match.group(1)) != start or int(match.group(2)) != end - 1:
            raise RuntimeError(
                f"unexpected Content-Range: {headers.get('Content-Range')!r}"
            )
        if len(body) != wanted:
            raise RuntimeError(f"short range read: got {len(body)}, wanted {wanted}")
        return body

    def read(self, length=-1):
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if self.pos >= self.size:
            return b""
        remaining = self.size - self.pos
        if length is None or length < 0:
            length = remaining
        if length > self.max_read_bytes:
            raise ValueError(
                f"refusing unbounded {length}-byte read at offset {self.pos}"
            )
        body = self.read_at(self.pos, min(length, remaining))
        self.pos += len(body)
        return body

    def close(self):
        self.closed = True
