"""Legacy read-only Alpaca paper REST gateway.

The production domain is intentionally absent. This adapter cannot be pointed at
live trading without a source-code change. Order entry has moved to the audited
AlpacaPaperService lifecycle; legacy mutation methods fail closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .domain import Order


PAPER_BASE_URL = "https://paper-api.alpaca.markets"


@dataclass(frozen=True, slots=True)
class AlpacaPaperGateway:
    key_id: str
    secret_key: str

    @classmethod
    def from_environment(cls) -> "AlpacaPaperGateway":
        key = os.environ.get("APCA_API_KEY_ID", "")
        secret = os.environ.get("APCA_API_SECRET_KEY", "")
        if not key or not secret:
            raise RuntimeError("paper API credentials are missing")
        return cls(key_id=key, secret_key=secret)

    def account(self) -> dict[str, object]:
        return self._request("GET", "/v2/account")

    def submit(self, order: Order) -> dict[str, object]:
        raise RuntimeError("legacy order entry is retired; use the audited AlpacaPaperService")

    def cancel_all(self) -> dict[str, object] | list[object]:
        raise RuntimeError("account-wide cancellation is disabled; reconcile specific strategy order ids")

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, object] | list[object]:
        if method != "GET":
            raise RuntimeError("legacy gateway only supports read-only requests")
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            PAPER_BASE_URL + path,
            data=body,
            method=method,
            headers={
                "APCA-API-KEY-ID": self.key_id,
                "APCA-API-SECRET-KEY": self.secret_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed paper host
                content = response.read()
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Alpaca paper API error {error.code}: {detail}") from error
        return json.loads(content) if content else {}
