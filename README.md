# KeeperHub Agent: Real-Time Fraud & Rug-Pull Detector with Onchain Response

An autonomous agent that **detects** correlated rug-pull / fraud signals on a smart
contract in real time, **decides** when the risk is critical, and **executes a real
onchain protective action through KeeperHub**, the execution and reliability layer
required by this hackathon.

> **Hackathon requirement:** *"Every project must use KeeperHub as its onchain
> execution layer. That is the one requirement."* This project satisfies it via
> KeeperHub's documented **Webhook-driven automation** pattern: our Python agent
> fires an HTTP request, and a KeeperHub workflow turns it into a signed onchain
> transaction.

## Why this fits the theme (not just "a bot that alerts")

Most fraud bots stop at step 2: detect, then send a Discord message. That's a
polished demo that never touches a chain, exactly what the hackathon brief says
loses to a working transaction. This project goes one step further:

```
 DETECT                    DECIDE                      EXECUTE (onchain)
┌─────────────┐   risk    ┌──────────────┐   webhook   ┌────────────────────┐
│ Python agent │ ────────▶│  RiskEngine   │───────────▶│  KeeperHub workflow │
│ (web3.py)    │  signals  │  score/severity│  (critical) │  Web3 action:      │
│ polls chain  │           │              │             │  revoke / withdraw  │
└─────────────┘           └──────────────┘             └────────────────────┘
                                                                  │
                                                                  ▼
                                                          real onchain tx hash
```

- **Detect**: `agent_logic.py` polls the target contract via `web3.py` for
  `Transfer`, `Burn` (liquidity removal), and `OwnershipTransferred` events.
- **Decide**: a weighted `RiskEngine` correlates multiple signals within a 5-minute
  window (a single large transfer is only a "warning"; ownership renounced plus
  liquidity pulled together escalates to "critical"). This correlation logic is
  the kind of thing that's easier to express in code than in a no-code trigger,
  which is exactly why we keep it outside KeeperHub and hand off only the final
  decision.
- **Execute**: on a critical verdict, `keeperhub_client.py` POSTs the event to a
  KeeperHub workflow's Webhook trigger. That workflow's own Web3 action then
  signs and submits a real onchain transaction through KeeperHub's Turnkey-secured
  wallet, this is the transaction you'll link in your submission.

## Project Structure

```
keeperhub_agent/
├── main.py               # FastAPI app + background task + live dashboard + demo endpoint
├── agent_logic.py         # web3.py polling, event evaluation, risk scoring, KeeperHub handoff
├── keeperhub_client.py     # POSTs critical events to your KeeperHub workflow's webhook
├── discord_bot.py          # Discord webhook alert sender (detection-side visibility)
├── alert_store.py          # In-memory alert history
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── .gitignore
```

## Setting Up the KeeperHub Workflow (do this first)

This is the part that makes the submission valid. Without it, the agent only
detects and never executes onchain. KeeperHub's builder is no-code, so this is
a UI walkthrough, no SDK code required on your end.

1. **Create an account** at [app.keeperhub.com](https://app.keeperhub.com). A
   Turnkey wallet is provisioned automatically for your organization.
2. **Fund the wallet** with a small amount of test ETH on **Sepolia** (KeeperHub's
   free test network), this is the safest option for a hackathon demo, no real
   funds at risk.
3. **Create a new workflow** and add:
   - **Trigger: Webhook**, this generates a unique URL. Copy it; this is your
     `KEEPERHUB_WEBHOOK_URL` for `.env`.
   - **Action: Web3**, the actual protective onchain response. For a demo, the
     clearest option is a **transfer** action: send a small amount of Sepolia test
     ETH from the org wallet to a "safe wallet" address you control, labeled as an
     emergency fund-sweep in response to the detected risk. (In a production
     setup, this action would instead be a token-approval revoke or an emergency
     withdrawal from the affected vault/position.)
   - **Action: Notification** (Discord/Slack/Telegram), optional, since our agent
     already sends its own Discord alert, but useful to prove KeeperHub itself
     completed the step.
4. **Test with a manual trigger** in the builder before going live, to confirm the
   Web3 action executes correctly and produces a transaction hash.
5. **Enable / go-live** the workflow so the Webhook trigger is active and ready
   to receive our agent's POST requests.
6. Paste the Webhook URL into `.env` as `KEEPERHUB_WEBHOOK_URL`.

Full official walkthrough: [KeeperHub Quick Start Guide](https://docs.keeperhub.com/getting-started/quickstart).

## Quick Start (Windows, macOS, Linux)

```bash
# 1. Create a virtual environment
python -m venv venv

# Activate it:
#   Windows:        venv\Scripts\activate
#   macOS / Linux:  source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
copy .env.example .env      # Windows
# cp .env.example .env      # macOS / Linux
# fill in RPC_URL, CONTRACT_ADDRESS, WEBHOOK_URL, KEEPERHUB_WEBHOOK_URL

# 4. Run
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Then open:
- `http://localhost:8000/dashboard`, live dashboard, including KeeperHub
  execution status
- `http://localhost:8000/status`, raw JSON health check
- `http://localhost:8000/alerts`, raw JSON alert history

## Demoing the Onchain Execution (important for judging)

Real rug-pull-grade correlated signals are rare, you may not get one to fire
naturally during a short demo window. Use the built-in safety net:

```bash
curl -X POST http://localhost:8000/simulate-critical
```

This manually raises a critical-severity event and immediately fires the same
KeeperHub webhook call the real detection path would use, so you get a **real,
verifiable onchain transaction** on cue, during your presentation, without
depending on live market conditions.

For your submission, you need:
1. A short demo video showing this flow end-to-end (detection or simulated event
   into KeeperHub trigger into dashboard showing "Executed via KeeperHub").
2. **The transaction hash** from the KeeperHub Run logs (Runs panel in
   app.keeperhub.com, or in the workflow's execution history), link it in your
   DoraHacks submission.

## Run with Docker

```bash
docker compose up --build
```

## Configuration (`.env`)

| Variable                   | Description                                                        |
|----------------------------|----------------------------------------------------------------------|
| `RPC_URL`                  | HTTPS RPC endpoint (Alchemy, Infura, or public node)                  |
| `CONTRACT_ADDRESS`         | Target contract to monitor                                            |
| `WEBHOOK_URL`              | Discord webhook for detection-side alerts                             |
| `KEEPERHUB_WEBHOOK_URL`    | Webhook trigger URL from your KeeperHub workflow (the execution step) |
| `THRESHOLD_ETH`            | ETH-equivalent value that triggers a signal (default: 50)             |
| `POLL_INTERVAL_SECONDS`    | How often to check for new blocks (default: 5)                        |
| `RECONNECT_DELAY_SECONDS`  | Wait time before retrying a dropped RPC connection (default: 10)      |

## Anti-False-Positive Safeguards

Two things keep this agent from over-triggering on normal market activity,
important to call out to judges, since a detector that cries wolf is worse
than one that under-detects:

- **Score capping**: `large_transfer` signals can contribute at most 40
  points total within the 5-minute correlation window, no matter how many
  fire. Two ordinary whale transfers (30 pts each, common and usually
  benign) can no longer sum to 60 and falsely trigger "critical" on their
  own. Reaching critical requires combining with a genuinely high-risk
  signal like `liquidity_removed` (50) or `ownership_change` (25).
- **CEX hot-wallet whitelist**: known exchange hot wallets (see
  `WHITELISTED_ADDRESSES` in `agent_logic.py`) are skipped entirely when
  they appear as either side of a transfer, since deposits/withdrawals to
  exchanges are routine, not fraud. Addresses are compared case-insensitively
  (`.lower()`) to avoid checksum-casing mismatches. The list included is
  illustrative for the demo, swap in a maintained address set for
  production use.

## Precision Improvements

- **Correct WETH-side detection**: rather than assuming a Uniswap V2 pair's
  `amount1` is always the ETH side, the agent reads `token0()`/`token1()`
  at connect time and values `Burn` events from whichever side is actually
  WETH.
- **Pool-drained percentage**: the agent fetches pool reserves from the
  block immediately before a `Burn` event and computes what percentage of
  liquidity was removed. This catches rug-pulls on small/new pools that
  would otherwise slip under the fixed ETH threshold (e.g. a 5 ETH pool
  fully drained is a textbook rug-pull, even though 5 ETH alone wouldn't
  trip a 50 ETH threshold). A near-total drain (80% or more) also adds
  extra weight to the signal.

## Webhook Request Signing

Every payload sent to KeeperHub can be HMAC-SHA256 signed (set
`KEEPERHUB_SIGNING_SECRET` in `.env`) with a timestamp and nonce, so a
captured request can't be trivially replayed or forged by someone who
discovers your webhook URL. **Important caveat**: KeeperHub's Webhook
trigger does not verify this signature automatically. The signature only
provides real protection if something on the receiving end checks it (e.g.
a `Condition` or `Run Code` step added right after your Webhook trigger
that recomputes the HMAC and rejects mismatches). Without that verification
step, signing still proves intent and gives you an audit trail, but doesn't
by itself stop an unauthorized POST to the webhook URL.

## Presenting the Safety Story to Judges

Worth stating explicitly during your demo: the KeeperHub action in this
walkthrough sends the emergency-response transfer to a **safe wallet you
control**, not a burn/dead address. This is a deliberate choice: the point
of the "execute onchain" step is fund *recovery*, not fund destruction.
Emphasizing this distinguishes the agent as thinking about user fund safety
end-to-end, not just detection theater.

## Known Limitations / Next Steps

- **Polling vs. WebSocket**: this version polls over HTTP for simplicity and
  resilience to reconnects. For lower latency, swap in `WebsocketProvider`.
- **`Burn` ABI** assumes a Uniswap V2-style LP pair. Adjust for other DEX versions.
- **KeeperHub action** in this walkthrough uses a simple test-ETH transfer for
  demo safety; a production deployment would wire the Web3 action to the actual
  protective operation (approval revoke, vault withdrawal) relevant to the
  monitored contract.
- **MCP integration**: KeeperHub also exposes an MCP server so an AI agent
  runtime (Claude, etc.) can create/trigger workflows natively, a natural
  extension if you want to let an LLM decide the response strategy dynamically
  instead of a fixed workflow.

