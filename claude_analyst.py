# =============================================================================
# ALPHA-QUANT · claude_analyst.py
# Interface com a API do Claude:
#   · Formata o contexto de mercado em JSON compacto
#   · Envia ao Claude com system prompt especializado
#   · Valida a resposta com travas anti-alucinação
#   · Retorna TradeSignal estruturado ou None
# =============================================================================

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from config import (
    ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_MAX_TOKENS,
    RISK, SYSTEM,
)
from didi_indicators import StrategySnapshot, ConfluenceScore

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# ESTRUTURAS DE DADOS
# -----------------------------------------------------------------------------

@dataclass
class TradeSignal:
    """
    Sinal de trading validado pelo sistema.
    Só existe se passar TODAS as travas de segurança.
    """
    symbol:    str
    direction: str      # "BUY" | "SELL"
    entry:     float
    sl:        float
    tp:        float

    # Calculados pelo Python (não pela IA)
    sl_pips:   float
    tp_pips:   float
    rr_ratio:  float
    lot_size:  float
    risk_eur:  float

    # Da análise do Claude
    confidence:    int    # 0-10 (score de confluência)
    reasoning:     str    # Justificativa em texto (para briefing)
    claude_raw:    str    # JSON original do Claude (para log)

    # Metadados
    generated_at:  str
    candle_pattern: str


@dataclass
class GuardianAlert:
    """
    Alerta do Guardião para operação aberta.
    """
    symbol:      str
    direction:   str
    alert_type:  str   # "REVERSAL_WARNING" | "TP_APPROACHING" | "TRAILING_UPDATE"
    message:     str
    action:      str   # "CLOSE" | "HOLD" | "MOVE_TRAILING"
    urgency:     str   # "HIGH" | "MEDIUM" | "LOW"
    reasoning:   str
    generated_at: str


# -----------------------------------------------------------------------------
# SYSTEM PROMPT DO AGENTE
# -----------------------------------------------------------------------------

SCOUT_SYSTEM_PROMPT = """És o analista de trading do sistema Alpha-Quant. A tua especialidade é a estratégia Didi Index criada por Didi Aguiar, combinada com Bollinger Bands e MACD.

MISSÃO: Analisar o contexto de mercado fornecido e decidir se existe um setup de alta probabilidade para entrar numa operação. Receberás dados de qualquer instrumento — Forex, Cripto, Ouro, Índices — e deves adaptar os teus parâmetros ao instrumento específico (o JSON inclui pip_size e pip_value para o instrumento).

FILOSOFIA DE TRADING:
- Só recomendas entrada quando existe confluência FORTE entre os 3 indicadores
- A agulhada do Didi é o gatilho principal — sem agulhada confirmada não há sinal
- O candle de confirmação já fechou antes de chegares a esta análise
- Preferes perder uma oportunidade a entrar num setup duvidoso
- Gestão de risco é soberana: SL sempre abaixo do swing low mais recente

REGRAS INEGOCIÁVEIS:
1. Nunca sugerires SL maior que 25 pips
2. O R:R mínimo é 1.8:1
3. Se o spread estiver acima de 1.5 pips, não entras
4. Se existir notícia de alto impacto em menos de 30 minutos, não entras
5. Se o estocástico estiver em zona extrema contrária ao sinal, não entras

TRAILING STOP (saída dinâmica):
- O TP que defines é apenas para calcular o R:R mínimo
- A saída real é por trailing stop que segue a MA8 do Didi
- Define o TP como 2× o SL para garantir R:R mínimo de 2:1

FORMATO DE RESPOSTA — APENAS JSON, SEM TEXTO ANTES OU DEPOIS:
{
  "decision": "BUY" | "SELL" | "NO_TRADE",
  "entry": <float — preço de entrada>,
  "sl": <float — stop loss>,
  "tp": <float — take profit>,
  "confidence": <int 0-10>,
  "reasoning": "<string — máximo 200 caracteres, em português>",
  "candle_pattern": "<string — padrão de vela detectado ou NONE>",
  "primary_signal": "<string — razão principal da decisão>"
}

Se a decisão for NO_TRADE, os campos entry/sl/tp podem ser 0."""

GUARDIAN_SYSTEM_PROMPT = """És o guardião de trading do sistema Alpha-Quant. Monitorizas operações abertas em tempo real.

MISSÃO: Avaliar se uma operação aberta deve continuar, ser fechada, ou se o trailing stop deve ser ajustado.

SINAIS DE REVERSÃO A MONITORIZAR:
- Agulhada inversa do Didi (MA3 cruzando na direcção oposta)
- MACD cruzando a linha de sinal na direcção oposta
- Estocástico a atingir zona extrema na direcção oposta
- Preço a fechar abaixo/acima da MA8 do Didi (para operação aberta)
- Bollinger: preço a tocar a banda oposta

FORMATO DE RESPOSTA — APENAS JSON:
{
  "alert_type": "REVERSAL_WARNING" | "TP_APPROACHING" | "TRAILING_UPDATE" | "ALL_GOOD",
  "action": "CLOSE" | "HOLD" | "MOVE_TRAILING",
  "urgency": "HIGH" | "MEDIUM" | "LOW",
  "message": "<string — máximo 150 caracteres, em português>",
  "reasoning": "<string — máximo 200 caracteres, em português>",
  "new_trailing_sl": <float | null — novo nível de SL sugerido>
}"""


# -----------------------------------------------------------------------------
# ANALISTA CLAUDE
# -----------------------------------------------------------------------------

class ClaudeAnalyst:
    """
    Interface com a API do Claude para análise de setups e monitorização.
    """

    def __init__(self):
        if not ANTHROPIC_AVAILABLE or not ANTHROPIC_API_KEY:
            logger.warning("Anthropic não disponível ou API key ausente — modo simulação")
            self._client = None
        else:
            self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        self._total_tokens_today = 0
        self._calls_today = 0

    # -------------------------------------------------------------------------
    # SCOUT: ANÁLISE DE NOVO SETUP
    # -------------------------------------------------------------------------

    def analyse_setup(
        self,
        symbol:    str,
        bid:       float,
        ask:       float,
        spread:    float,
        snap_m15:  StrategySnapshot,
        snap_h1:   StrategySnapshot,
        snap_h4:   StrategySnapshot,
        account_balance: float,
        news_warning:    str   = "none",
        sd_context = None,   # SDContext opcional
    ) -> Optional[TradeSignal]:
        """
        Analisa um potencial setup de entrada.
        Retorna TradeSignal validado ou None se não houver setup.
        """
        # Pré-filtro Python: verifica confluência mínima ANTES de chamar a API
        # Isto poupa tokens e evita chamadas desnecessárias
        sd_bonus = sd_context.confluence_bonus if sd_context else 0
        if not self._pre_filter(snap_m15, snap_h1, spread, sd_bonus):
            logger.debug(f"Setup {symbol} descartado no pré-filtro Python")
            return None

        # Constrói o payload JSON para o Claude
        payload = self._build_scout_payload(
            symbol, bid, ask, spread,
            snap_m15, snap_h1, snap_h4,
            account_balance, news_warning,
            sd_context=sd_context,
        )

        # Chama a API
        raw_response = self._call_api(
            system_prompt=SCOUT_SYSTEM_PROMPT,
            user_message=payload,
            context="scout",
        )
        if raw_response is None:
            return None

        # Parse e validação rigorosa
        parsed = self._parse_and_validate_signal(raw_response, symbol, bid, ask, account_balance)
        return parsed

    def _pre_filter(
        self,
        snap_m15:  StrategySnapshot,
        snap_h1:   StrategySnapshot,
        spread:    float,
        sd_bonus:  int = 0,
    ) -> bool:
        """
        Filtros rápidos em Python ANTES de chamar o Claude.
        Retorna False se o setup deve ser descartado imediatamente.
        sd_bonus: bónus do Supply & Demand (0-3) — reduz threshold necessário.
        """
        from config import FILTERS

        # 1. Spread demasiado alto
        if spread > FILTERS.max_spread_pips:
            logger.debug(f"Pré-filtro: spread {spread} > {FILTERS.max_spread_pips}")
            return False

        # 2. Score de confluência M15 — limiar reduzido se em zona S&D
        min_score = max(4, 5 - sd_bonus)
        if snap_m15.confluence.total < min_score:
            logger.debug(f"Pré-filtro: score M15 {snap_m15.confluence.total} < {min_score}")
            return False

        # 3. Direcções M15 e H1 contraditórias
        if (snap_m15.confluence.direction != "NEUTRAL"
                and snap_h1.confluence.direction != "NEUTRAL"
                and snap_m15.confluence.direction != snap_h1.confluence.direction):
            logger.debug("Pré-filtro: M15 e H1 em direcções opostas")
            return False

        # 4. Sem agulhada no M15 (gatilho obrigatório)
        if (not snap_m15.didi.agulhada_bull
                and not snap_m15.didi.agulhada_bear):
            logger.debug("Pré-filtro: sem agulhada no M15")
            return False

        return True

    def _build_scout_payload(
        self,
        symbol: str, bid: float, ask: float, spread: float,
        snap_m15: StrategySnapshot,
        snap_h1:  StrategySnapshot,
        snap_h4:  StrategySnapshot,
        balance:  float,
        news:     str,
        sd_context = None,
    ) -> str:
        """Constrói o JSON compacto enviado ao Claude (minimiza tokens)."""

        def tf_payload(s: StrategySnapshot) -> dict:
            return {
                "close":      s.close,
                "didi": {
                    "ma3":          s.didi.ma3,
                    "ma8":          s.didi.ma8,
                    "ma20":         s.didi.ma20,
                    "agulhada_bull": s.didi.agulhada_bull,
                    "agulhada_bear": s.didi.agulhada_bear,
                    "hist_pips":    s.didi.hist_value,
                    "hist_dir":     s.didi.hist_direction,
                    "aligned_bull": s.didi.fully_aligned_bull,
                    "aligned_bear": s.didi.fully_aligned_bear,
                },
                "stoch": {
                    "k": s.stoch.k, "d": s.stoch.d,
                    "zone": s.stoch.zone,
                    "cross_bull": s.stoch.cross_bull,
                    "cross_bear": s.stoch.cross_bear,
                },
                "bb": {
                    "upper":   s.bollinger.upper,
                    "middle":  s.bollinger.middle,
                    "lower":   s.bollinger.lower,
                    "pos":     s.bollinger.price_vs_bands,
                    "squeeze": s.bollinger.squeeze,
                },
                "macd": {
                    "line":      s.macd.macd_line,
                    "signal":    s.macd.signal_line,
                    "hist":      s.macd.histogram,
                    "dir":       s.macd.direction,
                    "cross_bull": s.macd.cross_bull,
                    "cross_bear": s.macd.cross_bear,
                },
                "score":     s.confluence.total,
                "direction": s.confluence.direction,
            }

        from instruments import get_instrument
        _inst = get_instrument(symbol)
        data = {
            "symbol":  symbol,
            "instrument": {
                "category":      _inst.category,
                "pip_size":      _inst.pip_size,
                "pip_value_eur": _inst.pip_value_eur,
                "avg_atr_pips":  _inst.avg_atr_pips,
                "max_sl_pips":   _inst.max_sl_pips,
            },
            "time":    datetime.now(tz=timezone.utc).strftime("%H:%M UTC"),
            "price":   {"bid": bid, "ask": ask, "spread_pips": spread},
            "balance": balance,
            "news":    news,
            "m15":     tf_payload(snap_m15),
            "h1":      tf_payload(snap_h1),
            "h4":      tf_payload(snap_h4),
        }
        if sd_context:
            data["sd"] = {
                "in_demand": sd_context.in_demand_zone,
                "in_supply": sd_context.in_supply_zone,
                "pips_to_demand": sd_context.pips_to_demand,
                "pips_to_supply": sd_context.pips_to_supply,
                "bonus": sd_context.confluence_bonus,
                "note": sd_context.confluence_note[:120],
            }
        return json.dumps(data, separators=(",", ":"))

    # -------------------------------------------------------------------------
    # GUARDIÃO: MONITORIZAÇÃO DE OPERAÇÃO ABERTA
    # -------------------------------------------------------------------------

    def monitor_trade(
        self,
        symbol:        str,
        direction:     str,
        entry_price:   float,
        current_price: float,
        current_sl:    float,
        open_pips:     float,
        snap_m15:      StrategySnapshot,
        snap_h1:       StrategySnapshot,
    ) -> Optional[GuardianAlert]:
        """
        Monitoriza uma operação aberta e alerta se necessário.
        Só gera alerta se detectar sinal de reversão ou actualização do trailing.
        """
        # Pré-filtro: só chama a API se algo mudou de relevante
        if not self._should_alert(direction, snap_m15, open_pips):
            return None

        payload = {
            "symbol":        symbol,
            "direction":     direction,
            "entry":         entry_price,
            "current_price": current_price,
            "current_sl":    current_sl,
            "open_pips":     round(open_pips, 1),
            "ma8_didi":      snap_m15.didi.ma8,
            "m15": {
                "agulhada_bull": snap_m15.didi.agulhada_bull,
                "agulhada_bear": snap_m15.didi.agulhada_bear,
                "macd_dir":      snap_m15.macd.direction,
                "macd_cross_bull": snap_m15.macd.cross_bull,
                "macd_cross_bear": snap_m15.macd.cross_bear,
                "stoch_zone":    snap_m15.stoch.zone,
                "bb_pos":        snap_m15.bollinger.price_vs_bands,
            },
            "h1_direction":  snap_h1.confluence.direction,
        }

        raw = self._call_api(
            system_prompt=GUARDIAN_SYSTEM_PROMPT,
            user_message=json.dumps(payload, separators=(",", ":")),
            context="guardian",
        )
        if raw is None:
            return None

        return self._parse_guardian_alert(raw, symbol, direction)

    def _should_alert(
        self,
        direction: str,
        snap_m15:  StrategySnapshot,
        open_pips: float,
    ) -> bool:
        """
        Decide se vale a pena chamar a API para monitorização.
        Evita chamadas desnecessárias quando não há nada novo.
        """
        d = snap_m15.didi
        m = snap_m15.macd
        s = snap_m15.stoch

        # Agulhada inversa — alerta imediato
        if direction == "BUY" and d.agulhada_bear:
            return True
        if direction == "SELL" and d.agulhada_bull:
            return True

        # MACD cruzou na direcção oposta
        if direction == "BUY"  and m.cross_bear:
            return True
        if direction == "SELL" and m.cross_bull:
            return True

        # Estocástico em zona extrema contrária
        if direction == "BUY"  and s.zone == "OVERBOUGHT":
            return True
        if direction == "SELL" and s.zone == "OVERSOLD":
            return True

        # Em lucro significativo — verifica se deve actualizar trailing
        if open_pips >= 15:
            return True

        return False

    # -------------------------------------------------------------------------
    # CHAMADA À API
    # -------------------------------------------------------------------------

    def _call_api(
        self,
        system_prompt: str,
        user_message:  str,
        context:       str = "unknown",
    ) -> Optional[str]:
        """
        Chama a API do Claude com retry automático.
        Retorna o texto da resposta ou None em caso de falha.
        """
        if self._client is None:
            logger.info(f"[{context}] API não configurada — simulando resposta")
            return self._mock_response(context)

        for attempt in range(1, SYSTEM.api_max_retries + 1):
            try:
                response = self._client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=ANTHROPIC_MAX_TOKENS,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                    timeout=SYSTEM.api_timeout_seconds,
                )

                raw = response.content[0].text.strip()
                tokens_used = response.usage.input_tokens + response.usage.output_tokens
                self._total_tokens_today += tokens_used
                self._calls_today += 1

                logger.info(
                    f"[{context}] API OK · {tokens_used} tokens · "
                    f"Total hoje: {self._total_tokens_today} tokens"
                )
                return raw

            except Exception as e:
                err_name = type(e).__name__
                if "RateLimit" in err_name:
                    logger.warning(f"[{context}] Rate limit — aguardando {SYSTEM.api_retry_delay}s")
                    time.sleep(SYSTEM.api_retry_delay)
                elif "Timeout" in err_name:
                    logger.warning(f"[{context}] Timeout (tentativa {attempt}/{SYSTEM.api_max_retries})")
                    if attempt < SYSTEM.api_max_retries:
                        time.sleep(SYSTEM.api_retry_delay)
                else:
                    logger.error(f"[{context}] Erro API ({err_name}): {e}")
                    if attempt < SYSTEM.api_max_retries:
                        time.sleep(SYSTEM.api_retry_delay)
                    else:
                        return None

        logger.error(f"[{context}] Todos os retries esgotados")
        return None

    # -------------------------------------------------------------------------
    # PARSE E VALIDAÇÃO ANTI-ALUCINAÇÃO
    # -------------------------------------------------------------------------

    def _parse_and_validate_signal(
        self,
        raw:     str,
        symbol:  str,
        bid:     float,
        ask:     float,
        balance: float,
    ) -> Optional[TradeSignal]:
        """
        Parse rigoroso da resposta do Claude.
        Rejeita qualquer resposta que não passe nas travas de segurança.
        """
        # 1. Extrai JSON (protege contra texto extra)
        parsed = self._extract_json(raw)
        if parsed is None:
            logger.warning(f"Resposta inválida (não é JSON): {raw[:200]}")
            return None

        # 2. Verifica campos obrigatórios
        required = ["decision", "entry", "sl", "tp", "confidence", "reasoning"]
        for field in required:
            if field not in parsed:
                logger.warning(f"Campo ausente na resposta: {field}")
                return None

        decision = str(parsed.get("decision", "NO_TRADE")).upper()

        if decision == "NO_TRADE":
            logger.info(f"Claude decidiu: NO_TRADE — {parsed.get('reasoning', '')[:80]}")
            return None

        if decision not in ("BUY", "SELL"):
            logger.warning(f"Decisão inválida: {decision}")
            return None

        # 3. Extrai e valida preços
        try:
            entry = float(parsed["entry"])
            sl    = float(parsed["sl"])
            tp    = float(parsed["tp"])
            confidence = int(parsed.get("confidence", 0))
        except (ValueError, TypeError) as e:
            logger.warning(f"Erro ao converter preços: {e}")
            return None

        # 4. TRAVAS DE SEGURANÇA (Python, não a IA)

        # Trava 1: preço de entrada próximo do bid/ask actual (max 5 pips de desvio)
        current_price = ask if decision == "BUY" else bid
        price_deviation_pips = abs(entry - current_price) * 10000
        if price_deviation_pips > 5:
            logger.warning(
                f"Alucinação de preço: entry={entry} vs actual={current_price:.5f} "
                f"({price_deviation_pips:.1f} pips de desvio)"
            )
            entry = current_price  # Corrige para preço actual

        # Trava 2: calcula pips de SL e TP
        from instruments import get_instrument
        inst = get_instrument(symbol)
        pip  = inst.pip_size
        if decision == "BUY":
            sl_pips = (entry - sl) / pip
            tp_pips = (tp - entry) / pip
        else:
            sl_pips = (sl - entry) / pip
            tp_pips = (entry - tp) / pip

        # Trava 3: SL deve ser positivo e dentro do limite
        if sl_pips <= 0:
            logger.warning(f"SL do lado errado: sl_pips={sl_pips:.1f}")
            return None

        if sl_pips > RISK.max_sl_pips:
            logger.warning(f"SL demasiado largo: {sl_pips:.1f} pips (máx {RISK.max_sl_pips})")
            return None

        # Trava 4: TP deve ser positivo
        if tp_pips <= 0:
            logger.warning(f"TP do lado errado: tp_pips={tp_pips:.1f}")
            return None

        # Trava 5: R:R mínimo
        rr = tp_pips / sl_pips if sl_pips > 0 else 0
        if rr < RISK.min_rr_ratio:
            logger.warning(f"R:R insuficiente: {rr:.2f} (mínimo {RISK.min_rr_ratio})")
            return None

        # Trava 6: confidence mínimo
        if confidence < 7:
            logger.info(f"Confidence insuficiente: {confidence}/10")
            return None

        # 5. Calcula lot size (Python, não a IA)
        lot_size = self._calc_lot(balance, RISK.risk_per_trade_pct, sl_pips, symbol)
        risk_eur = round(balance * RISK.risk_per_trade_pct / 100, 2)

        logger.info(
            f"Sinal validado: {decision} {symbol} · "
            f"entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} · "
            f"SL={sl_pips:.1f}p TP={tp_pips:.1f}p R:R={rr:.2f} · "
            f"lot={lot_size} risco=€{risk_eur} · conf={confidence}/10"
        )

        return TradeSignal(
            symbol=symbol,
            direction=decision,
            entry=round(entry, 5),
            sl=round(sl, 5),
            tp=round(tp, 5),
            sl_pips=round(sl_pips, 1),
            tp_pips=round(tp_pips, 1),
            rr_ratio=round(rr, 2),
            lot_size=lot_size,
            risk_eur=risk_eur,
            confidence=confidence,
            reasoning=str(parsed.get("reasoning", ""))[:300],
            claude_raw=raw[:500],
            generated_at=datetime.now(tz=timezone.utc).isoformat(),
            candle_pattern=str(parsed.get("candle_pattern", "NONE")),
        )

    def _parse_guardian_alert(
        self, raw: str, symbol: str, direction: str
    ) -> Optional[GuardianAlert]:
        """Parse da resposta do Guardião."""
        parsed = self._extract_json(raw)
        if parsed is None:
            return None

        alert_type = str(parsed.get("alert_type", "ALL_GOOD")).upper()
        if alert_type == "ALL_GOOD":
            return None

        return GuardianAlert(
            symbol=symbol,
            direction=direction,
            alert_type=alert_type,
            message=str(parsed.get("message", ""))[:150],
            action=str(parsed.get("action", "HOLD")).upper(),
            urgency=str(parsed.get("urgency", "LOW")).upper(),
            reasoning=str(parsed.get("reasoning", ""))[:200],
            generated_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    def _extract_json(self, text: str) -> Optional[dict]:
        """
        Extrai JSON de uma string — protege contra texto extra do modelo.
        Tenta primeiro parse directo, depois procura bloco entre {}.
        """
        text = text.strip()

        # Tenta parse directo
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Procura bloco JSON com regex
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return None

    def _calc_lot(
        self, balance: float, risk_pct: float, sl_pips: float, symbol: str
    ) -> float:
        """Calcula lot size com travas de segurança usando pip value correcto do instrumento."""
        if sl_pips <= 0:
            return RISK.min_lot_size
        from instruments import get_instrument
        inst = get_instrument(symbol)
        risk_eur = balance * (risk_pct / 100)
        lot = risk_eur / (sl_pips * inst.pip_value_eur)
        lot = round(lot - (lot % inst.lot_step), 2)
        return max(inst.min_lot, min(inst.max_lot, lot))

    # -------------------------------------------------------------------------
    # MODO SIMULAÇÃO
    # -------------------------------------------------------------------------

    def _mock_response(self, context: str) -> str:
        """Resposta simulada para testes sem API key."""
        if context == "scout":
            return json.dumps({
                "decision": "BUY",
                "entry": 0,
                "sl":    0,
                "tp":    0,
                "confidence": 8,
                "reasoning": "Agulhada bull Didi confirmada no M15 com H1 alinhado. MACD positivo e estocástico em zona neutra. Bollinger a pressionar banda superior.",
                "candle_pattern": "BULLISH_ENGULF",
                "primary_signal": "Didi agulhada bull + MACD cross"
            })
        else:
            return json.dumps({
                "alert_type": "ALL_GOOD",
                "action": "HOLD",
                "urgency": "LOW",
                "message": "Tendência mantém-se. Trailing a seguir MA8.",
                "reasoning": "Sem sinais de reversão. Didi alinhado bull.",
                "new_trailing_sl": None
            })

    @property
    def token_usage_today(self) -> dict:
        return {
            "tokens": self._total_tokens_today,
            "calls": self._calls_today,
            "estimated_cost_usd": round(self._total_tokens_today * 0.000003, 4),
        }


# -----------------------------------------------------------------------------
# INSTÂNCIA GLOBAL
# -----------------------------------------------------------------------------
analyst = ClaudeAnalyst()
