# signal-feed-mcp

MCP server for [Signal Feed API](https://signals.agenttrustapi.com): daily **market-regime context for trading
agents**. It answers one question: is the market risk-on or risk-off? Today's issue is paid per call with
[x402](https://x402.org) (0.10 USDC on Base, VAT included). Older issues, the rules and the full results record are
free.

One issue per day, the same for every buyer, with five rule-based market-state labels:
- BTC trend filter (close vs SMA20/50)
- trend scores for 9 altcoins
- US equity regime (SPY vs SMA200, VIX/VIX3M term structure)
- SPY RSI(2) state
- MSTR BTC filter

> **Honesty facts.** The rules come from the seller's own virtual (PAPER) research accounts. The first issue was
> 2026-10-26, so there is **no track record**. This is context, **not investment advice**. Every issue is
> hash-committed, OpenTimestamps-anchored and never edited, and the full PAPER results (losses included) are public.

## Install

Python 3.10+.

```bash
pip install "git+https://github.com/DinevDecor/signal-feed-mcp"
# or run without installing:
uvx --from git+https://github.com/DinevDecor/signal-feed-mcp signal-feed-mcp
```

Claude Desktop and other MCPB hosts can also install the one-file bundle (`signal-feed-mcp-<version>.mcpb`),
which runs through `uv` and asks for the optional wallet key in its settings.

## Configure your MCP client

Claude Desktop, Claude Code, Cursor or any stdio MCP client:

```json
{
  "mcpServers": {
    "signal-feed": {
      "command": "signal-feed-mcp",
      "env": {
        "X402_BUYER_PRIVATE_KEY": "<optional: key of a wallet holding a little USDC on Base>",
        "SIGNAL_FEED_MAX_PAID_CALLS": "3",
        "SIGNAL_FEED_PREFLIGHT": "block"
      }
    }
  }
}
```

Without `X402_BUYER_PRIVATE_KEY` every free tool works. Only `get_latest_issue` is disabled, and it says why.
Use a dedicated wallet that holds only what you are willing to spend.

## Tools

| Tool | Returns | Cost |
|---|---|---|
| `get_catalog` | indicators and their rules, schedule, price, free and paid access | free |
| `get_schema` | closed JSON Schema of one issue | free |
| `list_issues` | ledger: every issue date with its payload SHA-256 | free |
| `get_delayed_issue(issue_date)` | an issue at least 7 days old, with its hash recomputed and checked against the issue and the ledger | free |
| `get_results_record(indicator?)` | full daily PAPER results of the account behind each indicator, losses included | free |
| `get_deviations` | dated bugs, outages and other deviations | free |
| `get_disclosures` | the publisher's positions and conflicts | free |
| `get_payment_offer` | the live x402 offer, without paying | free |
| `get_latest_issue` | today's issue, the receipt, the settlement transaction and the hash check | 0.10 USDC |

## Payment safety

- **Your wallet key** is read from this process's environment only. It is used to sign locally with the official
  x402 client, and it is never logged, returned in tool output or sent anywhere. The seller never sees it.
- **Before signing**, the client refuses unless the offer is exactly:
  - scheme `exact`;
  - network Base mainnet (`eip155:8453`);
  - asset USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`;
  - amount `100000` (0.10 USDC);
  - for `https://signals.agenttrustapi.com/v1/feed/latest`;
  - payable to the published merchant `0x8b0ad0CE12Ac86822c4d5dE442F5e445d14F9183` (override with
    `SIGNAL_FEED_EXPECTED_PAY_TO`).
- **The x402 client's spend controls** are capped at 0.10 USDC per payment.
- **After paying**, the receipt hash, the recomputed hash and the hash in the public ledger must all match.
- **Session budget:** at most `SIGNAL_FEED_MAX_PAID_CALLS` payments per server process (default 3). Each payment
  is counted before it is attempted. A payment refused by the preflight below is not counted.
- **Independent preflight (optional):** with `SIGNAL_FEED_PREFLIGHT`, every payment is first sent to
  [Agent Trust API](https://api.agenttrustapi.com/v1/schema)'s payment preflight (`POST /v1/preflight/payment`,
  same seller, no charge at the time of writing). It answers `ALLOW`, `REVIEW` or `BLOCK` for that exact payment.
  - **What it checks:** the x402 offer is consistent, and the payee is bound to the resource. If you give it a signed
    delegation (`SIGNAL_FEED_AUTHORITY_FILE`), it also checks that this agent is authorized to make exactly this
    payment.
  - **Modes:** `block` pays on `ALLOW` or `REVIEW` and refuses on `BLOCK`. `allow-only` pays only on `ALLOW`.
  - **It fails closed:** if Agent Trust is unreachable or answers anything unexpected, nothing is signed.
  - **What it sends:** the agent id, the payment tuple, the 402 offer and the optional delegation. It never sends
    your key.
  - **Scope:** it checks the payment, not whether the data will be useful to you. It is not insurance.

### Suggested spend policy for any agent buying this feed

- Pay only `https://signals.agenttrustapi.com/v1/feed/latest`, only to the published merchant, only USDC on Base.
- At most 0.10 USDC per issue and 0.20 USDC per day. One new issue a day is enough, and the date is in the offer.
- Never retry a failed payment blindly. Stop on a non-200 answer or on a duplicate charge, and ask for a refund
  under the published policy.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `X402_BUYER_PRIVATE_KEY` | unset | wallet key for the paid tool (optional) |
| `SIGNAL_FEED_MAX_PAID_CALLS` | `3` | payments allowed per server process |
| `SIGNAL_FEED_EXPECTED_PAY_TO` | published merchant | the only payee the client will pay |
| `SIGNAL_FEED_BASE_URL` | `https://signals.agenttrustapi.com` | API base URL |
| `SIGNAL_FEED_PREFLIGHT` | `off` | `off`, `block` or `allow-only` (Agent Trust payment preflight) |
| `SIGNAL_FEED_AUTHORITY_FILE` | unset | path to a delegation (authority envelope) JSON for the preflight |
| `SIGNAL_FEED_AGENT_ID` | `signal-feed-mcp` | agent id sent to the preflight |
| `SIGNAL_FEED_PREFLIGHT_URL` | Agent Trust production | preflight endpoint |

## Offers

There is a design-partner refund and a 100 USDC build bounty for open-source agents that use the feed as
pre-decision context. Terms: <https://signals.agenttrustapi.com/offers>.

## More

- Catalog and rules: <https://signals.agenttrustapi.com/v1/catalog>
- For LLM agents: <https://signals.agenttrustapi.com/llms.txt>
- OpenAPI: <https://signals.agenttrustapi.com/openapi.json>
- Terms, privacy, risk, refunds: <https://signals.agenttrustapi.com/legal/terms>

## Development

```bash
pip install -e ".[dev]"
pytest && ruff check .
```

The tests run offline against a fake API. They make no network calls and no payments.

## License

MIT © 2026 Agrobroker LTD, Burgas, Bulgaria. Contact: hello@agenttrustapi.com
