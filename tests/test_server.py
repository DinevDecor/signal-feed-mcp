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


ATA = "https://ata.example/v1/preflight/payment"


class FakeAta:
    def __init__(self, decision="REVIEW", status=200, tamper=None, down=False):
        self.decision, self.status, self.tamper, self.down = decision, status, tamper or {}, down
        self.requests = []

    def __call__(self, request):
        if self.down:
            raise httpx.ConnectError("down")
        body = json.loads(request.content)
        self.requests.append((dict(request.headers), body))
        echoed = dict(body["payment"], **self.tamper)
        return httpx.Response(
            self.status,
            json={
                "version": "payment-preflight/v1",
                "decision": self.decision,
                "reason_codes": ["X402_REQUIREMENT_MATCHED"],
                "decision_scope": "THIS_PAYMENT_ONLY",
                "payment": echoed,
            },
        )


def make_guarded(mode, ata, authority_file=""):
    tools, api, signer = make_tools()
    tools._settings.preflight_mode = mode
    tools._settings.preflight_url = ATA
    tools._settings.authority_file = authority_file
    tools._preflight_client_factory = lambda: httpx.Client(transport=httpx.MockTransport(ata))
    return tools, api, signer


def test_preflight_off_by_default_makes_no_call(monkeypatch):
    for name in ("SIGNAL_FEED_PREFLIGHT", "SIGNAL_FEED_AUTHORITY_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert ms.Settings.from_env().preflight_mode == "off"
    tools, _, _ = make_tools()
    assert tools.latest_issue()["agent_trust_preflight"] is None


def test_block_mode_pays_on_review_and_sends_the_exact_tuple():
    ata = FakeAta("REVIEW")
    tools, api, signer = make_guarded("block", ata)
    out = tools.latest_issue()
    assert out["agent_trust_preflight"]["decision"] == "REVIEW" and signer.calls == 1
    headers, body = ata.requests[0]
    import uuid

    assert uuid.UUID(headers["payment-intent-id"]).version == 4
    assert body["version"] == "payment-preflight-input/v1" and "authority" not in body
    assert body["payment"]["x402_amount_atomic"] == "100000" and body["payment"]["pay_to"] == MERCHANT
    assert body["payment"]["resource"] == BASE + pay.PATH and body["x402_payment_required"]["accepts"] == [
        offer()
    ]


@pytest.mark.parametrize(
    "mode, ata",
    [
        ("block", FakeAta("BLOCK")),
        ("allow-only", FakeAta("REVIEW")),
        ("block", FakeAta(down=True)),
        ("block", FakeAta(status=503)),
        ("block", FakeAta("ALLOW", tamper={"pay_to": "0x2222222222222222222222222222222222222222"})),
        ("bogus", FakeAta("ALLOW")),
    ],
)
def test_preflight_refusals_never_sign_or_spend_budget(mode, ata):
    tools, api, signer = make_guarded(mode, ata)
    with pytest.raises(ms.ToolFailure, match="payment not attempted"):
        tools.latest_issue()
    assert signer.calls == 0 and api.paid_headers == [] and tools.paid_calls == 0


def test_allow_only_with_authority_envelope(tmp_path):
    envelope = {"version": "payment-authority-match-input/v1", "example": True}
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    ata = FakeAta("ALLOW")
    tools, _, signer = make_guarded("allow-only", ata, str(path))
    assert tools.latest_issue()["agent_trust_preflight"]["decision"] == "ALLOW" and signer.calls == 1
    assert ata.requests[0][1]["authority"] == envelope
