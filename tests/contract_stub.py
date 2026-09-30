"""A panel stub that rejects what the real panel would reject.

The other stub answers whatever it is asked. This one enforces the validation
rules the panel's own contract declares for the endpoints RemnaShop writes to,
so a request that would come back as a 400 from a live panel comes back as a 400
here.

Every rule below is transcribed from ``libs/contract`` at the tag it claims, and
the two lines are kept apart where they differ. Rules that are identical in both
are stated once.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
TAG_RE = re.compile(r"^[A-Z0-9_]+$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Optional but NOT nullable in either line: sending null fails validation.
NON_NULLABLE = frozenset(
    {
        "status",
        "expireAt",
        "trafficLimitBytes",
        "trafficLimitStrategy",
        "activeInternalSquads",
        "username",
        "shortUuid",
        "vlessUuid",
        "trojanPassword",
        "ssPassword",
    }
)

# Optional and nullable: null means "clear this".
NULLABLE = frozenset(
    {"description", "tag", "telegramId", "email", "hwidDeviceLimit", "externalSquadUuid"}
)


class PanelRejectionError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _check_nullability(body: Dict[str, Any]) -> None:
    for field, value in body.items():
        if value is None and field in NON_NULLABLE:
            raise PanelRejectionError(f"'{field}' is optional but not nullable")


def _check_tag(body: Dict[str, Any]) -> None:
    tag = body.get("tag")
    if tag is None:
        return
    if not TAG_RE.match(tag):
        raise PanelRejectionError(
            "Tag can only contain uppercase letters, numbers, underscores"
        )
    if len(tag) > 16:
        raise PanelRejectionError("Tag must be less than 16 characters")


def _check_limits(body: Dict[str, Any]) -> None:
    traffic = body.get("trafficLimitBytes")
    if traffic is not None and traffic < 0:
        raise PanelRejectionError("Traffic limit must be greater than 0")

    devices = body.get("hwidDeviceLimit")
    if devices is not None and devices < 0:
        raise PanelRejectionError("Device limit must be non-negative")


def _check_squads(body: Dict[str, Any]) -> None:
    for value in body.get("activeInternalSquads") or []:
        if not UUID_RE.match(str(value)):
            raise PanelRejectionError("'activeInternalSquads' must be an array of UUIDs")

    squad = body.get("externalSquadUuid")
    if squad is not None and not UUID_RE.match(str(squad)):
        raise PanelRejectionError("External squad must be a UUID")


def _check_common(body: Dict[str, Any]) -> None:
    _check_nullability(body)
    _check_tag(body)
    _check_limits(body)
    _check_squads(body)


def validate_create(body: Dict[str, Any], line: str) -> None:
    username = body.get("username")
    if not username:
        raise PanelRejectionError("Username is required")
    if not USERNAME_RE.match(username):
        raise PanelRejectionError(
            "Username can only contain letters, numbers, underscores and dashes"
        )
    if not 3 <= len(username) <= 36:
        raise PanelRejectionError("Username must be 3-36 characters")

    if not body.get("expireAt"):
        raise PanelRejectionError("Expiration date is required")

    if line == "v3" and "uuid" in body:
        # 3.0.0 removed the field; zod strips unknown keys, but pinning an
        # identity RemnaShop can no longer read back is a bug either way.
        raise PanelRejectionError("'uuid' is not accepted when creating a user")

    _check_common(body)


def validate_update(body: Dict[str, Any], line: str) -> None:
    key = "uuid" if line == "v2" else "id"

    if body.get(key) is None and not body.get("username"):
        raise PanelRejectionError(f"Either {key} or username must be provided")

    if line == "v2" and body.get("uuid") and not UUID_RE.match(str(body["uuid"])):
        raise PanelRejectionError("uuid must be a UUID")

    expire_at = body.get("expireAt")
    if expire_at is not None:
        when = datetime.fromisoformat(str(expire_at).replace("Z", "+00:00"))
        if when <= datetime.now(timezone.utc):
            # Present in both lines: .refine((date) => date > new Date())
            raise PanelRejectionError("Expiration date cannot be in the past")

    _check_common(body)


class ContractPanel:
    """Answers like a panel of the given line, and validates what it is sent."""

    def __init__(self, line: str, user: Dict[str, Any]) -> None:
        self.line = line
        self.user = user
        self.rejections: List[Tuple[str, str]] = []
        self.accepted: List[Tuple[str, Dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: Optional[Dict[str, Any]] = None
        if request.content:
            body = json.loads(request.content.decode())

        try:
            if request.method == "POST" and path == "/api/users":
                validate_create(body or {}, self.line)
                self.accepted.append(("create", body or {}))
            elif request.method == "PATCH" and path == "/api/users":
                validate_update(body or {}, self.line)
                self.accepted.append(("update", body or {}))
        except PanelRejectionError as e:
            self.rejections.append((path, e.message))
            return httpx.Response(
                400,
                json={"message": e.message, "code": "A019", "statusCode": 400},
            )

        return self._answer(path)

    def _answer(self, path: str) -> httpx.Response:
        if path.endswith("/system/metadata"):
            return httpx.Response(200, json={"response": self._metadata()})
        return httpx.Response(200, json={"response": self.user})

    def _metadata(self) -> Dict[str, Any]:
        return {
            "version": "2.7.4" if self.line == "v2" else "3.4.2",
            "build": {"time": "2026-01-01T00:00:00Z", "number": "1"},
            "git": {
                "backend": {
                    "commitSha": "abc1234",
                    "branch": "main",
                    "commitUrl": "https://example.invalid/a",
                },
                "frontend": {
                    "commitSha": "def5678",
                    "commitUrl": "https://example.invalid/f",
                },
            },
        }
