"""PrintLink discovery: mDNS/Zeroconf advertisement + host-ID resolution.

Every PC advertises service type _printlink._tcp.local. with its 9-digit ID
in the properties. Clients resolve an ID -> (ip, port) from the browse cache,
so no central server and no static IPs are needed.
"""
import socket
import threading
from zeroconf import NonUniqueNameException, ServiceInfo, ServiceBrowser, ServiceListener, Zeroconf

from identity import normalize_id
from config import MDNS_SERVICE_TYPE
from logutil import get_logger

log = get_logger("discovery")

SERVICE_TYPE = MDNS_SERVICE_TYPE
BROWSE_SETTLE_S = 1.5


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))   # no traffic is actually sent
        return s.getsockname()[0]
    finally:
        s.close()


class Discovery(ServiceListener):
    def __init__(self, my_id: str, port: int, advertise: bool = True):
        """advertise=False: browse-only (short-lived CLI processes must never
        claim the mDNS name or they collide with the running agent)."""
        self.my_id, self.port = normalize_id(my_id), port
        self.zc = Zeroconf()
        self._hosts: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()
        if advertise:
            info = ServiceInfo(
                SERVICE_TYPE,
                f"printlink-{self.my_id}.{SERVICE_TYPE}",
                addresses=[socket.inet_aton(_local_ip())],
                port=port,
                properties={b"id": self.my_id.encode()},
            )
            try:
                self.zc.register_service(info)
            except NonUniqueNameException:
                log.warning("mDNS name already advertised by another instance; "
                            "continuing in browse-only mode")
            except Exception as e:
                log.warning("mDNS advertise failed: %r; continuing browse-only", e)
        self._browser = ServiceBrowser(self.zc, SERVICE_TYPE, self)

    # ---- ServiceListener callbacks (called from zeroconf thread) ----
    def _record(self, zc, name):
        info = zc.get_service_info(SERVICE_TYPE, name, timeout=2000)
        if not info or not info.addresses:
            return
        host_id = (info.properties.get(b"id") or b"").decode()
        if host_id and host_id != self.my_id:
            with self._lock:
                self._hosts[host_id] = (socket.inet_ntoa(info.addresses[0]), info.port)

    def add_service(self, zc, type_, name):
        self._record(zc, name)

    def update_service(self, zc, type_, name):
        self._record(zc, name)

    def remove_service(self, zc, type_, name):
        pass  # hosts drop out via resolution failure; no need to track removal

    # ---- public API used by sender.py ----
    def resolve(self, host_id: str) -> tuple[str, int] | None:
        with self._lock:
            return self._hosts.get(normalize_id(host_id))

    def known_hosts(self) -> dict[str, tuple[str, int]]:
        with self._lock:
            return dict(self._hosts)

    def close(self):
        self.zc.close()
