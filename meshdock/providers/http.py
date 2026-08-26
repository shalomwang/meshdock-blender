from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import random
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urljoin, urlsplit

from ..core.errors import (
    ProviderAuthenticationError,
    ProviderDownloadError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRequestError,
)

DEFAULT_USER_AGENT = "MeshDock/0.9"


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    bytes_written: int
    sha256: str
    content_type: str


class HttpTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        token: str,
        payload: dict[str, Any] | None = None,
        authorization_scheme: str = "bearer",
    ) -> dict[str, Any]: ...

    def download(self, url: str, destination: Path, *, max_bytes: int) -> DownloadResult: ...

    def upload_file(self, url: str, source: Path, *, content_type: str) -> None: ...


def _validate_public_https(url: str, *, resolve_dns: bool = True) -> None:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError
        host = parts.hostname.rstrip(".")
        if host.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            if not resolve_dns:
                return
            try:
                addresses = {
                    ipaddress.ip_address(item[4][0])
                    for item in socket.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
                }
            except (OSError, ValueError) as exc:
                raise ProviderDownloadError("provider location could not be resolved") from exc
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError
    except ValueError as exc:
        raise ProviderDownloadError("provider returned an unsafe download location") from exc


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, authenticated_origin: tuple[str, str, int | None] | None = None) -> None:
        super().__init__()
        self.authenticated_origin = authenticated_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urljoin(req.full_url, newurl)
        _validate_public_https(target)
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected is not None and self.authenticated_origin is not None:
            parts = urlsplit(target)
            origin = (parts.scheme, parts.hostname or "", parts.port)
            if origin != self.authenticated_origin:
                redirected.remove_header("Authorization")
                redirected.remove_unredirected_header("Authorization")
        return redirected


class SecureHttpClient:
    """Small stdlib HTTPS client suitable for Blender's bundled Python.

    It never logs request headers/bodies, validates redirect targets, bounds response
    sizes, retries only transient failures, and writes downloads atomically.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.attempts = attempts
        self._sleep = sleep
        self._opener = urllib.request.build_opener(_SafeRedirectHandler())

    def request_json(
        self,
        method: str,
        url: str,
        *,
        token: str,
        payload: dict[str, Any] | None = None,
        authorization_scheme: str = "bearer",
    ) -> dict[str, Any]:
        _validate_public_https(url)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if authorization_scheme == "bearer":
            authorization = f"Bearer {token}"
        elif authorization_scheme == "raw":
            authorization = token
        else:
            raise ValueError("unsupported authorization scheme")
        headers = {
            "Accept": "application/json",
            "Authorization": authorization,
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        parts = urlsplit(url)
        opener = urllib.request.build_opener(
            _SafeRedirectHandler((parts.scheme, parts.hostname or "", parts.port))
        )
        for attempt in range(self.attempts):
            _validate_public_https(url)
            request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(4_000_001)
                    if len(raw) > 4_000_000:
                        raise ProviderProtocolError("provider JSON response exceeded the size limit")
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ProviderProtocolError("provider returned a non-object JSON response")
                    return value
            except urllib.error.HTTPError as exc:
                diagnostics = self._http_error_diagnostics(exc, attempt + 1)
                if exc.code in {401, 403}:
                    raise ProviderAuthenticationError(
                        "provider rejected the configured credential", diagnostics=diagnostics
                    ) from None
                if exc.code == 429:
                    if attempt + 1 >= self.attempts:
                        raise ProviderRateLimitError(
                            "provider rate limit persisted after retries", diagnostics=diagnostics
                        ) from None
                    self._sleep(self._retry_delay(attempt, exc.headers.get("Retry-After")))
                    continue
                if exc.code in {408, 425, 500, 502, 503, 504} and attempt + 1 < self.attempts:
                    self._sleep(self._retry_delay(attempt, exc.headers.get("Retry-After")))
                    continue
                raise ProviderRequestError(
                    f"provider request failed with HTTP {exc.code}", diagnostics=diagnostics
                ) from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
                if attempt + 1 >= self.attempts:
                    raise ProviderRequestError(
                        "provider network request failed after retries",
                        diagnostics={"attempts": self.attempts, "transport": "network"},
                    ) from None
                self._sleep(self._retry_delay(attempt, None))
            except json.JSONDecodeError as exc:
                raise ProviderProtocolError("provider returned invalid JSON") from exc
        raise ProviderRequestError("provider request failed")

    def download(self, url: str, destination: Path, *, max_bytes: int) -> DownloadResult:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        for attempt in range(self.attempts):
            _validate_public_https(url)
            request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            try:
                with self._opener.open(request, timeout=self.timeout) as response, temporary.open("wb") as output:
                    declared = response.headers.get("Content-Length")
                    if declared and int(declared) > max_bytes:
                        raise ProviderDownloadError("provider download exceeded the size limit")
                    digest = hashlib.sha256()
                    written = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise ProviderDownloadError("provider download exceeded the size limit")
                        digest.update(chunk)
                        output.write(chunk)
                    if written == 0:
                        raise ProviderDownloadError("provider returned an empty download")
                    content_type = response.headers.get_content_type()
                temporary.replace(destination)
                return DownloadResult(destination, written, digest.hexdigest(), content_type)
            except ProviderDownloadError:
                temporary.unlink(missing_ok=True)
                raise
            except urllib.error.HTTPError as exc:
                temporary.unlink(missing_ok=True)
                if exc.code not in {408, 425, 429, 500, 502, 503, 504} or attempt + 1 >= self.attempts:
                    raise ProviderDownloadError(
                        "failed to download provider output",
                        diagnostics={"http_status": exc.code, "attempts": attempt + 1},
                    ) from None
                self._sleep(self._retry_delay(attempt, exc.headers.get("Retry-After")))
            except (OSError, urllib.error.URLError, TimeoutError, socket.timeout):
                temporary.unlink(missing_ok=True)
                if attempt + 1 >= self.attempts:
                    raise ProviderDownloadError(
                        "failed to download provider output",
                        diagnostics={"attempts": self.attempts, "transport": "network"},
                    ) from None
                self._sleep(self._retry_delay(attempt, None))
        raise ProviderDownloadError("failed to download provider output")

    def upload_file(self, url: str, source: Path, *, content_type: str) -> None:
        if not source.is_file() or source.stat().st_size > 150_000_000:
            raise ProviderRequestError("reference upload source is unavailable or too large")
        size = source.stat().st_size
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        for attempt in range(self.attempts):
            _validate_public_https(url)
            connection = http.client.HTTPSConnection(
                parts.hostname, parts.port or 443, timeout=self.timeout,
                context=ssl.create_default_context(),
            )
            try:
                connection.putrequest("PUT", path, skip_accept_encoding=True)
                connection.putheader("Content-Type", content_type)
                connection.putheader("Content-Length", str(size))
                connection.putheader("User-Agent", DEFAULT_USER_AGENT)
                connection.endheaders()
                with source.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        connection.send(chunk)
                response = connection.getresponse()
                response.read(64_001)
                if 200 <= response.status < 300:
                    return
                if response.status in {301, 302, 303, 307, 308}:
                    raise ProviderRequestError("provider upload redirect was refused")
                if response.status not in {408, 425, 429, 500, 502, 503, 504}:
                    raise ProviderRequestError(
                        "provider upload was rejected",
                        diagnostics={"http_status": response.status, "attempts": attempt + 1},
                    )
            except ProviderRequestError:
                raise
            except (OSError, TimeoutError, socket.timeout, http.client.HTTPException):
                if attempt + 1 >= self.attempts:
                    raise ProviderRequestError(
                        "provider reference upload failed",
                        diagnostics={"attempts": self.attempts, "transport": "network"},
                    ) from None
            finally:
                connection.close()
            self._sleep(self._retry_delay(attempt, None))
        raise ProviderRequestError("provider reference upload failed")

    @staticmethod
    def _http_error_diagnostics(exc: urllib.error.HTTPError, attempts: int) -> dict[str, Any]:
        result: dict[str, Any] = {"http_status": exc.code, "attempts": attempts}
        try:
            raw = exc.read(256_001)
            if len(raw) <= 256_000:
                value = json.loads(raw)
                if isinstance(value, dict):
                    data = value.get("data") if isinstance(value.get("data"), dict) else value
                    request_id = data.get("request_id") or data.get("RequestId")
                    error_code = data.get("code") or data.get("error_code")
                    if isinstance(request_id, (str, int)):
                        result["request_id"] = str(request_id)[:128]
                    if isinstance(error_code, (str, int)):
                        result["provider_code"] = str(error_code)[:64]
        except Exception:
            pass
        return result

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.1), 30.0)
            except ValueError:
                pass
        return min(0.5 * (2**attempt) + random.random() * 0.2, 8.0)
