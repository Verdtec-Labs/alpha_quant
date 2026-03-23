# =============================================================================
# ALPHA-QUANT · didi_indicators.py
# Indicadores específicos da estratégia:
#   · Didi Index (MA3, MA8, MA20 + agulhada + histograma)
#   · Estocástico
#   · Bandas de Bollinger
#   · MACD
#   · Score de confluência 0-10
# =============================================================================

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# ESTRUTURAS DE DADOS
# -----------------------------------------------------------------------------

@dataclass
class DidiSnapshot:
    """Resultado do Didi Index para um timeframe."""
    ma3:   float
    ma8:   float
    ma20:  float

    # Agulhada: MA3 cruzou MA8 de baixo para cima (bull) ou cima para baixo (bear)
    agulhada_bull: bool
    agulhada_bear: bool

    # Histograma: diferença MA3-MA20 — crescente = força
    hist_value:    float
    hist_growing:  bool   # último valor > penúltimo
    hist_direction: str   # "UP" | "DOWN" | "FLAT"

    # Ordem das MAs (alinhamento completo)
    fully_aligned_bull: bool  # MA3 > MA8 > MA20
    fully_aligned_bear: bool  # MA3 < MA8 < MA20


@dataclass
class StochSnapshot:
    k:      float
    d:      float
    zone:   str   # "OVERSOLD" | "OVERBOUGHT" | "NEUTRAL"
    signal: str   # "BUY" | "SELL" | "NEUTRAL"
    # K cruzou D de baixo para cima (bull) ou cima para baixo (bear)
    cross_bull: bool
    cross_bear: bool


@dataclass
class BollingerSnapshot:
    upper:    float
    middle:   float   # MA20
    lower:    float
    width:    float   # percentagem de largura (volatilidade)
    price_vs_bands: str  # "ABOVE_UPPER" | "BELOW_LOWER" | "NEAR_UPPER" | "NEAR_LOWER" | "MIDDLE"
    squeeze:  bool    # bandwidth abaixo de threshold — potencial breakout


@dataclass
class MacdSnapshot:
    macd_line:   float
    signal_line: float
    histogram:   float
    hist_growing: bool
    direction:   str   # "BULL" | "BEAR" | "NEUTRAL"
    # Cruzamento recente (nas últimas 2 velas)
    cross_bull:  bool
    cross_bear:  bool


@dataclass
class ConfluenceScore:
    """
    Score total 0-10 baseado em todos os indicadores.
    Cada indicador contribui com pontos positivos ou negativos.
    """
    total:    int          # 0-10
    direction: str         # "BUY" | "SELL" | "NEUTRAL"
    breakdown: dict        # detalhe de cada componente
    tradeable: bool        # True se score >= 7
    reasons:   list[str]   # Razões em texto para o briefing


@dataclass
class StrategySnapshot:
    """
    Snapshot completo da estratégia para um timeframe.
    É este objecto que alimenta o prompt do Claude.
    """
    timeframe:   str
    close:       float
    didi:        DidiSnapshot
    stoch:       StochSnapshot
    bollinger:   BollingerSnapshot
    macd:        MacdSnapshot
    confluence:  ConfluenceScore


# -----------------------------------------------------------------------------
# CALCULADOR DA ESTRATÉGIA
# -----------------------------------------------------------------------------

class DidiStrategyCalculator:
    """
    Calcula todos os indicadores da estratégia Didi + confluência.
    """

    # Parâmetros do Didi Index (Didi Aguiar)
    DIDI_FAST   = 3
    DIDI_MID    = 8
    DIDI_SLOW   = 20

    # Parâmetros Estocástico
    STOCH_K     = 14
    STOCH_D     = 3
    STOCH_OB    = 80.0
    STOCH_OS    = 20.0

    # Parâmetros Bollinger
    BB_PERIOD   = 20
    BB_STD      = 2.0
    BB_SQUEEZE_THRESHOLD = 0.002  # width < 0.2% = squeeze

    # Parâmetros MACD
    MACD_FAST   = 12
    MACD_SLOW   = 26
    MACD_SIGNAL = 9

    def compute(
        self,
        df: pd.DataFrame,
        timeframe: str,
    ) -> Optional[StrategySnapshot]:
        """
        Ponto de entrada principal.
        df deve ter colunas: open, high, low, close, volume
        """
        min_bars = max(self.DIDI_SLOW, self.BB_PERIOD, self.MACD_SLOW + self.MACD_SIGNAL) + 5
        if len(df) < min_bars:
            logger.warning(f"{timeframe}: {len(df)} velas (mínimo {min_bars})")
            return None

        try:
            close  = df["close"].values.astype(float)
            high   = df["high"].values.astype(float)
            low    = df["low"].values.astype(float)

            didi      = self._calc_didi(close)
            stoch     = self._calc_stochastic(high, low, close)
            bollinger = self._calc_bollinger(close)
            macd      = self._calc_macd(close)
            confluence = self._calc_confluence(didi, stoch, bollinger, macd)

            return StrategySnapshot(
                timeframe=timeframe,
                close=round(float(close[-1]), 5),
                didi=didi,
                stoch=stoch,
                bollinger=bollinger,
                macd=macd,
                confluence=confluence,
            )

        except Exception as e:
            logger.error(f"Erro em DidiStrategyCalculator.compute({timeframe}): {e}", exc_info=True)
            return None

    # -------------------------------------------------------------------------
    # DIDI INDEX
    # -------------------------------------------------------------------------

    def _calc_didi(self, close: np.ndarray) -> DidiSnapshot:
        """
        Calcula as 3 MAs do Didi e detecta agulhada.

        Agulhada BULL: MA3 cruza MA8 de baixo para cima NA MESMA vela
        que MA3 cruza MA20 — as 3 MAs divergem em leque.

        Agulhada BEAR: inverso.
        """
        ma3  = self._ema(close, self.DIDI_FAST)
        ma8  = self._ema(close, self.DIDI_MID)
        ma20 = self._ema(close, self.DIDI_SLOW)

        # Valores actuais e anteriores
        ma3_now,  ma3_prev  = ma3[-1],  ma3[-2]
        ma8_now,  ma8_prev  = ma8[-1],  ma8[-2]
        ma20_now, ma20_prev = ma20[-1], ma20[-2]

        # Agulhada: MA3 cruza MA8 E MA3 cruza MA20 na mesma vela
        agulhada_bull = (
            ma3_prev <= ma8_prev and ma3_now > ma8_now and   # MA3 cruzou MA8
            ma3_prev <= ma20_prev and ma3_now > ma20_prev    # MA3 também acima de MA20
        )
        agulhada_bear = (
            ma3_prev >= ma8_prev and ma3_now < ma8_now and
            ma3_prev >= ma20_prev and ma3_now < ma20_prev
        )

        # Histograma: MA3 - MA20 (mede divergência)
        hist_now  = float(ma3[-1]  - ma20[-1])
        hist_prev = float(ma3[-2]  - ma20[-2])
        hist_growing = hist_now > hist_prev
        hist_direction = "UP" if hist_now > hist_prev else ("DOWN" if hist_now < hist_prev else "FLAT")

        # Alinhamento completo
        fully_aligned_bull = float(ma3_now) > float(ma8_now) > float(ma20_now)
        fully_aligned_bear = float(ma3_now) < float(ma8_now) < float(ma20_now)

        return DidiSnapshot(
            ma3=round(float(ma3_now), 5),
            ma8=round(float(ma8_now), 5),
            ma20=round(float(ma20_now), 5),
            agulhada_bull=agulhada_bull,
            agulhada_bear=agulhada_bear,
            hist_value=round(hist_now * 10000, 2),  # em pips
            hist_growing=hist_growing,
            hist_direction=hist_direction,
            fully_aligned_bull=fully_aligned_bull,
            fully_aligned_bear=fully_aligned_bear,
        )

    # -------------------------------------------------------------------------
    # ESTOCÁSTICO
    # -------------------------------------------------------------------------

    def _calc_stochastic(
        self, high: np.ndarray, low: np.ndarray, close: np.ndarray
    ) -> StochSnapshot:
        """Estocástico %K e %D."""
        period = self.STOCH_K
        highs  = pd.Series(high).rolling(period).max().values
        lows   = pd.Series(low).rolling(period).min().values

        # %K
        denom = highs - lows
        denom = np.where(denom == 0, 1e-10, denom)
        k_raw = 100 * (close - lows) / denom
        k = pd.Series(k_raw).rolling(self.STOCH_D).mean().values

        # %D (suavização de K)
        d = pd.Series(k).rolling(self.STOCH_D).mean().values

        k_now, k_prev = float(k[-1]), float(k[-2])
        d_now, d_prev = float(d[-1]), float(d[-2])

        zone = (
            "OVERBOUGHT" if k_now >= self.STOCH_OB
            else "OVERSOLD"  if k_now <= self.STOCH_OS
            else "NEUTRAL"
        )

        # Cruzamento K/D
        cross_bull = k_prev <= d_prev and k_now > d_now
        cross_bear = k_prev >= d_prev and k_now < d_now

        signal = (
            "BUY"  if cross_bull and zone != "OVERBOUGHT"
            else "SELL" if cross_bear and zone != "OVERSOLD"
            else "NEUTRAL"
        )

        return StochSnapshot(
            k=round(k_now, 1),
            d=round(d_now, 1),
            zone=zone,
            signal=signal,
            cross_bull=cross_bull,
            cross_bear=cross_bear,
        )

    # -------------------------------------------------------------------------
    # BOLLINGER BANDS
    # -------------------------------------------------------------------------

    def _calc_bollinger(self, close: np.ndarray) -> BollingerSnapshot:
        """Bandas de Bollinger com detecção de squeeze e posição do preço."""
        s      = pd.Series(close)
        middle = s.rolling(self.BB_PERIOD).mean().values
        std    = s.rolling(self.BB_PERIOD).std().values

        upper  = middle + self.BB_STD * std
        lower  = middle - self.BB_STD * std

        price  = float(close[-1])
        up     = float(upper[-1])
        mid    = float(middle[-1])
        lo     = float(lower[-1])

        # Largura relativa (normalizada pelo meio)
        width  = (up - lo) / mid if mid > 0 else 0.0
        squeeze = width < self.BB_SQUEEZE_THRESHOLD

        # Posição do preço relativamente às bandas
        band_range = up - lo if (up - lo) > 0 else 1e-10
        if price > up:
            pos = "ABOVE_UPPER"
        elif price < lo:
            pos = "BELOW_LOWER"
        elif price > mid + (up - mid) * 0.7:
            pos = "NEAR_UPPER"
        elif price < mid - (mid - lo) * 0.7:
            pos = "NEAR_LOWER"
        else:
            pos = "MIDDLE"

        return BollingerSnapshot(
            upper=round(up, 5),
            middle=round(mid, 5),
            lower=round(lo, 5),
            width=round(width * 100, 3),
            price_vs_bands=pos,
            squeeze=squeeze,
        )

    # -------------------------------------------------------------------------
    # MACD
    # -------------------------------------------------------------------------

    def _calc_macd(self, close: np.ndarray) -> MacdSnapshot:
        """MACD linha + linha de sinal + histograma."""
        ema_fast   = self._ema(close, self.MACD_FAST)
        ema_slow   = self._ema(close, self.MACD_SLOW)
        macd_line  = ema_fast - ema_slow
        signal_line = self._ema(macd_line, self.MACD_SIGNAL)
        histogram  = macd_line - signal_line

        ml_now,  ml_prev  = float(macd_line[-1]),   float(macd_line[-2])
        sl_now,  sl_prev  = float(signal_line[-1]),  float(signal_line[-2])
        hl_now,  hl_prev  = float(histogram[-1]),    float(histogram[-2])

        hist_growing = hl_now > hl_prev

        direction = (
            "BULL" if ml_now > sl_now and hl_now > 0
            else "BEAR" if ml_now < sl_now and hl_now < 0
            else "NEUTRAL"
        )

        cross_bull = ml_prev <= sl_prev and ml_now > sl_now
        cross_bear = ml_prev >= sl_prev and ml_now < sl_now

        return MacdSnapshot(
            macd_line=round(ml_now * 10000, 2),
            signal_line=round(sl_now * 10000, 2),
            histogram=round(hl_now * 10000, 2),
            hist_growing=hist_growing,
            direction=direction,
            cross_bull=cross_bull,
            cross_bear=cross_bear,
        )

    # -------------------------------------------------------------------------
    # SCORE DE CONFLUÊNCIA
    # -------------------------------------------------------------------------

    def _calc_confluence(
        self,
        didi:      DidiSnapshot,
        stoch:     StochSnapshot,
        bollinger: BollingerSnapshot,
        macd:      MacdSnapshot,
    ) -> ConfluenceScore:
        """
        Sistema de pontuação 0-10.

        BULL scoring:
          Didi agulhada bull           → +3 (gatilho principal)
          Didi alinhado bull           → +1 (confirmação)
          Didi histograma crescente UP → +1
          Estocástico cross bull       → +1
          Estocástico não overbought   → +0.5
          MACD bull + histograma pos   → +1.5
          MACD cross bull              → +0.5
          Bollinger: preço saiu banda  → +1
          Bollinger: sem squeeze       → +0.5

        BEAR scoring: simétrico.
        Score final = soma dos pontos alinhados com a direcção detectada.
        Máximo teórico: 10.
        """
        bull_pts  = 0.0
        bear_pts  = 0.0
        breakdown = {}
        reasons   = []

        # --- DIDI (peso total: 5) ---
        if didi.agulhada_bull:
            bull_pts += 3.0
            breakdown["didi_agulhada"] = ("BULL", 3.0)
            reasons.append("Agulhada BULL detectada no Didi Index")
        elif didi.agulhada_bear:
            bear_pts += 3.0
            breakdown["didi_agulhada"] = ("BEAR", 3.0)
            reasons.append("Agulhada BEAR detectada no Didi Index")
        else:
            breakdown["didi_agulhada"] = ("NONE", 0)

        if didi.fully_aligned_bull:
            bull_pts += 1.0
            breakdown["didi_aligned"] = ("BULL", 1.0)
            reasons.append("MAs Didi totalmente alinhadas em alta (MA3>MA8>MA20)")
        elif didi.fully_aligned_bear:
            bear_pts += 1.0
            breakdown["didi_aligned"] = ("BEAR", 1.0)
            reasons.append("MAs Didi totalmente alinhadas em baixa (MA3<MA8<MA20)")
        else:
            breakdown["didi_aligned"] = ("NONE", 0)

        if didi.hist_direction == "UP" and didi.hist_growing:
            bull_pts += 1.0
            breakdown["didi_hist"] = ("BULL", 1.0)
            reasons.append("Histograma Didi a crescer — força crescente")
        elif didi.hist_direction == "DOWN" and not didi.hist_growing:
            bear_pts += 1.0
            breakdown["didi_hist"] = ("BEAR", 1.0)
            reasons.append("Histograma Didi a cair — pressão vendedora")
        else:
            breakdown["didi_hist"] = ("NONE", 0)

        # --- ESTOCÁSTICO (peso total: 1.5) ---
        if stoch.cross_bull:
            bull_pts += 1.0
            breakdown["stoch_cross"] = ("BULL", 1.0)
            reasons.append(f"Estocástico cruzou para cima (K={stoch.k}, D={stoch.d})")
        elif stoch.cross_bear:
            bear_pts += 1.0
            breakdown["stoch_cross"] = ("BEAR", 1.0)
            reasons.append(f"Estocástico cruzou para baixo (K={stoch.k}, D={stoch.d})")
        else:
            breakdown["stoch_cross"] = ("NONE", 0)

        if stoch.zone == "NEUTRAL":
            bull_pts += 0.5
            bear_pts += 0.5
            breakdown["stoch_zone"] = ("OK", 0.5)
        elif stoch.zone == "OVERSOLD":
            bull_pts += 0.5
            breakdown["stoch_zone"] = ("BULL_BONUS", 0.5)
            reasons.append("Estocástico em sobrevenda — potencial reversão bull")
        elif stoch.zone == "OVERBOUGHT":
            bear_pts += 0.5
            breakdown["stoch_zone"] = ("BEAR_BONUS", 0.5)
            reasons.append("Estocástico em sobrecompra — potencial reversão bear")

        # --- MACD (peso total: 2) ---
        if macd.direction == "BULL":
            bull_pts += 1.5
            breakdown["macd_dir"] = ("BULL", 1.5)
            reasons.append("MACD em território positivo e crescente")
        elif macd.direction == "BEAR":
            bear_pts += 1.5
            breakdown["macd_dir"] = ("BEAR", 1.5)
            reasons.append("MACD em território negativo e a cair")
        else:
            breakdown["macd_dir"] = ("NONE", 0)

        if macd.cross_bull:
            bull_pts += 0.5
            breakdown["macd_cross"] = ("BULL", 0.5)
            reasons.append("MACD cruzou a linha de sinal para cima")
        elif macd.cross_bear:
            bear_pts += 0.5
            breakdown["macd_cross"] = ("BEAR", 0.5)
            reasons.append("MACD cruzou a linha de sinal para baixo")
        else:
            breakdown["macd_cross"] = ("NONE", 0)

        # --- BOLLINGER (peso total: 1.5) ---
        if bollinger.price_vs_bands in ("NEAR_UPPER", "ABOVE_UPPER"):
            bull_pts += 1.0
            breakdown["bb_pos"] = ("BULL", 1.0)
            reasons.append("Preço a pressionar a banda superior — momentum bull")
        elif bollinger.price_vs_bands in ("NEAR_LOWER", "BELOW_LOWER"):
            bear_pts += 1.0
            breakdown["bb_pos"] = ("BEAR", 1.0)
            reasons.append("Preço a pressionar a banda inferior — momentum bear")
        else:
            breakdown["bb_pos"] = ("NONE", 0)

        if not bollinger.squeeze:
            bull_pts += 0.5
            bear_pts += 0.5
            breakdown["bb_squeeze"] = ("OK", 0.5)
        else:
            breakdown["bb_squeeze"] = ("SQUEEZE", 0)
            reasons.append("Bollinger em squeeze — aguardar breakout")

        # --- DIRECÇÃO E SCORE FINAL ---
        if bull_pts > bear_pts:
            direction = "BUY"
            total_raw = bull_pts
        elif bear_pts > bull_pts:
            direction = "SELL"
            total_raw = bear_pts
        else:
            direction = "NEUTRAL"
            total_raw = 0.0

        total = min(10, round(total_raw))
        tradeable = total >= 7 and direction != "NEUTRAL"

        if not tradeable and total > 0:
            reasons.append(f"Score {total}/10 insuficiente — mínimo 7 necessário")

        return ConfluenceScore(
            total=total,
            direction=direction,
            breakdown=breakdown,
            tradeable=tradeable,
            reasons=reasons,
        )

    # -------------------------------------------------------------------------
    # UTILITÁRIOS
    # -------------------------------------------------------------------------

    def _ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """EMA com suavização padrão."""
        alpha = 2.0 / (period + 1)
        result = np.zeros_like(data, dtype=float)
        result[0] = data[0]
        for i in range(1, len(data)):
            result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
        return result


# -----------------------------------------------------------------------------
# INSTÂNCIA GLOBAL
# -----------------------------------------------------------------------------
didi_calculator = DidiStrategyCalculator()
