# =============================================================================
# ALPHA-QUANT · correlation_filter.py
# Filtro de correlação entre instrumentos
#
# Problema: EUR/USD e GBP/USD têm correlação ~0.85.
# Entrar nos dois ao mesmo tempo = duplicar o risco numa única ideia.
#
# Solução:
#   · Matriz de correlação estática (conhecimento de mercado)
#   · Regra: se dois instrumentos correlacionados têm sinal simultâneo,
#     só executa o de score mais alto
#   · Verifica correlação com operações já abertas
# =============================================================================

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ─── MATRIZ DE CORRELAÇÃO ─────────────────────────────────────────────────────
# Valores: 1.0 = correlação perfeita, -1.0 = correlação inversa perfeita
# Só incluímos correlações significativas (|r| > 0.6)

CORRELATION_MATRIX: dict[frozenset, float] = {
    # Forex — pares fortemente correlacionados (positivos)
    frozenset({"EURUSD", "GBPUSD"}):  0.85,
    frozenset({"EURUSD", "AUDUSD"}):  0.78,
    frozenset({"EURUSD", "NZDUSD"}):  0.72,
    frozenset({"GBPUSD", "AUDUSD"}):  0.74,
    frozenset({"EURUSD", "EURJPY"}):  0.68,
    frozenset({"GBPUSD", "GBPJPY"}):  0.70,

    # Forex — correlações inversas (negativos)
    frozenset({"EURUSD", "USDCHF"}): -0.90,  # muito forte — quase espelho
    frozenset({"EURUSD", "USDJPY"}): -0.65,
    frozenset({"GBPUSD", "USDCHF"}): -0.82,
    frozenset({"AUDUSD", "USDCAD"}): -0.68,

    # Metais vs USD
    frozenset({"XAUUSD", "EURUSD"}):  0.65,  # ouro sobe quando USD fraco
    frozenset({"XAUUSD", "USDJPY"}): -0.60,

    # Índices correlacionados entre si
    frozenset({"US30",  "NAS100"}):   0.88,
    frozenset({"US30",  "SPX500"}):   0.95,
    frozenset({"NAS100","SPX500"}):   0.92,

    # Cripto com índices (correlação moderada em risk-on)
    frozenset({"BTCUSD", "NAS100"}):  0.62,
    frozenset({"ETHUSD", "BTCUSD"}):  0.88,
}

# Threshold: correlação acima deste valor = instrumentos considerados "iguais"
CORRELATION_THRESHOLD = 0.70


@dataclass
class CorrelationCheck:
    """Resultado de uma verificação de correlação."""
    allowed:     bool
    reason:      str
    blocking_symbol: str = ""   # símbolo que bloqueou a entrada
    correlation: float = 0.0
    is_inverse:  bool  = False  # True = correlação inversa (mesma ideia, dir oposta)


class CorrelationFilter:
    """
    Filtra sinais para evitar exposição duplicada em instrumentos correlacionados.
    """

    def check_new_signal(
        self,
        new_symbol:   str,
        new_direction: str,          # "BUY" | "SELL"
        new_score:    int,
        open_trades:  dict,          # {ticket: OpenTrade} do RiskManager
        pending_signal = None,       # sinal pendente de decisão humana
    ) -> CorrelationCheck:
        """
        Verifica se o novo sinal pode ser executado dado o estado actual.

        Regras:
        1. Se há trade aberto num instrumento correlacionado na mesma direcção
           → bloqueia (risco duplicado)
        2. Se há trade aberto num instrumento correlacionado inversamente e
           na direcção oposta → bloqueia (mesma ideia de mercado)
        3. Se há sinal pendente num instrumento correlacionado
           → só deixa passar o de score mais alto
        """
        # Verifica contra trades abertos
        for ticket, trade in open_trades.items():
            check = self._check_pair(
                new_symbol, new_direction, new_score,
                trade.symbol, trade.direction, 999,  # trades abertos têm prioridade
                context="trade aberto",
            )
            if not check.allowed:
                return check

        # Verifica contra sinal pendente
        if pending_signal:
            check = self._check_pair(
                new_symbol, new_direction, new_score,
                pending_signal.symbol, pending_signal.direction,
                pending_signal.confidence,
                context="sinal pendente",
            )
            if not check.allowed:
                return check

        return CorrelationCheck(allowed=True, reason="Sem conflitos de correlação")

    def _check_pair(
        self,
        sym_a: str, dir_a: str, score_a: int,
        sym_b: str, dir_b: str, score_b: int,
        context: str,
    ) -> CorrelationCheck:
        """Verifica correlação entre dois instrumentos específicos."""
        if sym_a.upper() == sym_b.upper():
            return CorrelationCheck(
                allowed=False,
                reason=f"Instrumento idêntico já em {context}",
                blocking_symbol=sym_b,
                correlation=1.0,
            )

        corr = self.get_correlation(sym_a, sym_b)

        if abs(corr) < CORRELATION_THRESHOLD:
            return CorrelationCheck(allowed=True, reason="Correlação baixa — seguro")

        # Determina se as posições vão na mesma "direcção de mercado"
        same_market_idea = (
            (corr > 0 and dir_a == dir_b) or      # correlação + mesma dir
            (corr < 0 and dir_a != dir_b)          # correlação - dir oposta
        )

        if not same_market_idea:
            return CorrelationCheck(allowed=True, reason="Posições opostas — hedge aceitável")

        # Conflito: mesma ideia de mercado
        if score_a > score_b:
            # O novo sinal é melhor — mas já há algo aberto/pendente
            reason = (
                f"{sym_a} correlacionado com {sym_b} ({context}) "
                f"r={corr:.2f} — mesmo risco. "
                f"Novo score ({score_a}) > actual ({score_b}) mas {context} tem prioridade."
            )
            return CorrelationCheck(
                allowed=False,
                reason=reason,
                blocking_symbol=sym_b,
                correlation=corr,
                is_inverse=(corr < 0),
            )
        else:
            reason = (
                f"{sym_a} correlacionado com {sym_b} ({context}) "
                f"r={corr:.2f} — mesmo risco. "
                f"Score {score_b} ≥ {score_a} — {sym_b} tem prioridade."
            )
            return CorrelationCheck(
                allowed=False,
                reason=reason,
                blocking_symbol=sym_b,
                correlation=corr,
                is_inverse=(corr < 0),
            )

    @staticmethod
    def get_correlation(sym_a: str, sym_b: str) -> float:
        """Retorna a correlação entre dois símbolos."""
        key = frozenset({sym_a.upper(), sym_b.upper()})
        return CORRELATION_MATRIX.get(key, 0.0)

    @staticmethod
    def get_correlated_symbols(symbol: str, min_correlation: float = 0.6) -> list[tuple]:
        """
        Retorna lista de símbolos correlacionados com este.
        Formato: [(symbol, correlation), ...]
        """
        sym = symbol.upper()
        result = []
        for pair, corr in CORRELATION_MATRIX.items():
            pair_list = list(pair)
            if sym in pair_list and abs(corr) >= min_correlation:
                other = [s for s in pair_list if s != sym][0]
                result.append((other, corr))
        return sorted(result, key=lambda x: -abs(x[1]))

    @staticmethod
    def suggest_hedge(symbol: str, direction: str) -> list[dict]:
        """
        Sugere instrumentos para hedge (correlação inversa).
        Útil para gestão de risco avançada.
        """
        sym = symbol.upper()
        hedges = []

        for pair, corr in CORRELATION_MATRIX.items():
            pair_list = list(pair)
            if sym not in pair_list:
                continue
            if corr >= -0.7:  # só correlações inversas fortes
                continue

            other = [s for s in pair_list if s != sym][0]
            hedge_direction = "SELL" if direction == "BUY" else "BUY"

            hedges.append({
                "symbol":     other,
                "direction":  hedge_direction,
                "correlation": corr,
                "reason": (
                    f"{other} tem correlação {corr:.2f} com {sym}. "
                    f"Uma posição {hedge_direction} em {other} reduz o risco."
                ),
            })

        return sorted(hedges, key=lambda x: x["correlation"])


# ─── INSTÂNCIA GLOBAL ─────────────────────────────────────────────────────────
corr_filter = CorrelationFilter()
