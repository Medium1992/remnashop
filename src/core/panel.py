"""Which Remnawave API contract a connected panel speaks.

Across the supported range the panel's API forms two stable contracts rather
than a version-by-version drift, so the compatibility layer branches once
instead of carrying per-version special cases:

* ``V2`` -- panels 2.7.0 through 2.8.x. A user is addressed by ``uuid``.
* ``V3`` -- panels 3.0.0 and up. ``users.uuid`` no longer exists; a user is
  addressed by the numeric ``id``, the ip-control controller became
  ``connections``, and the per-user lookup routes (by telegram id, email, tag)
  were replaced by filters on the user list.

Verified against the panel's own contract history: within each line the request
and response shapes RemnaShop depends on are unchanged (3.0.0 -> 3.4.x is purely
additive for them, and 2.7.4 -> 2.8.1 differs only in the webhook event names
and the hosts model). The one break inside the 2.x line is the expiry webhook
events, which changed in 2.8.0 and is handled where those events are parsed.
"""

from enum import StrEnum
from typing import Final

from packaging.version import Version

# 2.7.0 is the oldest panel RemnaShop has ever supported; below it the user
# model differs in ways this layer deliberately does not carry.
MIN_PANEL_VERSION: Final[Version] = Version("2.7.0")

# The boundary between the two contracts. Chosen from the panel's own history:
# 3.0.0 is the release that dropped users.uuid and renamed ip-control.
V3_PANEL_VERSION: Final[Version] = Version("3.0.0")

# The newest panel line this layer has been checked against. Panels above it are
# allowed but warned about, since a future line may break the contract again.
MAX_PANEL_VERSION: Final[Version] = Version("3.5.0")

# Expiry notifications switched from four discrete events to a single
# user.expiration carrying meta.expiration.
EXPIRATION_EVENT_VERSION: Final[Version] = Version("2.8.0")


def _released(version: Version) -> Version:
    """Strip any pre-release or local marker before comparing.

    The panel reports whatever sits in its package.json. Under PEP 440 a
    ``3.0.0-rc.1`` sorts *below* ``3.0.0``, so comparing it directly would file a
    3.0.0 release candidate under the V2 contract and make every user lookup hit
    a route that no longer exists. Compare on the release numbers instead, so a
    pre-release is treated as the line it belongs to.
    """
    return Version(version.base_version)


class PanelLine(StrEnum):
    V2 = "v2"
    V3 = "v3"

    @classmethod
    def of(cls, version: Version) -> "PanelLine":
        return cls.V3 if _released(version) >= V3_PANEL_VERSION else cls.V2

    @property
    def is_v3(self) -> bool:
        return self is PanelLine.V3


def is_supported(version: Version) -> bool:
    return _released(version) >= MIN_PANEL_VERSION


def is_ahead_of_known(version: Version) -> bool:
    """Whether the panel is newer than any line this layer was verified against."""
    return _released(version) >= MAX_PANEL_VERSION


def uses_expiration_event(version: Version) -> bool:
    """Whether the panel emits ``user.expiration`` instead of the four legacy events."""
    return _released(version) >= EXPIRATION_EVENT_VERSION
