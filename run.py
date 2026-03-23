#!/usr/bin/env python3
# =============================================================================
# ALPHA-QUANT · run.py
# Ponto de entrada único — executa tudo com um comando:
#   python run.py
#   python run.py --demo          (modo demo, sem ordens reais)
#   python run.py --check         (só verifica ligações e sai)
#   python run.py --dashboard     (só arranca a dashboard, sem trading)
# =============================================================================

import argparse
import logging
import os
import sys

# Reconfigura stdout/stderr para UTF-8 antes de qualquer import
# (evita UnicodeEncodeError no Windows com CP1252 quando os logs têm €, →, ✓)
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from mt5_connector import MT5Connector

# ── Carrega .env ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✓ .env carregado")
except ImportError:
    print("⚠ python-dotenv não instalado — usa variáveis de ambiente do sistema")

# ── Argumentos ────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Alpha-Quant Trading System")
parser.add_argument("--demo",      action="store_true", help="Força modo demo (sem ordens reais)")
parser.add_argument("--check",     action="store_true", help="Verifica ligações e sai")
parser.add_argument("--dashboard", action="store_true", help="Só arranca a dashboard (sem trading)")
parser.add_argument("--port",      type=int, default=5000, help="Porta da dashboard (default: 5000)")
parser.add_argument("--loglevel",  default="INFO", help="Nível de log (DEBUG/INFO/WARNING)")
args = parser.parse_args()

# ── Força demo se pedido ──────────────────────────────────────────────────────
if args.demo:
    os.environ["DEMO_MODE"] = "true"
    print("⚠ MODO DEMO ACTIVO — nenhuma ordem real será executada")

# ── Setup logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, args.loglevel.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("STARTUP")


# =============================================================================
# VERIFICAÇÃO DE DEPENDÊNCIAS
# =============================================================================
def check_dependencies() -> list[str]:
    missing = []
    deps = [
        ("pandas",    "pandas"),
        ("numpy",     "numpy"),
        ("anthropic", "anthropic"),
        ("flask",     "flask"),
    ]
    for name, pkg in deps:
        try:
            __import__(name)
        except ImportError:
            missing.append(pkg)
    return missing


# =============================================================================
# VERIFICAÇÃO DE CONFIGURAÇÃO
# =============================================================================
def check_config() -> tuple[bool, list[str]]:
    from config import validate_config, SYSTEM, ANTHROPIC_API_KEY
    errors = validate_config()
    warnings = []

    if not ANTHROPIC_API_KEY:
        warnings.append("ANTHROPIC_API_KEY não definida — sistema correrá em modo simulação")

    if SYSTEM.demo_mode:
        warnings.append("demo_mode=True — nenhuma ordem real será executada")

    return len(errors) == 0, errors + warnings


# =============================================================================
# VERIFICAÇÃO DE LIGAÇÕES
# =============================================================================
def check_connections() -> dict:
    results = {}

    # MT5
    try:
        from mt5_connector import MT5Connector
        conn = MT5Connector()
        ok = conn.connect()
        results["mt5"] = "OK" if ok else "SIMULAÇÃO (MT5 não disponível nesta plataforma)"
        conn.disconnect()
    except Exception as e:
        results["mt5"] = f"ERRO: {e}"

    # Claude API
    try:
        from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
        if not ANTHROPIC_API_KEY:
            results["claude"] = "SIMULAÇÃO (API key não definida)"
        else:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            resp = client.messages.create(
                model=ANTHROPIC_MODEL, max_tokens=10,
                messages=[{"role": "user", "content": "ping"}]
            )
            results["claude"] = "OK"
    except Exception as e:
        results["claude"] = f"ERRO: {e}"

    # WhatsApp (Twilio)
    try:
        from config import WHATSAPP_ENABLED, WHATSAPP_ACCOUNT_SID, WHATSAPP_AUTH_TOKEN, WHATSAPP_TO
        if not WHATSAPP_ENABLED:
            results["whatsapp"] = "NÃO CONFIGURADO (opcional) — ver INSTALL.md"
        else:
            import urllib.request, urllib.parse, base64
            url = f"https://api.twilio.com/2010-04-01/Accounts/{WHATSAPP_ACCOUNT_SID}.json"
            credentials = base64.b64encode(f"{WHATSAPP_ACCOUNT_SID}:{WHATSAPP_AUTH_TOKEN}".encode()).decode()
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {credentials}"})
            resp = urllib.request.urlopen(req, timeout=5)
            results["whatsapp"] = f"OK — conta Twilio activa → {WHATSAPP_TO}"
    except Exception as e:
        results["whatsapp"] = f"ERRO: {e}"

    return results


# =============================================================================
# MODO --check
# =============================================================================
if args.check:
    print("\n" + "="*55)
    print("  ALPHA-QUANT · VERIFICAÇÃO DE SISTEMA")
    print("="*55)

    # Dependências
    print("\n[1/3] Dependências Python:")
    missing = check_dependencies()
    if missing:
        for m in missing:
            print(f"  ✗ {m} — instala com: pip install {m}")
    else:
        print("  ✓ Todas as dependências instaladas")

    # Config
    print("\n[2/3] Configuração:")
    ok, msgs = check_config()
    for msg in msgs:
        icon = "✗" if "Erro" in msg or "inválido" in msg.lower() else "⚠"
        print(f"  {icon} {msg}")
    if ok and not msgs:
        print("  ✓ Configuração válida")

    # Ligações
    print("\n[3/3] Ligações:")
    conns = check_connections()
    for name, status in conns.items():
        icon = "✓" if status.startswith("OK") else ("⚠" if "SIMUL" in status or "NÃO" in status else "✗")
        print(f"  {icon} {name.upper()}: {status}")

    print("\n" + "="*55)
    ok_count = sum(1 for s in conns.values() if s.startswith("OK"))
    sim_count = sum(1 for s in conns.values() if "SIMUL" in s or "NÃO" in s)
    print(f"  Resultado: {ok_count} ligações reais · {sim_count} simulações")

    if ok_count == 0 and sim_count > 0:
        print("  → Pronto para MODO DEMO (sem MT5 nem API key)")
    elif ok_count >= 2:
        print("  → Pronto para OPERAÇÃO REAL")
    print("="*55 + "\n")
    sys.exit(0)


# =============================================================================
# MODO --dashboard
# =============================================================================
if args.dashboard:
    print(f"\n  Dashboard em: http://localhost:{args.port}")
    print("  Ctrl+C para parar\n")
    import time
    from risk_manager import TradeDatabase, RiskManager
    from dashboard_server import init_server, run_server, update_state

    db   = TradeDatabase()
    risk = RiskManager(db)
    _dash_conn = MT5Connector()
    _account   = _dash_conn.get_account_info() if _dash_conn.connect() else None
    balance    = _account.balance if _account else float(os.environ.get("DEMO_BALANCE", "500.0"))
    risk.start_day(balance)
    init_server(db, risk)
    run_server(port=args.port)
    update_state("status", risk.get_status())

    try:
        while True:
            time.sleep(2)
            update_state("status", risk.get_status())
    except KeyboardInterrupt:
        print("\nDashboard encerrada.")
    sys.exit(0)


# =============================================================================
# MODO NORMAL — SISTEMA COMPLETO
# =============================================================================
print("\n" + "="*55)
print("  ALPHA-QUANT v0.2 — ARRANQUE DO SISTEMA")
print("="*55)

# Verifica dependências críticas
missing = check_dependencies()
if missing:
    print(f"\n✗ Dependências em falta: {', '.join(missing)}")
    print(f"  Instala com: pip install {' '.join(missing)}")
    sys.exit(1)

print("\n✓ Dependências OK")

# Verifica config
ok, msgs = check_config()
for msg in msgs:
    print(f"⚠ {msg}")

# Mostra parâmetros de risco
from config import RISK, SYSTEM, SYMBOLS
print(f"\nParâmetros de risco:")
print(f"  Símbolos:       {SYMBOLS}")
print(f"  Risco/trade:    {RISK.risk_per_trade_pct}%")
print(f"  Envelope diário: {RISK.max_daily_risk_pct}%")
print(f"  SL máximo:       {RISK.max_sl_pips} pips")
print(f"  R:R mínimo:      {RISK.min_rr_ratio}")
print(f"  Modo demo:       {SYSTEM.demo_mode}")
print(f"  Dashboard:       http://localhost:{args.port}")

print("\nA iniciar em 3 segundos... (Ctrl+C para cancelar)")
import time
try:
    time.sleep(3)
except KeyboardInterrupt:
    print("\nCancelado.")
    sys.exit(0)

# Arranca o orquestrador
from orchestrator import AlphaQuantOrchestrator
orch = AlphaQuantOrchestrator()

# Sobrepõe porta se especificada
if args.port != 5000:
    import dashboard_server
    dashboard_server.run_server = lambda **kw: dashboard_server.run_server(port=args.port, **kw)

orch.start()
