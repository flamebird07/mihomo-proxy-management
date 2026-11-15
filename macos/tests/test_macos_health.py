"""Controller readiness, provider health and the OK/DEGRADED/FAILED model."""

from __future__ import annotations

import json
import secrets
from urllib.parse import urlsplit

import pytest

from mpm_common.controller_health import (
    ControllerClient,
    ControllerError,
    HEALTH_DEGRADED,
    HEALTH_FAILED,
    HEALTH_INACTIVE,
    HEALTH_OK,
    evaluate,
    provider_summary,
    readiness,
)
from mpm_common.errors import FailClosed, NotReady

pytestmark = pytest.mark.macos

# Runtime canary: never a credential-shaped literal in the repository.
SECRET = "canary-" + secrets.token_hex(8)


def _client(
    *,
    version: bool = True,
    providers: dict | None = None,
    proxies: dict | None = None,
    healthcheck: bool = True,
    secret: str = SECRET,
    captured: list | None = None,
) -> ControllerClient:
    provider_payload = providers if providers is not None else {
        "subscription": {
            "all": [{"name": "node-a", "alive": True}, {"name": "node-b", "alive": True}]
        }
    }
    proxy_payload = proxies if proxies is not None else {
        "node-a": {"alive": True},
        "node-b": {"alive": True},
    }

    def transport(method, url, headers, body, timeout):
        if captured is not None:
            captured.append({"method": method, "url": url, "headers": dict(headers)})
        path = urlsplit(url).path
        if path == "/version":
            if not version:
                return (503, b"")
            return (200, json.dumps({"version": "1.19.0"}).encode())
        if path == "/providers/proxies":
            return (200, json.dumps({"providers": provider_payload}).encode())
        if path.endswith("/healthcheck"):
            if not healthcheck:
                return (500, b"")
            return (204, b"")
        if path == "/proxies":
            return (200, json.dumps({"proxies": proxy_payload}).encode())
        return (404, b"")

    return ControllerClient(secret=secret, transport=transport)


def test_client_refuses_an_empty_secret():
    with pytest.raises(FailClosed):
        ControllerClient(secret="   ")


def test_every_request_carries_the_bearer_header():
    captured: list = []
    client = _client(captured=captured)
    client.version()
    assert captured
    assert captured[0]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_non_2xx_never_echoes_the_body():
    def transport(method, url, headers, body, timeout):
        return (500, b"upstream error with token=leak")

    client = ControllerClient(secret=SECRET, transport=transport)
    with pytest.raises(ControllerError) as excinfo:
        client.version()
    assert "token=leak" not in str(excinfo.value)


def test_readiness_returns_the_version():
    assert readiness(_client(), attempts=1, sleeper=lambda _s: None) == "1.19.0"


def test_readiness_fails_when_the_controller_never_answers():
    with pytest.raises(NotReady):
        readiness(_client(version=False), attempts=2, sleeper=lambda _s: None)


def test_provider_summary_counts_nodes_and_live_nodes():
    summary = provider_summary(_client())
    assert summary["subscription"]["node_count"] == 2
    assert summary["subscription"]["alive_count"] == 2


def test_evaluate_ok():
    report = evaluate(_client())
    assert report.state == HEALTH_OK
    assert report.reasons == []
    assert report.data["alive_count"] == 2


def test_evaluate_without_live_nodes_is_failed():
    report = evaluate(
        _client(
            providers={"subscription": {"all": [{"name": "node-a", "alive": False}]}},
            proxies={"node-a": {"alive": False}},
        )
    )
    assert report.state == HEALTH_FAILED
    assert any("no live proxy node" in reason for reason in report.reasons)


def test_evaluate_unknown_liveness_is_failed():
    report = evaluate(
        _client(providers={"subscription": {"all": ["node-a"]}}, proxies={})
    )
    assert report.state == HEALTH_FAILED
    assert any("liveness is unknown" in reason for reason in report.reasons)


def test_evaluate_missing_provider_is_failed():
    report = evaluate(_client(providers={"other": {"all": []}}))
    assert report.state == HEALTH_FAILED


def test_evaluate_unreachable_controller_is_failed():
    report = evaluate(_client(version=False), healthcheck=False)
    assert report.state == HEALTH_FAILED
    assert any("unreachable" in reason for reason in report.reasons)


def test_evaluate_healthcheck_failure_is_failed():
    report = evaluate(_client(healthcheck=False))
    assert report.state == HEALTH_FAILED
    assert any("healthcheck" in reason for reason in report.reasons)


def test_evaluate_degraded_reasons_are_reported():
    report = evaluate(_client(), degraded_reasons=["TUN unavailable: utun missing"])
    assert report.state == HEALTH_DEGRADED
    assert "utun missing" in report.reasons[0]


def test_evaluate_inactive_when_not_running():
    report = evaluate(_client(), running=False)
    assert report.state == HEALTH_INACTIVE


def test_evaluate_without_a_client_is_failed():
    assert evaluate(None).state == HEALTH_FAILED


def test_messages_never_contain_the_secret():
    def transport(method, url, headers, body, timeout):
        raise ControllerError(f"auth failed for {SECRET}")

    client = ControllerClient(secret=SECRET, transport=transport)
    report = evaluate(client, healthcheck=False)
    payload = json.dumps(report.to_dict())
    assert SECRET not in payload
    assert "REDACTED" in payload
