# =============================================================================
# ALPHA-QUANT · dashboard_server.py
# Servidor web leve (Flask + SSE) que expõe os dados do sistema à dashboard
# Server-Sent Events em vez de WebSocket — mais simples, sem dependências extra
# =============================================================================

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

try:
    from flask import Flask, Response, jsonify, request
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

from risk_manager import RiskManager, TradeDatabase
from config import SYSTEM

logger = logging.getLogger(__name__)

app = Flask(__name__) if FLASK_AVAILABLE else None

# Estado partilhado (thread-safe via lock)
_lock  = threading.Lock()
_state = {
    "system_on":      True,
    "status":         {},
    "pending_signal": None,
    "last_alert":     None,
    "price":          {"bid": None, "ask": None, "spread": None},
    "sd_zones":       {"demand": [], "supply": []},
    "stats":          {},
    "recent_trades":  [],
    "log_lines":      [],
    "updated_at":     "",
}

_db:   Optional[TradeDatabase]  = None
_risk: Optional[RiskManager]    = None
_orchestrator = None


def init_server(db: TradeDatabase, risk: RiskManager, orch=None):
    """Inicializa o servidor com referências ao estado do sistema."""
    global _db, _risk, _orchestrator
    _db   = db
    _risk = risk
    _orchestrator = orch


def update_state(key: str, value):
    """Actualiza o estado partilhado de forma thread-safe."""
    with _lock:
        _state[key] = value
        _state["updated_at"] = datetime.now(tz=timezone.utc).isoformat()


def push_log(msg: str):
    """Adiciona linha ao log da dashboard (máx 100 linhas)."""
    with _lock:
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        _state["log_lines"].append(f"[{ts}] {msg}")
        if len(_state["log_lines"]) > 100:
            _state["log_lines"] = _state["log_lines"][-100:]


# -----------------------------------------------------------------------------
# ENDPOINTS REST
# -----------------------------------------------------------------------------

if FLASK_AVAILABLE:

    @app.route("/api/state")
    def api_state():
        """Snapshot completo do estado — chamado pela dashboard ao iniciar."""
        with _lock:
            data = dict(_state)
        if _db:
            data["stats"] = _db.get_stats(days=30)
            data["recent_trades"] = _db.get_recent_trades(20)
        if _risk:
            data["status"] = _risk.get_status()
        return jsonify(data)

    @app.route("/api/decision", methods=["POST"])
    def api_decision():
        """Recebe decisão SIM/NÃO do utilizador via dashboard."""
        body     = request.get_json(silent=True) or {}
        approved = body.get("approved", False)

        if _orchestrator:
            _orchestrator.human_decision(approved)
            action = "executado" if approved else "rejeitado"
            push_log(f"Decisão humana: sinal {action}")
            return jsonify({"ok": True, "action": action})

        return jsonify({"ok": False, "error": "Orquestrador não disponível"})

    @app.route("/api/candles")
    def api_candles():
        """Retorna últimas 100 velas M15 + indicadores Didi para o gráfico."""
        from config import SYMBOLS
        symbol = request.args.get("symbol") or (SYMBOLS[0] if SYMBOLS else None)
        if not symbol: return jsonify({"error": "symbol required"}), 400
        try:
            from mt5_connector import MT5Connector
            from didi_indicators import DidiStrategyCalculator
            from supply_demand import SDDetector

            conn = MT5Connector()
            df = conn.get_candles(symbol, "M15", 100) if conn.is_connected else conn._mock_candles(symbol, "M15", 100)

            calc = DidiStrategyCalculator()
            snap = calc.compute(df, "M15")

            # Formata velas para Chart.js
            candles = []
            for _, row in df.iterrows():
                candles.append({
                    "t": row["time"].isoformat() if hasattr(row["time"], "isoformat") else str(row["time"]),
                    "o": float(row["open"]),
                    "h": float(row["high"]),
                    "l": float(row["low"]),
                    "c": float(row["close"]),
                    "v": int(row["volume"]),
                })

            # Indicadores Didi
            didi_data = None
            if snap:
                n = len(df)
                didi_data = {
                    "ma3":  snap.didi.ma3,
                    "ma8":  snap.didi.ma8,
                    "ma20": snap.didi.ma20,
                    "agulhada_bull": snap.didi.agulhada_bull,
                    "agulhada_bear": snap.didi.agulhada_bear,
                    "score": snap.confluence.total,
                    "direction": snap.confluence.direction,
                }

            # Zonas S&D
            sd_det = SDDetector()
            tick_price = candles[-1]["c"] if candles else 1.08
            sd_ctx = sd_det.compute(symbol, tick_price, df, df)
            zones = {
                "demand": [{"top": z.price_top, "bot": z.price_bot, "strength": z.strength, "fresh": z.fresh} for z in sd_ctx.demand_zones[:3]],
                "supply": [{"top": z.price_top, "bot": z.price_bot, "strength": z.strength, "fresh": z.fresh} for z in sd_ctx.supply_zones[:3]],
            }

            return jsonify({"candles": candles, "didi": didi_data, "zones": zones})
        except Exception as e:
            return jsonify({"error": str(e), "candles": [], "didi": None, "zones": {}}), 500

    @app.route("/api/toggle", methods=["POST"])
    def api_toggle():
        """Liga/desliga o sistema."""
        with _lock:
            _state["system_on"] = not _state["system_on"]
            on = _state["system_on"]
        push_log(f"Sistema {'activado' if on else 'pausado'} pelo utilizador")
        return jsonify({"ok": True, "system_on": on})

    @app.route("/stream")
    def stream():
        """
        Server-Sent Events — dashboard subscreve este endpoint
        e recebe actualizações automáticas a cada 2 segundos.
        """
        def generate():
            last_sent = ""
            while True:
                time.sleep(2)
                with _lock:
                    ts = _state.get("updated_at", "")

                if ts != last_sent:
                    last_sent = ts
                    with _lock:
                        payload = json.dumps({
                            "status":    _state.get("status", {}),
                            "price":     _state.get("price", {}),
                            "signal":    _state.get("pending_signal"),
                            "alert":     _state.get("last_alert"),
                            "system_on": _state.get("system_on", True),
                            "log":       _state.get("log_lines", [])[-20:],
                        })
                    yield f"data: {payload}\n\n"

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )

    def run_server(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
        """Arranca o servidor numa thread separada."""
        t = threading.Thread(
            target=lambda: app.run(host=host, port=port, debug=debug, use_reloader=False),
            daemon=True,
        )
        t.start()
        logger.info(f"Dashboard server: http://{host}:{port}")
        return t
