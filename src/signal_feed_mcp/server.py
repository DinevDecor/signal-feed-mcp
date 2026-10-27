"""MCP server for Signal Feed API: the daily Regime Feed as tools for agents that speak MCP.

Runs on the agent's own machine (stdio) and calls the public HTTP API at
``SIGNAL_FEED_BASE_URL`` (default ``https://signals.agenttrustapi.com``). It adds no
data of its own: every tool returns what the API returns, plus hash checks.

Free tools: catalog, schema, ledger, delayed issues (with hash check against the
ledger), full results record, deviations, disclosures, and the payment offer.

Paid tool ``get_latest_issue``: one x402 payment of 0.10 USDC on Base mainnet (VAT included),
signed locally with the agent operator's own wallet key (``X402_BUYER_PRIVATE_KEY``,
read from this process environment only, never logged or returned). Before signing
it hard-fails unless the offer is exactly the reviewed tuple and ``payTo`` is the
published merchant (``SIGNAL_FEED_EXPECTED_PAY_TO``); after paying it checks receipt
hash == recomputed hash == ledger hash. A session budget (``SIGNAL_FEED_MAX_PAID_CALLS``,
default 3) caps how many payments one server process can make. Without a key the paid
tool is unavailable and says so. Optionally (``SIGNAL_FEED_PREFLIGHT=block|allow-only``) every payment is
first checked by Agent Trust API's free payment preflight and is not signed unless it clears (preflight.py).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from signal_feed_mcp import payment as pay
from signal_feed_mcp import preflight as pf
from signal_feed_mcp.fingerprint import fingerprint

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

BASE_URL_ENV = "SIGNAL_FEED_BASE_URL"
EXPECTED_PAY_TO_ENV = "SIGNAL_FEED_EXPECTED_PAY_TO"
MAX_PAID_CALLS_ENV = "SIGNAL_FEED_MAX_PAID_CALLS"
PREFLIGHT_ENV = "SIGNAL_FEED_PREFLIGHT"  # off (default) | block | allow-only, see preflight.py
PREFLIGHT_URL_ENV = "SIGNAL_FEED_PREFLIGHT_URL"
AGENT_ID_ENV = "SIGNAL_FEED_AGENT_ID"
AUTHORITY_FILE_ENV = "SIGNAL_FEED_AUTHORITY_FILE"
NETWORK_ENV = "SIGNAL_FEED_NETWORK"  # network the paid tool pays on; only Base mainnet is supported
BUYER_PRIVATE_KEY_ENV = "X402_BUYER_PRIVATE_KEY"
DEFAULT_BASE_URL = "https://signals.agenttrustapi.com"
# The seller's published merchant address, the only payee this client pays. Public.
DEFAULT_PAY_TO = "0x8b0ad0CE12Ac86822c4d5dE442F5e445d14F9183"
DEFAULT_MAX_PAID_CALLS = 3
TIMEOUT_SECONDS = 60.0
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

INSTRUCTIONS = (
    "Signal Feed API publishes one Regime Feed issue per day: five machine-readable market-regime "
    "indicators (BTC trend, alt trend, US equity regime, SPY RSI-2 state, MSTR BTC filter) derived from "
    "virtual PAPER research accounts; there is no track record. Not investment advice. "
    "Issues older than 7 days, the full PAPER results record (losses included), deviations and "
    "disclosures are free. Today's issue is paid per call via x402 (0.10 USDC on Base, VAT included); "
    "get_payment_offer shows the live offer."
)


class ToolFailure(Exception):
    """A tool could not complete; the message is safe to show to the agent."""


@dataclass
class Settings:
    base_url: str = DEFAULT_BASE_URL
    expected_pay_to: str = DEFAULT_PAY_TO
    max_paid_calls: int = DEFAULT_MAX_PAID_CALLS
    buyer_key: str = field(default="", repr=False)
    network: str = pay.NETWORK_BASE_MAINNET
    preflight_mode: str = "off"
    preflight_url: str = pf.DEFAULT_URL
    agent_id: str = "signal-feed-mcp"
    authority_file: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        try:
            max_calls = int(os.environ.get(MAX_PAID_CALLS_ENV) or DEFAULT_MAX_PAID_CALLS)
        except ValueError:
            max_calls = DEFAULT_MAX_PAID_CALLS
        return cls(
            base_url=(os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/"),
            expected_pay_to=os.environ.get(EXPECTED_PAY_TO_ENV) or DEFAULT_PAY_TO,
            max_paid_calls=max(0, max_calls),
            buyer_key=os.environ.get(BUYER_PRIVATE_KEY_ENV) or "",
            network=(os.environ.get(NETWORK_ENV) or pay.NETWORK_BASE_MAINNET).strip(),
            preflight_mode=(os.environ.get(PREFLIGHT_ENV) or "off").strip().lower(),
            preflight_url=os.environ.get(PREFLIGHT_URL_ENV) or pf.DEFAULT_URL,
            agent_id=(os.environ.get(AGENT_ID_ENV) or "signal-feed-mcp").strip(),
            authority_file=os.environ.get(AUTHORITY_FILE_ENV) or "",
        )


ClientFactory = Callable[[], httpx.Client]
SignerFactory = Callable[[str, str], pay.PaymentSigner]


class SignalFeedTools:
    """The tool logic, separate from MCP so it can be tested without a transport."""

    def __init__(
        self,
        settings: Settings,
        client_factory: ClientFactory | None = None,
        signer_factory: SignerFactory = pay.OfficialX402Signer,
        preflight_client_factory: ClientFactory | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or (
            lambda: httpx.Client(base_url=settings.base_url, timeout=TIMEOUT_SECONDS)
        )
        self._signer_factory = signer_factory
        self._preflight_client_factory = preflight_client_factory
        self._signer: pay.PaymentSigner | None = None
        self.paid_calls = 0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        with self._client_factory() as client:
            try:
                return client.get(path, params=params)
            except httpx.HTTPError as exc:
                raise ToolFailure(f"Signal Feed API unreachable: {exc}") from None

    def _json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self._get(path, params)
        if r.status_code != 200:
            raise ToolFailure(f"GET {path} returned {r.status_code}: {r.text[:300]}")
        return r.json()

    def catalog(self) -> dict[str, Any]:
        return self._json("/v1/catalog")

    def schema(self) -> dict[str, Any]:
        return self._json("/v1/schema")

    def ledger(self) -> dict[str, Any]:
        return self._json("/v1/ledger")

    def results_record(self, indicator: str | None = None) -> dict[str, Any]:
        return self._json("/v1/record", {"indicator": indicator} if indicator else None)

    def deviations(self) -> dict[str, Any]:
        return self._json("/v1/deviations")

    def disclosures(self) -> dict[str, Any]:
        return self._json("/v1/disclosures")

    def delayed_issue(self, issue_date: str) -> dict[str, Any]:
        if not DATE.match(issue_date):
            raise ToolFailure("issue_date must be YYYY-MM-DD")
        r = self._get(f"/v1/feed/delayed/{issue_date}")
        if r.status_code != 200:
            raise ToolFailure(f"issue {issue_date} not available ({r.status_code}): {r.text[:300]}")
        issue = r.json()
        recomputed = fingerprint({k: v for k, v in issue.items() if k != "integrity"})
        listed = {e.get("issue_date"): e.get("payload_sha256") for e in self.ledger().get("entries") or []}
        return {
            "issue": issue,
            "verification": {
                "recomputed_payload_sha256": recomputed,
                "matches_issue_integrity": recomputed == (issue.get("integrity") or {}).get("payload_sha256"),
                "matches_public_ledger": recomputed == listed.get(issue_date),
                "timestamp_proof": f"{self._settings.base_url}/v1/feed/proof/{issue_date}",
            },
        }

    def payment_offer(self) -> dict[str, Any]:
        r = self._get(pay.PATH)
        if r.status_code != 402:
            raise ToolFailure(f"expected a 402 offer, got {r.status_code}: {r.text[:300]}")
        try:
            offer = pay.decode_header(r.headers.get(pay.HEADER_PAYMENT_REQUIRED, ""))
        except pay.PaymentError:
            raise ToolFailure("the 402 response has no readable PAYMENT-REQUIRED header") from None
        return {
            "resource": offer.get("resource"),
            "accepts": offer.get("accepts"),
            "paid_tool_enabled": bool(self._settings.buyer_key),
            "paid_calls_left_this_session": max(0, self._settings.max_paid_calls - self.paid_calls),
            "agent_trust_preflight": self._settings.preflight_mode,
        }

    def _preflight(self) -> pf.AgentTrustPreflight | None:
        mode = self._settings.preflight_mode
        if mode == "off":
            return None
        if mode not in pf.MODES:
            raise ToolFailure(f"{PREFLIGHT_ENV} must be one of {list(pf.MODES)}; payment not attempted")
        authority = None
        if self._settings.authority_file:
            try:
                with open(self._settings.authority_file, encoding="utf-8") as f:
                    authority = json.load(f)
            except (OSError, ValueError):
                raise ToolFailure(
                    f"{AUTHORITY_FILE_ENV} could not be read as JSON; payment not attempted"
                ) from None
        return pf.AgentTrustPreflight(
            mode,
            self._settings.agent_id,
            authority=authority,
            url=self._settings.preflight_url,
            client_factory=self._preflight_client_factory,
        )

    def latest_issue(self) -> dict[str, Any]:
        if not self._settings.buyer_key:
            raise ToolFailure(
                f"Paid tool unavailable: set {BUYER_PRIVATE_KEY_ENV} in this MCP server's environment "
                "to a wallet holding USDC on Base. Older issues are free via "
                "get_delayed_issue."
            )
        if self.paid_calls >= self._settings.max_paid_calls:
            raise ToolFailure(
                f"Session payment budget used ({self._settings.max_paid_calls} calls; {MAX_PAID_CALLS_ENV})."
            )
        if self._settings.network not in pay.PROFILES:
            raise ToolFailure(f"{NETWORK_ENV} must be one of {sorted(pay.PROFILES)}")
        if self._signer is None:
            try:
                self._signer = self._signer_factory(self._settings.buyer_key, self._settings.network)
            except pay.PaymentError as exc:
                raise ToolFailure(str(exc)) from None
        signer = self._signer
        preflight = self._preflight()
        config = pay.BuyerConfig(
            base_url=self._settings.base_url,
            expected_pay_to=self._settings.expected_pay_to,
            network=self._settings.network,
        )
        self.paid_calls += 1  # counted before the request: a failed settle may still have spent
        with self._client_factory() as client:
            try:
                result = pay.pay_for_latest(config, signer, client, preflight=preflight)
            except pf.PreflightRefused as exc:
                self.paid_calls -= 1  # refused before signing: nothing was spent
                raise ToolFailure(str(exc)) from None
            except pay.PaymentError as exc:
                raise ToolFailure(f"payment not completed: {exc}") from None
            except httpx.HTTPError as exc:
                raise ToolFailure(f"Signal Feed API unreachable: {exc}") from None
        return {
            "issue": result.issue,
            "receipt": result.receipt,
            "settlement": {
                "network": result.network,
                "transaction": result.transaction,
                "payer": result.payer,
                "amount_atomic": result.receipt.get("amount_atomic"),
                "asset": result.receipt.get("asset"),
                "explorer": pay.explorer_url(result.network, result.transaction),
            },
            "verification": {
                "payload_sha256": result.payload_sha256,
                "receipt_issue_ledger_hashes_match": True,
            },
            "agent_trust_preflight": result.preflight,
        }


def build_server(tools: SignalFeedTools) -> Any:
    server = MCPServer("signal-feed", instructions=INSTRUCTIONS)

    def wrap(fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except ToolFailure as exc:
            raise ToolError(str(exc)) from None  # shown to the agent as the tool's error text

    @server.tool()
    def get_catalog() -> dict[str, Any]:
        """Describe the feed: indicators and their rules, schedule, price, latest and first issue dates,
        free and paid access, timestamp proofs and the results record."""
        return wrap(tools.catalog)

    @server.tool()
    def get_schema() -> dict[str, Any]:
        """Closed JSON Schema of one Regime Feed issue."""
        return wrap(tools.schema)

    @server.tool()
    def list_issues() -> dict[str, Any]:
        """Append-only ledger of every published issue date with its payload SHA-256."""
        return wrap(tools.ledger)

    @server.tool()
    def get_delayed_issue(issue_date: str) -> dict[str, Any]:
        """Free Regime Feed issue for a date at least 7 days old (YYYY-MM-DD), with its hash recomputed and
        compared to the issue's integrity block and to the public ledger."""
        return wrap(tools.delayed_issue, issue_date)

    @server.tool()
    def get_results_record(indicator: str | None = None) -> dict[str, Any]:
        """Full daily PAPER results of the virtual account behind each indicator (equity, return, benchmark,
        exposure, flat days, weekly fees and drawdown), losses included. Optional indicator id filter."""
        return wrap(tools.results_record, indicator)

    @server.tool()
    def get_deviations() -> dict[str, Any]:
        """Dated list of bugs, outages, stops and other deviations that affected an indicator's account."""
        return wrap(tools.deviations)

    @server.tool()
    def get_disclosures() -> dict[str, Any]:
        """The publisher's disclosures: personal positions, affiliate commissions, conflicts of interest."""
        return wrap(tools.disclosures)

    @server.tool()
    def get_payment_offer() -> dict[str, Any]:
        """The x402 payment offer for today's issue (price, network, asset, payee) without paying, and whether
        the paid tool is configured in this server."""
        return wrap(tools.payment_offer)

    @server.tool()
    def get_latest_issue() -> dict[str, Any]:
        """Today's Regime Feed issue, paid per call via x402 (0.10 USDC on Base mainnet) from the wallet
        configured in this server, optionally checked first by Agent Trust preflight; returns the issue,
        the receipt, the settlement transaction and the hash check against the public ledger."""
        return wrap(tools.latest_issue)

    return server


def main() -> None:
    build_server(SignalFeedTools(Settings.from_env())).run()


if __name__ == "__main__":
    main()
