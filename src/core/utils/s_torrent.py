"""Where torrent traffic went, as the node's report tells it (s edition).

Xray reports up to three spellings of the target: ``destination``,
``originalTarget`` (the address the connection really went to, e.g.
``tcp:1.2.3.4:6881``) and ``routeTarget`` (what routing matched, often the
sniffed domain). The IP and the domain are picked from whichever has them.
"""

import ipaddress
import re
from dataclasses import dataclass
from typing import Optional

# [tcp:|udp:]host[:port], the host an IPv4, a [v6] or a name
_ENDPOINT = re.compile(
    r"^(?:(?P<net>tcp|udp):)?(?:\[(?P<v6>[0-9a-fA-F:.]+)\]|(?P<host>[^:\s]+))(?::(?P<port>\d+))?$"
)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: Optional[str]

    @property
    def is_ip(self) -> bool:
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            return False
        return True

    def address(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}" if self.port else host


def parse_endpoint(raw: Optional[str]) -> Optional[Endpoint]:
    text = (raw or "").strip()
    if not text or text == "unknown":
        return None
    match = _ENDPOINT.match(text)
    if match is None:
        return None
    host = match.group("v6") or match.group("host")
    if not host:
        return None
    return Endpoint(host=host, port=match.group("port"))


@dataclass(frozen=True)
class TorrentTarget:
    ip: Optional[str]  # with the port when known
    domain: Optional[str]


def torrent_target(*raws: Optional[str]) -> TorrentTarget:
    """The IP (with its port) and the sniffed domain from the report's fields."""
    endpoints = [e for e in (parse_endpoint(raw) for raw in raws) if e is not None]
    ip = next((e for e in endpoints if e.is_ip), None)
    domain = next((e.host for e in endpoints if not e.is_ip), None)
    port = next((e.port for e in endpoints if e.port), None)
    if ip is not None and ip.port is None and port:
        ip = Endpoint(host=ip.host, port=port)
    return TorrentTarget(ip=ip.address() if ip else None, domain=domain)
