"""Offline tests against a fake Signal Feed API (httpx.MockTransport). No network, no real payments."""

import asyncio
import base64
import json

import httpx
import pytest
from eth_account import Account

from signal_feed_mcp import payment as pay
from signal_feed_mcp import server as ms
from signal_feed_mcp.fingerprint import fingerprint

BASE = "https://signals.example"
MERCHANT = ms.DEFAULT_PAY_TO
BUYER = "0x1111111111111111111111111111111111111111"


def b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def make_issue(date):
    body = {"issue_date": date, "indicators": [{"id": "btc_trend_gate", "state": "risk_on"}]}
    return dict(body, integrity={"payload_sha256": fingerprint(body)})


OLD = make_issue("2026-01-05")
TODAY = make_issue("2026-01-20")


def offer(**over):
    p = pay.PROFILES[pay.NETWORK_BASE_MAINNET]
    o = {
        "scheme": "exact",
        "network": p.network,
        "asset": p.asset,
        "amount": p.amount,
        "payTo": MERCHANT,
        "maxTimeoutSeconds": 300,
        "extra": {"name": p.token_name, "version": p.token_version},
    }
    return dict(o, **over)


class FakeApi:
    def __init__(self, accepts=None, ledger_issue=TODAY):
        self.accepts = accepts or [offer()]
        self.ledger_issue = ledger_issue
        self.paid_headers = []

    def __call__(self, request):
        path = request.url.path
        if path == "/v1/ledger":
            entries = [OLD, self.ledger_issue]
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {"issue_date": i["issue_date"], "payload_sha256": i["integrity"]["payload_sha256"]}
                        for i in entries
                    ]
                },
            )
        if path in ("/v1/catalog", "/v1/schema", "/v1/record", "/v1/deviations", "/v1/disclosures"):
            return httpx.Response(200, json={"path": path, "params": dict(request.url.params)})
        if path == "/v1/feed/delayed/2026-01-05":
            return httpx.Response(200, json=OLD)
        if path.startswith("/v1/feed/delayed/"):
            return httpx.Response(403, json={"error": "NOT_YET_FREE"})
        if path == pay.PATH:
            sig = request.headers.get(pay.HEADER_PAYMENT_SIGNATURE)
            if not sig:
                required = {"x402Version": 2, "resource": {"url": BASE + pay.PATH}, "accepts": self.accepts}
                return httpx.Response(402, json={}, headers={pay.HEADER_PAYMENT_REQUIRED: b64(required)})
            self.paid_headers.append(json.loads(base64.b64decode(sig)))
            settle = {
                "success": True,
                "network": pay.NETWORK_BASE_MAINNET,
                "transaction": "0xtx",
                "payer": BUYER,
            }
            receipt = {
                "payload_sha256": TODAY["integrity"]["payload_sha256"],
                "amount_atomic": "100000",
                "asset": offer()["asset"],
            }
            headers = {pay.HEADER_PAYMENT_RESPONSE: b64(settle)}
            return httpx.Response(200, json={"issue": TODAY, "receipt": receipt}, headers=headers)
        return httpx.Response(404)


class FakeSigner:
    def __init__(self, address=BUYER):
        self.address, self.calls = address, 0

    def payer_address(self):
        return self.address

    def create_payment_signature_header(self, payment_required, requirements):
        self.calls += 1
        return b64({"x402Version": 2, "accepted": requirements, "payload": {"signature": "0xs"}})


def make_tools(api=None, key="k", signer=None, max_calls=3):
    api = api or FakeApi()
    settings = ms.Settings(base_url=BASE, buyer_key=key, max_paid_calls=max_calls)
    signer = signer or FakeSigner()
    tools = ms.SignalFeedTools(
        settings,
        client_factory=lambda: httpx.Client(base_url=BASE, transport=httpx.MockTransport(api)),
        signer_factory=lambda k, n: signer,
    )
    return tools, api, signer


def test_free_tools_pass_through():
    tools, _, _ = make_tools()
    assert tools.catalog()["path"] == "/v1/catalog"
    assert tools.results_record("btc_trend_gate")["params"] == {"indicator": "btc_trend_gate"}
    assert len(tools.ledger()["entries"]) == 2


def test_delayed_issue_is_hash_checked():
    tools, _, _ = make_tools()
    v = tools.delayed_issue("2026-01-05")["verification"]
    assert v["matches_issue_integrity"] and v["matches_public_ledger"]
    assert v["timestamp_proof"] == BASE + "/v1/feed/proof/2026-01-05"
    with pytest.raises(ms.ToolFailure, match="403"):
        tools.delayed_issue("2026-01-19")
    with pytest.raises(ms.ToolFailure, match="YYYY-MM-DD"):
        tools.delayed_issue("../v1")


def test_payment_offer_without_paying():
    tools, api, _ = make_tools(key="")
    out = tools.payment_offer()
    assert out["accepts"] == [offer()] and out["paid_tool_enabled"] is False
    assert api.paid_headers == []


def test_paid_tool_refuses_without_key():
    tools, _, _ = make_tools(key="")
    with pytest.raises(ms.ToolFailure, match="X402_BUYER_PRIVATE_KEY"):
        tools.latest_issue()


def test_paid_tool_returns_verified_issue_and_respects_budget():
    tools, api, signer = make_tools(max_calls=1)
    out = tools.latest_issue()
    assert out["issue"] == TODAY and out["settlement"]["transaction"] == "0xtx"
    assert out["settlement"]["explorer"] == "https://basescan.org/tx/0xtx"
    assert api.paid_headers[0]["accepted"] == offer()
    with pytest.raises(ms.ToolFailure, match="budget"):
        tools.latest_issue()
    assert signer.calls == 1


@pytest.mark.parametrize(
    "accepts, message",
    [
        ([offer(payTo="0x2222222222222222222222222222222222222222")], "payTo"),
        ([offer(amount="1000000")], "reviewed tuple"),
        ([offer(network="eip155:84532")], "reviewed tuple"),
    ],
)
def test_unexpected_offer_is_refused_before_signing(accepts, message):
    tools, _, signer = make_tools(api=FakeApi(accepts=accepts))
    with pytest.raises(ms.ToolFailure, match=message):
        tools.latest_issue()
    assert signer.calls == 0


def test_issue_not_in_ledger_is_rejected():
    tools, _, _ = make_tools(api=FakeApi(ledger_issue=make_issue("2026-01-19")))
    with pytest.raises(ms.ToolFailure, match="ledger"):
        tools.latest_issue()


def test_official_x402_signer_signs_exactly_the_offer():
    account = Account.create()
    signer = pay.OfficialX402Signer(account.key.hex())
    tools, api, _ = make_tools(key=account.key.hex(), signer=signer)
    with pytest.raises(ms.ToolFailure, match="payer"):  # the fake settles as BUYER, not this wallet
        tools.latest_issue()
    sent = api.paid_headers[0]
    assert sent["accepted"] == offer()
    assert sent["payload"]["authorization"]["from"].lower() == account.address.lower()
    assert sent["payload"]["authorization"]["value"] == "100000"


def test_key_never_in_repr_or_output():
    account = Account.create()
    key = account.key.hex()
    signer = pay.OfficialX402Signer(key)
    tools, _, _ = make_tools(key=key, signer=FakeSigner())
    assert key not in repr(tools._settings) and key not in repr(signer)
    out = json.dumps(tools.latest_issue()) + json.dumps(tools.payment_offer())
    assert key.removeprefix("0x") not in out


def test_unsupported_network_is_refused():
    with pytest.raises(pay.PaymentError, match="unsupported network"):
        pay.OfficialX402Signer(Account.create().key.hex(), "eip155:84532")


def test_mcp_server_lists_nine_tools(monkeypatch):
    monkeypatch.delenv("SIGNAL_FEED_NETWORK", raising=False)
    assert ms.Settings.from_env().network == "eip155:8453"
    tools, _, _ = make_tools(key="")
    names = {t.name for t in asyncio.run(ms.build_server(tools).list_tools())}
    assert names == {
        "get_catalog",
        "get_schema",
        "list_issues",
        "get_delayed_issue",
        "get_results_record",
        "get_deviations",
        "get_disclosures",
        "get_payment_offer",
        "get_latest_issue",
    }
