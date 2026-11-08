"""Redis keys of the "s" edition (docs/s/README.md).

Kept apart from ``keys.py`` so that merges from upstream never touch them.
"""

from dataclasses import dataclass

from .key_builder import StorageKey


@dataclass(frozen=True)
class WebhookDeliveryKey(StorageKey, prefix="s_webhook_delivery"):
    signature: str
