from loguru import logger
from redis.asyncio import Redis

from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.s_keys import WebhookDeliveryKey

# The panel gives up after three attempts five seconds apart, so a few minutes
# cover every retry of one delivery with room to spare.
DELIVERY_TTL_SECONDS = 300


class WebhookDeliveryGuard:
    """Lets each signed panel delivery through once.

    The signature is an HMAC of the body, so two deliveries with the same one
    are the same event sent again -- a panel retry after a slow answer, or a
    proxy replaying the request. A retry the panel makes because processing
    failed must still get through, so a failed delivery is released.
    """

    def __init__(self, redis: Redis, ttl: int = DELIVERY_TTL_SECONDS) -> None:
        self.redis = redis
        self.ttl = ttl

    @staticmethod
    def _key(signature: str) -> str:
        return serialize_storage_key(WebhookDeliveryKey(signature=signature.strip().lower()))

    async def claim(self, signature: str) -> bool:
        """True for the first delivery of this signature, False for a repeat."""
        if not signature.strip():
            return True
        claimed = await self.redis.set(self._key(signature), "1", ex=self.ttl, nx=True)
        if not claimed:
            logger.info("Repeated Remnawave webhook delivery ignored")
        return bool(claimed)

    async def release(self, signature: str) -> None:
        if signature.strip():
            await self.redis.delete(self._key(signature))
