from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import deque
from typing import Any, Callable

from ..core.errors import AuthenticationError, ValidationError


class TripoWebhookVerifier:
    """Verify Tripo HMAC events and reject replayed deliveries.

    The verifier has no listener and opens no public port. A user-operated HTTPS
    relay may forward the untouched body and headers through authenticated local IPC.
    """

    def __init__(
        self,
        secret_reader: Callable[[], str],
        *,
        clock: Callable[[], float] = time.time,
        max_age_seconds: int = 300,
        dedupe_limit: int = 4096,
    ) -> None:
        self._secret_reader = secret_reader
        self._clock = clock
        self._max_age = max_age_seconds
        self._dedupe_limit = dedupe_limit
        self._deliveries: set[str] = set()
        self._delivery_order: deque[str] = deque()
        self._lock = threading.Lock()

    def verify(
        self, raw_body: bytes, *, timestamp: str, signature: str, delivery_id: str
    ) -> dict[str, Any]:
        if not raw_body or len(raw_body) > 1_000_000:
            raise ValidationError("webhook body is empty or too large")
        try:
            numeric_timestamp = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise AuthenticationError("webhook timestamp is invalid") from exc
        if abs(self._clock() - numeric_timestamp) > self._max_age:
            raise AuthenticationError("webhook timestamp is outside the allowed window")
        if not delivery_id or len(delivery_id) > 256:
            raise AuthenticationError("webhook delivery id is invalid")
        versions = {}
        for part in signature.split(","):
            key, separator, value = part.strip().partition("=")
            if separator and key in {"t", "v1"}:
                versions[key] = value
        signed_timestamp = versions.get("t", timestamp)
        if signed_timestamp != timestamp or not versions.get("v1"):
            raise AuthenticationError("webhook signature is invalid")
        expected = hmac.new(
            self._secret_reader().encode("utf-8"),
            timestamp.encode("ascii") + b"." + raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, versions["v1"]):
            raise AuthenticationError("webhook signature is invalid")
        with self._lock:
            if delivery_id in self._deliveries:
                return {"duplicate": True}
            self._deliveries.add(delivery_id)
            self._delivery_order.append(delivery_id)
            while len(self._delivery_order) > self._dedupe_limit:
                self._deliveries.discard(self._delivery_order.popleft())
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise ValidationError("webhook body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("webhook body must be an object")
        return {"duplicate": False, "payload": payload}
