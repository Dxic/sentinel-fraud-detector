import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

from web3 import Web3
from web3.exceptions import Web3Exception
from web3.types import LogReceipt

from alert_store import record_alert
from discord_bot import send_discord_alert
from keeperhub_client import trigger_onchain_response

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (from environment variables — see .env.example)
# ---------------------------------------------------------------------------
RPC_URL: str = os.getenv("RPC_URL", "")
CONTRACT_ADDRESS: str = os.getenv("CONTRACT_ADDRESS", "")
THRESHOLD_ETH: float = float(os.getenv("THRESHOLD_ETH", "50"))
POLL_INTERVAL: float = float(os.getenv("POLL_INTERVAL_SECONDS", "5"))
RECONNECT_DELAY: float = float(os.getenv("RECONNECT_DELAY_SECONDS", "10"))

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Risk scoring thresholds -> severity mapping
RISK_WARNING_THRESHOLD = 30
RISK_CRITICAL_THRESHOLD = 60

# Per-signal-type caps on cumulative contribution within the correlation
# window. Without this, two routine whale transfers (30 pts each, common
# and often benign) would sum to 60 and falsely trigger "critical" on
# their own. Capping large_transfer's total contribution at 40 means
# critical can only be reached by combining it with a genuinely high-risk
# signal — liquidity_removed (50) or ownership_change (25) — matching how
# real rug-pulls actually unfold (multiple distinct red flags, not just
# repeated normal-looking volume).
SIGNAL_CAPS: Dict[str, int] = {
    "large_transfer": 40,
}

# How long correlated signals stay "active" and get combined into one score
CORRELATION_WINDOW_SECONDS = 300  # 5 minutes

# Known centralized-exchange hot wallets. Transfers to/from these addresses
# are routine exchange flow (deposits, withdrawals, rebalancing) and are
# excluded from scoring entirely rather than merely down-weighted — treating
# them as a weak signal would still let repeated CEX activity accumulate
# into false positives over the correlation window.
# NOTE: these are illustrative examples for the hackathon demo — for
# production use, source a maintained, comprehensive CEX address list.
WHITELISTED_ADDRESSES: Set[str] = {
    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance hot wallet (example)
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",  # Binance hot wallet (example)
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",  # Coinbase hot wallet (example)
}

# Canonical WETH address on Ethereum mainnet, used to figure out which side
# of a Uniswap V2 pair (token0/token1) is ETH-denominated, so Burn event
# amounts are read from the correct side instead of assumed to be amount1.
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

# Minimal ABI covering the signals we care about:
#   - Transfer: standard ERC20 event (also used to detect mint: from == 0x0)
#   - Burn: Uniswap V2 style liquidity-remove event
#   - OwnershipTransferred: standard OpenZeppelin Ownable event
CONTRACT_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "sender", "type": "address"},
            {"indexed": False, "name": "amount0", "type": "uint256"},
            {"indexed": False, "name": "amount1", "type": "uint256"},
            {"indexed": True, "name": "to", "type": "address"},
        ],
        "name": "Burn",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "previousOwner", "type": "address"},
            {"indexed": True, "name": "newOwner", "type": "address"},
        ],
        "name": "OwnershipTransferred",
        "type": "event",
    },
    # Uniswap V2 pair read functions — used to determine which side of the
    # pair is WETH and to fetch reserves for precise liquidity-drained-%
    # calculations. Calls to these silently no-op (caught) on contracts
    # that aren't Uniswap V2-style pairs.
    {
        "constant": True,
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"},
        ],
        "type": "function",
    },
]


class RiskEngine:
    """
    Accumulates weighted signals for a contract within a rolling time
    window and decides overall severity. This is what lets the agent
    escalate from "warning" to "critical" when multiple red flags line
    up close together (e.g. ownership renounced + liquidity pulled).
    """

    def __init__(self) -> None:
        self._events: List[Dict[str, Any]] = []

    def add_signal(self, signal_type: str, weight: int, detail: Dict[str, Any]) -> int:
        now = time.time()
        self._events = [e for e in self._events if now - e["ts"] <= CORRELATION_WINDOW_SECONDS]
        self._events.append({"ts": now, "type": signal_type, "weight": weight, "detail": detail})
        return self.current_score()

    def current_score(self) -> int:
        now = time.time()
        active = [e for e in self._events if now - e["ts"] <= CORRELATION_WINDOW_SECONDS]

        # Sum contributions per signal type first, then apply that type's
        # cap (if any) before adding to the total. This is what stops
        # repeated instances of a noisy-but-common signal (large_transfer)
        # from single-handedly reaching "critical".
        per_type_total: Dict[str, int] = {}
        for e in active:
            per_type_total[e["type"]] = per_type_total.get(e["type"], 0) + e["weight"]

        total = 0
        for signal_type, subtotal in per_type_total.items():
            cap = SIGNAL_CAPS.get(signal_type)
            total += min(subtotal, cap) if cap is not None else subtotal
        return total

    def active_signals(self) -> List[str]:
        now = time.time()
        return [e["type"] for e in self._events if now - e["ts"] <= CORRELATION_WINDOW_SECONDS]

    @staticmethod
    def severity_for(score: int) -> str:
        if score >= RISK_CRITICAL_THRESHOLD:
            return "critical"
        if score >= RISK_WARNING_THRESHOLD:
            return "warning"
        return "info"


class FraudDetectorAgent:
    """Monitors the target contract and raises correlated fraud/rug-pull alerts."""

    def __init__(self) -> None:
        self.w3: Optional[Web3] = None
        self.contract = None
        self.is_running: bool = False
        self.last_checked_block: Optional[int] = None
        self.status: str = "initializing"
        self.risk_engine = RiskEngine()
        # Dedup: avoid alerting twice for the same transaction hash
        self._alerted_tx_hashes: Set[str] = set()
        # Result of the most recent KeeperHub trigger call, exposed via /status
        self.last_keeperhub_trigger: Optional[Dict[str, Any]] = None
        # Which side of a Uniswap V2 pair (0 or 1) is WETH, detected on
        # connect. None if undetectable (not a V2-style pair, or the calls
        # simply aren't supported) — Burn valuation then falls back to
        # treating amount1 as the ETH side, as before.
        self.weth_index: Optional[int] = None

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------
    def _connect(self) -> bool:
        """Establish (or re-establish) the Web3 connection to the RPC node."""
        try:
            self.w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
            if not self.w3.is_connected():
                raise ConnectionError("Web3 provider did not respond.")

            self.contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(CONTRACT_ADDRESS),
                abi=CONTRACT_ABI,
            )
            self.last_checked_block = self.w3.eth.block_number
            self.status = "connected"
            logger.info("Connected to RPC. Starting block: %s", self.last_checked_block)

            # Best-effort: figure out which token index is WETH so Burn
            # events can be valued from the correct side of the pair
            # instead of assuming amount1. Silently skipped for contracts
            # that aren't Uniswap V2-style pairs (plain ERC20 tokens, etc).
            try:
                token0 = self.contract.functions.token0().call()
                token1 = self.contract.functions.token1().call()
                if token0.lower() == WETH_ADDRESS.lower():
                    self.weth_index = 0
                elif token1.lower() == WETH_ADDRESS.lower():
                    self.weth_index = 1
                else:
                    self.weth_index = None
            except Exception:  # noqa: BLE001 - not every contract exposes token0/1
                self.weth_index = None

            return True
        except Exception as exc:  # noqa: BLE001 - catch all connection failures
            self.status = f"connection_error: {exc}"
            logger.error("Failed to connect to RPC: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Signal evaluation
    # ------------------------------------------------------------------
    def _dispatch_alert(
        self,
        alert_type: str,
        title: str,
        message: str,
        weight: int,
        fields: Dict[str, Any],
        tx_hash: str,
    ) -> None:
        """Score the signal, decide severity, store it, and notify Discord."""
        if tx_hash in self._alerted_tx_hashes:
            return  # Already alerted for this exact transaction

        score = self.risk_engine.add_signal(alert_type, weight, fields)
        severity = RiskEngine.severity_for(score)

        record_alert(
            alert_type=alert_type,
            severity=severity,
            risk_score=score,
            message=message,
            details=fields,
        )

        send_discord_alert(
            title=f"{title} (Risk Score: {score}, {severity.upper()})",
            message=message,
            severity=severity,
            fields=fields,
        )

        # This is the "decide -> execute" handoff: once correlated signals
        # cross the critical threshold, we don't just alert — we ask
        # KeeperHub's workflow to actually execute the configured onchain
        # protective action (e.g. revoke approval / emergency withdrawal).
        if severity == "critical":
            kh_result = trigger_onchain_response(
                contract_address=CONTRACT_ADDRESS,
                severity=severity,
                risk_score=score,
                active_signals=self.risk_engine.active_signals(),
                trigger_reason=alert_type,
                tx_hash=tx_hash,
            )
            self.last_keeperhub_trigger = kh_result

        self._alerted_tx_hashes.add(tx_hash)
        # Keep the dedup set from growing unbounded
        if len(self._alerted_tx_hashes) > 5000:
            self._alerted_tx_hashes.clear()

        logger.warning(
            "[%s] score=%s severity=%s tx=%s", alert_type, score, severity, tx_hash
        )

    def _evaluate_transfer_event(self, event: LogReceipt) -> None:
        """Check a Transfer event for large-value moves or mint events."""
        try:
            value_wei = event["args"]["value"]
            value_eth = self.w3.from_wei(value_wei, "ether") if self.w3 else 0
            from_addr = event["args"]["from"]
            to_addr = event["args"]["to"]
            tx_hash = event["transactionHash"].hex()

            # Routine CEX deposit/withdrawal — not a fraud signal, skip entirely
            # rather than let repeated exchange flow accumulate risk score.
            if from_addr.lower() in WHITELISTED_ADDRESSES or to_addr.lower() in WHITELISTED_ADDRESSES:
                return

            is_mint = from_addr.lower() == ZERO_ADDRESS.lower()

            if is_mint and value_eth >= THRESHOLD_ETH:
                self._dispatch_alert(
                    alert_type="large_mint",
                    title="🪙 Large Mint Detected",
                    message=(
                        f"A large mint of **{value_eth} tokens** was detected on "
                        f"`{CONTRACT_ADDRESS}`. Sudden supply inflation is a common "
                        f"precursor to dev dumps."
                    ),
                    weight=40,
                    fields={"To": to_addr, "Tx Hash": tx_hash, "Block": event["blockNumber"]},
                    tx_hash=tx_hash,
                )
            elif value_eth >= THRESHOLD_ETH:
                self._dispatch_alert(
                    alert_type="large_transfer",
                    title="🚨 Large Transfer Detected",
                    message=(
                        f"A transfer of **{value_eth} ETH-equivalent** was detected on "
                        f"`{CONTRACT_ADDRESS}`, exceeding the {THRESHOLD_ETH} ETH threshold."
                    ),
                    weight=30,
                    fields={
                        "From": from_addr,
                        "To": to_addr,
                        "Tx Hash": tx_hash,
                        "Block": event["blockNumber"],
                    },
                    tx_hash=tx_hash,
                )
        except (KeyError, TypeError) as exc:
            logger.error("Failed to process Transfer event: %s", exc)

    def _evaluate_burn_event(self, event: LogReceipt) -> None:
        """
        Check a Burn event (liquidity removal) — a classic rug-pull signature.

        Instead of blindly assuming amount1 is the ETH side (the previous
        approach), this reads the actual WETH-side amount based on the
        pair's token0/token1 order detected at connect time, and — where
        possible — computes what percentage of the pool's liquidity was
        drained by fetching reserves from the block just before the burn.
        A small/new pool losing 90% of its liquidity is a far stronger
        rug-pull signal than a raw ETH figure alone, especially for
        low-liquidity tokens where the absolute amount may sit under the
        ETH threshold entirely.
        """
        try:
            amount0 = event["args"]["amount0"]
            amount1 = event["args"]["amount1"]
            tx_hash = event["transactionHash"].hex()

            weth_amount = amount0 if self.weth_index == 0 else amount1
            value_eth = self.w3.from_wei(weth_amount, "ether") if self.w3 else 0

            pct_removed: Optional[float] = None
            try:
                pre_block = event["blockNumber"] - 1
                reserves = self.contract.functions.getReserves().call(block_identifier=pre_block)
                pre_reserve = reserves[self.weth_index] if self.weth_index is not None else reserves[1]
                if pre_reserve > 0:
                    pct_removed = (weth_amount / pre_reserve) * 100
            except Exception as exc:  # noqa: BLE001 - reserve lookup is best-effort
                logger.debug("Could not fetch pre-burn reserves for %s: %s", tx_hash, exc)

            # Trigger on the absolute ETH threshold OR on draining a large
            # share of the pool — the latter catches rug-pulls on small,
            # low-liquidity tokens that would otherwise slip under the
            # fixed ETH threshold.
            is_large_pct = pct_removed is not None and pct_removed >= 50
            if value_eth >= THRESHOLD_ETH or is_large_pct:
                pct_note = f" (~{pct_removed:.0f}% of pool liquidity)" if pct_removed is not None else ""
                # A near-total drain is worse than a partial one — add extra
                # weight so e.g. a 95%-drained micro-pool still reaches
                # "critical" territory rather than sitting at the flat 50.
                weight = 50 + (20 if pct_removed is not None and pct_removed >= 80 else 0)

                self._dispatch_alert(
                    alert_type="liquidity_removed",
                    title="⚠️ Liquidity Removal Detected",
                    message=(
                        f"~**{value_eth} ETH**{pct_note} of liquidity was removed from "
                        f"`{CONTRACT_ADDRESS}`. This is one of the strongest rug-pull "
                        f"indicators — verify manually immediately."
                    ),
                    weight=weight,
                    fields={
                        "Sender": event["args"]["sender"],
                        "Pool % Removed": f"{pct_removed:.1f}%" if pct_removed is not None else "unknown",
                        "Tx Hash": tx_hash,
                        "Block": event["blockNumber"],
                    },
                    tx_hash=tx_hash,
                )
        except (KeyError, TypeError) as exc:
            logger.error("Failed to process Burn event: %s", exc)

    def _evaluate_ownership_event(self, event: LogReceipt) -> None:
        """Check OwnershipTransferred — ownership changes often precede rug-pulls."""
        try:
            tx_hash = event["transactionHash"].hex()
            new_owner = event["args"]["newOwner"]
            is_renounce = new_owner.lower() == ZERO_ADDRESS.lower()

            self._dispatch_alert(
                alert_type="ownership_change",
                title="👑 Ownership Renounced" if is_renounce else "👑 Ownership Transferred",
                message=(
                    f"Ownership of `{CONTRACT_ADDRESS}` was "
                    + ("renounced." if is_renounce else f"transferred to `{new_owner}`.")
                    + " Correlated with other signals, this can indicate an imminent exit."
                ),
                weight=25,
                fields={
                    "Previous Owner": event["args"]["previousOwner"],
                    "New Owner": new_owner,
                    "Tx Hash": tx_hash,
                    "Block": event["blockNumber"],
                },
                tx_hash=tx_hash,
            )
        except (KeyError, TypeError) as exc:
            logger.error("Failed to process OwnershipTransferred event: %s", exc)

    # ------------------------------------------------------------------
    # Main polling loop (async, low-latency)
    # ------------------------------------------------------------------
    async def _poll_events(self) -> None:
        """Fetch and evaluate all new events since the last checked block."""
        assert self.w3 is not None and self.contract is not None

        latest_block = self.w3.eth.block_number
        if latest_block <= self.last_checked_block:
            return  # No new blocks yet

        from_block = self.last_checked_block + 1
        to_block = latest_block

        for event in self.contract.events.Transfer().get_logs(from_block=from_block, to_block=to_block):
            self._evaluate_transfer_event(event)

        # Burn/OwnershipTransferred may not exist on every contract type (e.g. plain
        # ERC20 without Ownable) — each is wrapped independently so one missing
        # event type never blocks evaluation of the others.
        try:
            for event in self.contract.events.Burn().get_logs(from_block=from_block, to_block=to_block):
                self._evaluate_burn_event(event)
        except Web3Exception:
            pass

        try:
            for event in self.contract.events.OwnershipTransferred().get_logs(
                from_block=from_block, to_block=to_block
            ):
                self._evaluate_ownership_event(event)
        except Web3Exception:
            pass

        self.last_checked_block = latest_block

    async def run(self) -> None:
        """
        Main monitoring loop, running continuously and asynchronously.
        Automatically reconnects on RPC failure so the process never crashes
        due to a transient network/node issue.
        """
        self.is_running = True
        logger.info("Starting Fraud & Rug-Pull Detector Agent...")

        while self.is_running:
            if self.w3 is None or not self._is_connected_safe():
                connected = self._connect()
                if not connected:
                    self.status = "reconnecting"
                    logger.warning("Retrying RPC connection in %s seconds...", RECONNECT_DELAY)
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue

            try:
                await self._poll_events()
                self.status = "monitoring"
            except Exception as exc:  # noqa: BLE001 - broad on purpose: keep the agent alive
                logger.error("Error while polling events: %s", exc)
                self.status = f"error: {exc}"
                self.w3 = None  # Force reconnect on next loop iteration

            await asyncio.sleep(POLL_INTERVAL)

    def _is_connected_safe(self) -> bool:
        """Check RPC connectivity without letting exceptions escape."""
        try:
            return bool(self.w3 and self.w3.is_connected())
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        """Stop the monitoring loop (called on app shutdown)."""
        self.is_running = False
        self.status = "stopped"
        logger.info("Monitoring agent stopped.")

    def get_status(self) -> Dict[str, Any]:
        """Return current agent status, used by the /status endpoint."""
        return {
            "status": self.status,
            "is_running": self.is_running,
            "last_checked_block": self.last_checked_block,
            "contract_address": CONTRACT_ADDRESS,
            "threshold_eth": THRESHOLD_ETH,
            "current_risk_score": self.risk_engine.current_score(),
            "active_signals": self.risk_engine.active_signals(),
            "last_keeperhub_trigger": self.last_keeperhub_trigger,
        }


# Singleton instance used by main.py
agent = FraudDetectorAgent()
