"""Shaped panel payloads and a recording transport.

The payloads are built field-by-field from the panel's own contract at the tags
they claim to represent (``libs/contract`` at 2.7.4 and at 3.4.x), so a test that
passes here is a test against what the panel actually sends -- including the
fields that differ between the lines, which is the whole point of this layer.
"""

import json
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx
import pytest
from remnapy import RemnawaveSDK

PANEL_ID = 18465
PANEL_UUID = "6f4d3a2b-1c0e-4f8a-9b7c-5d3e2f1a0b9c"
SHORT_UUID = "abcdef0123456789abcdef01"
USERNAME = "rs_123456789"
TELEGRAM_ID = 123456789
SQUAD_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def user_payload(*, line: str, panel_id: int = PANEL_ID, **overrides: Any) -> Dict[str, Any]:
    """A user as the given panel line returns it.

    The only structural difference is ``uuid``: present through 2.8.x, dropped
    by the 3.0.0 migration that made the numeric id the primary key.
    """
    payload: Dict[str, Any] = {
        "id": panel_id,
        "shortUuid": SHORT_UUID,
        "username": USERNAME,
        "status": "ACTIVE",
        "trafficLimitBytes": 107374182400,
        "trafficLimitStrategy": "NO_RESET",
        "expireAt": "2026-12-31T23:59:59.000Z",
        "telegramId": TELEGRAM_ID,
        "email": "user@example.com",
        "description": "name: Test",
        "tag": None,
        "hwidDeviceLimit": 3,
        "externalSquadUuid": None,
        "trojanPassword": "trojanpassword12",
        "vlessUuid": "11111111-2222-3333-4444-555555555555",
        "ssPassword": "sspassword123456",
        "lastTriggeredThreshold": 0,
        "subRevokedAt": None,
        "lastTrafficResetAt": None,
        "createdAt": "2025-01-17T15:38:45.065Z",
        "updatedAt": "2025-06-01T10:00:00.000Z",
        "subscriptionUrl": f"https://sub.example.com/{SHORT_UUID}",
        "activeInternalSquads": [{"uuid": SQUAD_UUID, "name": "Main"}],
        "userTraffic": {
            "usedTrafficBytes": 1073741824,
            "lifetimeUsedTrafficBytes": 5368709120,
            "onlineAt": "2025-06-01T09:00:00.000Z",
            "firstConnectedAt": "2025-02-01T09:00:00.000Z",
            "lastConnectedNodeUuid": "99999999-8888-7777-6666-555555555555",
        },
    }

    if line == "v2":
        payload["uuid"] = PANEL_UUID

    payload.update(overrides)
    return payload


def device_payload(*, line: str, hwid: str = "HW-1", panel_id: int = PANEL_ID) -> Dict[str, Any]:
    """A HWID device as the given line returns it.

    2.x labels the owner ``userUuid``; 3.x renamed it to ``userId`` and added
    ``requestIp``.
    """
    payload: Dict[str, Any] = {
        "hwid": hwid,
        "platform": "android",
        "osVersion": "14",
        "deviceModel": "Pixel",
        "userAgent": "Happ/1.0",
        "createdAt": "2025-03-01T00:00:00.000Z",
        "updatedAt": "2025-03-02T00:00:00.000Z",
    }

    if line == "v2":
        payload["userUuid"] = PANEL_UUID
    else:
        payload["userId"] = panel_id
        payload["requestIp"] = "203.0.113.7"

    return payload


class RecordedCall:
    def __init__(self, request: httpx.Request) -> None:
        self.method = request.method
        self.path = request.url.path
        self.query = dict(request.url.params)
        self.body: Optional[Any] = None
        if request.content:
            self.body = json.loads(request.content.decode())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.method} {self.path} query={self.query} body={self.body}>"


class PanelStub:
    """Answers panel routes from a routing table and records what was asked."""

    def __init__(self) -> None:
        self.calls: List[RecordedCall] = []
        self._routes: Dict[tuple, Any] = {}

    def on(self, method: str, path: str, payload: Any, status: int = 200) -> None:
        self._routes[(method.upper(), path)] = (status, payload)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(RecordedCall(request))
        key = (request.method, request.url.path)

        if key not in self._routes:
            return httpx.Response(
                404,
                json={"message": "Route not stubbed", "code": "A025", "statusCode": 404},
            )

        status, payload = self._routes[key]
        if status >= 400:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, json={"response": payload})

    @property
    def last(self) -> RecordedCall:
        assert self.calls, "no call was made"
        return self.calls[-1]

    def paths(self) -> List[str]:
        return [f"{c.method} {c.path}" for c in self.calls]


@pytest.fixture
def panel() -> PanelStub:
    return PanelStub()


@pytest.fixture
def http(panel: PanelStub) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://panel.test/api",
        transport=httpx.MockTransport(panel.handler),
    )


@pytest.fixture
def sdk(http: httpx.AsyncClient) -> Any:
    return RemnawaveSDK(http)


def as_uuid(value: str) -> UUID:
    return UUID(value)
