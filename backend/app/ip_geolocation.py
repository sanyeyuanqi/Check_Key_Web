"""Offline IP region lookup using the bundled, versioned ip2region databases."""

import atexit
import logging
from functools import lru_cache
from ipaddress import IPv6Address, ip_address, ip_network
from pathlib import Path
from threading import Lock

from ip2region import searcher as xdb
from ip2region import util


_LOGGER = logging.getLogger(__name__)
_DATA_DIRECTORY = Path(__file__).resolve().parent.parent / "data" / "ip2region"
_PRIVATE_NETWORKS = tuple(map(ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")))
_SHARED_NETWORK = ip_network("100.64.0.0/10")
_ADDITIONAL_RESERVED = tuple(map(ip_network, ("3fff::/20", "fec0::/10")))
_COUNTRY_NAMES = {
    "CN": "中国", "US": "美国", "AU": "澳大利亚", "CA": "加拿大", "GB": "英国",
    "HK": "中国香港", "MO": "中国澳门", "TW": "中国台湾", "JP": "日本", "KR": "韩国",
    "SG": "新加坡", "DE": "德国", "FR": "法国", "NL": "荷兰", "RU": "俄罗斯",
    "IN": "印度", "BR": "巴西", "VN": "越南", "TH": "泰国", "MY": "马来西亚",
    "ID": "印度尼西亚", "PH": "菲律宾", "IT": "意大利", "ES": "西班牙", "CH": "瑞士",
    "SE": "瑞典", "FI": "芬兰", "NO": "挪威", "AE": "阿联酋", "TR": "土耳其",
    "MX": "墨西哥", "ZA": "南非", "NZ": "新西兰",
}
_READERS: dict[int, "_LockedReader | None"] = {}
_READERS_LOCK = Lock()


class _LockedReader:
    """Vector-index readers share a seekable file, so each query holds its lock."""

    def __init__(self, version: int):
        path = str(_DATA_DIRECTORY / f"ip2region_v{version}.xdb")
        util.verify_from_file(path)
        index = util.load_vector_index_from_file(path)
        if len(index) != util.VectorIndexLength:
            raise ValueError("Incomplete geolocation vector index")
        self.searcher = xdb.new_with_vector_index(util.IPv4 if version == 4 else util.IPv6, path, index)
        self.lock = Lock()

    def search(self, address: str) -> str:
        with self.lock:
            return self.searcher.search(address)

    def close(self) -> None:
        with self.lock:
            self.searcher.close()


def _get_reader(version: int) -> _LockedReader | None:
    with _READERS_LOCK:
        if version not in _READERS:
            try:
                _READERS[version] = _LockedReader(version)
            except Exception:
                # Fail once per address family. No IPs or credentials are logged.
                _LOGGER.warning("Offline IPv%s geolocation data unavailable; public regions will be empty", version)
                _READERS[version] = None
        return _READERS[version]


def _format_region(region: str) -> str | None:
    fields = region.split("|")
    if len(fields) != 5:
        return None
    country, province, city, _, code = (field.strip() for field in fields)
    country = _COUNTRY_NAMES.get(code.upper(), country)
    parts = []
    for field in (country, province, city):
        if field and field not in {"0", "-", "Unknown", "未知"} and field not in parts:
            parts.append(field)
    return " · ".join(parts) or None


@lru_cache(maxsize=4096)
def _resolve_public_ip(address: str, version: int) -> str | None:
    reader = _get_reader(version)
    if reader is None:
        return None
    try:
        return _format_region(reader.search(address))
    except Exception:
        # Corrupt/unreadable optional data must not break the export records API.
        return None


def resolve_ip_region(ip: str | None) -> str | None:
    """Return an offline region or an explicit non-geographic address-class label."""
    if not isinstance(ip, str) or not ip.strip() or len(ip) > 80:
        return None
    try:
        address = ip_address(ip.strip())
        if isinstance(address, IPv6Address):
            address = address.ipv4_mapped or ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return None
    if address.is_loopback:
        return "本机"
    if address.is_unspecified:
        return "未指定地址"
    if address.is_multicast:
        return "组播地址"
    if address.is_link_local:
        return "链路本地"
    if any(address.version == network.version and address in network for network in _PRIVATE_NETWORKS):
        return "内网"
    if address.version == 4 and address in _SHARED_NETWORK:
        return "共享地址"
    if not address.is_global or address.is_reserved or any(
        address.version == network.version and address in network for network in _ADDITIONAL_RESERVED
    ):
        return "保留地址"
    return _resolve_public_ip(str(address), address.version)


def clear_ip_region_cache() -> None:
    """Close readers and clear bounded caches, e.g. after replacing the data files."""
    with _READERS_LOCK:
        for reader in _READERS.values():
            if reader is not None:
                reader.close()
        _READERS.clear()
        _resolve_public_ip.cache_clear()


atexit.register(clear_ip_region_cache)
