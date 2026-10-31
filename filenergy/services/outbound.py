"""Outbound HTTP to URLs that users choose (URL ingestion, webhooks).

Both features fetch an address typed by a user from inside our network, so
without care they reach things only the server can: 127.0.0.1, the Docker
network, the cloud metadata endpoint (169.254.169.254). `/file/from_url/`
even stores the response as a downloadable file.

Every request made through `request()`:

* only goes to http/https on port 80 or 443, with no credentials in the URL;
* resolves the host once, rejects it if ANY address is not public
  (private, loopback, link-local/metadata, CGNAT, multicast, reserved…),
  and connects to that same checked address (no second DNS lookup that a
  rebinding name could answer differently); TLS is still verified against
  the hostname;
* follows redirects only when asked, re-checking every hop;
* stops at `max_bytes`, and at a deadline of `timeout` seconds checked
  between reads (each socket operation also times out, so a stalled peer
  costs at most about twice `timeout`).
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import time
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urljoin, urlsplit

ALLOWED_SCHEMES = {"http": 80, "https": 443}
ALLOWED_PORTS = {80, 443}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CHUNK = 64 * 1024

# NAT64 prefix: the IPv4 address is embedded in the last 32 bits.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UnsafeURL(ValueError):
    """The URL points somewhere we refuse to connect to."""


class OutboundError(RuntimeError):
    """Network failure, timeout or oversized response."""


class ResponseTooLarge(OutboundError):
    pass


@dataclass
class Response:
    status: int
    headers: Message
    body: bytes
    url: str


@dataclass
class _Target:
    scheme: str
    host: str
    port: int
    path: str


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not is_public_ip(str(embedded)):
            return False
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def parse_url(url: str) -> _Target:
    """Syntactic checks: scheme, host, port, no userinfo."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("Invalid URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURL("Only http(s) URLs are supported")
    if not parts.hostname:
        raise UnsafeURL("URL has no host")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("Credentials in the URL are not supported")
    port = port or ALLOWED_SCHEMES[scheme]
    if port not in ALLOWED_PORTS:
        raise UnsafeURL("Only ports 80 and 443 are allowed")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _Target(scheme, parts.hostname, port, path)


def _getaddrinfo(host: str, port: int):
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def resolve_public(host: str, port: int) -> str:
    """Resolve `host`; return one address if every address is public."""
    try:
        infos = _getaddrinfo(host, port)
    except (socket.gaierror, UnicodeError) as exc:
        raise OutboundError(f"Could not resolve {host}") from exc
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise OutboundError(f"Could not resolve {host}")
    for address in addresses:
        if not is_public_ip(address):
            raise UnsafeURL("URL points to a private or reserved address")
    return addresses[0]


def check_url(url: str, *, resolve: bool = True) -> None:
    """Validate a URL before storing it (e.g. a webhook).

    A host that does not resolve yet is accepted; one that resolves to a
    non-public address is not. Delivery checks again on every request.
    """
    target = parse_url(url)
    if resolve:
        try:
            resolve_public(target.host, target.port)
        except OutboundError:
            pass


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, *, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # SNI and certificate check use the hostname, not the IP.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _open(target: _Target, ip: str, timeout: float):
    cls = _PinnedHTTPSConnection if target.scheme == "https" else _PinnedHTTPConnection
    return cls(target.host, target.port, ip=ip, timeout=timeout)


def request(
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
    max_bytes: int = 10 * 1024 * 1024,
    max_redirects: int = 0,
    truncate: bool = False,
) -> Response:
    """Make one guarded request. Raises UnsafeURL or OutboundError.

    HTTP error statuses are returned, not raised. With `max_redirects=0`
    a 3xx comes back as is. A body over `max_bytes` raises
    ResponseTooLarge, or is cut there with `truncate=True`.
    """
    deadline = time.monotonic() + timeout
    method = method.upper()
    for hop in range(max_redirects + 1):
        target = parse_url(url)
        ip = resolve_public(target.host, target.port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OutboundError("Timed out")
        conn = _open(target, ip, remaining)
        try:
            conn.request(method, target.path, body=data, headers=dict(headers or {}))
            resp = conn.getresponse()
            location = resp.getheader("Location")
            if resp.status in REDIRECT_STATUSES and location and hop < max_redirects:
                url = urljoin(url, location)
                if resp.status == 303 or (resp.status in (301, 302) and method != "GET"):
                    method, data = "GET", None
                continue
            body = _read_capped(resp, max_bytes, deadline, truncate)
            return Response(resp.status, resp.msg, body, url)
        except (UnsafeURL, OutboundError):
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise OutboundError(f"Request failed: {exc}") from exc
        finally:
            conn.close()
    raise OutboundError("Too many redirects")


def _read_capped(resp, max_bytes: int, deadline: float, truncate: bool) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OutboundError("Timed out")
        chunk = resp.read(min(_CHUNK, max_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            if truncate:
                return b"".join(chunks)[:max_bytes]
            raise ResponseTooLarge(f"Response exceeds {max_bytes} bytes")
