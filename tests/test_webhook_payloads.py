"""Webhook payloads as each panel line really sends them, end to end.

This is the least defended path in the whole integration: the panel pushes, and
if the payload does not parse the bot answers 401 or 503 and the event is simply
lost. A 3.x payload has no ``uuid`` and labels a device owner ``userId``, so the
models had to accept both shapes.

Payloads are built from the panel's webhook contract, signed the way the panel
signs them, and pushed through the same parsing the endpoint uses.
"""

import hashlib
import hmac
import json
from typing import Any, Dict

import pytest
from conftest import PANEL_ID, PANEL_UUID, SQUAD_UUID, user_payload
from remnapy.controllers import WebhookUtility
from remnapy.models.webhook import (
    UserDto,
    UserHwidDeviceEventDto,
    WebhookNodeDto,
)

from src.application.services.remnawave import (
    RemnaUserEvent,
    RemnaUserHwidDevicesEvent,
)
from src.core.utils.remna_id import decode_remna_id, remna_key
from src.infrastructure.services.remnawave._compat import normalise_webhook_payload

SECRET = "test_webhook_secret"


def sign(body: str) -> Dict[str, str]:
    """Headers the panel sends alongside a webhook."""
    digest = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return {
        "x-remnawave-signature": digest,
        "x-remnawave-timestamp": "1780000000",
    }


def envelope(event: str, data: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    payload = {
        "event": event,
        "timestamp": "2026-06-01T12:00:00.000Z",
        "data": data,
    }
    payload.update(extra)
    return payload


def parse(payload: Dict[str, Any]) -> Any:
    """Exactly what the endpoint does: verify the raw body, then reshape it."""
    body = json.dumps(payload)

    if not WebhookUtility.validate_webhook_with_headers(body, sign(body), SECRET):
        return None

    return WebhookUtility.parse_webhook(
        body=normalise_webhook_payload(json.loads(body)),
        headers=sign(body),
        webhook_secret=SECRET,
        validate=False,
    )


class TestSignature:
    def test_a_correctly_signed_payload_is_accepted(self) -> None:
        assert parse(envelope("user.enabled", user_payload(line="v3"))) is not None

    def test_a_payload_with_a_wrong_signature_is_rejected(self) -> None:
        body = json.dumps(envelope("user.enabled", user_payload(line="v3")))
        result = WebhookUtility.parse_webhook(
            body=body,
            headers={
                "x-remnawave-signature": "0" * 64,
                "x-remnawave-timestamp": "1780000000",
            },
            webhook_secret=SECRET,
            validate=True,
        )
        assert result is None


class TestUserEvents:
    @pytest.mark.parametrize("line", ["v2", "v3"])
    @pytest.mark.parametrize(
        "event",
        [
            RemnaUserEvent.CREATED,
            RemnaUserEvent.MODIFIED,
            RemnaUserEvent.DELETED,
            RemnaUserEvent.ENABLED,
            RemnaUserEvent.DISABLED,
            RemnaUserEvent.LIMITED,
            RemnaUserEvent.EXPIRED,
            RemnaUserEvent.REVOKED,
            RemnaUserEvent.FIRST_CONNECTED,
        ],
    )
    def test_every_user_event_parses_on_both_lines(self, line: str, event: str) -> None:
        payload = parse(envelope(event, user_payload(line=line)))

        assert payload is not None
        assert WebhookUtility.is_user_event(payload.event)
        user = WebhookUtility.get_typed_data(payload)
        assert isinstance(user, UserDto)
        assert user.id == PANEL_ID

    def test_a_three_x_payload_parses_without_a_uuid(self) -> None:
        # The field the 3.0.0 migration dropped. Requiring it would have made
        # every webhook from a 3.x panel fail validation and be lost.
        data = user_payload(line="v3")
        assert "uuid" not in data

        payload = parse(envelope("user.enabled", data))

        assert payload is not None

    def test_the_stored_key_is_derivable_from_a_webhook_on_both_lines(self) -> None:
        # This is how the handler finds the local user for an incoming event.
        for line in ("v2", "v3"):
            payload = parse(envelope("user.enabled", user_payload(line=line)))
            user = WebhookUtility.get_typed_data(payload)
            assert decode_remna_id(remna_key(user)) == PANEL_ID

    def test_a_two_x_payload_still_carries_its_uuid_but_it_is_not_the_key(self) -> None:
        # The uuid is present on 2.x and must not be what RemnaShop keys on,
        # or the same user would be keyed differently before and after upgrade.
        data = user_payload(line="v2")
        assert data["uuid"] == PANEL_UUID

        payload = parse(envelope("user.enabled", data))
        user = WebhookUtility.get_typed_data(payload)

        assert decode_remna_id(remna_key(user)) == PANEL_ID

    def test_the_squads_on_a_user_event_survive_parsing(self) -> None:
        payload = parse(envelope("user.modified", user_payload(line="v3")))
        user = WebhookUtility.get_typed_data(payload)

        assert [str(s.uuid) for s in user.active_internal_squads] == [SQUAD_UUID]


class TestExpirationEvent:
    def test_the_new_event_carries_its_hours_in_meta(self) -> None:
        # From 2.8.0 the expiry notification is one event plus meta.expiration.
        payload = parse(
            envelope(
                RemnaUserEvent.EXPIRATION,
                user_payload(line="v3"),
                meta={"expiration": -48},
            )
        )

        assert payload is not None
        assert payload.meta is not None
        assert payload.meta.expiration == -48

    def test_a_past_expiry_is_reported_as_positive_hours(self) -> None:
        payload = parse(
            envelope(
                RemnaUserEvent.EXPIRATION,
                user_payload(line="v3"),
                meta={"expiration": 24},
            )
        )

        assert payload.meta.expiration == 24

    def test_the_legacy_events_parse_with_no_meta_at_all(self) -> None:
        # A 2.7.x panel sends no meta; the endpoint must not trip over that.
        for event in [
            RemnaUserEvent.EXPIRES_IN_72_HOURS,
            RemnaUserEvent.EXPIRES_IN_48_HOURS,
            RemnaUserEvent.EXPIRES_IN_24_HOURS,
            RemnaUserEvent.EXPIRED_24_HOURS_AGO,
        ]:
            payload = parse(envelope(event, user_payload(line="v2")))

            assert payload is not None
            assert getattr(payload, "meta", None) is None or payload.meta.expiration is None

    def test_the_not_connected_event_keeps_its_own_meta_field(self) -> None:
        payload = parse(
            envelope(
                RemnaUserEvent.NOT_CONNECTED,
                user_payload(line="v3"),
                meta={"notConnectedAfterHours": 72},
            )
        )

        assert payload.meta.not_connected_after_hours == 72


class TestDeviceEvents:
    @pytest.mark.parametrize("line", ["v2", "v3"])
    @pytest.mark.parametrize(
        "event",
        [RemnaUserHwidDevicesEvent.ADDED, RemnaUserHwidDevicesEvent.DELETED],
    )
    def test_a_device_event_parses_on_both_lines(self, line: str, event: str) -> None:
        # 3.x renamed the device owner from userUuid to userId, and the event
        # nests the user next to the device.
        device: Dict[str, Any] = {
            "hwid": "HW-1",
            "platform": "android",
            "osVersion": "14",
            "deviceModel": "Pixel",
            "userAgent": "Happ/1.0",
            "createdAt": "2025-03-01T00:00:00.000Z",
            "updatedAt": "2025-03-02T00:00:00.000Z",
        }
        if line == "v2":
            device["userUuid"] = PANEL_UUID
        else:
            device["userId"] = PANEL_ID
            device["requestIp"] = "203.0.113.7"

        payload = parse(
            envelope(event, {"user": user_payload(line=line), "hwidUserDevice": device})
        )

        assert payload is not None
        assert WebhookUtility.is_user_hwid_devices_event(payload.event)
        typed = WebhookUtility.get_typed_data(payload)
        assert isinstance(typed, UserHwidDeviceEventDto)
        assert typed.hwid_user_device.hwid == "HW-1"
        assert typed.user.id == PANEL_ID


def node_payload(*, line: str) -> Dict[str, Any]:
    """A node event as the given panel line sends it.

    2.8.0 added proxyUrl, note and nodeConsumptionMultiplier; 3.1.0 added id.
    """
    node: Dict[str, Any] = {
        "uuid": "11111111-2222-3333-4444-555555555555",
        "name": "DE-1",
        "address": "de1.example.com",
        "port": 2222,
        "countryCode": "DE",
        "isConnected": False,
        "isDisabled": False,
        "isConnecting": False,
        "isTrafficTrackingActive": False,
        "lastStatusMessage": "connection lost",
        "lastStatusChange": "2026-06-01T12:00:00.000Z",
        "usersOnline": 0,
        "viewPosition": 1,
        "trafficLimitBytes": 0,
        "trafficUsedBytes": 0,
        "trafficResetDay": None,
        "notifyPercent": None,
        "consumptionMultiplier": 1.0,
        "tags": [],
        "configProfile": {
            "activeConfigProfileUuid": None,
            "activeInbounds": [],
            "configProfileName": None,
            "configProfileInbounds": [],
        },
        "providerUuid": None,
        "provider": None,
        "activePluginUuid": None,
        "system": None,
        "versions": None,
        "xrayUptime": "0",
        "createdAt": "2025-01-01T00:00:00.000Z",
        "updatedAt": "2026-06-01T12:00:00.000Z",
    }

    if line != "2.7":
        node["proxyUrl"] = None
        node["note"] = None
        node["nodeConsumptionMultiplier"] = 1.0
    if line == "3.1+":
        node["id"] = 42

    return node


class TestNodeEvents:
    @pytest.mark.parametrize("line", ["2.7", "2.8", "3.1+"])
    def test_a_node_event_parses_on_every_supported_panel(self, line: str) -> None:
        # A node event that fails to parse is not degraded, it is lost: the
        # operator never hears that a node went down.
        payload = parse(envelope("node.connection_lost", node_payload(line=line)))

        assert payload is not None
        assert WebhookUtility.is_node_event(payload.event)
        node = WebhookUtility.get_typed_data(payload)
        assert isinstance(node, WebhookNodeDto)
        assert node.name == "DE-1"

    def test_a_newer_node_keeps_its_real_id(self) -> None:
        payload = parse(envelope("node.connection_lost", node_payload(line="3.1+")))
        node = WebhookUtility.get_typed_data(payload)

        assert node.id == 42
