from __future__ import annotations

import asyncio
import http.client
import http.server
import json
import socket
import threading
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import fastapi
import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from moco import cli
from moco.cli import (
    _read_runtime_state,
    _request_review_bootstrap,
    _review_bootstrap_url,
    _review_page_url,
    _runtime_state_payload,
    app,
)
from moco.config import MocoSettings, ServerSettings
from moco.errors import CodexReviewError
from moco.web.app import create_app
from moco.web.review import ReviewerCapability, ReviewGate, is_valid_control_secret

runner = CliRunner()
CONTROL_SECRET = "control-secret"  # noqa: S105 - deterministic test credential
MEDIA_TOKEN = "media-token"  # noqa: S105 - deterministic test credential
BOOTSTRAP_NONCE = "bootstrap-nonce"
_PROXY_ENVIRONMENT_KEYS = (
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


@dataclass
class FakeClock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value


def trusted_request(
    *,
    peer_host: str = "127.0.0.1",
    host: str = "127.0.0.1:8765",
    origin: str = "http://127.0.0.1:8765",
) -> dict[str, str]:
    return {"peer_host": peer_host, "host": host, "origin": origin}


def issue(gate: ReviewGate, secret: str = CONTROL_SECRET) -> str:
    return gate.issue_bootstrap_nonce(secret, **trusted_request())


def redeem(
    gate: ReviewGate,
    nonce: str,
    **request: str,
) -> ReviewerCapability:
    return gate.redeem_bootstrap_nonce(
        nonce,
        **(trusted_request() | request),
    )


@contextmanager
def local_http_server(
    handler: type[http.server.BaseHTTPRequestHandler],
) -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
        server.server_close()


@asynccontextmanager
async def running_uvicorn(application: fastapi.FastAPI) -> AsyncIterator[int]:
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=0,
            access_log=False,
            lifespan="off",
            log_config=None,
        ),
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0)
        assert server.servers
        sockets = server.servers[0].sockets
        assert sockets
        address = sockets[0].getsockname()
        assert isinstance(address, tuple)
        yield int(address[1])
    finally:
        server.should_exit = True
        await task


def json_response_handler(
    requests: list[dict[str, str]],
    *,
    status: int = 200,
    location: str | None = None,
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._record()
            self._respond()

        def do_POST(self) -> None:
            self._record()
            self._respond()

        def _record(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            requests.append(
                {
                    "host": self.headers.get("Host", ""),
                    "method": self.command,
                    "path": self.path,
                    "control_secret": self.headers.get("X-Moco-Control-Secret", ""),
                    "origin": self.headers.get("Origin", ""),
                }
            )

        def _respond(self) -> None:
            body = b'{"nonce":"proxy-or-redirect-nonce"}'
            response_status = status
            response_location = location
            if location is not None and self.path != "/review/bootstrap":
                response_status = 200
                response_location = None
            self.send_response(response_status)
            if response_location is not None:
                self.send_header("Location", response_location)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def closed_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def clear_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _PROXY_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_review_bootstrap_never_uses_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_requests: list[dict[str, str]] = []
    with local_http_server(json_response_handler(proxy_requests)) as proxy_url:
        clear_proxy_environment(monkeypatch)
        monkeypatch.setenv("http_proxy", proxy_url)

        with pytest.raises(ValueError, match="review state is unavailable"):
            _request_review_bootstrap(
                f"http://127.0.0.1:{closed_loopback_port()}/review/bootstrap",
                CONTROL_SECRET,
            )

    assert proxy_requests == []


def test_review_bootstrap_rejects_hostname_authority_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, str]] = []
    with local_http_server(json_response_handler(requests)) as numeric_url:
        clear_proxy_environment(monkeypatch)
        endpoint = numeric_url.replace("127.0.0.1", "localhost") + "/review/bootstrap"

        with pytest.raises(ValueError, match="review state is unavailable"):
            _request_review_bootstrap(endpoint, CONTROL_SECRET)

    assert requests == []


@pytest.mark.parametrize("cross_authority", [False, True])
def test_review_bootstrap_rejects_redirects_without_following(
    monkeypatch: pytest.MonkeyPatch,
    cross_authority: bool,
) -> None:
    clear_proxy_environment(monkeypatch)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    final_requests: list[dict[str, str]] = []
    with local_http_server(json_response_handler(final_requests)) as final_url:
        redirect_requests: list[dict[str, str]] = []
        location = f"{final_url}/final" if cross_authority else "/final"
        with (
            local_http_server(
                json_response_handler(redirect_requests, status=302, location=location),
            ) as redirect_url,
            pytest.raises(ValueError, match="review state is unavailable"),
        ):
            _request_review_bootstrap(
                f"{redirect_url}/review/bootstrap",
                CONTROL_SECRET,
            )

    assert len(redirect_requests) == 1
    assert redirect_requests[0]["control_secret"] == CONTROL_SECRET
    assert final_requests == []


@pytest.mark.parametrize(
    ("endpoint_host", "expected_host"),
    [("127.0.0.1", "127.0.0.1:80"), ("[::1]", "[::1]:80")],
)
def test_review_bootstrap_sends_default_http_port_in_wire_host_header(
    monkeypatch: pytest.MonkeyPatch,
    endpoint_host: str,
    expected_host: str,
) -> None:
    clear_proxy_environment(monkeypatch)
    requests: list[dict[str, str]] = []
    with local_http_server(json_response_handler(requests)) as capture_url:
        capture = urlsplit(capture_url)
        assert capture.hostname is not None
        assert capture.port is not None
        real_create_connection = socket.create_connection
        real_putrequest = http.client.HTTPConnection.putrequest
        putrequest_calls: list[tuple[bool, str]] = []

        def connect_to_capture(
            _address: tuple[str, int],
            timeout: float | None = None,
            source_address: tuple[str, int] | None = None,
        ) -> socket.socket:
            return real_create_connection(
                (capture.hostname, capture.port),
                timeout,
                source_address,
            )

        def record_putrequest(
            connection: http.client.HTTPConnection,
            method: str,
            url: str,
            skip_host: bool = False,  # noqa: FBT002 - mirrors stdlib method
            skip_accept_encoding: bool = False,  # noqa: FBT002 - mirrors stdlib method
        ) -> None:
            putrequest_calls.append((skip_host, url))
            real_putrequest(
                connection,
                method,
                url,
                skip_host,
                skip_accept_encoding,
            )

        def leave_request_headers_unchanged(
            _handler: urllib.request.AbstractHTTPHandler,
            request: urllib.request.Request,
        ) -> urllib.request.Request:
            return request

        monkeypatch.setattr(socket, "create_connection", connect_to_capture)
        monkeypatch.setattr(http.client.HTTPConnection, "putrequest", record_putrequest)
        monkeypatch.setattr(
            urllib.request.AbstractHTTPHandler,
            "do_request_",
            leave_request_headers_unchanged,
        )
        monkeypatch.setattr(
            urllib.request.HTTPHandler,
            "http_request",
            leave_request_headers_unchanged,
        )
        nonce = _request_review_bootstrap(
            f"http://{endpoint_host}:80/review/bootstrap",
            CONTROL_SECRET,
        )

    assert nonce == "proxy-or-redirect-nonce"
    assert putrequest_calls == [(True, "/review/bootstrap")]
    assert requests[0]["host"] == expected_host


def test_bootstrap_expires_at_the_exact_thirty_second_boundary() -> None:
    clock = FakeClock()
    gate = ReviewGate(
        CONTROL_SECRET,
        clock=clock,
        nonce_source=lambda: "fresh-nonce",
    )
    nonce = issue(gate)

    clock.value = 129.999
    capability = redeem(gate, nonce)
    capability.release()

    clock.value = 100.0
    nonce = issue(gate)
    clock.value = 130.0
    with pytest.raises(CodexReviewError):
        redeem(gate, nonce)


def test_bootstrap_is_single_use_and_reviewer_slot_is_singleton() -> None:
    gate = ReviewGate(
        CONTROL_SECRET,
        nonce_source=iter(("first-nonce", "second-nonce")).__next__,
    )
    first = redeem(gate, issue(gate))

    with pytest.raises(CodexReviewError):
        redeem(gate, "first-nonce")
    with pytest.raises(CodexReviewError):
        issue(gate)

    first.release()
    second = redeem(gate, issue(gate))
    assert second.active
    second.release()
    assert not second.active


def test_reconnect_requires_a_new_review_after_another_nonce_is_redeemed() -> None:
    values = iter(("first-nonce", "stale-nonce", "new-nonce"))
    gate = ReviewGate(CONTROL_SECRET, nonce_source=values.__next__)
    first = issue(gate)
    stale = issue(gate)
    capability = redeem(gate, first)
    capability.release()

    with pytest.raises(CodexReviewError):
        redeem(gate, stale)

    new_capability = redeem(gate, issue(gate))
    new_capability.release()


def test_bootstrap_from_another_gate_fails_closed() -> None:
    first = ReviewGate(CONTROL_SECRET)
    nonce = issue(first)
    second = ReviewGate(CONTROL_SECRET)

    with pytest.raises(CodexReviewError):
        redeem(second, nonce)


@pytest.mark.parametrize(
    ("peer_host", "host", "origin"),
    [
        ("::1", "[::1]:8765", "http://[::1]:8765"),
        ("127.0.0.42", "127.0.0.42:8765", "http://127.0.0.42:8765"),
    ],
)
def test_ipv4_and_ipv6_loopback_requests_are_accepted(
    peer_host: str,
    host: str,
    origin: str,
) -> None:
    gate = ReviewGate(CONTROL_SECRET, nonce_source=lambda: "loopback-nonce")
    request = {"peer_host": peer_host, "host": host, "origin": origin}
    nonce = gate.issue_bootstrap_nonce(CONTROL_SECRET, **request)
    capability = gate.redeem_bootstrap_nonce(
        nonce,
        **request,
    )
    capability.release()


def test_review_gate_accepts_an_os_mapped_peer_for_a_supported_browser_authority() -> None:
    gate = ReviewGate(CONTROL_SECRET, nonce_source=lambda: "mapped-peer-nonce")
    request = trusted_request(
        peer_host="::ffff:127.0.0.1",
        host="127.0.0.42:8765",
        origin="http://127.0.0.42:8765",
    )

    nonce = gate.issue_bootstrap_nonce(CONTROL_SECRET, **request)
    capability = gate.redeem_bootstrap_nonce(
        nonce,
        **request,
    )
    capability.release()


@pytest.mark.parametrize(
    ("peer_host", "host", "origin"),
    [
        ("127.0.0.1.evil", "127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("127.0.0.1", "localhost.evil:8765", "http://localhost.evil:8765"),
        ("127.0.0.1", "127.0.0.1:8765", "http://127.0.0.1.evil:8765"),
        ("192.0.2.1", "127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("127.0.0.1", "[::ffff:127.0.0.1]:8765", "http://[::ffff:127.0.0.1]:8765"),
        ("127.0.0.1", "[::ffff:7f00:1]:8765", "http://[::ffff:7f00:1]:8765"),
        ("127.0.0.1", "[::1%lo0]:8765", "http://[::1%lo0]:8765"),
        ("127.0.0.1", "[::1%25lo0]:8765", "http://[::1%25lo0]:8765"),
    ],
)
def test_non_loopback_or_hostname_trick_cannot_issue_bootstrap(
    peer_host: str,
    host: str,
    origin: str,
) -> None:
    gate = ReviewGate(CONTROL_SECRET, nonce_source=lambda: "rejected-nonce")

    with pytest.raises(CodexReviewError):
        gate.issue_bootstrap_nonce(
            CONTROL_SECRET,
            peer_host=peer_host,
            host=host,
            origin=origin,
        )


def test_control_secret_and_media_token_cannot_cross_authentication_boundary() -> None:
    gate = ReviewGate(CONTROL_SECRET, nonce_source=lambda: "boundary-nonce")

    with pytest.raises(CodexReviewError):
        gate.issue_bootstrap_nonce(
            MEDIA_TOKEN,
            **trusted_request(),
        )
    nonce = issue(gate)
    with pytest.raises(CodexReviewError):
        gate.redeem_bootstrap_nonce(
            nonce,
            **trusted_request(origin="http://127.0.0.1.evil:8765"),
        )


@pytest.mark.parametrize(
    ("candidate", "valid"),
    [
        (CONTROL_SECRET, True),
        ("A0-z_", True),
        ("é", False),
        ("secret value", False),
        ("secret.value", False),
        ("secret/value", False),
        ("secret=", False),
        ("secret+", False),
    ],
)
def test_control_secret_validator_accepts_only_urlsafe_ascii(
    candidate: str,
    valid: bool,
) -> None:
    assert is_valid_control_secret(candidate) is valid


def test_pending_bootstraps_are_bounded_and_oldest_nonce_is_evicted() -> None:
    values = iter(f"nonce-{index}" for index in range(ReviewGate.MAX_PENDING_BOOTSTRAPS + 1))
    gate = ReviewGate(CONTROL_SECRET, nonce_source=values.__next__)
    issued = [issue(gate) for _ in range(ReviewGate.MAX_PENDING_BOOTSTRAPS + 1)]

    with pytest.raises(CodexReviewError):
        redeem(gate, issued[0])
    capability = redeem(gate, issued[-1])
    capability.release()


def test_simultaneous_redeem_consumes_one_nonce_and_one_slot() -> None:
    gate = ReviewGate(CONTROL_SECRET, nonce_source=lambda: "concurrent-nonce")
    nonce = issue(gate)

    async def redeem_in_thread() -> ReviewerCapability:
        return await asyncio.to_thread(redeem, gate, nonce)

    async def run() -> tuple[ReviewerCapability | None, int]:
        results = await asyncio.gather(
            redeem_in_thread(),
            redeem_in_thread(),
            return_exceptions=True,
        )
        successful = [result for result in results if isinstance(result, ReviewerCapability)]
        failures = [result for result in results if isinstance(result, CodexReviewError)]
        return (successful[0] if successful else None, len(failures))

    capability, failures = asyncio.run(run())
    assert capability is not None
    assert failures == 1
    capability.release()


def test_review_gate_repr_does_not_contain_control_material() -> None:
    gate = ReviewGate(CONTROL_SECRET, nonce_source=lambda: "private-nonce")
    issue(gate)

    rendered = repr(gate)
    assert CONTROL_SECRET not in rendered
    assert "private-nonce" not in rendered


def test_runtime_state_separates_media_token_and_control_secret() -> None:
    payload = _runtime_state_payload(
        MocoSettings(),
        MEDIA_TOKEN,
        CONTROL_SECRET,
    )

    assert payload["version"] == 1
    assert payload["control_secret"] == CONTROL_SECRET
    assert CONTROL_SECRET not in cast("str", payload["url"])
    assert MEDIA_TOKEN in cast("str", payload["url"])


@pytest.mark.parametrize(
    ("configured_host", "expected_authority"),
    [("127.0.0.42", "127.0.0.42"), ("::1", "[::1]")],
)
def test_numeric_loopback_runtime_state_round_trips_through_both_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_host: str,
    expected_authority: str,
) -> None:
    settings = MocoSettings(server=ServerSettings(host=configured_host))
    payload = _runtime_state_payload(settings, MEDIA_TOKEN, CONTROL_SECRET)
    state_path = tmp_path / "runtime.json"
    monkeypatch.setattr(
        cli,
        "read_private_state",
        lambda _path: json.dumps(payload).encode(),
    )

    state = _read_runtime_state(state_path)
    local_url = state["url"]
    assert local_url == f"http://{expected_authority}:8765/#media-token"
    assert _review_bootstrap_url(local_url) == (
        f"http://{expected_authority}:8765/review/bootstrap"
    )
    assert _review_page_url(local_url, BOOTSTRAP_NONCE) == (
        f"http://{expected_authority}:8765/review#bootstrap-nonce"
    )

    opened: list[str] = []

    def open_successfully(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: state_path)
    monkeypatch.setattr(cli, "_request_review_bootstrap", lambda *_args: BOOTSTRAP_NONCE)
    monkeypatch.setattr(cli, "open_browser", open_successfully)

    review_result = runner.invoke(app, ["review"])
    open_result = runner.invoke(app, ["open"])

    assert review_result.exit_code == 0
    assert open_result.exit_code == 0
    assert opened == [
        f"http://{expected_authority}:8765/review#bootstrap-nonce",
        f"http://{expected_authority}:8765/#media-token",
    ]
    assert CONTROL_SECRET not in review_result.output
    assert MEDIA_TOKEN not in review_result.output


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "example.com",
        "192.0.2.1",
        "::ffff:127.0.0.1",
        "::ffff:7f00:1",
        "::1%lo0",
        "::1%25lo0",
    ],
)
def test_runtime_state_rejects_hostname_and_non_loopback_authorities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: str,
) -> None:
    authority = f"[{host}]" if ":" in host else host
    monkeypatch.setattr(
        cli,
        "read_private_state",
        lambda _path: json.dumps(
            {
                "version": 1,
                "url": f"http://{authority}:8765/#{MEDIA_TOKEN}",
                "control_secret": CONTROL_SECRET,
            }
        ).encode(),
    )

    with pytest.raises(ValueError, match="review state is unavailable"):
        _read_runtime_state(tmp_path / "runtime.json")


def test_runtime_state_rejects_malformed_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cli,
        "read_private_state",
        lambda _path: json.dumps(
            {
                "version": 1,
                "url": f"http://127.0.0.1:8765/#{MEDIA_TOKEN}",
                "control_secret": CONTROL_SECRET,
                "mobile_url": None,
            }
        ).encode(),
    )

    with pytest.raises(ValueError, match="review state is unavailable"):
        _read_runtime_state(tmp_path / "runtime.json")


def test_review_command_opens_only_a_fixed_fragment_url_without_printing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "runtime.json"
    media_url = f"http://127.0.0.1:8765/#{MEDIA_TOKEN}"
    control_secret = CONTROL_SECRET
    nonce = BOOTSTRAP_NONCE
    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: state_path)
    monkeypatch.setattr(
        cli,
        "read_private_state",
        lambda _path: json.dumps(
            {
                "version": 1,
                "url": media_url,
                "control_secret": control_secret,
            }
        ).encode(),
    )
    requested: list[tuple[str, str]] = []
    opened: list[str] = []

    def fetch(endpoint: str, secret: str) -> str:
        requested.append((endpoint, secret))
        return nonce

    def open_review(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(cli, "_request_review_bootstrap", fetch)
    monkeypatch.setattr(cli, "open_browser", open_review)

    result = runner.invoke(app, ["review"])

    assert result.exit_code == 0
    assert result.output == "review page opened\n"
    assert requested == [
        ("http://127.0.0.1:8765/review/bootstrap", control_secret),
    ]
    assert opened == [f"http://127.0.0.1:8765/review#{BOOTSTRAP_NONCE}"]
    assert control_secret not in result.output
    assert nonce not in result.output
    assert media_url not in result.output


def test_review_command_rejects_unknown_runtime_state_without_opening_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "default_runtime_state_path", lambda: Path("state"))
    monkeypatch.setattr(
        cli,
        "read_private_state",
        lambda _path: json.dumps(
            {
                "version": 1,
                "url": f"http://127.0.0.1:8765/#{MEDIA_TOKEN}",
                "control_secret": CONTROL_SECRET,
                "unexpected": True,
            }
        ).encode(),
    )
    monkeypatch.setattr(cli, "open_browser", lambda _url: pytest.fail("must not open"))

    result = runner.invoke(app, ["review"])

    assert result.exit_code == 1
    assert result.output == "ERROR [review]: unavailable\n"
    assert CONTROL_SECRET not in result.output


@pytest.mark.asyncio
async def test_review_bootstrap_endpoint_requires_private_loopback_boundary() -> None:
    application = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
    )
    transport = httpx.ASGITransport(
        app=application,
        client=("127.0.0.1", 40000),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
    ) as client:
        accepted = await client.post(
            "/review/bootstrap",
            headers={
                "origin": "http://127.0.0.1:8765",
                "x-moco-control-secret": CONTROL_SECRET,
            },
        )
        media_cross_use = await client.post(
            "/review/bootstrap",
            headers={
                "origin": "http://127.0.0.1:8765",
                "x-moco-control-secret": MEDIA_TOKEN,
            },
        )
        public_origin = await client.post(
            "/review/bootstrap",
            headers={
                "origin": "https://voice.example.com",
                "x-moco-control-secret": CONTROL_SECRET,
            },
        )

    assert accepted.status_code == 200
    assert set(accepted.json()) == {"nonce"}
    assert CONTROL_SECRET not in accepted.text
    assert MEDIA_TOKEN not in accepted.text
    assert accepted.headers["cache-control"] == "no-store"
    assert media_cross_use.status_code == 404
    assert public_origin.status_code == 404


@pytest.mark.asyncio
async def test_review_bootstrap_rejects_non_ascii_control_secret_at_raw_http_boundary() -> None:
    application = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
    )
    review_gate = application.state.review_gate
    before = repr(review_gate)

    async with running_uvicorn(application) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        port_text = str(port).encode("ascii")
        writer.write(
            b"POST /review/bootstrap HTTP/1.1\r\n"
            b"Host: 127.0.0.1:" + port_text + b"\r\n"
            b"Origin: http://127.0.0.1:" + port_text + b"\r\n"
            b"X-Moco-Control-Secret: \xe9\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n"
            b"\r\n",
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()

    assert response.startswith(b"HTTP/1.1 404")
    assert b"500 Internal Server Error" not in response
    assert b"control-secret" not in response
    assert repr(review_gate) == before


@pytest.mark.asyncio
async def test_remote_peer_cannot_issue_a_reviewer_bootstrap() -> None:
    application = create_app(
        capability_token=MEDIA_TOKEN,
        control_secret=CONTROL_SECRET,
    )
    transport = httpx.ASGITransport(
        app=application,
        client=("192.0.2.10", 40000),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8765",
    ) as client:
        response = await client.post(
            "/review/bootstrap",
            headers={
                "origin": "http://127.0.0.1:8765",
                "x-moco-control-secret": CONTROL_SECRET,
            },
        )

    assert response.status_code == 404
