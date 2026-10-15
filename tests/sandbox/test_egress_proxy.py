"""Unit and real-socket tests for ``omicsclaw.sandbox.egress``."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omicsclaw.sandbox import (
    EGRESS_DENYLIST,
    EGRESS_GROUPS,
    EgressProxy,
    domain_matches,
    evaluate_egress,
    extract_blocked_host,
    format_egress_blocked_hint,
    is_ip_allowed,
    parse_upstream_proxy,
)


def test_evaluate_egress_six_step_order():
    # 1. Invalid host
    dec = evaluate_egress("bad host/path", 443)
    assert not dec.allowed and dec.reason == "invalid-host"

    # 2. SSRF / private / loopback / metadata resolved IP
    for private_ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "100.64.0.1"):
        dec = evaluate_egress(
            "pypi.org",
            443,
            egress_groups=("pkg",),
            resolved_ips=(private_ip,),
        )
        assert not dec.allowed and dec.reason == "blocked-by-resolved-ip"

    # Private IP allowed when allow_private_cidr matches
    dec = evaluate_egress(
        "internal.biobank.local",
        443,
        egress_allow=("internal.biobank.local",),
        allow_private_cidr=("10.0.0.0/8",),
        resolved_ips=("10.1.2.3",),
    )
    assert dec.allowed and dec.reason == "allowed"

    # 3. Exfiltration denylist is never overridable even if in egress_allow / granted_domains
    for blocked in ("pastebin.com", "sub.webhook.site", "abc.ngrok-free.app", "upload.pypi.org"):
        dec = evaluate_egress(
            blocked,
            443,
            egress_allow=(blocked, "*"),
            granted_domains=(blocked,),
            resolved_ips=("93.184.216.34",),
        )
        assert not dec.allowed and dec.reason == "blocked-by-denylist"

    # 4. Port check ({80, 443} unless matched by egress_endpoints)
    dec = evaluate_egress("pypi.org", 8443, egress_groups=("pkg",), resolved_ips=("151.101.0.223",))
    assert not dec.allowed and dec.reason == "blocked-by-port"

    dec = evaluate_egress(
        "custom.api.org",
        8443,
        egress_endpoints=("custom.api.org:8443",),
        resolved_ips=("93.184.216.34",),
    )
    assert dec.allowed and dec.reason == "allowed"

    # 5. Preset group allowlist + wildcard matching
    dec = evaluate_egress("pypi.org", 443, egress_groups=("pkg",), resolved_ips=("151.101.0.223",))
    assert dec.allowed

    dec = evaluate_egress(
        "eutils.ncbi.nlm.nih.gov",
        443,
        egress_groups=("nih",),
        resolved_ips=("130.14.29.110",),
    )
    assert dec.allowed

    # 6. Default deny for unlisted domain
    dec = evaluate_egress(
        "unknown-domain.example.org",
        443,
        egress_groups=("pkg",),
        resolved_ips=("93.184.216.34",),
    )
    assert not dec.allowed and dec.reason == "blocked-by-allowlist"


def test_domain_matches_and_ip_checks():
    assert domain_matches("files.pythonhosted.org", EGRESS_GROUPS["pkg"])
    assert domain_matches("sub.bioconductor.org", EGRESS_GROUPS["pkg"])
    assert domain_matches("bioconductor.org", EGRESS_GROUPS["pkg"])
    assert not domain_matches("evil-bioconductor.org", EGRESS_GROUPS["pkg"])
    assert domain_matches("pastebin.com", EGRESS_DENYLIST)
    assert domain_matches("abc.ngrok-free.app", EGRESS_DENYLIST)

    assert is_ip_allowed("93.184.216.34")
    assert not is_ip_allowed("127.0.0.1")
    assert not is_ip_allowed("169.254.169.254")
    assert is_ip_allowed("127.0.0.1", allow_private_cidr=("127.0.0.0/8",))


def test_parse_upstream_proxy_and_hints():
    up = parse_upstream_proxy({"HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "localhost,127.0.0.1"})
    assert up is not None and up.host == "127.0.0.1" and up.port == 7890
    assert "localhost" in up.no_proxy
    assert parse_upstream_proxy({"HTTPS_PROXY": "http://127.0.0.1:7890"}, ignore_port=7890) is None

    hint = format_egress_blocked_hint("api.custom-biobank.org", "blocked-by-allowlist")
    assert "api.custom-biobank.org" in hint
    assert "request_network_access" in hint

    curl_err = (
        "curl: (56) CONNECT tunnel failed, response 403\n"
        "HTTP/1.1 403 Forbidden\r\n"
        "X-Sandbox-Blocked-Domain: restricted.biobank.org\r\n"
    )
    assert extract_blocked_host(curl_err) == "restricted.biobank.org"


def test_real_egress_proxy_blocks_and_allows_traffic(tmp_path: Path):
    async def run() -> None:
        # Start a local upstream target server on 127.0.0.1
        async def handle_target(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            _ = await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\nConnection: close\r\n\r\nhello-target"
            )
            await writer.drain()
            writer.close()

        target_srv = await asyncio.start_server(handle_target, "127.0.0.1", 0)
        target_port = target_srv.sockets[0].getsockname()[1]

        uds_path = tmp_path / "egress.sock"
        proxy = EgressProxy(
            allowed_domains=(),
            egress_groups=("pkg",),
            egress_endpoints=(f"127.0.0.1:{target_port}",),
            unix_socket_path=uds_path,
        )
        await proxy.start()
        try:
            assert proxy.port > 0
            assert uds_path.exists()

            # 1. Request to unallowed domain over TCP proxy -> 403 Forbidden + BlockedEvent recorded
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(
                b"CONNECT unapproved.example.com:443 HTTP/1.1\r\n"
                b"Host: unapproved.example.com:443\r\n\r\n"
            )
            await writer.drain()
            resp = (await reader.read(4096)).decode("utf-8", errors="replace")
            writer.close()
            assert "403" in resp
            assert "unapproved.example.com" in resp
            events = proxy.drain_recent_blocked_events()
            assert len(events) == 1
            assert events[0].host == "unapproved.example.com"
            assert events[0].reason == "blocked-by-allowlist"

            # 2. Denylisted domain cannot be granted dynamically
            assert not proxy.allow_domain_for_session("pastebin.com")
            assert not proxy.allow_domain_for_session("sub.webhook.site")

            # 3. Allowed endpoint via Unix domain socket CONNECT tunnel succeeds
            u_reader, u_writer = await asyncio.open_unix_connection(str(uds_path))
            u_writer.write(
                f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode("ascii")
            )
            await u_writer.drain()
            connect_line = await u_reader.readuntil(b"\r\n\r\n")
            assert b"200 Connection Established" in connect_line

            u_writer.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await u_writer.drain()
            payload = await u_reader.read(4096)
            u_writer.close()
            assert b"hello-target" in payload
        finally:
            await proxy.stop()
            target_srv.close()
            await target_srv.wait_closed()

    asyncio.run(asyncio.wait_for(run(), timeout=15.0))
