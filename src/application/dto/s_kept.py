from dataclasses import dataclass
from typing import Final

KEPT_FIELDS: Final[tuple[str, ...]] = ("devices", "traffic", "squads")


@dataclass(frozen=True)
class KeptLimitsDto:
    """What of a subscription an admin set by hand and chose to keep on renewal."""

    devices: bool = False
    traffic: bool = False
    squads: bool = False

    @property
    def any(self) -> bool:
        return self.devices or self.traffic or self.squads
