import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

# Webhook URL is read from the environment (.env)
WEBHOOK_URL: Optional[str] = os.getenv("WEBHOOK_URL")

# Embed color per severity level (Discord decimal color codes)
COLOR_MAP = {
    "critical": 0xE74C3C,  # red
    "warning": 0xF39C12,   # orange
    "info": 0x3498DB,      # blue
}


def send_discord_alert(
    title: str,
    message: str,
    severity: str = "critical",
    fields: Optional[dict] = None,
) -> bool:
    """
    Send an alert as a Discord embed via webhook.

    Args:
        title: Alert headline (e.g. "Rug-Pull Detected!").
        message: Main body describing what happened.
        severity: "critical" | "warning" | "info" -> controls embed color.
        fields: Optional dict of extra key/value fields
                (e.g. {"Tx Hash": "0x123...", "Value": "75 ETH"}).

    Returns:
        True if the webhook accepted the message (HTTP 2xx), False otherwise.
    """
    if not WEBHOOK_URL:
        logger.error("WEBHOOK_URL is not set in the environment. Alert skipped.")
        return False

    embed = {
        "title": title,
        "description": message,
        "color": COLOR_MAP.get(severity, COLOR_MAP["info"]),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "KeeperHub Fraud & Rug-Pull Detector"},
    }

    if fields:
        embed["fields"] = [
            {"name": str(k), "value": str(v), "inline": True} for k, v in fields.items()
        ]

    payload = {"embeds": [embed]}

    try:
        # A timeout prevents this call from ever hanging the event loop
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Discord alert sent: %s", title)
        return True
    except RequestException as exc:
        # A failed alert must never crash or stall the main monitoring loop
        logger.error("Failed to send Discord alert: %s", exc)
        return False
