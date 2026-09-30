import json
from typing import cast

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, HTTPException, Request, Response, status
from loguru import logger
from remnapy.controllers import WebhookUtility
from remnapy.models.webhook import (
    TorrentBlockerReportDto,
    UserDto,
    UserHwidDeviceEventDto,
    WebhookNodeDto,
)

from src.application.common import EventPublisher
from src.application.events import ErrorEvent
from src.application.services import RemnaWebhookService
from src.core.config import AppConfig
from src.core.constants import API_V1, REMNAWAVE_WEBHOOK_PATH
from src.infrastructure.services.remnawave._compat import normalise_webhook_payload

router = APIRouter(prefix=API_V1, include_in_schema=False)


async def _process_remnawave_webhook(
    request: Request,
    config: AppConfig,
    remna_webhook_service: RemnaWebhookService,
    event_publisher: EventPublisher,
) -> Response:
    try:
        raw_body = await request.body()
        decoded = raw_body.decode("utf-8")
        logger.debug(f"Received Remnawave webhook raw body: '{decoded[:500]}'")

        secret = config.remnawave.webhook_secret.get_secret_value()

        # The signature covers the body exactly as it arrived, so it is checked
        # first; only then is the payload reshaped for panels that predate some
        # of the fields the models require.
        if not WebhookUtility.validate_webhook_with_headers(
            decoded, dict(request.headers), secret
        ):
            raise HTTPException(status_code=401)

        payload = WebhookUtility.parse_webhook(
            body=normalise_webhook_payload(json.loads(decoded)),
            headers=dict(request.headers),
            webhook_secret=secret,
            validate=False,
        )
    except Exception as e:
        logger.exception(f"Webhook validation failed with error '{e}'")
        raise HTTPException(status_code=401)

    if not payload:
        logger.warning("Payload is empty after validation")
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        if WebhookUtility.is_user_event(payload.event):
            user = cast(UserDto, WebhookUtility.get_typed_data(payload))
            # Panels from 2.8.0 carry the hours-to-expiry here; older ones encode
            # it in the event name instead and leave meta empty.
            meta = getattr(payload, "meta", None)
            expiration_hours = getattr(meta, "expiration", None) if meta else None
            await remna_webhook_service.handle_user_event(
                payload.event, user, expiration_hours
            )

        elif WebhookUtility.is_user_hwid_devices_event(payload.event):
            event = cast(UserHwidDeviceEventDto, WebhookUtility.get_typed_data(payload))
            await remna_webhook_service.handle_device_event(
                payload.event,
                event.user,
                event.hwid_user_device,
            )

        elif WebhookUtility.is_node_event(payload.event):
            node = cast(WebhookNodeDto, WebhookUtility.get_typed_data(payload))
            await remna_webhook_service.handle_node_event(payload.event, node)

        elif WebhookUtility.is_torrent_blocker_event(payload.event):
            report = cast(TorrentBlockerReportDto, WebhookUtility.get_typed_data(payload))
            await remna_webhook_service.handle_torrent_blocker_event(report)

        else:
            logger.warning(f"Unhandled Remnawave event type '{payload.event}'")

    except Exception as e:
        logger.exception(f"Failed to process Remnawave webhook due to '{e}'")
        error_event = ErrorEvent(**config.build.data, exception=e)
        await event_publisher.publish(error_event)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(status_code=status.HTTP_200_OK)


@router.post(REMNAWAVE_WEBHOOK_PATH)
@inject
async def remnawave_webhook(
    request: Request,
    config: FromDishka[AppConfig],
    remna_webhook_service: FromDishka[RemnaWebhookService],
    event_publisher: FromDishka[EventPublisher],
) -> Response:
    return await _process_remnawave_webhook(
        request=request,
        config=config,
        remna_webhook_service=remna_webhook_service,
        event_publisher=event_publisher,
    )
