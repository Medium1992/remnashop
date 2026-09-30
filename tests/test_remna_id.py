from uuid import UUID, uuid4

import pytest

from src.core.utils.remna_id import (
    SYNTHETIC_UPPER_BOUND,
    UNRESOLVED_REMNA_ID,
    decode_remna_id,
    encode_remna_id,
    is_synthetic_remna_id,
)


class TestRoundTrip:
    @pytest.mark.parametrize(
        "panel_id",
        [1, 2, 42, 18465, 999_999, 2**31 - 1, 2**31, 2**32, 2**47, (1 << 48) - 1],
    )
    def test_encode_then_decode_returns_the_same_id(self, panel_id: int) -> None:
        assert decode_remna_id(encode_remna_id(panel_id)) == panel_id

    def test_encoding_is_deterministic(self) -> None:
        assert encode_remna_id(18465) == encode_remna_id(18465)

    def test_distinct_ids_never_collide(self) -> None:
        encoded = {encode_remna_id(i) for i in range(1, 5000)}
        assert len(encoded) == 4999

    def test_known_value_matches_the_documented_layout(self) -> None:
        # The docstring of the codec advertises this exact mapping; the future
        # SQL migration depends on the id living in the last 12 hex digits.
        assert encode_remna_id(18465) == UUID("00000000-0000-0000-0000-000000004821")


class TestRejectedInput:
    @pytest.mark.parametrize("panel_id", [0, -1, -18465])
    def test_non_positive_ids_are_rejected(self, panel_id: int) -> None:
        # The panel validates user ids as positive, so a non-positive id is a bug
        # upstream of the codec and must not be silently encoded.
        with pytest.raises(ValueError):
            encode_remna_id(panel_id)

    def test_id_beyond_the_encoding_width_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            encode_remna_id(1 << 48)


class TestLegacyValues:
    def test_a_real_panel_uuid_decodes_to_none(self) -> None:
        # Legacy rows hold a genuine 2.x panel UUID, which must be recognised as
        # "needs resolving" rather than silently decoded into a bogus id.
        legacy = UUID("6f4d3a2b-1c0e-4f8a-9b7c-5d3e2f1a0b9c")
        assert decode_remna_id(legacy) is None
        assert not is_synthetic_remna_id(legacy)

    def test_random_uuids_are_practically_never_mistaken_for_synthetic(self) -> None:
        # A v4 UUID would need its top 80 bits to be zero to be misread.
        assert all(decode_remna_id(uuid4()) is None for _ in range(20_000))

    def test_the_nil_uuid_is_not_a_valid_identity(self) -> None:
        assert decode_remna_id(UNRESOLVED_REMNA_ID) is None
        assert not is_synthetic_remna_id(UNRESOLVED_REMNA_ID)


class TestSyntheticBoundary:
    def test_largest_encodable_id_is_below_the_upper_bound(self) -> None:
        assert encode_remna_id((1 << 48) - 1) < SYNTHETIC_UPPER_BOUND

    def test_upper_bound_itself_is_not_synthetic(self) -> None:
        assert decode_remna_id(SYNTHETIC_UPPER_BOUND) is None

    def test_the_bound_matches_the_sql_migration_predicate(self) -> None:
        # The documented migration selects rows with
        #   user_remna_id < '00000000-0000-0000-0001-000000000000'
        assert SYNTHETIC_UPPER_BOUND == UUID("00000000-0000-0000-0001-000000000000")

    def test_the_sql_predicate_separates_synthetic_from_legacy(self) -> None:
        # Mirrors what the future migration's WHERE clause will decide, so a
        # drift between the codec and the documented SQL fails here.
        synthetic = encode_remna_id(18465)
        legacy = UUID("6f4d3a2b-1c0e-4f8a-9b7c-5d3e2f1a0b9c")

        assert synthetic < SYNTHETIC_UPPER_BOUND
        assert not legacy < SYNTHETIC_UPPER_BOUND

    def test_last_twelve_hex_digits_hold_the_id(self) -> None:
        # The migration reads the id with right(user_remna_id::text, 12).
        assert int(str(encode_remna_id(18465)).replace("-", "")[-12:], 16) == 18465
