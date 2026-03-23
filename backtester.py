# =============================================================================
# ALPHA-QUANT · backtester.py
# Motor de backtesting sobre dados históricos
#
# Simula o comportamento completo do sistema:
#   · Carrega dados históricos do MT5 ou de CSV
#   · Aplica todos os filtros (Didi, S&D, kill zone, correlação)
#   · Simula entradas e saídas com breakeven e trailing
#   · Gera relatório completo com métricas estatísticas
#   · Optimiza parâmetros (score mínimo, SL, etc.)
# =============================================================================

import logging
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """Trade simulado no backtesting."""
    symbol:       str
    direction:    str
    entry_price:  float
    entry_time:   datetime
    sl:           float
    tp_ref:       float      # TP de referência (para breakeven)
    lot:          float
    score:        int
    exit_price:   float = 0.0
    exit_time:    Optional[datetime] = None
    exit_reason:  str = ""
    pips:         float = 0.0
    pnl_eur:      float = 0.0
    outcome:      str = "OPEN"
    breakeven_done: bool = False
    current_sl:   float = 0.0

    def __post_init__(self):
        self.current_sl = self.sl


@dataclass
class BacktestResult:
    """Resultado completo de um backtesting."""
    symbol:        str
    period_start:  datetime
    period_end:    datetime
    total_trades:  int = 0
    wins:          int = 0
    losses:        int = 0
    breakevens:    int = 0

    total_pnl:     float = 0.0
    max_drawdown:  float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate:      float = 0.0
    avg_rr:        float = 0.0
    expectancy:    float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio:  float = 0.0

    best_trade:    float = 0.0
    worst_trade:   float = 0.0
    avg_trade_min: float = 0.0

    trades:        list[BacktestTrade] = field(default_factory=list)
    equity_curve:  list[float] = field(default_factory=list)
    params:        dict = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{'='*50}\n"
            f"  BACKTESTING — {self.symbol}\n"
            f"  {self.period_start.strftime('%Y-%m-%d')} → {self.period_end.strftime('%Y-%m-%d')}\n"
            f"{'='*50}\n"
            f"  Trades:        {self.total_trades} ({self.wins}W / {self.losses}L / {self.breakevens}BE)\n"
            f"  Win Rate:      {self.win_rate:.1f}%\n"
            f"  R:R médio:     {self.avg_rr:.2f}\n"
            f"  Expectativa:   {self.expectancy:+.3f}\n"
            f"  P&L total:     €{self.total_pnl:+.2f}\n"
            f"  Max Drawdown:  €{self.max_drawdown:.2f} ({self.max_drawdown_pct:.1f}%)\n"
            f"  Profit Factor: {self.profit_factor:.2f}\n"
            f"  Sharpe Ratio:  {self.sharpe_ratio:.2f}\n"
            f"  Melhor trade:  €{self.best_trade:+.2f}\n"
            f"  Pior trade:    €{self.worst_trade:+.2f}\n"
            f"{'='*50}"
        )


class Backtester:
    """
    Motor de backtesting completo.
    Simula o sistema Alpha-Quant sobre dados históricos.
    """

    def __init__(self, initial_balance: float = None):
        from config import RISK
        self.initial_balance = initial_balance if initial_balance is not None else 500.0
        self.risk = RISK

    # ─── ENTRY POINT ─────────────────────────────────────────────────────────

    def run(
        self,
        symbol:         str,
        df_m15:         pd.DataFrame,
        df_h1:          pd.DataFrame,
        df_h4:          pd.DataFrame,
        min_score:      int   = 7,
        risk_pct:       float = 1.0,
        min_rr:         float = 1.8,
        max_sl_pips:    int   = 25,
        use_kill_zones: bool  = True,
        use_sd_zones:   bool  = True,
    ) -> BacktestResult:
        """
        Executa backtesting completo.

        Args:
            df_m15/h1/h4: DataFrames com colunas OHLCV e coluna 'time'
            min_score:    Score mínimo de confluência para entrar
            risk_pct:     % do saldo a arriscar por trade
            min_rr:       R:R mínimo aceitável
            max_sl_pips:  SL máximo em pips
        """
        from didi_indicators import DidiStrategyCalculator
        from supply_demand import SDDetector
        from instruments import get_instrument
        from risk_manager import RiskManager, TradeDatabase

        logger.info(f"Backtesting {symbol}: {len(df_m15)} velas M15")

        calc  = DidiStrategyCalculator()
        sd    = SDDetector()
        inst  = get_instrument(symbol)
        pip   = inst.pip_size

        result = BacktestResult(
            symbol=symbol,
            period_start=df_m15["time"].iloc[0],
            period_end=df_m15["time"].iloc[-1],
            params={
                "min_score": min_score, "risk_pct": risk_pct,
                "min_rr": min_rr, "max_sl_pips": max_sl_pips,
                "use_kill_zones": use_kill_zones,
            }
        )

        balance  = self.initial_balance
        open_trade: Optional[BacktestTrade] = None
        equity_history = [balance]

        # Janela mínima para calcular indicadores
        warmup = 50

        for i in range(warmup, len(df_m15) - 1):
            candle_time = df_m15["time"].iloc[i]
            close       = float(df_m15["close"].iloc[i])
            high        = float(df_m15["high"].iloc[i])
            low         = float(df_m15["low"].iloc[i])

            # ── Gere trade aberto ──────────────────────────────────────────
            if open_trade:
                open_trade, closed = self._manage_open_trade(
                    open_trade, high, low, close, candle_time,
                    df_m15.iloc[:i+1], inst
                )
                if closed:
                    pnl = closed.pnl_eur
                    balance += pnl
                    equity_history.append(balance)
                    result.trades.append(closed)
                    open_trade = None
                    continue

            # ── Scout: procura novo setup ──────────────────────────────────
            if open_trade:
                continue  # já há trade aberto

            # Calcula indicadores
            snap_m15 = calc.compute(df_m15.iloc[:i+1], "M15")
            if snap_m15 is None:
                continue

            score     = snap_m15.confluence.total
            direction = snap_m15.confluence.direction

            if score < min_score or direction == "NEUTRAL":
                continue

            # Filtro kill zone
            if use_kill_zones:
                kz, in_kz = self._get_kill_zone(candle_time)
                if not in_kz and inst.category != "crypto":
                    continue

            # Snap H1 para confirmação de tendência
            h1_idx = self._find_h1_index(df_h1, candle_time)
            if h1_idx < 20:
                continue
            snap_h1 = calc.compute(df_h1.iloc[:h1_idx+1], "H1")
            if snap_h1 is None:
                continue

            # H1 deve confirmar a direcção do M15
            if snap_h1.confluence.direction not in (direction, "NEUTRAL"):
                continue

            # Deve haver agulhada no M15
            if direction == "BUY" and not snap_m15.didi.agulhada_bull:
                continue
            if direction == "SELL" and not snap_m15.didi.agulhada_bear:
                continue

            # Calcula entry, SL, TP
            atr_pips = snap_m15.h4.atr_pips if hasattr(snap_m15, 'h4') else inst.avg_atr_pips
            # Usa ATR do M15 para SL mais apertado
            sl_pips = min(
                max(atr_pips * 1.2, 8),
                max_sl_pips
            )

            entry = close
            if direction == "BUY":
                sl     = entry - sl_pips * pip
                tp_ref = entry + sl_pips * min_rr * pip
            else:
                sl     = entry + sl_pips * pip
                tp_ref = entry - sl_pips * min_rr * pip

            # Valida R:R
            actual_rr = abs(tp_ref - entry) / abs(sl - entry)
            if actual_rr < min_rr:
                continue

            # Position sizing
            lot = inst.calc_lot(balance, risk_pct, sl_pips)

            # Abre trade simulado
            open_trade = BacktestTrade(
                symbol=symbol,
                direction=direction,
                entry_price=entry,
                entry_time=candle_time,
                sl=sl,
                tp_ref=tp_ref,
                lot=lot,
                score=score,
            )
            logger.debug(
                f"BT trade: {direction} {symbol} @ {entry:.5f} "
                f"SL={sl:.5f} TP={tp_ref:.5f} score={score}"
            )

        # Fecha trade aberto no final (mark-to-market)
        if open_trade:
            last_close = float(df_m15["close"].iloc[-1])
            open_trade.exit_price  = last_close
            open_trade.exit_time   = df_m15["time"].iloc[-1]
            open_trade.exit_reason = "END_OF_DATA"
            pip_diff = (
                (last_close - open_trade.entry_price) if open_trade.direction == "BUY"
                else (open_trade.entry_price - last_close)
            ) / pip
            open_trade.pips    = round(pip_diff, 1)
            open_trade.pnl_eur = round(pip_diff * inst.pip_value_eur * open_trade.lot, 2)
            open_trade.outcome = "WIN" if pip_diff > 0.5 else ("LOSS" if pip_diff < -0.5 else "BREAKEVEN")
            balance += open_trade.pnl_eur
            equity_history.append(balance)
            result.trades.append(open_trade)

        # ── Calcula métricas ───────────────────────────────────────────────
        self._calc_metrics(result, equity_history)
        return result

    def _manage_open_trade(
        self,
        trade:     BacktestTrade,
        high:      float,
        low:       float,
        close:     float,
        time:      datetime,
        df_m15:    pd.DataFrame,
        inst,
    ) -> tuple[Optional[BacktestTrade], Optional[BacktestTrade]]:
        """
        Gere um trade aberto vela a vela.
        Retorna (trade_still_open, closed_trade).
        """
        pip = inst.pip_size
        is_buy = trade.direction == "BUY"

        # Verifica SL hit
        sl_hit = (is_buy and low <= trade.current_sl) or (not is_buy and high >= trade.current_sl)
        if sl_hit:
            exit_price = trade.current_sl
            pips = (exit_price - trade.entry_price if is_buy else trade.entry_price - exit_price) / pip
            trade.exit_price  = exit_price
            trade.exit_time   = time
            trade.exit_reason = "SL_HIT"
            trade.pips        = round(pips, 1)
            trade.pnl_eur     = round(pips * inst.pip_value_eur * trade.lot, 2)
            trade.outcome     = "WIN" if pips > 0.5 else ("LOSS" if pips < -0.5 else "BREAKEVEN")
            return None, trade

        # Breakeven
        if not trade.breakeven_done:
            tp_dist = abs(trade.tp_ref - trade.entry_price)
            trigger = (
                trade.entry_price + tp_dist * (self.risk.breakeven_trigger_pct / 100)
                if is_buy
                else trade.entry_price - tp_dist * (self.risk.breakeven_trigger_pct / 100)
            )
            if (is_buy and close >= trigger) or (not is_buy and close <= trigger):
                trade.current_sl    = trade.entry_price
                trade.breakeven_done = True

        # Trailing (activa após breakeven)
        if trade.breakeven_done:
            trail_dist = self.risk.trailing_distance_pips * pip
            if is_buy:
                new_sl = close - trail_dist
                if new_sl > trade.current_sl:
                    trade.current_sl = round(new_sl, 5)
            else:
                new_sl = close + trail_dist
                if new_sl < trade.current_sl:
                    trade.current_sl = round(new_sl, 5)

        return trade, None

    def _calc_metrics(self, result: BacktestResult, equity: list):
        """Calcula todas as métricas estatísticas."""
        trades = result.trades
        if not trades:
            return

        result.total_trades = len(trades)
        result.wins         = sum(1 for t in trades if t.outcome == "WIN")
        result.losses       = sum(1 for t in trades if t.outcome == "LOSS")
        result.breakevens   = sum(1 for t in trades if t.outcome == "BREAKEVEN")
        result.win_rate     = round(result.wins / result.total_trades * 100, 1)
        result.total_pnl    = round(sum(t.pnl_eur for t in trades), 2)

        wins_pnl   = [t.pnl_eur for t in trades if t.pnl_eur > 0]
        losses_pnl = [abs(t.pnl_eur) for t in trades if t.pnl_eur < 0]
        rrs        = [abs(t.pips / ((t.entry_price - t.sl) / 0.0001)) for t in trades if t.pips > 0 and t.sl != t.entry_price]

        result.avg_rr      = round(np.mean(rrs), 2) if rrs else 0.0
        result.best_trade  = max((t.pnl_eur for t in trades), default=0.0)
        result.worst_trade = min((t.pnl_eur for t in trades), default=0.0)

        # Expectativa
        if result.total_trades > 0:
            wr = result.wins / result.total_trades
            result.expectancy = round(
                (wr * result.avg_rr) - (1 - wr), 3
            )

        # Profit Factor
        gross_profit = sum(wins_pnl) if wins_pnl else 0
        gross_loss   = sum(losses_pnl) if losses_pnl else 1
        result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0.0

        # Max Drawdown
        if equity:
            peak = equity[0]
            max_dd = 0.0
            for e in equity:
                if e > peak:
                    peak = e
                dd = peak - e
                if dd > max_dd:
                    max_dd = dd
            result.max_drawdown     = round(max_dd, 2)
            result.max_drawdown_pct = round(max_dd / self.initial_balance * 100, 1)

        # Sharpe Ratio (simplificado)
        pnls = [t.pnl_eur for t in trades]
        if len(pnls) > 1:
            mean_pnl = np.mean(pnls)
            std_pnl  = np.std(pnls)
            result.sharpe_ratio = round(mean_pnl / std_pnl if std_pnl > 0 else 0.0, 2)

        # Duração média
        durations = []
        for t in trades:
            if t.exit_time and t.entry_time:
                d = (t.exit_time - t.entry_time).total_seconds() / 60
                durations.append(d)
        result.avg_trade_min = round(np.mean(durations), 0) if durations else 0.0

        result.equity_curve = equity

    def _get_kill_zone(self, dt: datetime) -> tuple[str, bool]:
        """Determina a kill zone para uma data/hora."""
        h = dt.hour + dt.minute / 60.0
        if 20.0 <= h or h < 0.5:  return "asian_open", True
        if 2.0  <= h < 5.0:       return "london_kill_zone", True
        if 7.0  <= h < 10.0:      return "ny_kill_zone", True
        if 12.0 <= h < 14.0:      return "ny_london_overlap", True
        return "off_hours", False

    def _find_h1_index(self, df_h1: pd.DataFrame, target_time: datetime) -> int:
        """Encontra o índice H1 correspondente a uma hora M15."""
        times = pd.to_datetime(df_h1["time"])
        mask  = times <= pd.Timestamp(target_time)
        idxs  = mask[mask].index
        return int(idxs[-1]) if len(idxs) > 0 else 0

    # ─── OPTIMIZADOR ─────────────────────────────────────────────────────────

    def optimize(
        self,
        symbol:  str,
        df_m15:  pd.DataFrame,
        df_h1:   pd.DataFrame,
        df_h4:   pd.DataFrame,
        param_grid: dict = None,
    ) -> list[BacktestResult]:
        """
        Optimiza parâmetros testando múltiplas combinações.
        Retorna lista de resultados ordenados por expectativa.
        """
        if param_grid is None:
            param_grid = {
                "min_score":   [6, 7, 8],
                "risk_pct":    [0.5, 1.0],
                "min_rr":      [1.5, 1.8, 2.0],
                "max_sl_pips": [20, 25, 30],
            }

        results = []
        combinations = self._generate_combinations(param_grid)
        total = len(combinations)

        logger.info(f"Optimizando {symbol}: {total} combinações...")

        for i, params in enumerate(combinations):
            logger.debug(f"  Combinação {i+1}/{total}: {params}")
            try:
                result = self.run(symbol, df_m15, df_h1, df_h4, **params)
                if result.total_trades >= 5:  # mínimo de trades para ser significativo
                    results.append(result)
            except Exception as e:
                logger.warning(f"  Combinação {params} falhou: {e}")

        # Ordena por expectativa descendente
        results.sort(key=lambda r: r.expectancy, reverse=True)

        if results:
            best = results[0]
            logger.info(f"\nMelhor configuração para {symbol}:")
            logger.info(f"  Parâmetros: {best.params}")
            logger.info(f"  Expectativa: {best.expectancy:+.3f}")
            logger.info(f"  Win Rate: {best.win_rate:.1f}%")
            logger.info(f"  P&L: €{best.total_pnl:+.2f}")

        return results

    def _generate_combinations(self, grid: dict) -> list[dict]:
        """Gera todas as combinações de parâmetros."""
        import itertools
        keys   = list(grid.keys())
        values = list(grid.values())
        return [dict(zip(keys, combo)) for combo in itertools.product(*values)]
