import socket

import pytest

from backend.search import downloader
from backend.search.providers import _normalize_query_variants


# ------------------------------------------------------------ SSRF guard
def _fake_dns(monkeypatch, ip):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def fake(host, port, *a, **kw):
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port or 443))]

    monkeypatch.setattr(downloader.socket, "getaddrinfo", fake)


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.3.4", "169.254.169.254", "0.0.0.0", "::1", "fe80::1", "fc00::1", "100.64.0.1"],
)
def test_ssrf_blocks_non_public(monkeypatch, ip):
    _fake_dns(monkeypatch, ip)
    with pytest.raises(ValueError):
        downloader._check_public_url_sync("http://evil.example/book.pdf")


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_ssrf_allows_public(monkeypatch, ip):
    _fake_dns(monkeypatch, ip)
    downloader._check_public_url_sync("https://example.org/book.epub")


def test_ssrf_blocks_if_any_resolved_address_private(monkeypatch):
    def fake(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(downloader.socket, "getaddrinfo", fake)
    with pytest.raises(ValueError):
        downloader._check_public_url_sync("https://rebind.example/x")


@pytest.mark.parametrize("url", ["ftp://x.com/a", "file:///etc/passwd", "javascript:alert(1)", "http://", "not a url"])
def test_ssrf_rejects_bad_scheme_or_host(monkeypatch, url):
    _fake_dns(monkeypatch, "8.8.8.8")
    with pytest.raises(ValueError):
        downloader._check_public_url_sync(url)


def test_ssrf_dns_failure(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nx")

    monkeypatch.setattr(downloader.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError):
        downloader._check_public_url_sync("http://nonexistent.invalid/")


def test_ssrf_literal_ip_urls(monkeypatch):
    # No DNS stubbing: literal IPs resolve locally without network.
    with pytest.raises(ValueError):
        downloader._check_public_url_sync("http://127.0.0.1:8088/api/settings")
    with pytest.raises(ValueError):
        downloader._check_public_url_sync("http://[::1]/")


# ---------------------------------------------------- special URL helpers
def test_normalize_special_urls():
    assert downloader._normalize_special_url("https://github.com/u/r/blob/main/b.pdf") == "https://raw.githubusercontent.com/u/r/main/b.pdf"
    gd = downloader._normalize_special_url("https://drive.google.com/file/d/ABCDEFGHIJKLMNOPQRS/view")
    assert gd == "https://drive.google.com/uc?export=download&id=ABCDEFGHIJKLMNOPQRS"
    assert downloader._normalize_special_url(" https://a.com/x ") == "https://a.com/x"


def test_sanitize_filename():
    s = downloader._sanitize_filename('a<b>:"c"/d\\e|f?g*.pdf')
    assert not any(ch in s for ch in '<>:"/\\|?*')
    assert downloader._sanitize_filename("   ") == "downloaded_book"
    assert len(downloader._sanitize_filename("x" * 500)) <= 120


# --------------------------------------------------- query normalization
def test_query_variants_title_subtitle_author():
    q = "The Art of Learning: An Inner Journey to Optimal Performance by Josh Waitzkin"
    v = _normalize_query_variants(q)
    assert v[0] == "The Art of Learning Josh Waitzkin"
    assert "The Art of Learning An Inner Journey to Optimal Performance Josh Waitzkin" in v
    assert all(":" not in x and " by " not in x for x in v)


def test_query_variants_plain_and_empty():
    assert _normalize_query_variants("Dune") == ["Dune"]
    assert _normalize_query_variants("") == []
    assert _normalize_query_variants("   ") == []


def test_query_variants_strips_special_chars_and_dedupes():
    v = _normalize_query_variants('Hello? "World" (2nd ed)')
    assert v == ["Hello World 2nd ed"]
    assert len(v) == len(set(v))


def test_query_variants_dash_subtitle():
    v = _normalize_query_variants("Sapiens - A Brief History of Humankind")
    assert v[0] == "Sapiens"
