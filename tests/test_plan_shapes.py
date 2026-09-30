"""Every plan shape RemnaShop can build, put past a validating panel.

Plans are operator-configured, so the request built from one is the least
predictable thing RemnaShop sends. Each case below is a plan an operator can
actually create in the dashboard.
"""

from typing import List
from uuid import UUID

import pytest
from conftest import PANEL_ID
from test_against_contract import connected
from test_service_end_to_end import a_plan, a_user

from src.core.enums import PlanType
from src.core.utils.remna_id import encode_remna_id

STORED_KEY = encode_remna_id(PANEL_ID)
LINES = ["v2", "v3"]


class TestDurations:
    @pytest.mark.parametrize("duration", [1, 7, 30, 90, 180, 365, 730])
    @pytest.mark.parametrize("line", LINES)
    async def test_an_ordinary_duration_is_accepted(self, line: str, duration: int) -> None:
        service, panel = await connected(line)

        await service.create_user(a_user(), plan=a_plan(duration=duration))

        assert panel.rejections == [], panel.rejections

    @pytest.mark.parametrize("line", LINES)
    async def test_an_unlimited_plan_is_accepted(self, line: str) -> None:
        # Duration 0 means unlimited, which the converter turns into the year
        # 2099 rather than a null the panel would refuse.
        service, panel = await connected(line)

        await service.create_user(a_user(), plan=a_plan(duration=0))

        assert panel.rejections == [], panel.rejections
        assert panel.accepted[-1][1]["expireAt"].startswith("2099")

    @pytest.mark.parametrize("line", LINES)
    async def test_an_unlimited_plan_updates_too(self, line: str) -> None:
        # The update path refines the date as being in the future; 2099 is.
        service, panel = await connected(line)

        await service.update_user(a_user(), STORED_KEY, plan=a_plan(duration=0))

        assert panel.rejections == [], panel.rejections


class TestLimits:
    @pytest.mark.parametrize("traffic", [0, 1, 50, 1000, 10_000])
    @pytest.mark.parametrize("line", LINES)
    async def test_any_traffic_limit_is_accepted(self, line: str, traffic: int) -> None:
        service, panel = await connected(line)

        await service.create_user(a_user(), plan=a_plan(traffic_limit=traffic))

        assert panel.rejections == [], panel.rejections

    @pytest.mark.parametrize("devices", [0, 1, 3, 100])
    @pytest.mark.parametrize("line", LINES)
    async def test_any_device_limit_is_accepted(self, line: str, devices: int) -> None:
        service, panel = await connected(line)

        await service.create_user(a_user(), plan=a_plan(device_limit=devices))

        assert panel.rejections == [], panel.rejections

    @pytest.mark.parametrize("line", LINES)
    async def test_an_unlimited_plan_sends_zero_not_null(self, line: str) -> None:
        # trafficLimitBytes is optional but not nullable; zero is how the panel
        # spells unlimited.
        service, panel = await connected(line)

        await service.create_user(
            a_user(), plan=a_plan(traffic_limit=0, device_limit=0, type=PlanType.UNLIMITED)
        )

        body = panel.accepted[-1][1]
        assert body.get("trafficLimitBytes") in (0, None)
        assert body.get("trafficLimitBytes") is not None or "trafficLimitBytes" not in body


class TestSquads:
    @pytest.mark.parametrize("line", LINES)
    async def test_a_plan_with_no_squads_is_accepted(self, line: str) -> None:
        service, panel = await connected(line)

        await service.create_user(a_user(), plan=a_plan(internal_squads=[]))

        assert panel.rejections == [], panel.rejections

    @pytest.mark.parametrize("line", LINES)
    async def test_a_plan_with_several_squads_is_accepted(self, line: str) -> None:
        squads: List[UUID] = [
            UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            UUID("11111111-2222-3333-4444-555555555555"),
        ]
        service, panel = await connected(line)

        await service.create_user(a_user(), plan=a_plan(internal_squads=squads))

        assert panel.rejections == [], panel.rejections
        assert len(panel.accepted[-1][1]["activeInternalSquads"]) == 2

    @pytest.mark.parametrize("line", LINES)
    async def test_a_plan_with_an_external_squad_is_accepted(self, line: str) -> None:
        service, panel = await connected(line)

        await service.create_user(
            a_user(),
            plan=a_plan(external_squad=UUID("99999999-8888-7777-6666-555555555555")),
        )

        assert panel.rejections == [], panel.rejections


class TestUsernames:
    @pytest.mark.parametrize(
        "telegram_id", [1, 12345, 999999999, 9999999999, 1234567890123456789]
    )
    @pytest.mark.parametrize("line", LINES)
    async def test_the_generated_username_always_satisfies_the_panel(
        self, line: str, telegram_id: int
    ) -> None:
        # rs_<telegram_id> has to stay inside 3-36 characters and the allowed
        # character set, for any id Telegram can issue.
        service, panel = await connected(line)
        user = a_user()
        user.telegram_id = telegram_id

        await service.create_user(user, plan=a_plan())

        assert panel.rejections == [], panel.rejections
        assert panel.accepted[-1][1]["username"] == f"rs_{telegram_id}"

    @pytest.mark.parametrize("line", LINES)
    async def test_a_web_only_account_gets_an_acceptable_username(self, line: str) -> None:
        service, panel = await connected(line)
        user = a_user()
        user.telegram_id = None
        user.id = 4242

        await service.create_user(user, plan=a_plan())

        assert panel.rejections == [], panel.rejections
        assert panel.accepted[-1][1]["username"] == "rs_web_4242"


class TestTrialAndFallback:
    @pytest.mark.parametrize("line", LINES)
    async def test_a_user_created_without_a_plan_is_accepted(self, line: str) -> None:
        # The fallback path gives them ten years rather than no expiry at all.
        service, panel = await connected(line)

        await service.create_user(a_user())

        assert panel.rejections == [], panel.rejections
        assert panel.accepted[-1][1]["expireAt"]
