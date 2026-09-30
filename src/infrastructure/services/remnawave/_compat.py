"""Filling in fields that only newer panels send.

remnapy 3.2.1 models the panel as of 3.2.1, and marks a few fields required that
older panels in the supported range simply do not send. Pydantic treats a
missing required field as a hard failure, so a dashboard screen that reads one
of these would not degrade -- it would raise.

Rather than carry a second set of models, the payload is normalised into the
shape remnapy expects before validation. Each entry here records the panel
version that introduced the field, so the list can be retired as the supported
floor moves up.

Established by comparing every required field of the models RemnaShop reads
against the panel's own contract at 2.7.4 and 3.4.x.
"""

from typing import Any, Dict, List


def normalise_node(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A node as remnapy expects it.

    ``id`` arrived in panel 3.2.1. Before that a node is identified by ``uuid``
    alone, and nothing RemnaShop shows uses the numeric id, so a placeholder is
    enough to let the rest of the record through.
    """
    if "id" not in raw:
        return {**raw, "id": 0}
    return raw


def normalise_host(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A host as remnapy expects it.

    Panel 2.8.0 renamed ``xHttpExtraParams`` to ``xhttpExtraParams`` and ``tag``
    to ``tags``, and dropped ``allowInsecure``. Only the first is required by
    remnapy, but the other two are mapped as well so a 2.7.x host still displays
    its tag rather than an empty one.
    """
    host = dict(raw)

    if "xhttpExtraParams" not in host:
        host["xhttpExtraParams"] = host.get("xHttpExtraParams")

    if "tags" not in host:
        tag = host.get("tag")
        host["tags"] = [tag] if tag else []

    return host


def normalise_nodes(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [normalise_node(node) for node in raw]


def normalise_webhook_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """A pushed event in the shape remnapy's webhook models expect.

    The webhook models are stricter than the API ones -- a node event insists on
    fields that arrived in 2.8.0 and 3.1.0, and a device event on the owner and
    request ip that 2.8.0 introduced. An older panel omits them, and an event
    that fails to parse is not degraded, it is dropped: the endpoint answers an
    error and the notification never reaches the user.

    The signature is checked against the untouched body before this runs.
    """
    event = str(body.get("event") or "")
    data = body.get("data")

    if not isinstance(data, dict):
        return body

    if event.startswith("node."):
        return {**body, "data": _webhook_node(data)}

    if event.startswith("user_hwid_devices."):
        return {**body, "data": _webhook_device_event(data)}

    return body


def _webhook_node(raw: Dict[str, Any]) -> Dict[str, Any]:
    # id arrived in 3.1.0; the other three in 2.8.0.
    node = dict(raw)
    node.setdefault("id", 0)
    node.setdefault("proxyUrl", None)
    node.setdefault("note", None)
    node.setdefault("nodeConsumptionMultiplier", node.get("consumptionMultiplier"))
    return node


def _webhook_device_event(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A ``user_hwid_devices.*`` event, whose data nests the user and the device."""
    device = raw.get("hwidUserDevice")
    if not isinstance(device, dict):
        return raw

    user = raw.get("user")
    owner = user.get("id") if isinstance(user, dict) else None

    # 2.7.x names the owner userUuid and sends no requestIp; the numeric owner
    # is taken from the user the same event carries.
    patched = dict(device)
    if "userId" not in patched and owner is not None:
        patched["userId"] = owner
    patched.setdefault("requestIp", None)

    return {**raw, "hwidUserDevice": patched}


def normalise_hosts(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [normalise_host(host) for host in raw]
