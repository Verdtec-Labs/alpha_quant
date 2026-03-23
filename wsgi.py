# =============================================================================
# ALPHA-QUANT · wsgi.py
# Entrypoint para deployment WSGI (Vercel, Gunicorn, etc.)
#
# AVISO IMPORTANTE:
#   Este ficheiro expõe APENAS a dashboard Flask (API + HTML).
#   O engine de trading (Scout, Guardian, RiskManager) NÃO corre em Vercel
#   porque é uma plataforma serverless sem suporte a:
#     - processos 24/7 em background
#     - SQLite persistente
#     - threads contínuas
#
#   Para o sistema completo usa um VPS (Hetzner, DigitalOcean, etc.)
#   ou corre localmente com: python run.py
# =============================================================================

import sys
import os

# UTF-8 no Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dashboard_server import app  # noqa: F401 — Vercel/Gunicorn encontram 'app' aqui

# Inicializa o servidor com base de dados e risk manager em modo estático
# (sem engine de trading activo — apenas leitura do histórico)
from risk_manager import TradeDatabase, RiskManager
from dashboard_server import init_server

_db   = TradeDatabase(os.getenv("DB_PATH", "alphaquant.db"))
_risk = RiskManager(_db)
_risk.start_day(float(os.getenv("DEMO_BALANCE", "500.0")))
init_server(_db, _risk, orch=None)

# Para correr localmente com gunicorn:
#   gunicorn wsgi:app --bind 0.0.0.0:5000
# Para correr localmente com Flask:
#   python wsgi.py
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
