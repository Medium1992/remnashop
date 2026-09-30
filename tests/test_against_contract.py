"""Real flows driven against a panel that validates like the real one.

The other suites assert what goes on the wire. This one asserts the panel would
accept it: the stub applies the validation rules from the panel's own contract,
so a request that a live panel would answer with a 400 fails here instead of in
production.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from conftest import PANEL_ID, user_payload
from contract_stub import ContractPanel, PanelRejectionError, validate_update
from remnapy import RemnawaveSDK
from test_service_end_to_end import a_plan, a_subscription, a_user

from src.core.enums import SubscriptionStatus
from src.core.utils.remna_id import encode_remna_id
from src.infrastructure.services.remnawave import RemnawaveImpl

STORED_KEY = encode_remna_id(PANEL_ID)
LINES = ["v2", "v3"]


def build(line: str) -> tuple[RemnawaveImpl, ContractPanel]:
    panel = ContractPanel(line, user_payload(line=line))
    http = httpx.AsyncClient(
        base_url="http://panel.test/api", transport=httpx.MockTransport(panel.handler)
    )
    return RemnawaveImpl(RemnawaveSDK(http), http), panel  # type: ignore[arg-type]


async def connected(line: str) -> tuple[RemnawaveImpl, ContractPanel]:
    service, panel = build(line)
    await service.try_connection()
    return service, panel


FUTURE = datetime.now(timezone.utc) + timedelta(days=30)


class TestCreationIsAccepted:
    @pytest.mark.parametrize("line", LINES)
    async def test_creating_from_a_plan_passes_validation(self, line: str) -> None:
        service, panel = await connected(line)

        await service.create_user(a_user(), plan=a_plan())

        assert panel.rejections == []
        assert panel.accepted[-1][0] == "create"

    @pytest.mark.parametrize("line", LINES)
    async def test_a_plan_without_a_tag_passes_validation(self, line: str) -> None:
        service, panel = await connected(line)

        await service.create_user(a_user(), plan=a_plan(tag=None))

        assert panel.rejections == []

    @pytest.mark.parametrize("line", LINES)
    async def test_a_user_without_an_email_passes_validation(self, line: str) -> None:
        service, panel = await connected(line)
        user = a_user()
        user.email = None

        await service.create_user(user, plan=a_plan())

        assert panel.rejections == []


class TestUpdatesAreAccepted:
    @pytest.mark.parametrize("line", LINES)
    async def test_a_renewal_passes_validation(self, line: str) -> None:
        service, panel = await connected(line)

        await service.update_user(
            a_user(), STORED_KEY, subscription=a_subscription(expire_at=FUTURE)
        )

        assert panel.rejections == []

    @pytest.mark.parametrize("line", LINES)
    async def test_detaching_a_squad_and_a_tag_passes_validation(self, line: str) -> None:
        # Both are sent as an explicit null, which only works because the panel
        # declares them nullable. If that were wrong, this is where it shows.
        service, panel = await connected(line)

        await service.update_user(
            a_user(),
            STORED_KEY,
            plan=a_plan(tag=None, external_squad=None, duration=30),
        )

        assert panel.rejections == []

    @pytest.mark.parametrize("line", LINES)
    async def test_a_disable_passes_validation(self, line: str) -> None:
        service, panel = await connected(line)

        await service.update_user(
            a_user(),
            STORED_KEY,
            subscription=a_subscription(
                expire_at=FUTURE, status=SubscriptionStatus.DISABLED
            ),
        )

        assert panel.rejections == []

    @pytest.mark.parametrize("line", LINES)
    async def test_the_addressing_key_satisfies_the_panel(self, line: str) -> None:
        # 2.x refines on (uuid | username), 3.x on (id | username). Sending the
        # wrong one for the line would be rejected outright.
        service, panel = await connected(line)

        await service.update_user(
            a_user(), STORED_KEY, subscription=a_subscription(expire_at=FUTURE)
        )

        body = panel.accepted[-1][1]
        assert body.get("uuid" if line == "v2" else "id") is not None


class TestTheExpiredSubscriptionRule:
    """Both lines refuse an update whose expireAt is already in the past."""

    def test_the_rule_is_transcribed_from_the_contract(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

        with pytest.raises(PanelRejectionError, match="past"):
            validate_update({"id": 1, "expireAt": past}, "v3")

    @pytest.mark.parametrize("line", LINES)
    async def test_a_lapsed_subscription_still_updates(self, line: str) -> None:
        # Reconciling an expired subscription, or applying a traffic promocode
        # to one, sends the local expiry unchanged -- and it is in the past. The
        # panel refuses the whole request over it, taking every other change
        # along with it, so the date is left out instead.
        service, panel = await connected(line)
        lapsed = a_subscription(
            expire_at=datetime.now(timezone.utc) - timedelta(days=1),
            status=SubscriptionStatus.EXPIRED,
            traffic_limit=500,
        )

        await service.update_user(a_user(), STORED_KEY, subscription=lapsed)

        assert panel.rejections == []
        body = panel.accepted[-1][1]
        assert "expireAt" not in body
        # The change that mattered still landed.
        assert body["trafficLimitBytes"] == 500 * 1024**3

    @pytest.mark.parametrize("line", LINES)
    async def test_a_lapsed_subscription_is_not_switched_back_on(self, line: str) -> None:
        # With the date left out, sending ACTIVE would re-enable the user
        # against an expiry that has passed, and they would keep access until
        # the panel's own expiry job noticed.
        service, panel = await connected(line)
        lapsed = a_subscription(
            expire_at=datetime.now(timezone.utc) - timedelta(days=1),
            status=SubscriptionStatus.EXPIRED,
        )

        await service.update_user(a_user(), STORED_KEY, subscription=lapsed)

        assert "status" not in panel.accepted[-1][1]

    @pytest.mark.parametrize("line", LINES)
    async def test_disabling_a_lapsed_subscription_still_disables(self, line: str) -> None:
        # DISABLED is an operator decision and holds whatever the date is.
        service, panel = await connected(line)
        lapsed = a_subscription(
            expire_at=datetime.now(timezone.utc) - timedelta(days=1),
            status=SubscriptionStatus.DISABLED,
        )

        await service.update_user(a_user(), STORED_KEY, subscription=lapsed)

        assert panel.accepted[-1][1]["status"] == "DISABLED"

    @pytest.mark.parametrize("line", LINES)
    async def test_a_future_expiry_is_always_sent(self, line: str) -> None:
        service, panel = await connected(line)

        await service.update_user(
            a_user(), STORED_KEY, subscription=a_subscription(expire_at=FUTURE)
        )

        assert "expireAt" in panel.accepted[-1][1]

    @pytest.mark.parametrize("line", LINES)
    async def test_a_renewal_of_a_lapsed_subscription_sends_the_new_date(
        self, line: str
    ) -> None:
        # The ordinary recovery path: the purchase moves the date forward first,
        # so the panel sees a future date and reactivates the user.
        service, panel = await connected(line)

        await service.update_user(
            a_user(),
            STORED_KEY,
            subscription=a_subscription(
                expire_at=FUTURE, status=SubscriptionStatus.ACTIVE
            ),
        )

        assert panel.rejections == []
        assert panel.accepted[-1][1]["status"] == "ACTIVE"
        assert "expireAt" in panel.accepted[-1][1]

    @pytest.mark.parametrize("line", LINES)
    async def test_a_renewal_that_moves_the_date_forward_is_fine(self, line: str) -> None:
        # The ordinary path: a purchase extends the date before the update runs.
        service, panel = await connected(line)

        await service.update_user(
            a_user(), STORED_KEY, subscription=a_subscription(expire_at=FUTURE)
        )

        assert panel.rejections == []


class TestRulesTheStubItselfEnforces:
    """Guards the stub: a rule that never fires would prove nothing."""

    def test_a_null_in_a_non_nullable_field_is_refused(self) -> None:
        with pytest.raises(PanelRejectionError, match="not nullable"):
            validate_update({"id": 1, "trafficLimitBytes": None}, "v3")

    def test_a_null_in_a_nullable_field_is_allowed(self) -> None:
        validate_update({"id": 1, "externalSquadUuid": None, "tag": None}, "v3")

    def test_an_update_with_no_addressing_key_is_refused(self) -> None:
        with pytest.raises(PanelRejectionError, match="must be provided"):
            validate_update({"trafficLimitBytes": 1}, "v3")

    def test_the_wrong_addressing_key_for_the_line_is_refused(self) -> None:
        # An id means nothing to a 2.x panel.
        with pytest.raises(PanelRejectionError, match="must be provided"):
            validate_update({"id": 1, "trafficLimitBytes": 1}, "v2")

    def test_a_lowercase_tag_is_refused(self) -> None:
        with pytest.raises(PanelRejectionError, match="uppercase"):
            validate_update({"id": 1, "tag": "basic"}, "v3")

    def test_an_overlong_tag_is_refused(self) -> None:
        with pytest.raises(PanelRejectionError, match="16 characters"):
            validate_update({"id": 1, "tag": "A" * 17}, "v3")


class TestPlanTagsRemnaShopCanProduce:
    @pytest.mark.parametrize("tag", ["BASIC", "PRO_1", "IMPORTED", "A", "X" * 16])
    @pytest.mark.parametrize("line", LINES)
    async def test_a_valid_plan_tag_is_accepted(
        self, line: str, tag: str, monkeypatch: Any
    ) -> None:
        service, panel = await connected(line)

        await service.create_user(a_user(), plan=a_plan(tag=tag))

        assert panel.rejections == []
