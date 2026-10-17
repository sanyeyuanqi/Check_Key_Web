import hashlib
import socket
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Address, ip_address
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.ip_geolocation as geo


@pytest.fixture(autouse=True)
def reset_geo_readers():
    geo.clear_ip_region_cache()
    yield
    geo.clear_ip_region_cache()


@pytest.mark.parametrize("address,expected", [
    (None, None), ("", None), ("not-an-ip", None), ("https://8.8.8.8", None),
    ("8.8.8.8:80", None), ("999.8.8.8", None), ("8.8.8.8%invalid", None),
    ("127.0.0.1", "本机"), ("127.23.4.5", "本机"), ("::1", "本机"),
    ("10.2.3.4", "内网"), ("172.20.0.1", "内网"), ("172.31.255.255", "内网"),
    ("192.168.1.1", "内网"), ("fc00::1", "内网"), ("fd12:3456::1", "内网"),
    ("::ffff:172.20.0.1", "内网"),
    ("0.0.0.0", "未指定地址"), ("::", "未指定地址"),
    ("169.254.0.1", "链路本地"), ("fe80::1%eth0", "链路本地"),
    ("100.64.0.1", "共享地址"), ("100.127.255.255", "共享地址"),
    ("224.0.0.1", "组播地址"), ("ff02::1", "组播地址"),
    ("192.0.2.1", "保留地址"), ("198.51.100.1", "保留地址"), ("203.0.113.1", "保留地址"),
    ("2001:db8::1", "保留地址"), ("3fff::1", "保留地址"), ("fec0::1", "保留地址"),
    ("198.18.0.1", "保留地址"), ("240.0.0.1", "保留地址"),
])
def test_special_addresses_are_classified_without_geographic_guesses_or_database_access(monkeypatch, address, expected):
    def forbidden_reader(*args):
        raise AssertionError("Special/invalid addresses must not query the database")

    monkeypatch.setattr(geo, "_get_reader", forbidden_reader)
    assert geo.resolve_ip_region(address) == expected


def test_bundled_public_ipv4_and_ipv6_work_with_network_disabled(monkeypatch):
    def forbidden_network(*args, **kwargs):
        raise AssertionError("Geolocation must remain completely offline")

    monkeypatch.setattr(socket, "socket", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    examples = {
        "113.118.113.77": "中国 · 广东省 · 深圳市",
        "240e:3b7:3272:d8d0:db09:c067:8d59:539e": "中国 · 广东省 · 深圳市",
        "8.8.8.8": "美国 · California",
        "2604:a840:3::a04d": "美国 · California · San Jose",
    }
    assert {address: geo.resolve_ip_region(address) for address in examples} == examples


@pytest.mark.parametrize("version,expected_hash", [
    (4, "6307a9696f5711f84bcb8b25f07894de68a64a0ed4a1cc7e990562dd3084f210"),
    (6, "5b93da35ac28bc316dccc54a758381f7a874ae0461dd51ff5df5e34815586f11"),
])
def test_bundled_databases_match_documented_upstream_snapshot(version, expected_hash):
    digest = hashlib.sha256()
    with (geo._DATA_DIRECTORY / f"ip2region_v{version}.xdb").open("rb") as database:
        while block := database.read(1024 * 1024):
            digest.update(block)
    assert digest.hexdigest() == expected_hash


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_data_does_not_break_records_and_keeps_local_classification(monkeypatch, tmp_path, corrupt):
    if corrupt:
        for version in (4, 6):
            (tmp_path / f"ip2region_v{version}.xdb").write_bytes(b"invalid xdb file")
    monkeypatch.setattr(geo, "_DATA_DIRECTORY", tmp_path)
    assert geo.resolve_ip_region("8.8.8.8") is None
    assert geo.resolve_ip_region("2604:a840:3::a04d") is None
    assert geo.resolve_ip_region("172.20.0.1") == "内网"
    assert geo.resolve_ip_region("127.0.0.1") == "本机"


@pytest.mark.parametrize("raw_region", ["", "0|0|0|0|0", "malformed data", "too|many|fields|in|a|record"])
def test_unmatched_or_malformed_database_result_returns_none(monkeypatch, raw_region):
    reader = SimpleNamespace(search=lambda address: raw_region)
    monkeypatch.setattr(geo, "_get_reader", lambda version: reader)
    assert geo.resolve_ip_region("8.8.8.8") is None


def test_unreadable_database_search_degrades_without_network_fallback(monkeypatch):
    reader = SimpleNamespace(search=MagicMock(side_effect=OSError("read failed")))
    monkeypatch.setattr(geo, "_get_reader", lambda version: reader)
    assert geo.resolve_ip_region("8.8.8.8") is None


def test_cached_lookup_canonicalizes_mapped_ipv4_and_has_bounded_memory(monkeypatch):
    search = MagicMock(return_value="United States|California|0|ISP not displayed|US")
    monkeypatch.setattr(geo, "_get_reader", lambda version: SimpleNamespace(search=search))
    assert geo.resolve_ip_region("8.8.8.8") == "美国 · California"
    assert geo.resolve_ip_region(" ::FFFF:8.8.8.8 ") == "美国 · California"
    search.assert_called_once_with("8.8.8.8")
    for offset in range(4200):
        geo.resolve_ip_region(str(IPv4Address(int(IPv4Address("8.8.0.0")) + offset)))
    assert geo._resolve_public_ip.cache_info().currsize == 4096


def test_file_readers_are_safe_for_concurrent_uncached_ipv4_and_ipv6_queries():
    examples = [
        ("113.118.113.77", "中国 · 广东省 · 深圳市"),
        ("240e:3b7:3272:d8d0:db09:c067:8d59:539e", "中国 · 广东省 · 深圳市"),
        ("8.8.8.8", "美国 · California"),
        ("2604:a840:3::a04d", "美国 · California · San Jose"),
    ] * 100

    def uncached_query(example):
        address, expected = example
        actual = geo._resolve_public_ip.__wrapped__(address, ip_address(address).version)
        return actual == expected

    with ThreadPoolExecutor(max_workers=12) as executor:
        assert all(executor.map(uncached_query, examples))
