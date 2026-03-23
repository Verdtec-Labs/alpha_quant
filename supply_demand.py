# =============================================================================
# ALPHA-QUANT · supply_demand.py
# Detecção automática de zonas institucionais de Supply & Demand
# Lógica: identifica swing highs/lows em H4/D1 com rally-base-drop
# =============================================================================

import logging
from instruments import get_instrument
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SDZone:
    """Uma zona de Supply ou Demand identificada."""
    zone_type:  str    # "DEMAND" | "SUPPLY"
    price_top:  float  # topo da zona
    price_bot:  float  # fundo da zona
    midpoint:   float  # meio da zona
    strength:   int    # 1-5 (baseado em nº de toques e impulsividade)
    timeframe:  str    # onde foi identificada
    fresh:      bool   # True = nunca foi retestada
    touches:    int    # quantas vezes o preço voltou à zona
    origin_idx: int    # índice da vela de origem

    @property
    def height_pips(self) -> float:
        return round((self.price_top - self.price_bot) * 10000, 1)


@dataclass
class SDContext:
    """Contexto completo de Supply & Demand para um par."""
    symbol:         str
    current_price:  float
    demand_zones:   list[SDZone] = field(default_factory=list)
    supply_zones:   list[SDZone] = field(default_factory=list)

    # Zona mais próxima acima e abaixo do preço actual
    nearest_supply: Optional[SDZone] = None
    nearest_demand: Optional[SDZone] = None

    # Está o preço DENTRO de uma zona agora?
    in_demand_zone: bool = False
    in_supply_zone: bool = False
    active_zone:    Optional[SDZone] = None

    # Distância à zona mais próxima (pips)
    pips_to_supply: float = 999.0
    pips_to_demand: float = 999.0

    # Contexto para o score de confluência
    confluence_bonus: int  = 0   # +2 se em zona, +1 se próximo (< 10 pips)
    confluence_note:  str  = ""


class SDDetector:
    """
    Detecta zonas de Supply & Demand usando a lógica Rally-Base-Drop / Drop-Base-Rally.

    Rally-Base-Rally → DEMAND (acumulação antes de subida)
    Drop-Base-Drop   → SUPPLY (distribuição antes de queda)
    Drop-Base-Rally  → DEMAND forte (reversão de queda para subida)
    Rally-Base-Drop  → SUPPLY forte (reversão de subida para queda)
    """

    # Parâmetros
    SWING_LOOKBACK    = 8    # velas para cada lado para confirmar swing
    MIN_IMPULSE_PIPS  = 10   # movimento mínimo para considerar impulso
    MAX_ZONE_PIPS     = 30   # zona máxima em altura (pips)
    MAX_ZONES         = 5    # máximo de zonas a manter por tipo
    PROXIMITY_PIPS    = 15   # distância para considerar "próximo" de zona

    def compute(
        self,
        symbol:        str,
        current_price: float,
        df_h4:         pd.DataFrame,
        df_h1:         Optional[pd.DataFrame] = None,
    ) -> SDContext:
        """
        Detecta zonas nos dois timeframes e avalia contexto actual.
        """
        from instruments import get_instrument as _gi
        self._current_pip = _gi(symbol).pip_size
        ctx = SDContext(symbol=symbol, current_price=current_price)

        # Detecta em H4 (zonas de maior relevância)
        h4_demand, h4_supply = self._detect_zones(df_h4, "H4")
        ctx.demand_zones.extend(h4_demand)
        ctx.supply_zones.extend(h4_supply)

        # Detecta em H1 (zonas de menor relevância mas mais precisas)
        if df_h1 is not None and len(df_h1) >= 50:
            h1_demand, h1_supply = self._detect_zones(df_h1, "H1")
            ctx.demand_zones.extend(h1_demand)
            ctx.supply_zones.extend(h1_supply)

        # Ordena por força e proximidade
        ctx.demand_zones.sort(key=lambda z: (-z.strength, abs(z.midpoint - current_price)))
        ctx.supply_zones.sort(key=lambda z: (-z.strength, abs(z.midpoint - current_price)))

        # Encontra zonas mais próximas acima e abaixo
        self._find_nearest(ctx)

        # Calcula bónus de confluência
        self._calc_confluence(ctx)

        logger.debug(
            f"S&D {symbol}: {len(ctx.demand_zones)} demand · "
            f"{len(ctx.supply_zones)} supply · "
            f"Em zona: {ctx.in_demand_zone or ctx.in_supply_zone}"
        )
        return ctx

    # -------------------------------------------------------------------------
    # DETECÇÃO DE ZONAS
    # -------------------------------------------------------------------------

    def _detect_zones(
        self,
        df:        pd.DataFrame,
        timeframe: str,
    ) -> tuple[list[SDZone], list[SDZone]]:
        """Detecta zonas Supply e Demand num DataFrame de candles."""
        demand_zones = []
        supply_zones = []

        if len(df) < self.SWING_LOOKBACK * 2 + 5:
            return demand_zones, supply_zones

        closes = df["close"].values.astype(float)
        highs  = df["high"].values.astype(float)
        lows   = df["low"].values.astype(float)
        opens  = df["open"].values.astype(float)
        n      = len(closes)
        pip = getattr(self, "_current_pip", 0.0001)  # set by compute()

        # Identifica candles base (pequeno range) e impulso (grande range)
        ranges = highs - lows
        avg_range = float(np.mean(ranges))

        for i in range(self.SWING_LOOKBACK, n - self.SWING_LOOKBACK - 3):
            # Candle base: range < 60% da média
            is_base = ranges[i] < avg_range * 0.6

            if not is_base:
                continue

            # Verifica o que veio ANTES do base (movimento de entrada)
            pre_move = self._calc_move(closes, i - self.SWING_LOOKBACK, i)
            # Verifica o que vem DEPOIS do base (movimento de saída/impulso)
            post_move = self._calc_move(closes, i, i + self.SWING_LOOKBACK)

            impulse_pips = abs(post_move) / pip

            if impulse_pips < self.MIN_IMPULSE_PIPS:
                continue

            # Drop-Base-Rally → DEMAND
            if pre_move < 0 and post_move > 0:
                zone_bot = min(lows[i], opens[i], closes[i])
                zone_top = max(highs[i], opens[i], closes[i])
                height   = (zone_top - zone_bot) / pip

                if 2 <= height <= self.MAX_ZONE_PIPS:
                    strength = self._calc_strength(impulse_pips, height, timeframe)
                    touches  = self._count_touches(lows, highs, zone_bot, zone_top, i)
                    zone = SDZone(
                        zone_type="DEMAND",
                        price_top=round(zone_top, 5),
                        price_bot=round(zone_bot, 5),
                        midpoint=round((zone_top + zone_bot) / 2, 5),
                        strength=strength,
                        timeframe=timeframe,
                        fresh=touches == 0,
                        touches=touches,
                        origin_idx=i,
                    )
                    demand_zones.append(zone)

            # Rally-Base-Drop → SUPPLY
            elif pre_move > 0 and post_move < 0:
                zone_bot = min(lows[i], opens[i], closes[i])
                zone_top = max(highs[i], opens[i], closes[i])
                height   = (zone_top - zone_bot) / pip

                if 2 <= height <= self.MAX_ZONE_PIPS:
                    strength = self._calc_strength(impulse_pips, height, timeframe)
                    touches  = self._count_touches(lows, highs, zone_bot, zone_top, i)
                    zone = SDZone(
                        zone_type="SUPPLY",
                        price_top=round(zone_top, 5),
                        price_bot=round(zone_bot, 5),
                        midpoint=round((zone_top + zone_bot) / 2, 5),
                        strength=strength,
                        timeframe=timeframe,
                        fresh=touches == 0,
                        touches=touches,
                        origin_idx=i,
                    )
                    supply_zones.append(zone)

        # Mantém apenas as N mais fortes e relevantes
        demand_zones = sorted(demand_zones, key=lambda z: -z.strength)[:self.MAX_ZONES]
        supply_zones = sorted(supply_zones, key=lambda z: -z.strength)[:self.MAX_ZONES]

        return demand_zones, supply_zones

    # -------------------------------------------------------------------------
    # UTILITÁRIOS
    # -------------------------------------------------------------------------

    def _calc_move(self, closes: np.ndarray, start: int, end: int) -> float:
        """Calcula o movimento líquido entre dois pontos."""
        if start < 0 or end >= len(closes) or start >= end:
            return 0.0
        return float(closes[end] - closes[start])

    def _calc_strength(self, impulse_pips: float, zone_height_pips: float, tf: str) -> int:
        """
        Força da zona 1-5:
        - Impulso grande = mais forte
        - Zona pequena = mais precisa = mais forte
        - H4 > H1
        """
        score = 1

        if impulse_pips > 50: score += 2
        elif impulse_pips > 25: score += 1

        if zone_height_pips < 8:  score += 1

        if tf == "H4": score += 1

        return min(5, score)

    def _count_touches(
        self,
        lows:     np.ndarray,
        highs:    np.ndarray,
        zone_bot: float,
        zone_top: float,
        origin:   int,
    ) -> int:
        """Conta quantas vezes o preço entrou na zona após a sua criação."""
        touches = 0
        for i in range(origin + 1, len(lows)):
            if lows[i] <= zone_top and highs[i] >= zone_bot:
                touches += 1
        return touches

    def _find_nearest(self, ctx: SDContext):
        """Encontra zonas mais próximas acima e abaixo do preço actual."""
        pip = getattr(self, "_current_pip", 0.0001)  # set by compute()
        price = ctx.current_price

        # Demand mais próxima abaixo do preço
        below = [z for z in ctx.demand_zones if z.midpoint <= price]
        if below:
            ctx.nearest_demand = min(below, key=lambda z: price - z.midpoint)
            ctx.pips_to_demand = round((price - ctx.nearest_demand.price_top) / pip, 1)
            ctx.pips_to_demand = max(0, ctx.pips_to_demand)

        # Supply mais próxima acima do preço
        above = [z for z in ctx.supply_zones if z.midpoint >= price]
        if above:
            ctx.nearest_supply = min(above, key=lambda z: z.midpoint - price)
            ctx.pips_to_supply = round((ctx.nearest_supply.price_bot - price) / pip, 1)
            ctx.pips_to_supply = max(0, ctx.pips_to_supply)

        # Verifica se o preço está DENTRO de uma zona
        for z in ctx.demand_zones:
            if z.price_bot <= price <= z.price_top:
                ctx.in_demand_zone = True
                ctx.active_zone    = z
                ctx.pips_to_demand = 0
                break

        for z in ctx.supply_zones:
            if z.price_bot <= price <= z.price_top:
                ctx.in_supply_zone = True
                ctx.active_zone    = z
                ctx.pips_to_supply = 0
                break

    def _calc_confluence(self, ctx: SDContext):
        """Calcula bónus de confluência para o score do sistema."""
        pip = getattr(self, "_current_pip", 0.0001)  # set by compute()

        if ctx.in_demand_zone and ctx.active_zone:
            z = ctx.active_zone
            ctx.confluence_bonus = 3 if z.fresh else 2
            ctx.confluence_note  = (
                f"Preço DENTRO de zona DEMAND {z.timeframe} "
                f"({z.price_bot:.5f}–{z.price_top:.5f}) "
                f"força={z.strength}/5 {'FRESH' if z.fresh else f'toques={z.touches}'}"
            )

        elif ctx.in_supply_zone and ctx.active_zone:
            z = ctx.active_zone
            ctx.confluence_bonus = 3 if z.fresh else 2
            ctx.confluence_note  = (
                f"Preço DENTRO de zona SUPPLY {z.timeframe} "
                f"({z.price_bot:.5f}–{z.price_top:.5f}) "
                f"força={z.strength}/5 {'FRESH' if z.fresh else f'toques={z.touches}'}"
            )

        elif ctx.nearest_demand and ctx.pips_to_demand <= self.PROXIMITY_PIPS:
            z = ctx.nearest_demand
            ctx.confluence_bonus = 1
            ctx.confluence_note  = (
                f"A {ctx.pips_to_demand:.1f}p de zona DEMAND {z.timeframe} "
                f"força={z.strength}/5"
            )

        elif ctx.nearest_supply and ctx.pips_to_supply <= self.PROXIMITY_PIPS:
            z = ctx.nearest_supply
            ctx.confluence_bonus = 1
            ctx.confluence_note  = (
                f"A {ctx.pips_to_supply:.1f}p de zona SUPPLY {z.timeframe} "
                f"força={z.strength}/5"
            )

        else:
            ctx.confluence_bonus = 0
            ctx.confluence_note  = "Fora de zona institucional identificada"


# Instância global
sd_detector = SDDetector()
