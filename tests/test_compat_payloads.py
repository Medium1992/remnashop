"""Records the panel fields remnapy requires but older panels do not send.

remnapy 3.2.1 models the panel as of 3.2.1. A field it marks required and an
older panel omits is not a missing value -- pydantic raises, and the screen that
reads it dies. These were found by comparing every required field of the models
RemnaShop reads against the panel's contract at 2.7.4 and at 3.4.x.
"""

from typing import Any, Dict

import pytest
from remnapy.models import HostResponseDto, NodeResponseDto

from src.infrastructure.services.remnawave._compat import (
    normalise_host,
    normalise_hosts,
    normalise_node,
    normalise_nodes,
)


def node_payload(*, line: str) -> Dict[str, Any]:
    """A node as the given panel line returns it. ``id`` arrived in 3.2.1."""
    node: Dict[str, Any] = {
        "uuid": "11111111-2222-3333-4444-555555555555",
        "name": "DE-1",
        "address": "de1.example.com",
        "port": 2222,
        "isConnected": True,
        "isDisabled": False,
        "isConnecting": False,
        "isTrafficTrackingActive": False,
        "viewPosition": 1,
        "countryCode": "DE",
        "consumptionMultiplier": 1.0,
        "trafficLimitBytes": 0,
        "trafficUsedBytes": 0,
        "notifyPercent": None,
        "trafficResetDay": None,
        "usersOnline": 5,
        "lastStatusChange": "2025-06-01T00:00:00.000Z",
        "lastStatusMessage": None,
        "xrayUptime": "0",
        "createdAt": "2025-01-01T00:00:00.000Z",
        "updatedAt": "2025-06-01T00:00:00.000Z",
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
        "tags": [],
    }
    if line == "new":
        node["id"] = 42
    return node


def host_payload(*, line: str) -> Dict[str, Any]:
    """A host as the given panel line returns it.

    2.8.0 renamed xHttpExtraParams -> xhttpExtraParams and tag -> tags, and
    dropped allowInsecure.
    """
    host: Dict[str, Any] = {
        "uuid": "aaaaaaaa-1111-2222-3333-444444444444",
        "viewPosition": 1,
        "remark": "DE",
        "address": "de.example.com",
        "port": 443,
        "path": None,
        "sni": None,
        "host": None,
        "alpn": None,
        "fingerprint": None,
        "muxParams": None,
        "sockoptParams": None,
        "inbound": {"configProfileUuid": None, "configProfileInboundUuid": None},
        "serverDescription": None,
        "vlessRouteId": None,
        "shuffleHost": False,
        "mihomoX25519": False,
        "nodes": [],
        "isDisabled": False,
        "securityLayer": "DEFAULT",
        "isHidden": False,
        "overrideSniFromAddress": False,
        "keepSniBlank": False,
        "xrayJsonTemplateUuid": None,
        "excludedInternalSquads": [],
        "excludeFromSubscriptionTypes": [],
    }
    if line == "old":
        host["xHttpExtraParams"] = {"mode": "auto"}
        host["tag"] = "PREMIUM"
        host["allowInsecure"] = False
    else:
        host["xhttpExtraParams"] = {"mode": "auto"}
        host["tags"] = ["PREMIUM"]
    return host


class TestNodes:
    def test_a_node_from_a_panel_before_3_2_1_parses(self) -> None:
        # Without the fill-in this raises: id is required by the model and
        # absent from every panel below 3.2.1.
        node = NodeResponseDto.model_validate(normalise_node(node_payload(line="old")))
        assert node.name == "DE-1"

    def test_a_node_from_a_newer_panel_keeps_its_real_id(self) -> None:
        node = NodeResponseDto.model_validate(normalise_node(node_payload(line="new")))
        assert node.id == 42

    def test_the_raw_payload_is_not_mutated(self) -> None:
        raw = node_payload(line="old")
        normalise_node(raw)
        assert "id" not in raw

    def test_a_list_is_normalised_element_by_element(self) -> None:
        raw = [node_payload(line="old"), node_payload(line="new")]
        nodes = [NodeResponseDto.model_validate(n) for n in normalise_nodes(raw)]
        assert [n.id for n in nodes] == [0, 42]

    def test_an_unparsed_old_node_would_have_failed(self) -> None:
        # Guards the premise: if remnapy ever relaxes id, this test tells us the
        # fill-in is no longer needed rather than leaving it as dead code.
        with pytest.raises(Exception):
            NodeResponseDto.model_validate(node_payload(line="old"))


class TestHosts:
    def test_a_host_from_a_2_7_panel_parses(self) -> None:
        host = HostResponseDto.model_validate(normalise_host(host_payload(line="old")))
        assert host.remark == "DE"
        assert host.xhttp_extra_params == {"mode": "auto"}

    def test_a_2_7_host_keeps_its_tag(self) -> None:
        # 2.7.x has a single tag; 2.8.0 turned it into a list. Dropping it would
        # show every older host as untagged.
        host = HostResponseDto.model_validate(normalise_host(host_payload(line="old")))
        assert host.tags == ["PREMIUM"]

    def test_a_2_7_host_without_a_tag_gets_an_empty_list(self) -> None:
        raw = host_payload(line="old")
        raw["tag"] = None
        host = HostResponseDto.model_validate(normalise_host(raw))
        assert host.tags == []

    def test_a_host_from_a_newer_panel_is_untouched(self) -> None:
        host = HostResponseDto.model_validate(normalise_host(host_payload(line="new")))
        assert host.tags == ["PREMIUM"]
        assert host.xhttp_extra_params == {"mode": "auto"}

    def test_the_raw_payload_is_not_mutated(self) -> None:
        raw = host_payload(line="old")
        normalise_host(raw)
        assert "xhttpExtraParams" not in raw
        assert "tags" not in raw

    def test_a_list_is_normalised_element_by_element(self) -> None:
        raw = [host_payload(line="old"), host_payload(line="new")]
        hosts = [HostResponseDto.model_validate(h) for h in normalise_hosts(raw)]
        assert [h.tags for h in hosts] == [["PREMIUM"], ["PREMIUM"]]

    def test_an_unparsed_old_host_would_have_failed(self) -> None:
        with pytest.raises(Exception):
            HostResponseDto.model_validate(host_payload(line="old"))
