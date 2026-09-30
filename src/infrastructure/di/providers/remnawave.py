from collections.abc import AsyncIterator

from dishka import Provider, Scope, provide
from httpx import AsyncClient, Timeout
from loguru import logger
from remnapy import RemnawaveSDK

from src.core.config import AppConfig
from src.core.types import RemnawaveHttpClient


class RemnawaveProvider(Provider):
    scope = Scope.APP

    @provide
    async def get_http_client(self, config: AppConfig) -> AsyncIterator[RemnawaveHttpClient]:
        logger.debug("Initializing Remnawave HTTP client")

        headers = {}
        headers["Authorization"] = f"Bearer {config.remnawave.token.get_secret_value()}"
        headers["X-Api-Key"] = config.remnawave.caddy_token.get_secret_value()
        headers["CF-Access-Client-Id"] = config.remnawave.cf_client_id.get_secret_value()
        headers["CF-Access-Client-Secret"] = config.remnawave.cf_client_secret.get_secret_value()

        if not config.remnawave.is_external:
            headers["x-forwarded-proto"] = "https"
            headers["x-forwarded-for"] = "127.0.0.1"

        client = AsyncClient(
            base_url=f"{config.remnawave.url.get_secret_value()}/api",
            headers=headers,
            cookies=config.remnawave.cookies,
            verify=True,
            timeout=Timeout(connect=15.0, read=25.0, write=10.0, pool=5.0),
        )

        try:
            yield RemnawaveHttpClient(client)
        finally:
            await client.aclose()
            logger.debug("Remnawave AsyncClient closed")

    @provide
    def get_remnawave(self, client: RemnawaveHttpClient) -> RemnawaveSDK:
        # The SDK and the panel client for the 2.x contract share one connection
        # pool and one set of auth headers, so a call made through either is
        # authenticated and routed identically.
        logger.debug("Initializing RemnawaveSDK")
        return RemnawaveSDK(client)
