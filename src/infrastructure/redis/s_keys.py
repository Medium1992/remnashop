"""Redis keys of the "s" edition (docs/s/README.md).

Kept apart from ``keys.py`` so that merges from upstream never touch them.
"""

from dataclasses import dataclass

from .key_builder import StorageKey


@dataclass(frozen=True)
class WebhookDeliveryKey(StorageKey, prefix="s_webhook_delivery"):
    signature: str


class ClientVersionsCacheKey(StorageKey, prefix="s_client_versions"): ...


@dataclass(frozen=True)
class DeviceNoticeKey(StorageKey, prefix="s_device_notice"):
    """One reminder of one kind about one device, kept for the reminder interval.

    ``device`` is a hash of the HWID: a raw HWID may contain the key separator.
    """

    kind: str
    device: str


@dataclass(frozen=True)
class SlotGrantKey(StorageKey, prefix="s_slot"):
    """An open temporary device slot, by the subscription's stored remna id."""

    remna_id: str
