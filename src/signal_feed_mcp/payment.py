"""Buyer side of one x402 v2 payment for ``GET /v1/feed/latest``, with hard checks before and after.

1. unpaid ``GET /v1/feed/latest`` -> expect 402 and decode ``PAYMENT-REQUIRED``;
2. hard-fail unless the offer is exactly the reviewed tuple (exact, Base mainnet, USDC, 0.10 USDC),
   ``payTo`` is the published merchant and ``resource.url`` is the expected endpoint;
3. sign with the official x402 v2 EVM client, with spend controls capped at that one price;
4. retry with ``PAYMENT-SIGNATURE``; decode ``PAYMENT-RESPONSE``;
5. check the paid issue: receipt hash == recomputed hash == hash in the public ledger.

The private key is passed in by the caller, used only to build the local signer, and never
printed, logged, stored or returned.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from signal_feed_mcp.fingerprint import fingerprint

PATH = "/v1/feed/latest"
X402_VERSION = 2
SCHEME_EXACT = "exact"
HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"
NETWORK_BASE_MAINNET = "eip155:8453"
EXPLORERS = {NETWORK_BASE_MAINNET: "https://basescan.org/tx/"}


@dataclass(frozen=True)
class NetworkProfile:
    """One reviewed payment tuple. EIP-712 name/version as read from the token contract on chain."""

    network: str
    asset: str
    amount: str
    token_name: str
    token_version: str
    price_usd: str


# The only tuple this client will sign: 0.10 USDC (6 decimals) on Base mainnet, VAT included.
PROFILES: dict[str, NetworkProfile] = {
    NETWORK_BASE_MAINNET: NetworkProfile(
        NETWORK_BASE_MAINNET, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "100000", "USD Coin", "2", "0.10"
    ),
}


class PaymentError(Exception):
    """The payment must stop rather than proceed. The message is safe to show."""


def explorer_url(network: str | None, tx: str | None) -> str:
    return EXPLORERS.get(network or "", EXPLORERS[NETWORK_BASE_MAINNET]) + (tx or "")


def decode_header(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(base64.b64decode(raw, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise PaymentError("x402 header is not valid base64 JSON") from exc
    if not isinstance(parsed, dict):
        raise PaymentError("x402 header must decode to a JSON object")
    return parsed


@dataclass(frozen=True)
class BuyerConfig:
    base_url: str
    expected_pay_to: str
    network: str = NETWORK_BASE_MAINNET


@dataclass(frozen=True)
class PaidResult:
    network: str
    payer: str
    transaction: str
    payload_sha256: str
    issue: dict[str, Any]
    receipt: dict[str, Any]
    preflight: dict[str, Any] | None = None


class PaymentSigner(Protocol):
    def payer_address(self) -> str: ...

    def create_payment_signature_header(
        self, payment_required: dict[str, Any], requirements: dict[str, Any]
    ) -> str: ...


class OfficialX402Signer:
    """Official x402 v2 EVM exact client, capped at exactly one offer's price on one network."""

    def __init__(self, private_key: str, network: str = NETWORK_BASE_MAINNET) -> None:
        if network not in PROFILES:
            raise PaymentError(f"unsupported network {network!r}; supported: {sorted(PROFILES)}")
        profile = PROFILES[network]
        try:
            account_module = importlib.import_module("eth_account")
            x402_module = importlib.import_module("x402")
            evm_module = importlib.import_module("x402.mechanisms.evm")
            exact_module = importlib.import_module("x402.mechanisms.evm.exact")
            schemas_module = importlib.import_module("x402.schemas")
            http_module = importlib.import_module("x402.http")
        except ImportError as exc:
            raise PaymentError('x402 client missing: pip install "x402[evm]"') from exc
        try:
            account = account_module.Account.from_key(private_key)
        except Exception as exc:
            raise PaymentError("the wallet key could not initialize a local EVM signer") from exc
        client = x402_module.x402ClientSync()
        client.register(network, exact_module.ExactEvmScheme(signer=evm_module.EthAccountSigner(account)))
        client.set_spend_controls(
            {
                "allowed_assets": [
                    {"network": network, "asset": profile.asset, "max_amount_per_payment": profile.amount}
                ],
                "max_amount_per_payment": f"${profile.price_usd}",
            }
        )
        self._client = client
        self._payment_required_cls = schemas_module.PaymentRequired
        self._encode = http_module.encode_payment_signature_header
        self._address = str(account.address)

    def __repr__(self) -> str:
        return f"OfficialX402Signer(address={self._address!r})"

    def payer_address(self) -> str:
        return self._address

    def create_payment_signature_header(
        self, payment_required: dict[str, Any], requirements: dict[str, Any]
    ) -> str:
        narrowed = dict(payment_required, accepts=[requirements])
        try:
            payload = self._client.create_payment_payload(self._payment_required_cls.model_validate(narrowed))
            header = self._encode(payload)
        except Exception as exc:
            raise PaymentError("the x402 client could not create the payment authorization") from exc
        if not isinstance(header, str):
            raise PaymentError("the x402 client returned a non-string payment header")
        return header


def select_requirements(payment_required: dict[str, Any], config: BuyerConfig) -> dict[str, Any]:
    """Hard-fail unless the server offers exactly the reviewed tuple for the expected endpoint."""
    if payment_required.get("x402Version") != X402_VERSION:
        raise PaymentError("PAYMENT-REQUIRED is not x402 v2")
    resource = payment_required.get("resource") or {}
    if resource.get("url") != config.base_url + PATH:
        raise PaymentError(f"resource.url {resource.get('url')!r} is not {config.base_url + PATH!r}")
    profile = PROFILES[config.network]
    for entry in payment_required.get("accepts") or []:
        if (
            isinstance(entry, dict)
            and entry.get("scheme") == SCHEME_EXACT
            and entry.get("network") == profile.network
            and str(entry.get("asset", "")).casefold() == profile.asset.casefold()
            and entry.get("amount") == profile.amount
            and (entry.get("extra") or {}).get("name") == profile.token_name
        ):
            if str(entry.get("payTo", "")).casefold() != config.expected_pay_to.casefold():
                raise PaymentError("payTo does not match the published merchant address")
            return entry
    accepts = payment_required.get("accepts")
    raise PaymentError(f"server did not offer the reviewed tuple; accepts={accepts!r}")


def _check_signed(header: str, requirements: dict[str, Any]) -> None:
    decoded = decode_header(header)
    if decoded.get("x402Version") != X402_VERSION or decoded.get("accepted") != requirements:
        raise PaymentError("the signed payment does not match the reviewed requirements")


Preflight = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def pay_for_latest(
    config: BuyerConfig, signer: PaymentSigner, client: httpx.Client, preflight: Preflight | None = None
) -> PaidResult:
    unpaid = client.get(PATH)
    if unpaid.status_code != 402:
        raise PaymentError(
            f"expected 402 on the unpaid request, got {unpaid.status_code}: {unpaid.text[:200]}"
        )
    raw = unpaid.headers.get(HEADER_PAYMENT_REQUIRED)
    if not raw:
        raise PaymentError(f"402 response has no {HEADER_PAYMENT_REQUIRED} header")
    payment_required = decode_header(raw)
    requirements = select_requirements(payment_required, config)
    # Optional independent check of this exact payment; it raises before anything is signed.
    preflight_result = preflight(payment_required, requirements) if preflight else None

    header = signer.create_payment_signature_header(payment_required, requirements)
    _check_signed(header, requirements)
    paid = client.get(PATH, headers={HEADER_PAYMENT_SIGNATURE: header})

    raw_settlement = paid.headers.get(HEADER_PAYMENT_RESPONSE)
    settlement = decode_header(raw_settlement) if raw_settlement else {}
    if paid.status_code != 200:
        raise PaymentError(f"paid request returned {paid.status_code}: {paid.text[:300]}")

    network, payer, tx = settlement.get("network"), settlement.get("payer"), settlement.get("transaction")
    if network != config.network:
        raise PaymentError(f"settlement network {network!r} is not {config.network}")
    if not isinstance(tx, str) or not tx:
        raise PaymentError("paid response has no transaction evidence")
    if isinstance(payer, str) and payer.casefold() != signer.payer_address().casefold():
        raise PaymentError("settlement payer is not this wallet")

    body = paid.json()
    issue, receipt = body.get("issue") or {}, body.get("receipt") or {}
    recomputed = fingerprint({k: v for k, v in issue.items() if k != "integrity"})
    claimed = (issue.get("integrity") or {}).get("payload_sha256")
    if not (recomputed == claimed == receipt.get("payload_sha256")):
        raise PaymentError("issue hash, receipt hash and recomputed hash disagree")
    ledger = client.get("/v1/ledger").json().get("entries") or []
    if not any(e.get("payload_sha256") == recomputed for e in ledger):
        raise PaymentError("paid issue hash is not in the public ledger")

    return PaidResult(
        network=network,
        payer=payer if isinstance(payer, str) else signer.payer_address(),
        transaction=tx,
        payload_sha256=recomputed,
        issue=issue,
        receipt=receipt,
        preflight=preflight_result,
    )
