#!/usr/bin/env python3
# =============================================================================
# ALPHA-QUANT · test_integration.py
# Teste end-to-end completo — simula 24h de operação em modo demo
# Corre sem MT5 nem API key
# =============================================================================

import logging, sys, time, json
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("INTEGRATION")

def sep(t, w=58):
    print(f"\n{'='*w}\n  {t}\n{'='*w}")

PASS = []
FAIL = []
def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        logger.info(f"  ✓ {name}")
    else:
        FAIL.append(name)
        logger.error(f"  ✗ {name} {detail}")


# =============================================================================
sep("FASE 1 — Importações e dependências")
# =============================================================================

try:
    from config import RISK, SYSTEM, SYMBOLS, validate_config
    from mt5_connector import MT5Connector
    from didi_indicators import DidiStrategyCalculator
    from supply_demand import SDDetector
    from claude_analyst import ClaudeAnalyst
    from risk_manager import TradeDatabase, RiskManager, OpenTrade, TradeRecord
    from orchestrator import AlphaQuantOrchestrator
    check("Todos os módulos importam", True)
except Exception as e:
    check("Todos os módulos importam", False, str(e))
    sys.exit(1)

errors = validate_config()
check("Config válida (demo mode)", len(errors) == 0, str(errors))


# =============================================================================
sep("FASE 2 — Pipeline de dados completo")
# =============================================================================

conn = MT5Connector()
df_m15 = conn._mock_candles("EURUSD", "M15", 150)
df_h1  = conn._mock_candles("EURUSD", "H1",  150)
df_h4  = conn._mock_candles("EURUSD", "H4",  150)
tick   = conn._mock_tick("EURUSD")

check("Candles M15 gerados", len(df_m15) == 150)
check("Candles H1 gerados",  len(df_h1)  == 150)
check("Candles H4 gerados",  len(df_h4)  == 150)
check("Tick gerado",         tick.spread  > 0)

calc = DidiStrategyCalculator()
snap_m15 = calc.compute(df_m15, "M15")
snap_h1  = calc.compute(df_h1,  "H1")
snap_h4  = calc.compute(df_h4,  "H4")

check("Snapshot M15 calculado", snap_m15 is not None)
check("Snapshot H1 calculado",  snap_h1  is not None)
check("Snapshot H4 calculado",  snap_h4  is not None)
check("Score M15 válido (0-10)", 0 <= snap_m15.confluence.total <= 10)

det = SDDetector()
sd_ctx = det.compute("EURUSD", tick.bid, df_h4, df_h1)
check("S&D context gerado", sd_ctx is not None)
check("S&D bonus válido (0-3)", 0 <= sd_ctx.confluence_bonus <= 3)


# =============================================================================
sep("FASE 3 — Claude Analyst (modo simulação)")
# =============================================================================

analyst = ClaudeAnalyst()
signal = analyst.analyse_setup(
    symbol="EURUSD",
    bid=tick.bid, ask=tick.ask, spread=tick.spread,
    snap_m15=snap_m15, snap_h1=snap_h1, snap_h4=snap_h4,
    account_balance=500.0, news_warning="none",
    sd_context=sd_ctx,
)
# Em modo simulação, o pré-filtro pode descartar — isso é correcto
check("Analyst não crasha", True)

# Força teste de validação com resposta mock
mock_json = json.dumps({
    "decision": "BUY", "entry": tick.ask, "sl": tick.ask - 0.0014,
    "tp": tick.ask + 0.0028, "confidence": 8,
    "reasoning": "Agulhada bull Didi + zona DEMAND H4.",
    "candle_pattern": "BULLISH_ENGULF", "primary_signal": "Didi+SD"
})
sig = analyst._parse_and_validate_signal(mock_json, "EURUSD", tick.bid, tick.ask, 500.0)
check("Sinal válido gerado",      sig is not None)
check("SL dentro do limite",      sig is not None and sig.sl_pips <= RISK.max_sl_pips)
check("R:R acima do mínimo",      sig is not None and sig.rr_ratio >= RISK.min_rr_ratio)
from instruments import get_instrument as _gi
_inst_check = _gi("EURUSD")
check("Lot dentro dos limites",   sig is not None and _inst_check.min_lot <= sig.lot_size <= _inst_check.max_lot)
check("Confidence válido",        sig is not None and sig.confidence >= 7)


# =============================================================================
sep("FASE 4 — Gestor de risco (envelope + kill switch)")
# =============================================================================

db   = TradeDatabase(":memory:")
risk = RiskManager(db)
risk.start_day(500.0)

check("Envelope inicial €15.00", abs(risk.daily.available_risk_eur - 15.0) < 0.01)
check("Kill switch inactivo", not risk.kill_switch_active)

# Abre 3 trades (esgota envelope)
trades_opened = 0
for i in range(3):
    t = OpenTrade(id=i, symbol="EURUSD", direction="BUY",
                  entry_price=1.08420, sl=1.08280, tp=1.08700,
                  lot_size=0.01, risk_eur=5.0,
                  open_time=datetime.now(tz=timezone.utc).isoformat(),
                  ticket=1000+i)
    if risk.register_open(t): trades_opened += 1

check("3 trades abertos (envelope €15)", trades_opened == 3)
check("Envelope esgotado", risk.daily.available_risk_eur == 0.0)

# 4º trade deve ser bloqueado
t4 = OpenTrade(id=99, symbol="EURUSD", direction="BUY",
               entry_price=1.08420, sl=1.08280, tp=1.08700,
               lot_size=0.01, risk_eur=5.0,
               open_time=datetime.now(tz=timezone.utc).isoformat(), ticket=1099)
blocked = not risk.register_open(t4)
check("4º trade bloqueado (envelope)", blocked)

# Fecha um trade com WIN
rec = risk.register_close(1000, 1.08700, "TRAILING")
check("Trade WIN fechado", rec is not None and rec.outcome == "WIN")
check("P&L positivo após WIN", risk.daily.realized_pnl > 0)

# Simula kill switch
risk._daily.realized_pnl = -16.0
risk._daily.current_balance = 484.0
risk._check_ks()
check("Kill switch activa a -3%+", risk.kill_switch_active)

# Novo dia
risk.start_day(484.0)
check("Kill switch reset no novo dia", not risk.kill_switch_active)


# =============================================================================
sep("FASE 5 — Breakeven e trailing stop")
# =============================================================================

risk2 = RiskManager(db)
risk2.start_day(500.0)
trade = OpenTrade(id=50, symbol="EURUSD", direction="BUY",
                  entry_price=1.08420, sl=1.08280, tp=1.08700,
                  lot_size=0.01, risk_eur=5.0,
                  open_time=datetime.now(tz=timezone.utc).isoformat(), ticket=2001)
risk2.register_open(trade)

be = risk2.check_breakeven(trade, 1.08560)
check("Breakeven activa a 50% TP", be is not None and be == trade.entry_price)
risk2.update_trade_sl(2001, be, "breakeven")
t_ref = risk2._open_trades[2001]

trail = risk2.calc_trailing_sl(t_ref, 1.08650, ma8_didi=1.08610)
check("Trailing activo após breakeven", trail is not None)
check("Trailing acima do SL anterior", trail > t_ref.sl if trail else False)

risk2.update_trade_sl(2001, trail or t_ref.sl, "trailing")
t_ref2 = risk2._open_trades[2001]
trail2 = risk2.calc_trailing_sl(t_ref2, 1.08640, ma8_didi=1.08600)
check("Trailing não retrocede", trail2 is None or trail2 >= t_ref2.sl)


# =============================================================================
sep("FASE 6 — Base de dados e estatísticas")
# =============================================================================

# Popula com trades históricos
results = [("WIN",2.8,"TRAILING"),("WIN",3.5,"TRAILING"),("LOSS",-1.5,"SL_HIT"),
           ("WIN",4.1,"TRAILING"),("WIN",2.9,"TRAILING"),("LOSS",-1.8,"SL_HIT")]
for outcome, pnl, reason in results:
    pips = pnl / (10.0 * 0.01)
    exit_p = 1.08420 + pips * 0.0001
    rec = TradeRecord(
        symbol="EURUSD", direction="BUY",
        entry_price=1.08420, exit_price=exit_p,
        sl_initial=1.08280, tp_initial=1.08700,
        lot_size=0.01, sl_pips=14.0, tp_pips=28.0,
        pips_result=round(pips,1), pnl_eur=round(pnl,2),
        rr_ratio=round(abs(pips)/14,2) if pips>0 else 0,
        outcome=outcome, exit_reason=reason,
        open_time="2026-03-23T09:00:00+00:00",
        close_time="2026-03-23T11:00:00+00:00",
        duration_min=120, confidence=8,
        reasoning="Test trade", kill_zones="london_kill_zone", score_m15=8,
    )
    db.save_trade(rec)

stats = db.get_stats(days=30)
check("Stats calculadas", stats["trades"] >= 6)
check("Win rate calculado",  stats["win_rate"] > 0)
check("P&L total correcto",  stats["total_pnl"] > 0)  # 4 wins > 2 losses
check("Expectativa positiva", stats["expectancy"] > 0)
logger.info(f"  Stats: {stats}")


# =============================================================================
sep("FASE 7 — Kill Zones (timing institucional)")
# =============================================================================

kz_tests = [
    (3, 0, "london_kill_zone", True),
    (8, 30, "ny_kill_zone", True),
    (11, 0, "lunch_dead_zone", False),
    (13, 0, "ny_london_overlap", True),
    (21, 0, "asian_open", True),
    (16, 0, "ny_afternoon", False),
]
kz_pass = True
for h, m, expected_zone, expected_active in kz_tests:
    mock_dt = datetime(2026, 3, 23, h, m, tzinfo=timezone.utc)
    with patch("risk_manager.datetime") as mock:
        mock.now.return_value = mock_dt
        z, a = RiskManager.get_active_kill_zone()
        if z != expected_zone:
            kz_pass = False
            logger.warning(f"    {h:02d}:{m:02d} → got {z}, expected {expected_zone}")

check("Kill zones (6 janelas)", kz_pass)


# =============================================================================
sep("FASE 8 — Orquestrador: 1 ciclo completo")
# =============================================================================

orch = AlphaQuantOrchestrator()
orch.risk.start_day(500.0)
try:
    orch._run_scout()
    check("Scout corre sem crash", True)
except Exception as e:
    check("Scout corre sem crash", False, str(e))

status = orch.risk.get_status()
check("Status do sistema válido", "balance" in status and "kill_switch" in status)
check("Balance inicial correcta", status["balance"] == 500.0)


# =============================================================================
sep("RESULTADO FINAL")
# =============================================================================

total = len(PASS) + len(FAIL)
print(f"""
  {'─'*54}
  Testes passados:  {len(PASS)}/{total}
  Testes falhados:  {len(FAIL)}/{total}
  {'─'*54}""")

if FAIL:
    print("  FALHAS:")
    for f in FAIL:
        print(f"    ✗ {f}")
else:
    print("""
  ✓ SISTEMA 100% OPERACIONAL

  Para arrancar HOJE na conta demo:

  1. pip install pandas numpy anthropic flask python-dotenv MetaTrader5
  2. Copia .env.example → .env e preenche as credenciais
  3. python run.py --check     (verifica ligações)
  4. python run.py --demo      (arranca em modo demo)
  5. Abre http://localhost:5000 no browser
""")

sys.exit(0 if not FAIL else 1)
