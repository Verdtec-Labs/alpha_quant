# =============================================================================
# ALPHA-QUANT · indicators.py
# Cálculo de indicadores técnicos sobre DataFrames de candles
# Sem dependências externas além de pandas e numpy
# =============================================================================

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from config import FILTERS, RISK

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# ESTRUTURAS DE DADOS
# -----------------------------------------------------------------------------

@dataclass
class IndicatorSnapshot:
    """
    Snapshot dos indicadores calculados para um timeframe.
    É este objecto que vai para o prompt do Claude.
    """
    timeframe: str

    # Preço
    close:   float
    open:    float
    high:    float
    low:     float

    # Médias Móveis
    ma20:    float
    ma200:   float
    ma_trend: str   # "BULL" | "BEAR" | "NEUTRAL"

    # RSI
    rsi:     float
    rsi_zone: str  # "NEUTRAL" | "OVERBOUGHT" | "OVERSOLD"

    # ATR (volatilidade em pips)
    atr_pips: float

    # Padrões de vela detectados
    candle_pattern: str  # Ex: "BULLISH_ENGULF" | "BEARISH_PIN" | "NONE"

    # Estrutura de preço
    higher_highs: bool  # Últimas 3 swing highs em alta?
    lower_lows:   bool  # Últimas 3 swing lows em baixa?


@dataclass
class MarketSnapshot:
    """
    Snapshot completo dos 3 timeframes — enviado ao Claude.
    """
    symbol:    str
    bid:       float
    ask:       float
    spread:    float
    timestamp: str

    h4:  IndicatorSnapshot
    h1:  IndicatorSnapshot
    m15: IndicatorSnapshot

    # Tendência consolidada
    trend_alignment: str  # "STRONG_BULL" | "STRONG_BEAR" | "MIXED" | "NEUTRAL"
    trend_score: int      # 0-6 (2 por timeframe: MA + estrutura)


# -----------------------------------------------------------------------------
# CALCULADOR DE INDICADORES
# -----------------------------------------------------------------------------

class IndicatorCalculator:
    """
    Calcula todos os indicadores necessários a partir de DataFrames de candles.
    """

    def compute(
        self,
        symbol: str,
        bid: float,
        ask: float,
        spread: float,
        timestamp: str,
        df_m15: pd.DataFrame,
        df_h1:  pd.DataFrame,
        df_h4:  pd.DataFrame,
    ) -> Optional[MarketSnapshot]:
        """
        Ponto de entrada principal.
        Retorna MarketSnapshot completo ou None se dados insuficientes.
        """
        try:
            snap_h4  = self._compute_timeframe(df_h4,  "H4")
            snap_h1  = self._compute_timeframe(df_h1,  "H1")
            snap_m15 = self._compute_timeframe(df_m15, "M15")

            if snap_h4 is None or snap_h1 is None or snap_m15 is None:
                logger.error("Falha no cálculo de indicadores — dados insuficientes")
                return None

            trend_alignment, trend_score = self._assess_trend_alignment(
                snap_h4, snap_h1, snap_m15
            )

            return MarketSnapshot(
                symbol=symbol,
                bid=bid,
                ask=ask,
                spread=spread,
                timestamp=timestamp,
                h4=snap_h4,
                h1=snap_h1,
                m15=snap_m15,
                trend_alignment=trend_alignment,
                trend_score=trend_score,
            )

        except Exception as e:
            logger.error(f"Erro em IndicatorCalculator.compute: {e}", exc_info=True)
            return None

    # -------------------------------------------------------------------------
    # CÁLCULO POR TIMEFRAME
    # -------------------------------------------------------------------------

    def _compute_timeframe(
        self, df: pd.DataFrame, timeframe: str
    ) -> Optional[IndicatorSnapshot]:
        """Calcula todos os indicadores para um timeframe."""
        min_required = FILTERS.rsi_period + 10
        if len(df) < min_required:
            logger.warning(
                f"{timeframe}: apenas {len(df)} velas "
                f"(mínimo {min_required} necessário)"
            )
            return None

        close = df["close"].values
        high  = df["high"].values
        low   = df["low"].values
        open_ = df["open"].values

        # Últimas velas
        last_close = float(close[-1])
        last_open  = float(open_[-1])
        last_high  = float(high[-1])
        last_low   = float(low[-1])

        # Médias Móveis
        ma20  = float(np.mean(close[-FILTERS.ma_fast:]))
        ma200 = float(np.mean(close[-FILTERS.ma_slow:])) if len(close) >= FILTERS.ma_slow else float(np.mean(close))

        ma_trend = (
            "BULL" if ma20 > ma200
            else "BEAR" if ma20 < ma200
            else "NEUTRAL"
        )

        # RSI
        rsi = self._calc_rsi(close, FILTERS.rsi_period)
        rsi_zone = (
            "OVERBOUGHT" if rsi >= FILTERS.rsi_overbought
            else "OVERSOLD" if rsi <= FILTERS.rsi_oversold
            else "NEUTRAL"
        )

        # ATR em pips
        atr_raw  = self._calc_atr(high, low, close, FILTERS.atr_period)
        atr_pips = round(atr_raw * 10000, 1)  # Assume par com 4-5 casas decimais

        # Padrão de vela (últimas 2 velas)
        candle_pattern = self._detect_candle_pattern(
            open_[-3:], high[-3:], low[-3:], close[-3:]
        )

        # Estrutura de preço (swing highs/lows das últimas 6 velas)
        higher_highs = self._check_higher_highs(high[-6:])
        lower_lows   = self._check_lower_lows(low[-6:])

        return IndicatorSnapshot(
            timeframe=timeframe,
            close=round(last_close, 5),
            open=round(last_open, 5),
            high=round(last_high, 5),
            low=round(last_low, 5),
            ma20=round(ma20, 5),
            ma200=round(ma200, 5),
            ma_trend=ma_trend,
            rsi=round(rsi, 1),
            rsi_zone=rsi_zone,
            atr_pips=atr_pips,
            candle_pattern=candle_pattern,
            higher_highs=higher_highs,
            lower_lows=lower_lows,
        )

    # -------------------------------------------------------------------------
    # INDICADORES TÉCNICOS
    # -------------------------------------------------------------------------

    def _calc_rsi(self, close: np.ndarray, period: int) -> float:
        """RSI de Wilder."""
        if len(close) < period + 1:
            return 50.0

        deltas = np.diff(close)
        gains  = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0

        rs  = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return float(np.clip(rsi, 0, 100))

    def _calc_atr(
        self,
        high: np.ndarray,
        low:  np.ndarray,
        close: np.ndarray,
        period: int,
    ) -> float:
        """Average True Range."""
        if len(close) < 2:
            return 0.0

        prev_close = np.roll(close, 1)
        prev_close[0] = close[0]

        tr = np.maximum(
            high - low,
            np.maximum(
                np.abs(high - prev_close),
                np.abs(low  - prev_close),
            )
        )
        return float(np.mean(tr[-period:]))

    def _detect_candle_pattern(
        self,
        opens:  np.ndarray,
        highs:  np.ndarray,
        lows:   np.ndarray,
        closes: np.ndarray,
    ) -> str:
        """
        Detecta padrões de vela de alta probabilidade.
        Olha para as últimas 2-3 velas.
        """
        if len(closes) < 2:
            return "NONE"

        # Última vela
        o1, h1, l1, c1 = opens[-1], highs[-1], lows[-1], closes[-1]
        # Penúltima vela
        o2, h2, l2, c2 = opens[-2], highs[-2], lows[-2], closes[-2]

        body1 = abs(c1 - o1)
        body2 = abs(c2 - o2)
        range1 = h1 - l1
        range2 = h2 - l2

        # Evita divisão por zero
        if range1 == 0 or range2 == 0:
            return "NONE"

        # Engolfo Bullish: vela bull maior que vela bear anterior
        if (c2 < o2 and c1 > o1           # anterior bear, actual bull
                and o1 <= c2              # abre abaixo do fecho anterior
                and c1 >= o2             # fecha acima da abertura anterior
                and body1 > body2 * 0.8):
            return "BULLISH_ENGULF"

        # Engolfo Bearish
        if (c2 > o2 and c1 < o1
                and o1 >= c2
                and c1 <= o2
                and body1 > body2 * 0.8):
            return "BEARISH_ENGULF"

        # Pin Bar Bullish (hammer): pavio inferior longo, corpo pequeno no topo
        lower_wick1 = min(o1, c1) - l1
        upper_wick1 = h1 - max(o1, c1)
        if (lower_wick1 > body1 * 2
                and upper_wick1 < body1 * 0.5
                and range1 > 0):
            return "BULLISH_PIN"

        # Pin Bar Bearish (shooting star): pavio superior longo
        if (upper_wick1 > body1 * 2
                and lower_wick1 < body1 * 0.5
                and range1 > 0):
            return "BEARISH_PIN"

        # Doji: corpo muito pequeno
        if body1 < range1 * 0.1:
            return "DOJI"

        return "NONE"

    def _check_higher_highs(self, highs: np.ndarray) -> bool:
        """Verifica se os últimos 3 máximos estão em alta."""
        if len(highs) < 3:
            return False
        return bool(highs[-1] > highs[-2] > highs[-3])

    def _check_lower_lows(self, lows: np.ndarray) -> bool:
        """Verifica se os últimos 3 mínimos estão em baixa."""
        if len(lows) < 3:
            return False
        return bool(lows[-1] < lows[-2] < lows[-3])

    # -------------------------------------------------------------------------
    # ALINHAMENTO DE TENDÊNCIA
    # -------------------------------------------------------------------------

    def _assess_trend_alignment(
        self,
        h4:  IndicatorSnapshot,
        h1:  IndicatorSnapshot,
        m15: IndicatorSnapshot,
    ) -> tuple[str, int]:
        """
        Pontua o alinhamento entre os 3 timeframes.
        Score 0-6: 2 pontos por timeframe (MA trend + estrutura de preço).
        """
        bull_score = 0
        bear_score = 0

        for snap in [h4, h1, m15]:
            # Ponto 1: MA trend
            if snap.ma_trend == "BULL":
                bull_score += 1
            elif snap.ma_trend == "BEAR":
                bear_score += 1

            # Ponto 2: estrutura de preço
            if snap.higher_highs:
                bull_score += 1
            elif snap.lower_lows:
                bear_score += 1

        total_score = max(bull_score, bear_score)

        if bull_score >= 5:
            alignment = "STRONG_BULL"
        elif bear_score >= 5:
            alignment = "STRONG_BEAR"
        elif bull_score >= 3 and bull_score > bear_score:
            alignment = "BULL"
        elif bear_score >= 3 and bear_score > bull_score:
            alignment = "BEAR"
        elif bull_score == bear_score:
            alignment = "NEUTRAL"
        else:
            alignment = "MIXED"

        logger.debug(
            f"Trend alignment: {alignment} "
            f"(bull={bull_score}, bear={bear_score})"
        )
        return alignment, total_score


# -----------------------------------------------------------------------------
# INSTÂNCIA GLOBAL
# -----------------------------------------------------------------------------
calculator = IndicatorCalculator()
