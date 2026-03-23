# =============================================================================
# ALPHA-QUANT · orchestrator.py
# O cérebro operacional — une todos os módulos num loop 24/7:
#   Scout (novo sinal) + Guardian (operações abertas) + Executor (MT5)
# =============================================================================

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv opcional

from config import SYMBOLS, SYSTEM, RISK, validate_config, MAX_SYMBOLS_SIMULTANEOUS
from mt5_connector import MT5Connector, MarketData
from indicators import IndicatorCalculator
from didi_indicators import DidiStrategyCalculator
from claude_analyst import ClaudeAnalyst, TradeSignal
from supply_demand import SDDetector, SDContext
from news_calendar import NewsCalendar, calendar as news_cal
from correlation_filter import CorrelationFilter, corr_filter
from watchdog import HeartbeatWriter
from risk_manager import RiskManager, TradeDatabase, OpenTrade

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# SETUP DE LOGGING
# -----------------------------------------------------------------------------

def setup_logging():
    import os, io
    os.makedirs("logs", exist_ok=True)

    # Garante UTF-8 no stream de consola (Windows usa CP1252 por defeito)
    stream = sys.stdout
    if sys.platform == "win32":
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        elif hasattr(stream, "buffer"):
            stream = io.TextIOWrapper(stream.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)

    handlers = [logging.StreamHandler(stream)]
    try:
        handlers.append(logging.FileHandler(SYSTEM.log_path, encoding="utf-8"))
    except Exception:
        pass

    logging.basicConfig(
        level=getattr(logging, SYSTEM.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


# -----------------------------------------------------------------------------
# SIGNAL HANDLER (Ctrl+C limpo)
# -----------------------------------------------------------------------------

class GracefulShutdown:
    def __init__(self):
        self.running = True
        signal.signal(signal.SIGINT,  self._handler)
        signal.signal(signal.SIGTERM, self._handler)

    def _handler(self, *_):
        logger.info("Sinal de paragem recebido — a terminar ciclo actual...")
        self.running = False


# -----------------------------------------------------------------------------
# ORQUESTRADOR PRINCIPAL
# -----------------------------------------------------------------------------

class AlphaQuantOrchestrator(HeartbeatWriter):
    """
    Loop principal do sistema:
    1. A cada fecho de vela M15 → Scout (procura setup)
    2. A cada 30s → Guardian (monitoriza trades abertos)
    3. Pending signals → espera confirmação humana (via dashboard/WhatsApp)
    """

    def __init__(self):
        self.conn    = MT5Connector()
        self.ind_calc = IndicatorCalculator()
        self.didi_calc = DidiStrategyCalculator()
        self.analyst  = ClaudeAnalyst()
        self.sd_det   = SDDetector()
        self.db      = TradeDatabase(SYSTEM.db_path)
        self.risk    = RiskManager(self.db)
        self.shutdown = GracefulShutdown()

        # Dashboard server (thread separada)
        self._dashboard_thread = None

        # Sinal pendente a aguardar decisão humana
        # Lock necessário: _pending_signal é acedido da thread do Scout
        # e da thread do dashboard (human_decision via HTTP)
        self._pending_lock    = threading.Lock()
        self._pending_signal: Optional[TradeSignal] = None
        self._pending_since:  Optional[float] = None
        self._signal_timeout  = 90.0  # segundos até expirar

        # Controlo de ciclos
        self._last_scout_candle = ""
        self._last_guardian_check = 0.0

    # -------------------------------------------------------------------------
    # ARRANQUE
    # -------------------------------------------------------------------------

    def start(self):
        """Ponto de entrada principal."""
        setup_logging()
        logger.info("=" * 60)
        logger.info("  ALPHA-QUANT v0.2 — A ARRANCAR")
        logger.info("=" * 60)

        # Valida configuração
        errors = validate_config()
        if errors:
            for e in errors:
                logger.error(f"Config inválida: {e}")
            if not SYSTEM.demo_mode:
                sys.exit(1)

        # Conecta ao MT5
        if not self.conn.connect():
            if not SYSTEM.demo_mode:
                logger.critical("Falha na ligação ao MT5 — a terminar")
                sys.exit(1)
            logger.warning("MT5 não disponível — modo simulação activo")

        # Inicializa o dia
        account = self.conn.get_account_info()
        if account is None:
            logger.critical("Não foi possível obter saldo da conta MT5")
            if not SYSTEM.demo_mode:
                sys.exit(1)
            balance = 500.0  # apenas em demo mode sem MT5
        else:
            balance = account.balance
        self.risk.start_day(balance)

        logger.info(f"Símbolos monitorizados: {SYMBOLS}")
        logger.info(f"Modo demo: {SYSTEM.demo_mode}")
        logger.info(f"Sistema pronto. A iniciar loop principal...")
        logger.info("=" * 60)

        self._main_loop()

    # -------------------------------------------------------------------------
    # LOOP PRINCIPAL
    # -------------------------------------------------------------------------

    def _main_loop(self):
        while self.shutdown.running:
            try:
                now = time.time()

                # Kill switch — não processa nada
                if self.risk.kill_switch_active:
                    logger.warning("Kill switch activo — sistema suspenso")
                    time.sleep(60)
                    continue

                # 1. Verifica sinal pendente (expiração)
                self._check_pending_expiry()

                # 2. Guardian: monitoriza operações abertas (a cada 30s)
                if now - self._last_guardian_check >= SYSTEM.guardian_interval_seconds:
                    self._run_guardian()
                    self._last_guardian_check = now

                # 3. Scout: procura novos setups (no fecho de cada vela M15)
                current_candle_id = self._get_current_candle_id()
                with self._pending_lock:
                    has_pending = self._pending_signal is not None
                if (current_candle_id != self._last_scout_candle and not has_pending):
                    self._run_scout()
                    self._last_scout_candle = current_candle_id

                # Pausa antes do próximo ciclo
                time.sleep(SYSTEM.scout_interval_seconds)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Erro no loop principal: {e}", exc_info=True)
                time.sleep(10)

        self._shutdown()

    # -------------------------------------------------------------------------
    # SCOUT — PROCURA DE SETUP
    # -------------------------------------------------------------------------

    def _run_scout(self):
        """Analisa cada símbolo à procura de setup de alta probabilidade."""
        kill_zone, in_kz = RiskManager.get_active_kill_zone()

        if not in_kz:
            logger.debug(f"Fora de kill zone ({kill_zone}) — Scout em modo passivo")

        # Filtra símbolos activos por sessão da kill zone
        from instruments import get_instrument, get_session_instruments
        active_symbols = []
        for sym in SYMBOLS:
            inst = get_instrument(sym)
            if inst and ("always" in inst.sessions or in_kz):
                active_symbols.append(sym)
            elif not in_kz and inst and inst.category == "crypto":
                active_symbols.append(sym)  # Cripto opera sempre

        # Limita ao máximo configurado
        if MAX_SYMBOLS_SIMULTANEOUS > 0:
            active_symbols = active_symbols[:MAX_SYMBOLS_SIMULTANEOUS]

        logger.info(f"Scout activo em {len(active_symbols)} símbolos: {active_symbols}")

        for symbol in active_symbols:
            try:
                self._scout_symbol(symbol, kill_zone, in_kz)
            except Exception as e:
                logger.error(f"Erro no Scout para {symbol}: {e}", exc_info=True)

    def _scout_symbol(self, symbol: str, kill_zone: str, in_kz: bool):
        """Análise completa de um símbolo."""
        # Obtém dados de mercado
        market = self._get_market_data(symbol)
        if market is None:
            return

        # Calcula indicadores Didi + confluência nos 3 timeframes
        snap_m15 = self.didi_calc.compute(market.candles_m15, "M15")
        snap_h1  = self.didi_calc.compute(market.candles_h1,  "H1")
        snap_h4  = self.didi_calc.compute(market.candles_h4,  "H4")

        if snap_m15 is None or snap_h1 is None or snap_h4 is None:
            logger.debug(f"{symbol}: dados insuficientes para análise")
            return

        score_m15 = snap_m15.confluence.total
        direction = snap_m15.confluence.direction

        # Log de actividade
        logger.info(
            f"Scout [{symbol}] · Score M15: {score_m15}/10 ({direction}) · "
            f"Kill zone: {kill_zone} ({'activa' if in_kz else 'inactiva'})"
        )

        # Regista no log mesmo que descarte
        if score_m15 < 5:
            self.db.log_signal(symbol, direction, score_m15, "DISCARDED_SCORE", f"Score {score_m15} insuficiente")
            return

        # Penaliza score se fora de kill zone (mas não bloqueia)
        effective_score = score_m15 if in_kz else score_m15 - 1
        if effective_score < 5:
            self.db.log_signal(symbol, direction, score_m15, "DISCARDED_KILLZONE", f"Fora de kill zone ({kill_zone})")
            return

        # Verifica calendário económico
        safe, news_reason = news_cal.is_safe_to_trade(symbol)
        if not safe:
            logger.info(f"Scout [{symbol}] bloqueado por notícia: {news_reason}")
            self.db.log_signal(symbol, "BLOCKED", 0, "NEWS_BLOCK", news_reason)
            return

        # Detecta zonas Supply & Demand
        sd_ctx = self.sd_det.compute(
            symbol=symbol,
            current_price=market.tick.bid,
            df_h4=market.candles_h4,
            df_h1=market.candles_h1,
        )
        if sd_ctx.confluence_bonus > 0:
            logger.info(f"S&D [{symbol}]: {sd_ctx.confluence_note}")

        # Envia ao Claude para análise
        signal = self.analyst.analyse_setup(
            symbol=symbol,
            bid=market.tick.bid,
            ask=market.tick.ask,
            spread=market.tick.spread,
            snap_m15=snap_m15,
            snap_h1=snap_h1,
            snap_h4=snap_h4,
            account_balance=self.risk.daily.current_balance if self.risk.daily else 500.0,
            news_warning="none",
            sd_context=sd_ctx,
        )

        if signal is None:
            self.db.log_signal(symbol, direction, score_m15, "NO_TRADE", "Claude: sem setup")
            return

        # Verifica correlação com trades abertos e sinal pendente
        corr_check = corr_filter.check_new_signal(
            symbol, signal.direction, signal.confidence,
            self.risk._open_trades,
            self._pending_signal,
        )
        if not corr_check.allowed:
            self.db.log_signal(symbol, signal.direction, score_m15, "BLOCKED_CORR", corr_check.reason)
            logger.info(f"Sinal bloqueado por correlação: {corr_check.reason}")
            return

        # Verifica se pode abrir (envelope de risco)
        can, reason = self.risk.can_open_trade(signal.risk_eur)
        if not can:
            self.db.log_signal(symbol, direction, score_m15, "BLOCKED_RISK", reason)
            logger.warning(f"Sinal bloqueado pelo gestor de risco: {reason}")
            return

        # Guarda sinal pendente e notifica o utilizador
        with self._pending_lock:
            self._pending_signal = signal
            self._pending_since  = time.time()

        self.db.log_signal(symbol, signal.direction, score_m15, "PENDING_HUMAN", signal.reasoning[:100])
        self._notify_signal(signal, kill_zone, in_kz)

    # -------------------------------------------------------------------------
    # GUARDIAN — MONITORIZAÇÃO DE OPERAÇÕES ABERTAS
    # -------------------------------------------------------------------------

    def _run_guardian(self):
        """Monitoriza todas as operações abertas."""
        open_trades = list(self.risk._open_trades.values())
        if not open_trades:
            return

        for trade in open_trades:
            try:
                self._guardian_check_trade(trade)
            except Exception as e:
                logger.error(f"Erro no Guardian para trade {trade.ticket}: {e}", exc_info=True)

    def _guardian_check_trade(self, trade: OpenTrade):
        """Verifica breakeven, trailing e sinais de reversão para uma operação."""
        market = self._get_market_data(trade.symbol)
        if market is None:
            return

        current_price = market.tick.bid if trade.is_buy else market.tick.ask
        snap_m15 = self.didi_calc.compute(market.candles_m15, "M15")
        snap_h1  = self.didi_calc.compute(market.candles_h1,  "H1")

        if snap_m15 is None:
            return

        ma8_didi = snap_m15.didi.ma8

        # Breakeven
        new_be = self.risk.check_breakeven(trade, current_price)
        if new_be is not None:
            self.risk.update_trade_sl(trade.ticket, new_be, "breakeven")
            if not SYSTEM.demo_mode:
                self._mt5_modify_sl(trade.ticket, new_be)
            logger.info(f"Breakeven aplicado: {trade.symbol} SL → {new_be:.5f}")

        # Trailing
        new_trail = self.risk.calc_trailing_sl(trade, current_price, ma8_didi)
        if new_trail is not None:
            self.risk.update_trade_sl(trade.ticket, new_trail, "trailing")
            if not SYSTEM.demo_mode:
                self._mt5_modify_sl(trade.ticket, new_trail)

        # Alerta de reversão via Claude
        from instruments import get_instrument
        _pip = get_instrument(trade.symbol).pip_size
        open_pips = (
            (current_price - trade.entry_price) / _pip if trade.is_buy
            else (trade.entry_price - current_price) / _pip
        )

        alert = self.analyst.monitor_trade(
            symbol=trade.symbol,
            direction=trade.direction,
            entry_price=trade.entry_price,
            current_price=current_price,
            current_sl=trade.sl,
            open_pips=open_pips,
            snap_m15=snap_m15,
            snap_h1=snap_h1,
        )

        if alert and alert.alert_type == "REVERSAL_WARNING":
            logger.warning(
                f"REVERSÃO DETECTADA: {trade.symbol} {trade.direction} · "
                f"{alert.urgency} · {alert.message}"
            )
            self._notify_reversal(trade, alert, open_pips)

    # -------------------------------------------------------------------------
    # DECISÃO HUMANA — SIM / NÃO
    # -------------------------------------------------------------------------

    def human_decision(self, approved: bool, ticket_override: int = 0):
        """
        Chamado externamente (dashboard, WhatsApp, CLI) quando o utilizador
        aprova ou rejeita um sinal pendente.
        """
        with self._pending_lock:
            if self._pending_signal is None:
                logger.warning("Não há sinal pendente para decisão")
                return
            signal = self._pending_signal
            self._pending_signal = None
            self._pending_since  = None

        if not approved:
            self.db.log_signal(signal.symbol, signal.direction, signal.confidence, "REJECTED_HUMAN", "Utilizador rejeitou")
            logger.info(f"Sinal rejeitado pelo utilizador: {signal.direction} {signal.symbol}")
            return

        # Executa a ordem
        self._execute_signal(signal, ticket_override)

    def _check_pending_expiry(self):
        """Cancela sinal pendente se expirou sem resposta."""
        with self._pending_lock:
            if self._pending_signal is None or self._pending_since is None:
                return
            elapsed = time.time() - self._pending_since
            if elapsed <= self._signal_timeout:
                return
            signal = self._pending_signal
            self._pending_signal = None
            self._pending_since  = None

        self.db.log_signal(signal.symbol, signal.direction, signal.confidence, "EXPIRED", f"Sem resposta em {self._signal_timeout:.0f}s")
        logger.info(f"Sinal expirado: {signal.direction} {signal.symbol} (sem resposta em {elapsed:.0f}s)")

    # -------------------------------------------------------------------------
    # EXECUÇÃO DE ORDEM
    # -------------------------------------------------------------------------

    def _execute_signal(self, signal: TradeSignal, ticket: int = 0):
        """Executa ordem no MT5 (ou simula em demo mode)."""
        if SYSTEM.demo_mode:
            ticket = ticket or int(time.time())
            logger.info(
                f"[DEMO] Ordem simulada: {signal.direction} {signal.symbol} · "
                f"entry={signal.entry:.5f} SL={signal.sl:.5f} TP={signal.tp:.5f} · "
                f"lot={signal.lot_size} · ticket={ticket}"
            )
        else:
            ticket = self._mt5_open_order(signal)
            if ticket == 0:
                logger.error(f"Falha na execução da ordem MT5")
                return

        kill_zone, _ = RiskManager.get_active_kill_zone()
        trade = OpenTrade(
            id=ticket,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            lot_size=signal.lot_size,
            risk_eur=signal.risk_eur,
            open_time=datetime.now(tz=timezone.utc).isoformat(),
            ticket=ticket,
            confidence=signal.confidence,
            reasoning=signal.reasoning,
        )

        self.risk.register_open(trade)
        self.db.log_signal(signal.symbol, signal.direction, signal.confidence, "EXECUTED", f"ticket={ticket}")

        logger.info(
            f"ORDEM ABERTA: {signal.direction} {signal.symbol} · "
            f"€{signal.risk_eur:.2f} em risco · ticket={ticket}"
        )

    # -------------------------------------------------------------------------
    # INTEGRAÇÃO MT5
    # -------------------------------------------------------------------------

    def _mt5_open_order(self, signal: TradeSignal) -> int:
        """Abre ordem real no MT5. Retorna ticket ou 0 em falha."""
        try:
            import MetaTrader5 as mt5
            order_type = mt5.ORDER_TYPE_BUY if signal.direction == "BUY" else mt5.ORDER_TYPE_SELL

            request = {
                "action":   mt5.TRADE_ACTION_DEAL,
                "symbol":   signal.symbol,
                "volume":   signal.lot_size,
                "type":     order_type,
                "price":    signal.entry,
                "sl":       signal.sl,
                "tp":       signal.tp,
                "deviation": 10,
                "magic":    20260323,
                "comment":  f"AQ conf={signal.confidence}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                retcode = result.retcode if result else "None"
                logger.error(f"Ordem MT5 falhou: retcode={retcode}")
                return 0

            return result.order

        except Exception as e:
            logger.error(f"Erro ao abrir ordem MT5: {e}")
            return 0

    def _mt5_modify_sl(self, ticket: int, new_sl: float):
        """Modifica SL de ordem aberta no MT5."""
        try:
            import MetaTrader5 as mt5
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                return

            pos = positions[0]
            request = {
                "action":   mt5.TRADE_ACTION_SLTP,
                "symbol":   pos.symbol,
                "sl":       new_sl,
                "tp":       pos.tp,
                "position": ticket,
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"SL MT5 modificado: ticket={ticket} novo SL={new_sl:.5f}")
            else:
                retcode = result.retcode if result else "None"
                logger.warning(f"Falha ao modificar SL MT5: retcode={retcode}")

        except Exception as e:
            logger.error(f"Erro ao modificar SL MT5: {e}")

    # -------------------------------------------------------------------------
    # NOTIFICAÇÕES
    # -------------------------------------------------------------------------

    def _notify_signal(self, signal: TradeSignal, kill_zone: str, in_kz: bool):
        """Notifica o utilizador de um novo sinal (WhatsApp + log)."""
        kz_tag = f"✓ {kill_zone}" if in_kz else f"⚠ fora de kill zone"
        message = (
            f"🎯 SINAL ALPHA-QUANT\n"
            f"{'─'*30}\n"
            f"Par:        {signal.symbol}\n"
            f"Direcção:   {signal.direction}\n"
            f"Entry:      {signal.entry:.5f}\n"
            f"Stop Loss:  {signal.sl:.5f} ({signal.sl_pips:.1f} pips)\n"
            f"Take Profit:{signal.tp:.5f} ({signal.tp_pips:.1f} pips)\n"
            f"R:R:        {signal.rr_ratio}\n"
            f"Lot:        {signal.lot_size}\n"
            f"Risco:      €{signal.risk_eur:.2f}\n"
            f"Confiança:  {signal.confidence}/10\n"
            f"Kill Zone:  {kz_tag}\n"
            f"Padrão:     {signal.candle_pattern}\n"
            f"{'─'*30}\n"
            f"{signal.reasoning}\n"
            f"{'─'*30}\n"
            f"⏱ 90s para responder · Expira às {self._expiry_time()}"
        )
        logger.info(f"\n{message}")
        self._whatsapp(message)

    def _notify_reversal(self, trade, alert, open_pips: float):
        """Notifica reversão detectada numa operação aberta."""
        pnl_str = f"+{open_pips:.1f}" if open_pips >= 0 else f"{open_pips:.1f}"
        message = (
            f"⚠ REVERSÃO DETECTADA — {alert.urgency}\n"
            f"{'─'*30}\n"
            f"Par:      {trade.symbol} ({trade.direction})\n"
            f"P&L:      {pnl_str} pips\n"
            f"SL actual: {trade.sl:.5f}\n"
            f"Acção:    {alert.action}\n"
            f"{'─'*30}\n"
            f"{alert.message}\n"
            f"{alert.reasoning}"
        )
        logger.warning(f"\n{message}")
        self._whatsapp(message)

    def _whatsapp(self, message: str):
        """Envia mensagem WhatsApp via Twilio API (se configurado)."""
        from config import WHATSAPP_ENABLED, WHATSAPP_ACCOUNT_SID, WHATSAPP_AUTH_TOKEN, WHATSAPP_FROM, WHATSAPP_TO
        if not WHATSAPP_ENABLED:
            return
        try:
            import urllib.request, urllib.parse, base64
            url  = f"https://api.twilio.com/2010-04-01/Accounts/{WHATSAPP_ACCOUNT_SID}/Messages.json"
            body = urllib.parse.urlencode({
                "From": WHATSAPP_FROM,
                "To":   WHATSAPP_TO,
                "Body": message,
            }).encode()
            credentials = base64.b64encode(
                f"{WHATSAPP_ACCOUNT_SID}:{WHATSAPP_AUTH_TOKEN}".encode()
            ).decode()
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type":  "application/x-www-form-urlencoded",
            })
            urllib.request.urlopen(req, timeout=8)
            logger.info("WhatsApp enviado com sucesso")
        except Exception as e:
            logger.warning(f"WhatsApp falhou: {e}")

    # -------------------------------------------------------------------------
    # UTILITÁRIOS
    # -------------------------------------------------------------------------

    def _get_market_data(self, symbol: str) -> Optional[MarketData]:
        """Obtém dados de mercado (real ou simulado)."""
        if self.conn.is_connected:
            return self.conn.get_market_data(symbol)
        else:
            tick     = self.conn._mock_tick(symbol)
            df_m15   = self.conn._mock_candles(symbol, "M15", 150)
            df_h1    = self.conn._mock_candles(symbol, "H1",  150)
            df_h4    = self.conn._mock_candles(symbol, "H4",  150)
            from mt5_connector import MarketData
            from datetime import datetime, timezone
            return MarketData(
                symbol=symbol, tick=tick,
                candles_m15=df_m15, candles_h1=df_h1, candles_h4=df_h4,
                fetched_at=datetime.now(tz=timezone.utc)
            )

    def _get_current_candle_id(self) -> str:
        """ID único da vela M15 actual (para detectar fecho)."""
        now = datetime.now(tz=timezone.utc)
        candle_min = (now.minute // 15) * 15
        return f"{now.strftime('%Y%m%d%H')}{candle_min:02d}"

    def _expiry_time(self) -> str:
        from datetime import timedelta
        exp = datetime.now(tz=timezone.utc) + timedelta(seconds=self._signal_timeout)
        return exp.strftime("%H:%M UTC")

    def _shutdown(self):
        logger.info("A terminar Alpha-Quant graciosamente...")
        status = self.risk.get_status()
        logger.info(
            f"Sessão terminada · P&L: €{status['daily_pnl_eur']:.2f} "
            f"({status['daily_pnl_pct']:+.2f}%) · "
            f"Trades: {status['trades_today']}"
        )
        self.conn.disconnect()
        logger.info("Sistema encerrado. Até amanhã.")


# -----------------------------------------------------------------------------
# PONTO DE ENTRADA
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    orchestrator = AlphaQuantOrchestrator()
    orchestrator.start()
