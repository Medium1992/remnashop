"""Codec that stores a Remnawave numeric user id inside a UUID column.

Remnawave dropped ``users.uuid`` in panel 3.0.0: the panel's own migration
renames ``users.t_id`` to ``users.id`` and drops the uuid column outright, so a
3.x panel has no UUID for a user at all. RemnaShop keeps the identity in
``subscriptions.user_remna_id``, which is ``UUID NOT NULL``, and that schema must
not change while this compatibility layer is a stopgap.

The numeric id is the one identifier that is stable across the whole supported
range: it is already the panel's primary key in 2.7.4 (exposed as ``id``, backed
by ``t_id``) and the 3.x migration only renames the column, so the same user
keeps the same number across a panel upgrade.

So the id is encoded into the UUID rather than a UUID being stored:

    panel id 18465  <->  00000000-0000-0000-0000-000000004821

The leading 80 bits are zero, which makes a synthetic value obvious on sight in
the database and keeps the encoding decodable in plain SQL, without touching the
panel, when an official RemnaShop release eventually replaces this layer::

    UPDATE subscriptions
       SET <new_column> = ('x' || right(user_remna_id::text, 12))::bit(48)::bigint
     WHERE user_remna_id < '00000000-0000-0000-0001-000000000000';

Values that are not of this shape are legacy real panel UUIDs written by an
earlier RemnaShop against a 2.x panel; :func:`decode_remna_id` returns ``None``
for them so callers can resolve and rewrite them.
"""

from typing import Final, Optional, Protocol
from uuid import UUID

# 48 bits for the id: the panel's id is a BigInt sequence starting at 1, and no
# deployment will come within many orders of magnitude of this ceiling.
_ID_BITS: Final[int] = 48
_MAX_ID: Final[int] = (1 << _ID_BITS) - 1

# Any synthetic value is strictly below this, because its top 80 bits are zero.
SYNTHETIC_UPPER_BOUND: Final[UUID] = UUID(int=1 << _ID_BITS)

# Reserved: id 0 never occurs, the panel validates user ids as positive.
UNRESOLVED_REMNA_ID: Final[UUID] = UUID(int=0)


def encode_remna_id(panel_id: int) -> UUID:
    """Encode a panel numeric user id into the UUID stored in the database."""
    if panel_id <= 0:
        raise ValueError(f"Panel user id must be positive, got '{panel_id}'")
    if panel_id > _MAX_ID:
        raise ValueError(f"Panel user id '{panel_id}' exceeds {_ID_BITS}-bit encoding")

    return UUID(int=panel_id)


def decode_remna_id(value: UUID) -> Optional[int]:
    """Decode a stored UUID back to a panel id, or ``None`` if it is a real UUID.

    ``None`` means the row still holds a genuine 2.x panel UUID (or the reserved
    unresolved marker) and the identity has to be resolved against the panel
    before it can be used.
    """
    as_int = value.int

    if as_int <= 0 or as_int > _MAX_ID:
        return None

    return as_int


def is_synthetic_remna_id(value: UUID) -> bool:
    """Whether the stored value encodes a panel id rather than being a real UUID."""
    return decode_remna_id(value) is not None


class HasPanelId(Protocol):
    id: int


def remna_key(remna_user: HasPanelId) -> UUID:
    """The value to store in ``user_remna_id`` for a user the panel returned.

    Every panel response and webhook carries the numeric ``id``, in both the 2.x
    and 3.x lines, so this is the one derivation that works everywhere. It
    replaces reading ``remna_user.uuid``, which no longer exists on 3.x.
    """
    return encode_remna_id(remna_user.id)
