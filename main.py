import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# Load environment variables BEFORE other local modules are imported, so
# agent_logic.py picks up the correct RPC_URL / CONTRACT_ADDRESS values.
load_dotenv()

from agent_logic import agent  # noqa: E402  (import after load_dotenv, intentional)
from alert_store import get_recent_alerts, record_alert  # noqa: E402
from discord_bot import send_discord_alert  # noqa: E402
from keeperhub_client import KEEPERHUB_WEBHOOK_URL, trigger_onchain_response  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Modern FastAPI lifespan handler (replaces the deprecated on_event
    startup/shutdown hooks). Starts the agent as a background asyncio
    task on startup and shuts it down cleanly on exit.
    """
    logger.info("FastAPI startup: launching monitoring agent in the background...")
    monitor_task = asyncio.create_task(agent.run())

    yield  # App runs here

    logger.info("FastAPI shutdown: stopping monitoring agent...")
    agent.stop()
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="KeeperHub Fraud & Rug-Pull Detector",
    description="Real-time monitoring agent that detects suspicious smart contract activity.",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/status")
async def get_status() -> Dict:
    """Health check endpoint: RPC connection state, last block, current risk score."""
    return {
        "service": "KeeperHub Fraud & Rug-Pull Detector",
        "healthy": True,
        **agent.get_status(),
    }


@app.get("/alerts")
async def get_alerts(limit: int = 50) -> Dict[str, List[Dict]]:
    """Return the most recent alerts raised by the agent, newest first."""
    return {"alerts": get_recent_alerts(limit=limit)}


@app.post("/simulate-critical")
async def simulate_critical() -> Dict[str, Any]:
    """
    Demo safety net: manually raises a critical-severity fraud event and
    triggers the KeeperHub workflow's onchain execution immediately,
    without waiting for a real rug-pull pattern to occur on-chain.

    """
    contract = agent.get_status()["contract_address"] or "0x0000000000000000000000000000000000dEaD"
    simulated_tx_hash = "0xSIMULATED_DEMO_EVENT"
    message = (
        f"Manually triggered demo event: correlated rug-pull signals "
        f"(liquidity removed + ownership renounced) simulated on `{contract}`."
    )

    record_alert(
        alert_type="simulated_critical_demo",
        severity="critical",
        risk_score=100,
        message=message,
        details={"note": "Triggered via POST /simulate-critical for demo purposes."},
    )

    # The real detection path (agent_logic._dispatch_alert) sends a Discord
    # alert alongside triggering KeeperHub — mirror that here so the demo
    # endpoint shows the exact same behavior, not just the onchain half.
    send_discord_alert(
        title="🚨 [DEMO] Critical Fraud Signal Simulated (Risk Score: 100, CRITICAL)",
        message=message,
        severity="critical",
        fields={"Contract": contract, "Triggered via": "POST /simulate-critical"},
    )

    kh_result = trigger_onchain_response(
        contract_address=contract,
        severity="critical",
        risk_score=100,
        active_signals=["liquidity_removed", "ownership_change"],
        trigger_reason="simulated_critical_demo",
        tx_hash=simulated_tx_hash,
    )
    agent.last_keeperhub_trigger = kh_result

    return {"simulated": True, "keeperhub_trigger_result": kh_result}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    """
    Auto-refreshing HTML dashboard styled as a security operations console —
    a live risk waveform plus HUD-style status panels. No frontend build
    step needed; rendered server-side and refreshed via meta tag every 5s.
    Intended for live demos: judges watch signals and execution happen in
    real time instead of reading raw JSON.
    """
    status = agent.get_status()
    alerts = get_recent_alerts(limit=20)

    SEV_COLOR = {"critical": "#FF4F4F", "warning": "#FFB300", "info": "#4FD1C5"}
    SEV_DIM = {"critical": "#5A2A2A", "warning": "#5A4415", "info": "#1F4A47"}

    # --- Build the risk waveform (server-rendered SVG, oldest -> newest) ---
    chrono = list(reversed(alerts))
    W, H, PAD = 1040, 160, 12
    CHART_MAX = 120  # clamp ceiling so a handful of stacked signals don't flatten the line

    def y_for(score: int) -> float:
        clamped = max(0, min(score, CHART_MAX))
        return H - PAD - (clamped / CHART_MAX) * (H - 2 * PAD)

    if len(chrono) >= 2:
        n = len(chrono)
        step = (W - 2 * PAD) / (n - 1)
        pts = [(PAD + i * step, y_for(a["risk_score"])) for i, a in enumerate(chrono)]
        polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        dots = "".join(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{SEV_COLOR.get(chrono[i]["severity"], "#4FD1C5")}" />'
            for i, (x, y) in enumerate(pts)
        )
        current_severity = chrono[-1]["severity"]
    elif len(chrono) == 1:
        y = y_for(chrono[0]["risk_score"])
        polyline = f"{PAD},{y:.1f} {W - PAD},{y:.1f}"
        dots = f'<circle cx="{W - PAD:.1f}" cy="{y:.1f}" r="3.5" fill="{SEV_COLOR.get(chrono[0]["severity"], "#4FD1C5")}" />'
        current_severity = chrono[0]["severity"]
    else:
        baseline = y_for(0)
        polyline = f"{PAD},{baseline:.1f} {W - PAD},{baseline:.1f}"
        dots = ""
        current_severity = "info"

    line_color = SEV_COLOR.get(current_severity, "#4FD1C5")
    warn_y = y_for(30)
    crit_y = y_for(60)

    # --- KeeperHub execution relay state ---
    kh = status.get("last_keeperhub_trigger")
    if kh and kh.get("triggered"):
        exec_id = None
        body = kh.get("response_body")
        if isinstance(body, dict):
            exec_id = body.get("executionId")
        relay_state = "FIRED"
        relay_color = "#4FD1C5"
        relay_detail = f"HTTP {kh.get('http_status')}" + (f" &middot; execution {exec_id}" if exec_id else "")
    elif not KEEPERHUB_WEBHOOK_URL:
        relay_state = "NOT CONFIGURED"
        relay_color = "#6B7684"
        relay_detail = "Set KEEPERHUB_WEBHOOK_URL in .env to arm the relay"
    elif kh and kh.get("error"):
        relay_state = "FAULT"
        relay_color = "#FF4F4F"
        relay_detail = kh.get("error", "")[:120]
    else:
        relay_state = "ARMED"
        relay_color = "#FFB300"
        relay_detail = "Waiting for a critical signal — webhook configured"

    contract = status.get("contract_address") or "—"
    contract_short = contract if contract == "—" else f"{contract[:10]}…{contract[-6:]}"

    log_rows = "".join(
        f"""
        <div class="log-row" style="border-left-color:{SEV_COLOR.get(a['severity'], '#4FD1C5')}">
            <span class="log-time">{a['timestamp'][11:19]}</span>
            <span class="log-sev" style="color:{SEV_COLOR.get(a['severity'], '#4FD1C5')}">{a['severity'].upper():<8}</span>
            <span class="log-type">{a['type']}</span>
            <span class="log-score">score {a['risk_score']:>3}</span>
            <span class="log-msg">{a['message']}</span>
        </div>"""
        for a in alerts
    ) or '<div class="log-empty">— no signals yet. console is watching. —</div>'

    signals_html = "".join(
        f'<span class="chip" style="border-color:{SEV_COLOR.get("warning")}">{s}</span>'
        for s in status.get("active_signals", [])
    ) or '<span class="chip chip-dim">none active</span>'

    return f"""
    <html>
    <head>
        <title>SENTINEL Onchain Threat Console</title>
        <meta http-equiv="refresh" content="5">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg: #0A0D12;
                --panel: #10141B;
                --hair: #1F2630;
                --amber: #FFB300;
                --cyan: #4FD1C5;
                --red: #FF4F4F;
                --text: #E7ECF2;
                --dim: #6B7684;
            }}
            * {{ box-sizing: border-box; }}
            body {{
                font-family: 'IBM Plex Mono', monospace;
                background: var(--bg);
                background-image:
                    linear-gradient(rgba(255,255,255,0.015) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(255,255,255,0.015) 1px, transparent 1px);
                background-size: 24px 24px;
                color: var(--text);
                margin: 0;
                padding: 28px;
            }}
            .wrap {{ max-width: 1080px; margin: 0 auto; }}

            .topbar {{
                display: flex;
                justify-content: space-between;
                align-items: baseline;
                border-bottom: 1px solid var(--hair);
                padding-bottom: 14px;
                margin-bottom: 20px;
                flex-wrap: wrap;
                gap: 8px;
            }}
            .brand {{ display: flex; align-items: baseline; gap: 10px; }}
            .brand h1 {{
                font-size: 19px; letter-spacing: 3px; margin: 0;
                color: var(--amber); font-weight: 700;
            }}
            .brand .sub {{ font-size: 12px; color: var(--dim); }}
            .live {{ font-size: 11px; color: var(--cyan); letter-spacing: 1px; }}
            .live .dot {{
                display: inline-block; width: 7px; height: 7px; border-radius: 50%;
                background: var(--cyan); margin-right: 6px;
                animation: pulse 1.6s ease-in-out infinite;
            }}
            @keyframes pulse {{
                0%, 100% {{ opacity: 1; }}
                50% {{ opacity: 0.25; }}
            }}
            .watching {{ font-size: 12px; color: var(--dim); margin-bottom: 20px; }}
            .watching b {{ color: var(--text); font-weight: 500; }}

            .panel {{
                position: relative;
                background: var(--panel);
                border: 1px solid var(--hair);
                margin-bottom: 16px;
            }}
            .panel::before, .panel::after {{
                content: ""; position: absolute; width: 10px; height: 10px;
                border: 1px solid var(--amber); opacity: 0.6;
            }}
            .panel::before {{ top: -1px; left: -1px; border-right: none; border-bottom: none; }}
            .panel::after {{ bottom: -1px; right: -1px; border-left: none; border-top: none; }}

            .panel-label {{
                font-size: 11px; letter-spacing: 2px; color: var(--dim);
                padding: 12px 16px 0 16px;
            }}
            .waveform {{ padding: 4px 16px 12px 16px; position: relative; overflow: hidden; }}
            .scanline {{
                position: absolute; top: 0; bottom: 0; width: 90px;
                background: linear-gradient(90deg, transparent, rgba(79,209,197,0.10), transparent);
                animation: sweep 4s linear infinite;
            }}
            @keyframes sweep {{
                0% {{ left: -90px; }}
                100% {{ left: 100%; }}
            }}

            .stat-strip {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 16px; }}
            .stat {{ padding: 14px 16px; }}
            .stat .val {{ font-size: 20px; font-weight: 600; margin-top: 6px; }}
            .chip {{
                display: inline-block; font-size: 11px; border: 1px solid;
                padding: 2px 8px; margin: 4px 4px 0 0; color: var(--text);
            }}
            .chip-dim {{ color: var(--dim); border-color: var(--hair) !important; }}

            .relay {{ display: flex; align-items: center; gap: 16px; padding: 16px; }}
            .relay-switch {{
                width: 46px; height: 46px; border: 2px solid; display: flex;
                align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0;
            }}
            .relay-text .state {{ font-size: 15px; font-weight: 700; letter-spacing: 1px; }}
            .relay-text .detail {{ font-size: 11px; color: var(--dim); margin-top: 3px; }}

            .log-row {{
                display: flex; gap: 14px; font-size: 12px; padding: 7px 16px;
                border-left: 3px solid; border-bottom: 1px solid var(--hair);
                align-items: flex-start;
            }}
            .log-time {{ color: var(--dim); flex-shrink: 0; white-space: nowrap; }}
            .log-sev {{ font-weight: 700; white-space: pre; flex-shrink: 0; width: 70px; }}
            .log-type {{
                color: var(--dim); width: 190px; flex-shrink: 0;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            }}
            .log-score {{ color: var(--dim); width: 80px; flex-shrink: 0; white-space: nowrap; }}
            .log-msg {{ color: var(--text); white-space: normal; word-break: break-word; min-width: 0; flex: 1; }}
            .log-empty {{ padding: 20px; text-align: center; color: var(--dim); font-size: 12px; }}

            .footer {{ font-size: 11px; color: var(--dim); margin-top: 18px; letter-spacing: 0.5px; }}
        </style>
    </head>
    <body>
        <div class="wrap">
            <div class="topbar">
                <div class="brand">
                    <h1>SENTINEL</h1>
                    <span class="sub">onchain threat console</span>
                </div>
                <div class="live"><span class="dot"></span>LIVE &middot; refreshes 5s</div>
            </div>
            <div class="watching">watching <b>{contract_short}</b> &middot; ethereum mainnet</div>

            <div class="panel">
                <div class="panel-label">RISK TIMELINE</div>
                <div class="waveform">
                    <div class="scanline"></div>
                    <svg viewBox="0 0 {W} {H}" width="100%" height="{H}" preserveAspectRatio="none">
                        <line x1="0" y1="{warn_y:.1f}" x2="{W}" y2="{warn_y:.1f}" stroke="#FFB300" stroke-width="1" stroke-dasharray="4 5" opacity="0.35" />
                        <line x1="0" y1="{crit_y:.1f}" x2="{W}" y2="{crit_y:.1f}" stroke="#FF4F4F" stroke-width="1" stroke-dasharray="4 5" opacity="0.35" />
                        <polyline points="{polyline}" fill="none" stroke="{line_color}" stroke-width="2" />
                        {dots}
                    </svg>
                </div>
            </div>

            <div class="stat-strip">
                <div class="panel stat">
                    <div class="panel-label" style="padding:0;">STATUS</div>
                    <div class="val">{status['status']}</div>
                </div>
                <div class="panel stat">
                    <div class="panel-label" style="padding:0;">LAST BLOCK</div>
                    <div class="val">{status['last_checked_block'] or '—'}</div>
                </div>
                <div class="panel stat">
                    <div class="panel-label" style="padding:0;">RISK SCORE</div>
                    <div class="val" style="color:{line_color}">{status['current_risk_score']}</div>
                </div>
                <div class="panel stat">
                    <div class="panel-label" style="padding:0;">ACTIVE SIGNALS</div>
                    <div>{signals_html}</div>
                </div>
            </div>

            <div class="panel">
                <div class="panel-label">EXECUTION RELAY &middot; KEEPERHUB</div>
                <div class="relay">
                    <div class="relay-switch" style="border-color:{relay_color}; color:{relay_color};">⏻</div>
                    <div class="relay-text">
                        <div class="state" style="color:{relay_color}">{relay_state}</div>
                        <div class="detail">{relay_detail}</div>
                    </div>
                </div>
            </div>

            <div class="panel">
                <div class="panel-label" style="padding-bottom:10px;">SIGNAL LOG</div>
                {log_rows}
            </div>

            <div class="footer">detect &rarr; decide &rarr; execute onchain via keeperhub &middot; POST /simulate-critical to trigger on demand</div>
        </div>
    </body>
    </html>
    """


@app.get("/")
async def root() -> Dict[str, str]:
    """Simple root sanity-check endpoint."""
    return {"message": "KeeperHub Agent is running. See /dashboard for a live view or /status for raw health data."}
