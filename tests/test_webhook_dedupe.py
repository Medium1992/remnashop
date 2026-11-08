"""A signed panel delivery is processed once.

The panel retries a webhook it did not get a quick answer for, and every
attempt carries the same signature. Without a guard each attempt published its
event again -- a user could get the same expiry notice twice. A delivery whose
processing failed is the exception: the panel retries it on purpose, and that
retry must get through.
"""

import json
from typing import Any, Dict, List, Optional

import pytest
from conftest import user_payload
from fastapi import HTTPException
from starlette.requests import Request
from test_webhook_payloads import envelope, sign

from src.core.config import AppConfig
from src.infrastructure.services.webhook_dedupe import WebhookDeliveryGuard
from src.web.endpoints.remnawave import _process_remnawave_webhook


class FakeRedis:
    def __init__(self) -> None:
        self.data: Dict[str, str] = {}

    async def set(self, name: str, value: str, ex: Optional[int] = None, nx: bool = False) -> bool:
        if nx and name in self.data:
            return False
        self.data[name] = value
        return True

    async def delete(self, *names: str) -> int:
        return sum(1 for n in names if self.data.pop(n, None) is not None)


class RecordingService:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.events: List[str] = []

    async def handle_user_event(self, event: str, user: Any, hours: Any) -> None:
        self.events.append(event)
        if self.fail:
            raise RuntimeError("boom")


class NullPublisher:
    async def publish(self, event: Any) -> None:
        return None


def request(body: str, headers: Dict[str, str]) -> Request:
    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": body.encode(), "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope, receive)


def a_delivery() -> tuple[str, Dict[str, str]]:
    body = json.dumps(envelope("user.expired", user_payload(line="v3")))
    return body, sign(body)


async def deliver(
    body: str, headers: Dict[str, str], service: RecordingService, guard: WebhookDeliveryGuard
) -> int:
    response = await _process_remnawave_webhook(
        request=request(body, headers),
        config=AppConfig.get(),
        remna_webhook_service=service,  # type: ignore[arg-type]
        event_publisher=NullPublisher(),  # type: ignore[arg-type]
        delivery_guard=guard,
    )
    return response.status_code


class TestRepeatedDeliveries:
    async def test_a_repeat_is_acknowledged_but_not_processed(self) -> None:
        guard = WebhookDeliveryGuard(FakeRedis())  # type: ignore[arg-type]
        service = RecordingService()
        body, headers = a_delivery()

        assert await deliver(body, headers, service, guard) == 200
        assert await deliver(body, headers, service, guard) == 200

        assert service.events == ["user.expired"]

    async def test_different_events_are_both_processed(self) -> None:
        guard = WebhookDeliveryGuard(FakeRedis())  # type: ignore[arg-type]
        service = RecordingService()
        first = json.dumps(envelope("user.expired", user_payload(line="v3")))
        second = json.dumps(envelope("user.enabled", user_payload(line="v3")))

        await deliver(first, sign(first), service, guard)
        await deliver(second, sign(second), service, guard)

        assert service.events == ["user.expired", "user.enabled"]

    async def test_a_failed_delivery_is_processed_again_on_retry(self) -> None:
        guard = WebhookDeliveryGuard(FakeRedis())  # type: ignore[arg-type]
        body, headers = a_delivery()

        with pytest.raises(HTTPException):
            await deliver(body, headers, RecordingService(fail=True), guard)

        retry = RecordingService()
        assert await deliver(body, headers, retry, guard) == 200
        assert retry.events == ["user.expired"]

    async def test_a_badly_signed_delivery_does_not_claim_the_signature(self) -> None:
        # Otherwise a forged request could pre-empt the real one.
        redis = FakeRedis()
        guard = WebhookDeliveryGuard(redis)  # type: ignore[arg-type]
        body, headers = a_delivery()
        forged = json.dumps(envelope("user.disabled", user_payload(line="v3")))

        with pytest.raises(HTTPException):
            await deliver(forged, headers, RecordingService(), guard)

        assert redis.data == {}
        service = RecordingService()
        await deliver(body, headers, service, guard)
        assert service.events == ["user.expired"]
