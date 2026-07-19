from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List

# Keep only the most recent N alerts in memory (enough for a demo, no DB needed)
MAX_ALERTS = 200

_alert_history: Deque[Dict[str, Any]] = deque(maxlen=MAX_ALERTS)


def record_alert(
    alert_type: str,
    severity: str,
    risk_score: int,
    message: str,
    details: Dict[str, Any],
) -> Dict[str, Any]:
    """Store an alert in memory and return the stored record."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": alert_type,
        "severity": severity,
        "risk_score": risk_score,
        "message": message,
        "details": details,
    }
    _alert_history.appendleft(record)  # newest first
    return record


def get_recent_alerts(limit: int = 50) -> List[Dict[str, Any]]:
    """Return the most recent alerts, newest first."""
    return list(_alert_history)[:limit]


def clear_alerts() -> None:
    """Clear alert history (useful for tests / demo reset)."""
    _alert_history.clear()
