"""Optional Agent Trust payment preflight before the paid tool signs anything.

Agent Trust API (https://api.agenttrustapi.com, same seller) evaluates one exact proposed payment and
answers ``ALLOW``, ``REVIEW`` or ``BLOCK`` for ``THIS_PAYMENT_ONLY``: it checks that the x402 offer is
consistent, that the payee is bound to the resource, and, when the operator supplies a delegation (authority
envelope), that the agent is authorized to make exactly this payment. It never signs, settles or moves funds.

Modes (``SIGNAL_FEED_PREFLIGHT``):

- ``off`` (default): no call is made.
- ``block``: refuse to pay on ``BLOCK``; pay on ``ALLOW`` or ``REVIEW``.
- ``allow-only``: pay only on ``ALLOW`` (needs an authority envelope, ``SIGNAL_FEED_AUTHORITY_FILE``).

In both active modes the check fails closed: if Agent Trust is unreachable or answers anything unexpected, the
payment is not attempted. The request carries the agent id, the payment tuple, the 402 offer and the optional
authority envelope; it never carries a private key.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx

from signal_feed_mcp.payment import PROFILES, PaymentError

MODES = ("off", "block", "allow-only")
DEFAULT_URL = "https://api.agenttrustapi.com/v1/preflight/payment"
INPUT_VERSION = "payment-preflight-input/v1"
INTENT_HEADER = "Payment-Intent-Id"
DECISIONS = ("ALLOW", "REVIEW", "BLOCK")
TIMEOUT_SECONDS = 20.0


class PreflightRefused(PaymentError):
    """The preflight did not clear this payment; nothing was signed or spent."""


class AgentTrustPreflight:
    def __init__(
        self,
        mode: str,
        agent_id: str,
        authority: dict[str, Any] | None = None,
        url: str = DEFAULT_URL,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        if mode not in MODES or mode == "off":
            raise PaymentError(f"preflight mode must be one of {MODES[1:]}")
        self.mode, self.agent_id, self.authority, self.url = mode, agent_id, authority, url
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=TIMEOUT_SECONDS))

    def __call__(self, payment_required: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
        profile = PROFILES[requirements["network"]]
        payment = {
            "amount": profile.price_usd,
            "asset": "USDC",
            "network": requirements["network"],
            "pay_to": requirements["payTo"],
            "resource": (payment_required.get("resource") or {}).get("url"),
            "x402_amount_atomic": requirements["amount"],
            "x402_asset_address": requirements["asset"],
        }
        body: dict[str, Any] = {
            "version": INPUT_VERSION,
            "agent": {"id": self.agent_id},
            "payment": payment,
            "x402_payment_required": payment_required,
        }
        if self.authority is not None:
            body["authority"] = self.authority
        intent = str(uuid.uuid4())
        try:
            with self._client_factory() as client:
                r = client.post(self.url, json=body, headers={INTENT_HEADER: intent})
        except httpx.HTTPError:
            raise PreflightRefused("Agent Trust preflight unreachable; payment not attempted") from None
        if r.status_code != 200:
            raise PreflightRefused(
                f"Agent Trust preflight returned HTTP {r.status_code}; payment not attempted"
            )
        try:
            data = r.json()
        except ValueError:
            raise PreflightRefused(
                "Agent Trust preflight answer is not JSON; payment not attempted"
            ) from None
        decision, reasons = data.get("decision"), data.get("reason_codes") or []
        echoed = data.get("payment") or {}
        if (
            decision not in DECISIONS
            or data.get("decision_scope") != "THIS_PAYMENT_ONLY"
            or str(echoed.get("pay_to", "")).casefold() != str(payment["pay_to"]).casefold()
            or echoed.get("resource") != payment["resource"]
            or echoed.get("x402_amount_atomic") != payment["x402_amount_atomic"]
        ):
            raise PreflightRefused(
                "Agent Trust preflight answer does not match this payment; payment not attempted"
            )
        summary = {
            "service": self.url,
            "mode": self.mode,
            "decision": decision,
            "reason_codes": reasons,
            "intent_id": intent,
        }
        if decision == "BLOCK" or (self.mode == "allow-only" and decision != "ALLOW"):
            raise PreflightRefused(f"Agent Trust preflight: {decision} {reasons}; payment not attempted")
        return summary
