"""The bot's login to "Мой налог": logging in, and keeping the login alive.

An access token lives about an hour; the refresh token for long (a year, or
with no end, by what others have seen), bound to the device id it was issued
to -- so the device id is made once and kept. A token is refreshed under a
row lock: the bot and the worker never refresh at once, and the one that
waited takes the token the other got.

When the refresh token stops working, a password login logs in again by
itself; an SMS login cannot, and the login is marked lost until an admin logs
in again. ``NalogLoginLostError`` is raised once, at that moment, so the caller
tells the admins once.
"""

import secrets
from dataclasses import replace
from datetime import timedelta
from typing import Callable, Final, NoReturn, Optional

from loguru import logger

from src.application.common.uow import UnitOfWork
from src.application.dto.s_receipts import NalogAccountDto, NalogLogin
from src.core.utils.time import datetime_now
from src.infrastructure.database.dao.s_receipts import NalogAccountDao

from .nalog import NalogAuthError, NalogClient, NalogError, NalogSession, SmsChallenge, token_expiry

# A token this close to its end is refreshed before use.
TOKEN_MARGIN: Final[timedelta] = timedelta(minutes=2)


class NalogNotLoggedInError(Exception):
    """No login to the cabinet, or it stopped working."""


class NalogLoginLostError(NalogNotLoggedInError):
    """The login stopped working just now."""


def _inn_check(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(w * d for w, d in zip(weights, digits)) % 11 % 10


def is_inn(value: str) -> bool:
    """A person's INN (a self-employed one, or a sole trader's): 12 digits that
    pass the checksum."""
    if len(value) != 12 or not value.isdigit():
        return False
    digits = [int(ch) for ch in value]
    first = _inn_check(digits, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
    second = _inn_check(digits, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
    return digits[10] == first and digits[11] == second


def is_payer_inn(value: str) -> bool:
    """An organisation's INN (10 digits) or a sole trader's (12)."""
    if len(value) == 10 and value.isdigit():
        digits = [int(ch) for ch in value]
        return digits[9] == _inn_check(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8))
    return is_inn(value)


def normalize_proxy(url: str) -> str:
    url = url.strip()
    # aiohttp_socks has no socks5h/socks4a; it resolves names remotely anyway.
    if url.startswith("socks5h://"):
        return url.replace("socks5h://", "socks5://", 1)
    if url.startswith("socks4a://"):
        return url.replace("socks4a://", "socks4://", 1)
    return url


def new_device_id() -> str:
    return secrets.token_hex(11)[:21]


class NalogAuth:
    def __init__(self, uow: UnitOfWork, account_dao: NalogAccountDao) -> None:
        self.uow = uow
        self.account_dao = account_dao
        # Tests point this at a fake cabinet.
        self.make_client: Callable[[Optional[str]], NalogClient] = lambda proxy: NalogClient(
            proxy=proxy
        )

    async def account(self) -> NalogAccountDto:
        async with self.uow:
            return await self.account_dao.get()

    async def client(self) -> NalogClient:
        return self.make_client((await self.account()).proxy)

    async def session(self) -> tuple[NalogClient, str, str]:
        """A client, a live token and the INN; ``NalogNotLoggedInError`` without."""
        account = await self.account()
        if not account.logged_in or not account.inn:
            raise NalogNotLoggedInError(account.lost_reason or "not logged in")
        if account.method == NalogLogin.KEY:
            return await self._key_session(account)
        if self._fresh(account):
            return self.make_client(account.proxy), account.token or "", account.inn

        async with self.uow:
            account = await self.account_dao.get(lock=True)
            if not account.logged_in or not account.inn:
                raise NalogNotLoggedInError(account.lost_reason or "not logged in")
            client = self.make_client(account.proxy)
            if self._fresh(account):
                # Refreshed by another process while this one waited.
                return client, account.token or "", account.inn

            try:
                session = await client.refresh(account.refresh_token or "", account.device_id)
            except NalogAuthError as e:
                session = await self._login_again(client, account, e)
            account = self._with_session(account, session)
            await self.account_dao.save(account)
            await self.uow.commit()
            logger.info("Tax cabinet token refreshed")
            return client, account.token or "", account.inn or ""

    async def _login_again(
        self, client: NalogClient, account: NalogAccountDto, error: NalogAuthError
    ) -> NalogSession:
        if account.method == NalogLogin.PASSWORD and account.inn and account.password:
            try:
                session = await client.login_password(
                    account.inn, account.password, account.device_id
                )
                logger.info("Tax cabinet refresh token was refused, logged in by password again")
                return session
            except NalogAuthError as e:
                error = e

        logger.error(f"Tax cabinet login lost: {error}")
        lost = replace(account, token=None, lost_at=datetime_now(), lost_reason=str(error))
        await self.account_dao.save(lost)
        await self.uow.commit()
        raise NalogLoginLostError(str(error)) from error

    async def _key_session(self, account: NalogAccountDto) -> tuple[NalogClient, str, str]:
        expires = account.token_expires_at
        if expires is not None and expires <= datetime_now():
            await self._lose(account, "the access key has expired")
        return self.make_client(account.proxy), account.token or "", account.inn or ""

    async def _lose(self, account: NalogAccountDto, reason: str) -> NoReturn:
        logger.error(f"Tax cabinet login lost: {reason}")
        async with self.uow:
            await self.account_dao.save(
                replace(account, lost_at=datetime_now(), lost_reason=reason[:500])
            )
            await self.uow.commit()
        raise NalogLoginLostError(reason)

    @staticmethod
    def _fresh(account: NalogAccountDto) -> bool:
        return bool(
            account.token
            and account.token_expires_at
            and account.token_expires_at - TOKEN_MARGIN > datetime_now()
        )

    @staticmethod
    def _with_session(account: NalogAccountDto, session: NalogSession) -> NalogAccountDto:
        return replace(
            account,
            token=session.token,
            token_expires_at=session.expires_at,
            refresh_token=session.refresh_token,
            inn=session.inn or account.inn,
            lost_at=None,
            lost_reason=None,
        )

    async def forget_token(self, reason: str = "") -> None:
        """The cabinet refused the token: the next ``session`` refreshes it.
        An access key has nothing to refresh it with: the login is lost then
        (``NalogLoginLostError``), the key kept for the admin to see."""
        account = await self.account()
        if account.method == NalogLogin.KEY:
            if account.lost_at is None:
                await self._lose(account, f"the access key was refused: {reason}".rstrip(": "))
            return
        async with self.uow:
            account = await self.account_dao.get(lock=True)
            if account.token:
                await self.account_dao.save(replace(account, token=None, token_expires_at=None))
            await self.uow.commit()

    # -- what an admin does -------------------------------------------------

    async def login_password(self, inn: str, password: str) -> NalogAccountDto:
        account = await self.account()
        device_id = account.device_id or new_device_id()
        session = await self.make_client(account.proxy).login_password(inn, password, device_id)
        account = self._with_session(
            replace(
                account,
                inn=inn,
                method=NalogLogin.PASSWORD,
                password=password,
                phone=None,
                device_id=device_id,
            ),
            session,
        )
        await self._save(account)
        logger.info("Logged in to the tax cabinet by password")
        return account

    async def sms_start(self, phone: str) -> SmsChallenge:
        return await (await self.client()).sms_start(phone)

    async def login_browser(self, inn: str, device_id: str, refresh_token: str) -> NalogAccountDto:
        """A session from a login made in a browser: its device id and refresh
        token. Refreshed once at once, so a key that does not work from here
        is refused now and not at the first payment."""
        account = await self.account()
        session = await self.make_client(account.proxy).refresh(refresh_token, device_id)
        if session.inn and session.inn != inn:
            raise NalogAuthError(f"The session belongs to INN {session.inn}, not {inn}", "inn")
        account = self._with_session(
            replace(
                account,
                inn=inn,
                method=NalogLogin.BROWSER,
                password=None,
                phone=None,
                device_id=device_id,
            ),
            session,
        )
        await self._save(account)
        logger.info("Logged in to the tax cabinet with a session from a browser")
        return account

    async def login_key(self, inn: str, key: str) -> NalogAccountDto:
        """An access key made in the cabinet. Tried at once on the taxpayer's
        profile, so a key that does not work is refused now."""
        account = await self.account()
        try:
            profile = await self.make_client(account.proxy).profile(key)
        except NalogError as e:
            if type(e) is not NalogError or e.status != 404:
                raise
            # The profile is not where it was looked for: the key itself is not
            # refused. The first receipt tells for sure.
            logger.warning(f"Access key not checked, no profile: {e}")
            profile = {}
        key_inn = str(profile.get("inn") or "")
        if key_inn and key_inn != inn:
            raise NalogAuthError(f"The key belongs to INN {key_inn}, not {inn}", "inn")
        account = replace(
            account,
            inn=inn,
            method=NalogLogin.KEY,
            token=key,
            token_expires_at=token_expiry(key),
            refresh_token=None,
            password=None,
            phone=None,
            lost_at=None,
            lost_reason=None,
        )
        await self._save(account)
        logger.info("Logged in to the tax cabinet with an access key")
        return account

    async def sms_verify(self, inn: str, phone: str, code: str, challenge: str) -> NalogAccountDto:
        account = await self.account()
        device_id = account.device_id or new_device_id()
        session = await self.make_client(account.proxy).sms_verify(
            phone, code, challenge, device_id
        )
        if session.inn and session.inn != inn:
            raise NalogAuthError(f"The phone belongs to INN {session.inn}, not {inn}", "inn")
        account = self._with_session(
            replace(
                account,
                inn=inn,
                method=NalogLogin.SMS,
                phone=phone,
                password=None,
                device_id=device_id,
            ),
            session,
        )
        await self._save(account)
        logger.info("Logged in to the tax cabinet by SMS")
        return account

    async def logout(self) -> None:
        account = await self.account()
        await self._save(NalogAccountDto(device_id=account.device_id, proxy=account.proxy))
        logger.info("Logged out of the tax cabinet")

    async def set_proxy(self, proxy: Optional[str]) -> None:
        account = await self.account()
        await self._save(replace(account, proxy=normalize_proxy(proxy) if proxy else None))

    async def _save(self, account: NalogAccountDto) -> None:
        async with self.uow:
            await self.account_dao.save(account)
            await self.uow.commit()
