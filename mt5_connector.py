import os
# =============================================================================
# ALPHA-QUANT · mt5_connector.py
# Ligação ao MetaTrader 5 — leitura de preços, candles e estado da conta
# =============================================================================

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

# MetaTrader5 só corre em Windows — importação protegida
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

from config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH
from config import CANDLES_HISTORY, SYSTEM

logger = logging.getLogger(__name__)

# Mapeamento de strings para constantes MT5
TIMEFRAME_MAP = {
    "M1":  1,
    "M5":  5,
    "M15": 15,
    "M30": 30,
    "H1":  16385,
    "H4":  16388,
    "D1":  16408,
}


# -----------------------------------------------------------------------------
# ESTRUTURAS DE DADOS
# -----------------------------------------------------------------------------

@dataclass
class AccountInfo:
    balance:   float
    equity:    float
    margin:    float
    free_margin: float
    profit:    float
    currency:  str
    leverage:  int
    server:    str


@dataclass
class TickData:
    symbol:   str
    bid:      float
    ask:      float
    spread:   float   # em pips
    time:     datetime


@dataclass
class MarketData:
    """
    Estrutura completa enviada ao sistema de análise.
    Inclui candles dos 3 timeframes + tick actual.
    """
    symbol:       str
    tick:         TickData
    candles_m15:  pd.DataFrame   # OHLCV dos últimos N candles M15
    candles_h1:   pd.DataFrame   # OHLCV dos últimos N candles H1
    candles_h4:   pd.DataFrame   # OHLCV dos últimos N candles H4
    fetched_at:   datetime


# -----------------------------------------------------------------------------
# CONNECTOR CLASS
# -----------------------------------------------------------------------------

class MT5Connector:
    """
    Gere toda a comunicação com o MetaTrader 5.

    Uso:
        conn = MT5Connector()
        if conn.connect():
            data = conn.get_market_data("EURUSD")
            conn.disconnect()
    """

    def __init__(self):
        self._connected = False
        self._last_connect_attempt = 0.0

    # -------------------------------------------------------------------------
    # LIGAÇÃO
    # -------------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Inicia ligação ao MT5.
        Retorna True se bem-sucedido, False caso contrário.
        """
        if not MT5_AVAILABLE:
            logger.warning(
                "MetaTrader5 não disponível — modo simulação activado. "
                "Instala com: pip install MetaTrader5"
            )
            return False

        # Evita reconexões demasiado frequentes
        now = time.time()
        if now - self._last_connect_attempt < 5:
            return self._connected
        self._last_connect_attempt = now

        try:
            # Inicia o terminal MT5
            init_ok = mt5.initialize(
                path=MT5_PATH if MT5_PATH else None,
                login=MT5_LOGIN if MT5_LOGIN else None,
                password=MT5_PASSWORD if MT5_PASSWORD else None,
                server=MT5_SERVER if MT5_SERVER else None,
                timeout=10000,
            )

            if not init_ok:
                error = mt5.last_error()
                logger.error(f"MT5 initialize falhou: {error}")
                self._connected = False
                return False

            # Verifica se a conta está autorizada
            account = mt5.account_info()
            if account is None:
                logger.error("MT5: não foi possível obter info da conta")
                self._connected = False
                return False

            self._connected = True
            logger.info(
                f"MT5 conectado · Conta: {account.login} · "
                f"Servidor: {account.server} · "
                f"Saldo: {account.balance:.2f} {account.currency}"
            )
            return True

        except Exception as e:
            logger.error(f"Erro inesperado ao conectar MT5: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """Encerra ligação ao MT5."""
        if MT5_AVAILABLE and self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 desconectado.")

    def ensure_connected(self) -> bool:
        """Verifica ligação e reconecta se necessário."""
        if self._connected:
            return True
        logger.warning("MT5 não conectado — a tentar reconectar...")
        return self.connect()

    @property
    def is_connected(self) -> bool:
        return self._connected

    # -------------------------------------------------------------------------
    # INFORMAÇÃO DA CONTA
    # -------------------------------------------------------------------------

    def get_account_info(self) -> Optional[AccountInfo]:
        """Retorna snapshot da conta."""
        if not self.ensure_connected():
            return None

        try:
            if not MT5_AVAILABLE:
                return self._mock_account_info()

            info = mt5.account_info()
            if info is None:
                logger.error(f"get_account_info falhou: {mt5.last_error()}")
                return None

            return AccountInfo(
                balance=info.balance,
                equity=info.equity,
                margin=info.margin,
                free_margin=info.margin_free,
                profit=info.profit,
                currency=info.currency,
                leverage=info.leverage,
                server=info.server,
            )
        except Exception as e:
            logger.error(f"Erro em get_account_info: {e}")
            return None

    # -------------------------------------------------------------------------
    # DADOS DE MERCADO
    # -------------------------------------------------------------------------

    def get_tick(self, symbol: str) -> Optional[TickData]:
        """Retorna o tick actual (bid/ask/spread) de um símbolo."""
        if not self.ensure_connected():
            return None

        try:
            if not MT5_AVAILABLE:
                return self._mock_tick(symbol)

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.warning(f"Tick não disponível para {symbol}: {mt5.last_error()}")
                return None

            # Calcula spread em pips (assume 5 casas decimais para pares major)
            symbol_info = mt5.symbol_info(symbol)
            digits = symbol_info.digits if symbol_info else 5
            pip_size = 10 ** -(digits - 1)
            spread_pips = round((tick.ask - tick.bid) / pip_size, 1)

            return TickData(
                symbol=symbol,
                bid=tick.bid,
                ask=tick.ask,
                spread=spread_pips,
                time=datetime.fromtimestamp(tick.time, tz=timezone.utc),
            )
        except Exception as e:
            logger.error(f"Erro em get_tick({symbol}): {e}")
            return None

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        count: int = CANDLES_HISTORY,
    ) -> Optional[pd.DataFrame]:
        """
        Retorna DataFrame com candles OHLCV.
        Colunas: time, open, high, low, close, volume
        """
        if not self.ensure_connected():
            return None

        try:
            if not MT5_AVAILABLE:
                return self._mock_candles(symbol, timeframe, count)

            tf_const = TIMEFRAME_MAP.get(timeframe)
            if tf_const is None:
                logger.error(f"Timeframe desconhecido: {timeframe}")
                return None

            rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, count)
            if rates is None or len(rates) == 0:
                logger.warning(
                    f"Sem dados para {symbol} {timeframe}: {mt5.last_error()}"
                )
                return None

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.rename(columns={"tick_volume": "volume"})
            df = df[["time", "open", "high", "low", "close", "volume"]]
            df = df.sort_values("time").reset_index(drop=True)

            logger.debug(
                f"Candles obtidos: {symbol} {timeframe} · {len(df)} velas · "
                f"Última: {df['time'].iloc[-1]}"
            )
            return df

        except Exception as e:
            logger.error(f"Erro em get_candles({symbol}, {timeframe}): {e}")
            return None

    def get_market_data(self, symbol: str) -> Optional[MarketData]:
        """
        Retorna dados completos de mercado para um símbolo:
        tick actual + candles dos 3 timeframes.
        Este é o objecto que vai para o sistema de análise.
        """
        logger.info(f"A obter dados de mercado para {symbol}...")

        tick = self.get_tick(symbol)
        if tick is None:
            logger.error(f"Sem tick para {symbol} — abortando get_market_data")
            return None

        candles_m15 = self.get_candles(symbol, "M15")
        candles_h1  = self.get_candles(symbol, "H1")
        candles_h4  = self.get_candles(symbol, "H4")

        if candles_m15 is None or candles_h1 is None or candles_h4 is None:
            logger.error(f"Candles incompletos para {symbol}")
            return None

        return MarketData(
            symbol=symbol,
            tick=tick,
            candles_m15=candles_m15,
            candles_h1=candles_h1,
            candles_h4=candles_h4,
            fetched_at=datetime.now(tz=timezone.utc),
        )

    # -------------------------------------------------------------------------
    # DADOS SIMULADOS (modo sem MT5 instalado)
    # -------------------------------------------------------------------------

    def _mock_account_info(self) -> AccountInfo:
        from config import SYSTEM
        demo_balance = float(os.environ.get("DEMO_BALANCE", "500.0"))
        return AccountInfo(
            balance=demo_balance, equity=demo_balance * 1.005,
            margin=demo_balance * 0.02, free_margin=demo_balance * 0.98,
            profit=demo_balance * 0.005,
            currency="EUR", leverage=100, server="Demo-Simulado",
        )

    def _mock_tick(self, symbol: str) -> TickData:
        import random
        from instruments import get_instrument
        inst = get_instrument(symbol)
        # Usa preço base realista por categoria (apenas para demo sem MT5)
        base_prices = {
            "forex":  1.10000,
            "crypto": 50000.0,
            "metal":  2000.0,
            "index":  15000.0,
            "energy": 80.0,
        }
        base = base_prices.get(inst.category, 1.10000)
        noise = base * 0.0005
        price = base + random.uniform(-noise, noise)
        return TickData(
            symbol=symbol,
            bid=round(price, 5),
            ask=round(price + 0.00008, 5),
            spread=0.8,
            time=datetime.now(tz=timezone.utc),
        )

    def _mock_candles(
        self, symbol: str, timeframe: str, count: int
    ) -> pd.DataFrame:
        """Gera candles sintéticos realistas para testes (demo sem MT5)."""
        import numpy as np
        from instruments import get_instrument
        inst = get_instrument(symbol)
        # Preço base por categoria — nunca hardcoded
        _bases = {"forex":1.10000,"crypto":50000.0,"metal":2000.0,"index":15000.0,"energy":80.0}
        base_price = _bases.get(inst.category, 1.10000)
        timestamps = pd.date_range(
            end=pd.Timestamp.now(tz="UTC"),
            periods=count,
            freq={"M15": "15min", "H1": "1h", "H4": "4h"}.get(timeframe, "15min"),
        )

        returns = np.random.normal(0, 0.0003, count)
        closes = base_price * np.cumprod(1 + returns)
        opens  = np.roll(closes, 1); opens[0] = base_price
        highs  = np.maximum(opens, closes) + np.abs(np.random.normal(0, 0.0002, count))
        lows   = np.minimum(opens, closes) - np.abs(np.random.normal(0, 0.0002, count))

        return pd.DataFrame({
            "time":   timestamps,
            "open":   np.round(opens, 5),
            "high":   np.round(highs, 5),
            "low":    np.round(lows, 5),
            "close":  np.round(closes, 5),
            "volume": np.random.randint(100, 2000, count),
        })


# -----------------------------------------------------------------------------
# INSTÂNCIA GLOBAL
# -----------------------------------------------------------------------------
connector = MT5Connector()
