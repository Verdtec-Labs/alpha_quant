# =============================================================================
# ALPHA-QUANT · instruments.py
# Catálogo universal de instrumentos — Forex, Cripto, Índices, Metais, Energia
#
# Resolve os 3 problemas de multi-mercado:
#   1. Tamanho do pip (0.0001 para EUR/USD ≠ 0.01 para JPY ≠ 1.0 para BTC)
#   2. Valor do pip em conta EUR (muda por par e conta)
#   3. Parâmetros de risco adaptados (SL máximo, ATR esperado, sessões)
# =============================================================================

from dataclasses import dataclass, field
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class Instrument:
    symbol:        str
    description:   str
    category:      str       # "forex" | "crypto" | "index" | "metal" | "energy"
    pip_size:      float     # tamanho de 1 pip na cotação
    pip_value_eur: float     # valor de 1 pip em EUR por lote padrão (1.0 lot)
    min_lot:       float     # lote mínimo
    max_lot:       float     # lote máximo seguro (para conta €500)
    lot_step:      float     # incremento de lote
    avg_spread:    float     # spread médio em pips (referência)
    max_spread:    float     # spread máximo aceitável em pips
    max_sl_pips:   int       # SL máximo específico para este instrumento
    avg_atr_pips:  float     # ATR médio esperado em M15 (para calibrar SL)
    sessions:      list[str] = field(default_factory=list)  # sessões activas
    digits:        int = 5   # casas decimais na cotação

    @property
    def pip_value_micro(self) -> float:
        """Valor de 1 pip em EUR com lote micro (0.01)."""
        return self.pip_value_eur * 0.01

    def pips_to_price(self, pips: float) -> float:
        """Converte pips para unidades de preço."""
        return pips * self.pip_size

    def price_to_pips(self, price_diff: float) -> float:
        """Converte diferença de preço para pips."""
        if self.pip_size == 0:
            return 0.0
        return abs(price_diff) / self.pip_size

    def calc_lot(self, balance_eur: float, risk_pct: float, sl_pips: float) -> float:
        """
        Calcula lote correcto para este instrumento.
        Fórmula: lot = (balance * risk%) / (sl_pips * pip_value_per_lot)
        """
        if sl_pips <= 0 or self.pip_value_eur <= 0:
            return self.min_lot

        risk_eur = balance_eur * (risk_pct / 100)
        lot = risk_eur / (sl_pips * self.pip_value_eur)
        lot = round(lot - (lot % self.lot_step), 2)
        return max(self.min_lot, min(self.max_lot, lot))

    def validate_sl(self, sl_pips: float) -> tuple[bool, str]:
        """Valida se o SL está dentro dos parâmetros do instrumento."""
        if sl_pips <= 0:
            return False, f"SL negativo: {sl_pips:.1f}"
        if sl_pips > self.max_sl_pips:
            return False, f"SL {sl_pips:.1f}p > máximo {self.max_sl_pips}p para {self.symbol}"
        return True, ""

    def validate_spread(self, current_spread: float) -> bool:
        return current_spread <= self.max_spread


# =============================================================================
# CATÁLOGO DE INSTRUMENTOS
# =============================================================================

INSTRUMENTS: dict[str, Instrument] = {

    # ── FOREX MAJORS ──────────────────────────────────────────────────────────
    "EURUSD": Instrument(
        symbol="EURUSD", description="Euro / US Dollar",
        category="forex", pip_size=0.0001, pip_value_eur=10.0,
        min_lot=0.01, max_lot=0.05, lot_step=0.01,
        avg_spread=0.8, max_spread=1.8, max_sl_pips=25,
        avg_atr_pips=8.0, digits=5,
        sessions=["london", "new_york", "london_ny_overlap"],
    ),
    "GBPUSD": Instrument(
        symbol="GBPUSD", description="British Pound / US Dollar",
        category="forex", pip_size=0.0001, pip_value_eur=10.0,
        min_lot=0.01, max_lot=0.04, lot_step=0.01,
        avg_spread=1.0, max_spread=2.5, max_sl_pips=30,
        avg_atr_pips=12.0, digits=5,
        sessions=["london", "new_york"],
    ),
    "USDJPY": Instrument(
        symbol="USDJPY", description="US Dollar / Japanese Yen",
        category="forex", pip_size=0.01, pip_value_eur=0.065,  # ~€0.065/pip com lote micro
        min_lot=0.01, max_lot=0.05, lot_step=0.01,
        avg_spread=0.7, max_spread=2.0, max_sl_pips=30,
        avg_atr_pips=8.0, digits=3,
        sessions=["tokyo", "london", "new_york"],
    ),
    "EURJPY": Instrument(
        symbol="EURJPY", description="Euro / Japanese Yen",
        category="forex", pip_size=0.01, pip_value_eur=0.070,
        min_lot=0.01, max_lot=0.04, lot_step=0.01,
        avg_spread=1.2, max_spread=3.0, max_sl_pips=35,
        avg_atr_pips=10.0, digits=3,
        sessions=["tokyo", "london"],
    ),
    "AUDUSD": Instrument(
        symbol="AUDUSD", description="Australian Dollar / US Dollar",
        category="forex", pip_size=0.0001, pip_value_eur=9.5,
        min_lot=0.01, max_lot=0.05, lot_step=0.01,
        avg_spread=0.9, max_spread=2.2, max_sl_pips=25,
        avg_atr_pips=7.0, digits=5,
        sessions=["sydney", "tokyo", "london"],
    ),
    "USDCAD": Instrument(
        symbol="USDCAD", description="US Dollar / Canadian Dollar",
        category="forex", pip_size=0.0001, pip_value_eur=9.2,
        min_lot=0.01, max_lot=0.05, lot_step=0.01,
        avg_spread=1.0, max_spread=2.5, max_sl_pips=25,
        avg_atr_pips=7.5, digits=5,
        sessions=["new_york"],
    ),
    "USDCHF": Instrument(
        symbol="USDCHF", description="US Dollar / Swiss Franc",
        category="forex", pip_size=0.0001, pip_value_eur=10.5,
        min_lot=0.01, max_lot=0.05, lot_step=0.01,
        avg_spread=1.0, max_spread=2.5, max_sl_pips=25,
        avg_atr_pips=7.0, digits=5,
        sessions=["london", "new_york"],
    ),
    "EURGBP": Instrument(
        symbol="EURGBP", description="Euro / British Pound",
        category="forex", pip_size=0.0001, pip_value_eur=11.5,
        min_lot=0.01, max_lot=0.04, lot_step=0.01,
        avg_spread=1.0, max_spread=2.5, max_sl_pips=20,
        avg_atr_pips=5.0, digits=5,
        sessions=["london"],
    ),

    # ── CRIPTO ────────────────────────────────────────────────────────────────
    "BTCUSD": Instrument(
        symbol="BTCUSD", description="Bitcoin / US Dollar",
        category="crypto", pip_size=1.0, pip_value_eur=0.0011,
        min_lot=0.001, max_lot=0.01, lot_step=0.001,
        avg_spread=20.0, max_spread=80.0, max_sl_pips=300,
        avg_atr_pips=200.0, digits=2,
        sessions=["always"],
    ),
    "ETHUSD": Instrument(
        symbol="ETHUSD", description="Ethereum / US Dollar",
        category="crypto", pip_size=0.1, pip_value_eur=0.009,
        min_lot=0.01, max_lot=0.1, lot_step=0.01,
        avg_spread=2.0, max_spread=10.0, max_sl_pips=500,
        avg_atr_pips=30.0, digits=3,
        sessions=["always"],
    ),
    "XRPUSD": Instrument(
        symbol="XRPUSD", description="Ripple / US Dollar",
        category="crypto", pip_size=0.0001, pip_value_eur=0.9,
        min_lot=0.01, max_lot=0.1, lot_step=0.01,
        avg_spread=0.5, max_spread=3.0, max_sl_pips=200,
        avg_atr_pips=30.0, digits=5,
        sessions=["always"],
    ),

    # ── METAIS ────────────────────────────────────────────────────────────────
    "XAUUSD": Instrument(
        symbol="XAUUSD", description="Gold / US Dollar",
        category="metal", pip_size=0.1, pip_value_eur=0.009,
        min_lot=0.01, max_lot=0.02, lot_step=0.01,
        avg_spread=2.0, max_spread=5.0, max_sl_pips=200,
        avg_atr_pips=80.0, digits=3,
        sessions=["london", "new_york"],
    ),
    "XAGUSD": Instrument(
        symbol="XAGUSD", description="Silver / US Dollar",
        category="metal", pip_size=0.001, pip_value_eur=0.9,
        min_lot=0.01, max_lot=0.05, lot_step=0.01,
        avg_spread=3.0, max_spread=8.0, max_sl_pips=300,
        avg_atr_pips=100.0, digits=4,
        sessions=["london", "new_york"],
    ),

    # ── ÍNDICES ───────────────────────────────────────────────────────────────
    "US30": Instrument(
        symbol="US30", description="Dow Jones Industrial Average",
        category="index", pip_size=1.0, pip_value_eur=0.009,
        min_lot=0.01, max_lot=0.02, lot_step=0.01,
        avg_spread=3.0, max_spread=10.0, max_sl_pips=150,
        avg_atr_pips=80.0, digits=2,
        sessions=["new_york"],
    ),
    "NAS100": Instrument(
        symbol="NAS100", description="Nasdaq 100",
        category="index", pip_size=1.0, pip_value_eur=0.009,
        min_lot=0.01, max_lot=0.02, lot_step=0.01,
        avg_spread=2.0, max_spread=8.0, max_sl_pips=200,
        avg_atr_pips=100.0, digits=2,
        sessions=["new_york"],
    ),
    "SPX500": Instrument(
        symbol="SPX500", description="S&P 500",
        category="index", pip_size=0.1, pip_value_eur=0.09,
        min_lot=0.01, max_lot=0.02, lot_step=0.01,
        avg_spread=1.0, max_spread=5.0, max_sl_pips=150,
        avg_atr_pips=30.0, digits=3,
        sessions=["new_york"],
    ),
    "GER40": Instrument(
        symbol="GER40", description="DAX 40 (Germany)",
        category="index", pip_size=1.0, pip_value_eur=0.01,
        min_lot=0.01, max_lot=0.02, lot_step=0.01,
        avg_spread=1.0, max_spread=4.0, max_sl_pips=150,
        avg_atr_pips=50.0, digits=2,
        sessions=["london"],
    ),

    # ── ENERGIA ───────────────────────────────────────────────────────────────
    "USOIL": Instrument(
        symbol="USOIL", description="WTI Crude Oil",
        category="energy", pip_size=0.01, pip_value_eur=0.09,
        min_lot=0.01, max_lot=0.05, lot_step=0.01,
        avg_spread=3.0, max_spread=8.0, max_sl_pips=200,
        avg_atr_pips=50.0, digits=3,
        sessions=["new_york"],
    ),
}

# Aliases comuns que brokers usam
SYMBOL_ALIASES: dict[str, str] = {
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
    "XAU": "XAUUSD",
    "DJ30": "US30",
    "DJIA": "US30",
    "NDX": "NAS100",
    "DAX": "GER40",
    "WTI": "USOIL",
}


def get_instrument(symbol: str) -> Optional[Instrument]:
    """
    Retorna o instrumento pelo símbolo, com suporte a aliases.
    Se não encontrar no catálogo, tenta inferir as propriedades.
    """
    # Normaliza o símbolo
    sym = symbol.upper().replace("-", "").replace("/", "").replace(".", "")

    # Procura directa
    if sym in INSTRUMENTS:
        return INSTRUMENTS[sym]

    # Procura por alias
    canonical = SYMBOL_ALIASES.get(sym)
    if canonical and canonical in INSTRUMENTS:
        return INSTRUMENTS[canonical]

    # Procura parcial (ex: "EURUSDm" → "EURUSD")
    for key in INSTRUMENTS:
        if sym.startswith(key) or key in sym:
            logger.info(f"Símbolo '{symbol}' mapeado para '{key}' por correspondência parcial")
            return INSTRUMENTS[key]

    # Infere um instrumento genérico
    logger.warning(f"Símbolo '{symbol}' não está no catálogo — usando parâmetros conservadores")
    return _infer_instrument(symbol)


def _infer_instrument(symbol: str) -> Instrument:
    """
    Infere parâmetros para um símbolo desconhecido.
    Usa heurísticas baseadas no nome do símbolo.
    """
    sym = symbol.upper()

    # JPY pairs: pip = 0.01
    if "JPY" in sym:
        return Instrument(
            symbol=symbol, description=f"{symbol} (inferido)",
            category="forex", pip_size=0.01, pip_value_eur=0.065,
            min_lot=0.01, max_lot=0.03, lot_step=0.01,
            avg_spread=1.5, max_spread=4.0, max_sl_pips=30,
            avg_atr_pips=10.0, digits=3, sessions=["london", "new_york"],
        )

    # Cripto (BTC, ETH, etc.)
    if any(c in sym for c in ["BTC", "ETH", "SOL", "ADA", "XRP", "BNB"]):
        return Instrument(
            symbol=symbol, description=f"{symbol} (inferido)",
            category="crypto", pip_size=0.01, pip_value_eur=0.009,
            min_lot=0.01, max_lot=0.05, lot_step=0.01,
            avg_spread=5.0, max_spread=20.0, max_sl_pips=500,
            avg_atr_pips=50.0, digits=4, sessions=["always"],
        )

    # Metais (XAU, XAG, XPT)
    if any(c in sym for c in ["XAU", "XAG", "XPT", "GOLD", "SILVER"]):
        return Instrument(
            symbol=symbol, description=f"{symbol} (inferido)",
            category="metal", pip_size=0.1, pip_value_eur=0.009,
            min_lot=0.01, max_lot=0.02, lot_step=0.01,
            avg_spread=3.0, max_spread=8.0, max_sl_pips=200,
            avg_atr_pips=80.0, digits=3, sessions=["london", "new_york"],
        )

    # Índices (US, GER, UK, etc.)
    if any(c in sym for c in ["US", "GER", "UK", "NAS", "SPX", "DOW", "DAX"]):
        return Instrument(
            symbol=symbol, description=f"{symbol} (inferido)",
            category="index", pip_size=1.0, pip_value_eur=0.009,
            min_lot=0.01, max_lot=0.02, lot_step=0.01,
            avg_spread=3.0, max_spread=10.0, max_sl_pips=200,
            avg_atr_pips=80.0, digits=2, sessions=["london", "new_york"],
        )

    # Default: trata como Forex major genérico
    return Instrument(
        symbol=symbol, description=f"{symbol} (genérico)",
        category="forex", pip_size=0.0001, pip_value_eur=10.0,
        min_lot=0.01, max_lot=0.03, lot_step=0.01,
        avg_spread=1.5, max_spread=3.0, max_sl_pips=25,
        avg_atr_pips=8.0, digits=5, sessions=["london", "new_york"],
    )


def list_by_category(category: str) -> list[Instrument]:
    """Lista instrumentos por categoria."""
    return [i for i in INSTRUMENTS.values() if i.category == category]


def get_session_instruments(session: str) -> list[Instrument]:
    """Retorna instrumentos activos numa determinada sessão."""
    return [i for i in INSTRUMENTS.values()
            if session in i.sessions or "always" in i.sessions]
