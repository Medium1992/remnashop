"""The web cabinet of the "s" edition (docs/s/README.md).

The cabinet is its own container (an nginx with the web app) on its own
domain; it reaches this backend through its nginx, which adds
``X-Cabinet-Proxy-Secret``. Two variables switch it on, the same names the
cabinet's own instructions use:

- ``CABINET_DOMAIN``: the cabinet's domain, without ``https://``;
- ``CABINET_PROXY_SECRET``: the same value as in the cabinet's ``.env``.

Without both, the cabinet's routes are not mounted at all.
"""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Optional

from loguru import logger

DOMAIN_ENV: Final[str] = "CABINET_DOMAIN"
SECRET_ENV: Final[str] = "CABINET_PROXY_SECRET"

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{32,}$")


@dataclass(frozen=True)
class CabinetConfig:
    domain: str
    proxy_secret: str

    @property
    def url(self) -> str:
        return f"https://{self.domain}"


@lru_cache(maxsize=1)
def cabinet_config() -> Optional[CabinetConfig]:
    domain = os.environ.get(DOMAIN_ENV, "").strip().lower().rstrip("/")
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not domain and not secret:
        return None
    if not _DOMAIN.match(domain):
        logger.warning(f"{DOMAIN_ENV} is not a domain (no https://, no slash): cabinet off")
        return None
    if not _SECRET.match(secret):
        logger.warning(
            f"{SECRET_ENV} must be 32+ letters, digits, _ or - (openssl rand -hex 32): cabinet off"
        )
        return None
    return CabinetConfig(domain=domain, proxy_secret=secret)


def signing_key(crypt_key: str) -> bytes:
    """The key cabinet sessions are signed with, derived from APP_CRYPT_KEY so
    that no other variable is needed and no other use of that key shares it."""
    return hmac.new(crypt_key.encode(), b"s-cabinet-session-v1", hashlib.sha256).digest()
