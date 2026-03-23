# =============================================================================
# ALPHA-QUANT · risk_manager.py
# =============================================================================

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, date, timezone
from typing import Optional

from config import RISK, SYSTEM

logger = logging.getLogger(__name__)


@dataclass
class OpenTrade:
    id:             int
    symbol:         str
    direction:      str
    entry_price:    float
    sl:             float
    tp:             float
    lot_size:       float
    risk_eur:       float
    open_time:      str
    ticket:         int  = 0
    breakeven_done: bool = False
    trailing_active: bool = False
    confidence:     int  = 0
    reasoning:      str  = ""

    @property
    def is_buy(self) -> bool:
        return self.direction == "BUY"


@dataclass
class DailyStats:
    date:             str
    starting_balance: float
    current_balance:  float
    realized_pnl:     float = 0.0
    open_risk_eur:    float = 0.0
    trades_taken:     int   = 0
    trades_won:       int   = 0
    trades_lost:      int   = 0
    kill_switch_active: bool = False

    @property
    def pnl_pct(self) -> float:
        return (self.realized_pnl / self.starting_balance * 100) if self.starting_balance else 0.0

    @property
    def available_risk_eur(self) -> float:
        max_r = self.starting_balance * (RISK.max_daily_risk_pct / 100)
        loss  = abs(self.realized_pnl) if self.realized_pnl < 0 else 0.0
        return max(0.0, max_r - loss - self.open_risk_eur)

    @property
    def win_rate(self) -> float:
        total = self.trades_won + self.trades_lost
        return (self.trades_won / total * 100) if total else 0.0


@dataclass
class TradeRecord:
    symbol:       str
    direction:    str
    entry_price:  float
    exit_price:   float
    sl_initial:   float
    tp_initial:   float
    lot_size:     float
    sl_pips:      float
    tp_pips:      float
    pips_result:  float
    pnl_eur:      float
    rr_ratio:     float
    outcome:      str
    exit_reason:  str
    open_time:    str
    close_time:   str
    duration_min: int
    confidence:   int
    reasoning:    str
    kill_zones:   str = ""
    score_m15:    int = 0


class TradeDatabase:

    def __init__(self, db_path: str = SYSTEM.db_path):
        self.db_path = db_path
        if db_path == ":memory:":
            self._mem = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self._mem = None
            os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self._init()

    def _c(self) -> sqlite3.Connection:
        return self._mem or sqlite3.connect(self.db_path)

    def _init(self):
        c = self._c()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, direction TEXT,
                entry_price REAL, exit_price REAL,
                sl_initial REAL, tp_initial REAL, lot_size REAL,
                sl_pips REAL, tp_pips REAL, pips_result REAL,
                pnl_eur REAL, rr_ratio REAL,
                outcome TEXT, exit_reason TEXT,
                open_time TEXT, close_time TEXT, duration_min INTEGER,
                confidence INTEGER, reasoning TEXT,
                kill_zones TEXT, score_m15 INTEGER
            );
            CREATE TABLE IF NOT EXISTS signals_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, symbol TEXT, direction TEXT,
                score INTEGER, action TEXT, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE, starting_balance REAL, ending_balance REAL,
                realized_pnl REAL, trades_taken INTEGER,
                trades_won INTEGER, trades_lost INTEGER,
                kill_switch_triggered INTEGER DEFAULT 0
            );
        """)
        c.commit()
        logger.info(f"DB pronta: {self.db_path}")

    def save_trade(self, r: TradeRecord) -> int:
        c = self._c()
        cur = c.execute(
            "INSERT INTO trades (symbol,direction,entry_price,exit_price,sl_initial,tp_initial,"
            "lot_size,sl_pips,tp_pips,pips_result,pnl_eur,rr_ratio,outcome,exit_reason,"
            "open_time,close_time,duration_min,confidence,reasoning,kill_zones,score_m15) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.symbol, r.direction, r.entry_price, r.exit_price,
             r.sl_initial, r.tp_initial, r.lot_size,
             r.sl_pips, r.tp_pips, r.pips_result, r.pnl_eur, r.rr_ratio,
             r.outcome, r.exit_reason, r.open_time, r.close_time,
             r.duration_min, r.confidence, r.reasoning, r.kill_zones, r.score_m15))
        c.commit()
        return cur.lastrowid

    def log_signal(self, symbol, direction, score, action, reason):
        c = self._c()
        c.execute(
            "INSERT INTO signals_log (timestamp,symbol,direction,score,action,reason) VALUES (?,?,?,?,?,?)",
            (datetime.now(tz=timezone.utc).isoformat(), symbol, direction, score, action, reason))
        c.commit()

    def get_stats(self, days: int = 30) -> dict:
        c = self._c()
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT outcome,pnl_eur,rr_ratio FROM trades WHERE close_time >= datetime('now',?)",
            (f"-{days} days",)).fetchall()
        if not rows:
            return {"trades": 0, "win_rate": 0.0, "avg_rr": 0.0, "total_pnl": 0.0, "expectancy": 0.0}
        total = len(rows)
        wins  = sum(1 for r in rows if r["outcome"] == "WIN")
        pnl   = sum(r["pnl_eur"]  for r in rows)
        rr    = sum(r["rr_ratio"] for r in rows) / total
        exp   = (wins/total * rr) - ((total-wins)/total)
        return {"trades": total, "wins": wins, "losses": total-wins,
                "win_rate": round(wins/total*100, 1), "avg_rr": round(rr, 2),
                "total_pnl": round(pnl, 2), "expectancy": round(exp, 3)}

    def get_recent_trades(self, limit: int = 20) -> list:
        c = self._c()
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


class RiskManager:

    def __init__(self, db: TradeDatabase):
        self.db = db
        self._open: dict[int, OpenTrade] = {}
        self._daily: Optional[DailyStats] = None
        self._nid = 1

    def start_day(self, balance: float):
        self._daily = DailyStats(date=date.today().isoformat(),
                                 starting_balance=balance, current_balance=balance)
        logger.info(f"Dia iniciado · Saldo: €{balance:.2f} · Envelope: €{balance*RISK.max_daily_risk_pct/100:.2f}")

    @property
    def daily(self): return self._daily

    @property
    def kill_switch_active(self) -> bool:
        return self._daily.kill_switch_active if self._daily else False

    # Alias para compatibilidade com orchestrator
    @property
    def _open_trades(self): return self._open

    def can_open_trade(self, risk_eur: float) -> tuple[bool, str]:
        if not self._daily: return False, "Dia não iniciado"
        if self._daily.kill_switch_active: return False, "Kill switch activo"
        avail = self._daily.available_risk_eur
        if risk_eur > avail: return False, f"Envelope: €{risk_eur:.2f} > €{avail:.2f}"
        return True, ""

    def register_open(self, trade: OpenTrade) -> bool:
        ok, reason = self.can_open_trade(trade.risk_eur)
        if not ok:
            logger.warning(f"Bloqueado: {reason}")
            return False
        key = trade.ticket or self._nid
        self._open[key] = trade
        self._nid += 1
        self._daily.open_risk_eur += trade.risk_eur
        self._daily.trades_taken  += 1
        logger.info(f"Aberto: {trade.direction} {trade.symbol} · €{trade.risk_eur:.2f}")
        return True

    def check_breakeven(self, trade: OpenTrade, price: float) -> Optional[float]:
        if trade.breakeven_done: return None
        tp_dist = abs(trade.tp - trade.entry_price)
        trigger = (trade.entry_price + tp_dist * RISK.breakeven_trigger_pct/100
                   if trade.is_buy
                   else trade.entry_price - tp_dist * RISK.breakeven_trigger_pct/100)
        hit = (trade.is_buy and price >= trigger) or (not trade.is_buy and price <= trigger)
        if hit:
            logger.info(f"BREAKEVEN: {trade.symbol} SL→{trade.entry_price:.5f}")
            return trade.entry_price
        return None

    def calc_trailing_sl(self, trade: OpenTrade, price: float, ma8_didi: float) -> Optional[float]:
        if not trade.breakeven_done: return None
        from instruments import get_instrument
        pip = get_instrument(trade.symbol).pip_size
        dist = RISK.trailing_distance_pips * pip
        if trade.is_buy:
            new_sl = max(ma8_didi - pip*2, price - dist)
            return round(new_sl, 5) if new_sl > trade.sl else None
        else:
            new_sl = min(ma8_didi + pip*2, price + dist)
            return round(new_sl, 5) if new_sl < trade.sl else None

    def update_trade_sl(self, ticket: int, new_sl: float, reason: str = "trailing"):
        t = self._open.get(ticket)
        if t:
            t.sl = new_sl
            if reason == "breakeven": t.breakeven_done = True
            if reason == "trailing":  t.trailing_active = True
            logger.info(f"SL [{reason}]: {t.symbol} → {new_sl:.5f}")

    def register_close(self, ticket: int, exit_price: float, exit_reason: str,
                       score_m15: int = 0, kill_zone: str = "") -> Optional[TradeRecord]:
        t = self._open.pop(ticket, None)
        if not t: return None
        from instruments import get_instrument
        inst  = get_instrument(t.symbol)
        pip   = inst.pip_size
        pips  = ((exit_price - t.entry_price) if t.is_buy else (t.entry_price - exit_price)) / pip
        pnl   = pips * inst.pip_value_eur * t.lot_size
        out   = "WIN" if pips > 0.5 else ("LOSS" if pips < -0.5 else "BREAKEVEN")
        self._daily.realized_pnl    += pnl
        self._daily.current_balance += pnl
        self._daily.open_risk_eur    = max(0, self._daily.open_risk_eur - t.risk_eur)
        if out == "WIN":   self._daily.trades_won  += 1
        elif out == "LOSS": self._daily.trades_lost += 1
        self._check_ks()
        try:
            dur = int((datetime.now(tz=timezone.utc) - datetime.fromisoformat(t.open_time)).total_seconds()/60)
        except Exception:
            dur = 0
        sl_p = abs(t.entry_price - t.sl) / pip
        rr   = abs(pips) / sl_p if sl_p else 0
        rec  = TradeRecord(
            symbol=t.symbol, direction=t.direction,
            entry_price=t.entry_price, exit_price=exit_price,
            sl_initial=t.sl, tp_initial=t.tp, lot_size=t.lot_size,
            sl_pips=round(sl_p,1), tp_pips=round(abs(t.tp-t.entry_price)/pip,1),
            pips_result=round(pips,1), pnl_eur=round(pnl,2), rr_ratio=round(rr,2),
            outcome=out, exit_reason=exit_reason,
            open_time=t.open_time, close_time=datetime.now(tz=timezone.utc).isoformat(),
            duration_min=dur, confidence=t.confidence, reasoning=t.reasoning,
            kill_zones=kill_zone, score_m15=score_m15)
        self.db.save_trade(rec)
        logger.info(f"Fechado [{exit_reason}]: {t.direction} {t.symbol} · {pips:+.1f}p · €{pnl:+.2f} · {out}")
        return rec

    def _check_ks(self):
        if not self._daily.kill_switch_active and self._daily.pnl_pct <= -RISK.max_daily_risk_pct:
            self._daily.kill_switch_active = True
            logger.critical(f"KILL SWITCH ACTIVADO · Perda: {self._daily.pnl_pct:.2f}%")

    @staticmethod
    def get_active_kill_zone() -> tuple[str, bool]:
        now = datetime.now(tz=timezone.utc)
        h = now.hour + now.minute / 60.0
        if 20.0 <= h or h < 0.5:  return "asian_open", True
        if 2.0  <= h < 5.0:       return "london_kill_zone", True
        if 7.0  <= h < 10.0:      return "ny_kill_zone", True
        if 12.0 <= h < 14.0:      return "ny_london_overlap", True
        if 10.0 <= h < 12.0:      return "lunch_dead_zone", False
        return "ny_afternoon", False

    def get_status(self) -> dict:
        kz, in_kz = self.get_active_kill_zone()
        d = self._daily
        return {
            "date": d.date if d else "N/A",
            "balance": d.current_balance if d else 0,
            "daily_pnl_eur": d.realized_pnl if d else 0,
            "daily_pnl_pct": round(d.pnl_pct, 2) if d else 0,
            "open_trades": len(self._open),
            "open_risk_eur": d.open_risk_eur if d else 0,
            "available_risk": d.available_risk_eur if d else 0,
            "kill_switch": self.kill_switch_active,
            "trades_today": d.trades_taken if d else 0,
            "win_rate_today": d.win_rate if d else 0,
            "kill_zone": kz, "in_kill_zone": in_kz,
        }
