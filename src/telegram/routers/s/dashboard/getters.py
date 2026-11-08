import time
from typing import Any

from aiogram_dialog import DialogManager
from dishka import FromDishka
from dishka.integrations.aiogram_dialog import inject

from src.application.common.dao import SettingsDao
from src.application.use_cases.s.settings import NUMBER_FIELDS
from src.core.utils.client_versions import merge_sources
from src.infrastructure.services.s.client_versions import ClientVersionsService
from src.infrastructure.services.s.inactive import InactiveProfiles

STATUS_ICONS = {"ok": "🟢", "fallback": "🟡", "error": "🔴", "disabled": "⚫", "unset": "⚪"}


def channel_id(app: str, key: str) -> str:
    return f"{app}-{key}"


def split_channel_id(value: str) -> tuple[str, str]:
    app, _, key = value.partition("-")
    return app, key


def _ago(checked_at: float) -> int:
    return max(0, int((time.time() - checked_at) // 60)) if checked_at else -1


@inject
async def s_getter(
    dialog_manager: DialogManager,
    settings_dao: FromDishka[SettingsDao],
    versions: FromDishka[ClientVersionsService],
    **kwargs: Any,
) -> dict[str, Any]:
    s = (await settings_dao.get()).extra.s
    cache = await versions.load()
    sources = merge_sources(s.versions.sources)

    channels = []
    for app, conf in sources.items():
        for key, channel in conf["channels"].items():
            state = cache.channel(app, key)
            status = state.status if channel.get("enabled", True) else "disabled"
            channels.append(
                {
                    "id": channel_id(app, key),
                    "icon": STATUS_ICONS.get(status, "⚪"),
                    "title": f"{conf['title']} · {channel.get('label', key)}",
                    "value": state.version or state.display or "—",
                }
            )

    data: dict[str, Any] = {
        "slots_enabled": int(s.slots.enabled),
        "slots_base": s.slots.base_limit,
        "slots_ttl": s.slots.slot_ttl_minutes,
        "versions_enabled": int(s.versions.enabled),
        "versions_notify": int(s.versions.notify_enabled),
        "versions_ignore_patch": int(s.versions.ignore_patch),
        "versions_interval": s.versions.interval_days,
        "versions_refresh": s.versions.refresh_hours,
        "versions_min_devices": s.versions.observed_min_devices,
        "versions_checked": _ago(cache.checked_at),
        "channels": channels,
        "stale_enabled": int(s.stale.enabled),
        "stale_hours": s.stale.stale_hours,
        "stale_interval": s.stale.interval_days,
        "inactive_enabled": int(s.inactive.enabled),
        "inactive_days": s.inactive.delete_after_days,
        "inactive_warn": s.inactive.warn_days,
        "inactive_include_disabled": int(s.inactive.include_disabled),
        "routers": "\n".join(f"<code>{h}</code>" for h in s.router_hwids) or "—",
        "routers_count": len(s.router_hwids),
        "kind": dialog_manager.dialog_data.get("s_kind", "stale"),
    }

    selected = dialog_manager.dialog_data.get("s_channel")
    if selected:
        app, key = split_channel_id(selected)
        channel = sources.get(app, {}).get("channels", {}).get(key, {})
        state = cache.channel(app, key)
        data.update(
            {
                "ch_title": f"{sources[app]['title']} · {channel.get('label', key)}",
                "ch_type": channel.get("type", "?"),
                "ch_where": channel.get("repo")
                or channel.get("app_id")
                or channel.get("package")
                or channel.get("version")
                or "—",
                "ch_url": channel.get("url") or "—",
                "ch_enabled": int(channel.get("enabled", True)),
                "ch_value": state.version or state.display or "—",
                "ch_published": state.published or "—",
                "ch_status": state.status or "—",
                "ch_source": state.source or "—",
                "ch_checked": _ago(state.checked_at),
            }
        )

    field = dialog_manager.dialog_data.get("s_field")
    if field:
        low, high = NUMBER_FIELDS[field]
        data.update({"field": field.replace(".", "-"), "field_low": low, "field_high": high})

    return data


@inject
async def inactive_list_getter(
    dialog_manager: DialogManager,
    inactive: FromDishka[InactiveProfiles],
    **kwargs: Any,
) -> dict[str, Any]:
    rows = await inactive.listing()
    return {"inactive": rows, "inactive_count": len(rows)}
