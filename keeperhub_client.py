import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

# The Webhook trigger URL for your KeeperHub workflow.
# Create this in app.keeperhub.com -> Workflows -> New Workflow -> Webhook trigger.
# See README.md ("Setting up the KeeperHub Workflow") for the full walkthrough.
KEEPERHUB_WEBHOOK_URL: Optional[str] = os.getenv("KEEPERHUB_WEBHOOK_URL")

# Some KeeperHub webhook triggers require an API key for authentication.
# Generate one from the "Generate API Key" option in app.keeperhub.com and
# set it here. Left blank, no Authorization header is sent (fine if your
# workflow's webhook doesn't require auth).
KEEPERHUB_API_KEY: Optional[str] = os.getenv("KEEPERHUB_API_KEY")

# Optional shared secret used to HMAC-sign every outgoing payload. This is
# a defense-in-depth measure: it proves a request genuinely came from this
# agent (and wasn't forged or replayed by someone who found your webhook
# URL) IF the receiving side verifies it. KeeperHub's webhook trigger itself
# does not verify this automatically — to get real enforcement, add a
# "Condition" (or "Run Code") step right after your Webhook trigger that
# recomputes the HMAC over the raw body using the same secret and rejects
# the run on mismatch. Left blank, requests are sent unsigned.
KEEPERHUB_SIGNING_SECRET: Optional[str] = os.getenv("KEEPERHUB_SIGNING_SECRET")


def _sign(body_bytes: bytes, timestamp: str, nonce: str) -> Optional[str]:
    """
    Compute an HMAC-SHA256 signature over timestamp + nonce + body, so a
    captured signature can't be replayed against a different payload, and
    a captured (timestamp, signature) pair can't be replayed later without
    also matching a fresh nonce. Returns None if no secret is configured.
    """
    if not KEEPERHUB_SIGNING_SECRET:
        return None
    message = timestamp.encode() + nonce.encode() + body_bytes
    return hmac.new(KEEPERHUB_SIGNING_SECRET.encode(), message, hashlib.sha256).hexdigest()


def trigger_onchain_response(
    contract_address: str,
    severity: str,
    risk_score: int,
    active_signals: list,
    trigger_reason: str,
    tx_hash: str,
) -> Dict[str, Any]:
    """
    Notify KeeperHub that a critical risk event occurred, so its workflow
    can execute the configured protective onchain action (e.g. revoke
    approval, emergency withdrawal, or move funds to a safe wallet).

    Returns a dict describing whether the call was made and accepted.
    This never raises — a KeeperHub call failure must not crash the
    detection agent, it should just be logged and surfaced via /status.
    """
    result: Dict[str, Any] = {
        "triggered": False,
        "keeperhub_configured": bool(KEEPERHUB_WEBHOOK_URL),
        "http_status": None,
        "error": None,
        "signed": bool(KEEPERHUB_SIGNING_SECRET),
    }

    if not KEEPERHUB_WEBHOOK_URL:
        result["error"] = "KEEPERHUB_WEBHOOK_URL is not set — onchain response skipped."
        logger.warning(result["error"])
        return result

    payload = {
        "source": "keeperhub-fraud-detector",
        "contract_address": contract_address,
        "severity": severity,
        "risk_score": risk_score,
        "active_signals": active_signals,
        "trigger_reason": trigger_reason,
        "triggering_tx_hash": tx_hash,
    }
    # Canonical (sorted-key) serialization so the signer and any verifier
    # compute the HMAC over identical bytes regardless of dict ordering.
    body_bytes = json.dumps(payload, sort_keys=True).encode()
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    signature = _sign(body_bytes, timestamp, nonce)

    try:
        headers = {"Content-Type": "application/json"}
        if KEEPERHUB_API_KEY:
            headers["Authorization"] = f"Bearer {KEEPERHUB_API_KEY}"
            # Some KeeperHub endpoints expect x-api-key instead of Authorization —
            # sending both is harmless and covers either convention.
            headers["x-api-key"] = KEEPERHUB_API_KEY
        if signature:
            headers["X-Signal-Timestamp"] = timestamp
            headers["X-Signal-Nonce"] = nonce
            headers["X-Signal-Signature"] = signature

        response = requests.post(KEEPERHUB_WEBHOOK_URL, data=body_bytes, headers=headers, timeout=15)
        response.raise_for_status()
        result["triggered"] = True
        result["http_status"] = response.status_code
        try:
            result["response_body"] = response.json()
        except ValueError:
            result["response_body"] = response.text
        logger.warning(
            "KeeperHub workflow triggered for %s (severity=%s, score=%s)",
            contract_address,
            severity,
            risk_score,
        )
    except RequestException as exc:
        result["error"] = str(exc)
        logger.error("Failed to trigger KeeperHub workflow: %s", exc)

    return result
