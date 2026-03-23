# =============================================================================
# ALPHA-QUANT · config.py
# Configuração central do sistema — edita APENAS este ficheiro
# =============================================================================

import os
from dataclasses import dataclass, field
from typing import List

# -----------------------------------------------------------------------------
# LIGAÇÃO AO METATRADER 5
# -----------------------------------------------------------------------------
MT5_LOGIN    = int(os.getenv("MT5_LOGIN", "0"))       # Nº da conta MT5
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")          # Palavra-passe MT5
MT5_SERVER   = os.getenv("MT5_SERVER", "")            # Ex: "ICMarkets-Demo"
MT5_PATH     = os.getenv(                             # Caminho do terminal MT5
    "MT5_PATH",
    "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
)

# -----------------------------------------------------------------------------
# ANTHROPIC API
# -----------------------------------------------------------------------------
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL     = "claude-sonnet-4-20250514"
ANTHROPIC_MAX_TOKENS = 1024  # Suficiente para JSON de análise

# -----------------------------------------------------------------------------
# INSTRUMENTOS E TIMEFRAMES
# -----------------------------------------------------------------------------
# Grupos de símbolos por perfil de risco
SYMBOLS_CONSERVATIVE = ["EURUSD", "GBPUSD", "USDJPY"]         # Forex majors — baixo spread
SYMBOLS_MODERATE     = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "GER40"]  # + Ouro e DAX
SYMBOLS_AGGRESSIVE   = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD", "NAS100"]  # + Cripto e índices

# Símbolos activos (muda conforme o teu perfil)
SYMBOLS: List[str] = SYMBOLS_CONSERVATIVE

# Máximo de símbolos em análise simultânea (0 = sem limite)
MAX_SYMBOLS_SIMULTANEOUS: int = 5

# Risco por símbolo quando múltiplos activos (% do envelope total)
RISK_DISTRIBUTION = "equal"  # "equal" | "ranked" (os melhores scores recebem mais risco)

TIMEFRAME_ENTRY  = "M15"   # Timeframe de entrada (sinal)
TIMEFRAME_TREND  = "H1"    # Timeframe de tendência (confirmação)
TIMEFRAME_MACRO  = "H4"    # Timeframe macro (contexto direcional)

CANDLES_HISTORY  = 100     # Nº de velas a enviar para análise

# -----------------------------------------------------------------------------
# GESTÃO DE RISCO — VALORES ABSOLUTOS (não alteres sem perceber as implicações)
# -----------------------------------------------------------------------------
@dataclass
class RiskConfig:
    # Risco por operação (% do saldo)
    risk_per_trade_pct: float = 1.0

    # Risco máximo simultâneo (% do saldo) — envelope diário
    max_daily_risk_pct: float = 3.0

    # R:R mínimo para aceitar um setup
    min_rr_ratio: float = 1.8

    # Stop Loss máximo permitido em pips (trava anti-alucinação)
    max_sl_pips: int = 25

    # Lot máximo absoluto (trava hard — nunca ultrapassa isto)
    max_lot_size: float = 0.02

    # Lot mínimo (micro)
    min_lot_size: float = 0.01

    # Breakeven: mover SL para entrada quando preço atinge X% do TP
    breakeven_trigger_pct: float = 50.0

    # Trailing: activar trailing stop quando em lucro de X pips
    trailing_activation_pips: int = 15

    # Trailing: distância do trailing stop em pips
    trailing_distance_pips: int = 10

RISK = RiskConfig()

# -----------------------------------------------------------------------------
# FILTROS DE QUALIDADE DE SETUP
# -----------------------------------------------------------------------------
@dataclass
class FilterConfig:
    # RSI — evitar entradas em zonas extremas
    rsi_overbought: float = 70.0
    rsi_oversold:   float = 30.0
    rsi_period:     int   = 14

    # Médias móveis
    ma_fast:   int = 20
    ma_slow:   int = 200

    # ATR — volatilidade mínima para entrar (pips)
    atr_min_pips: float = 5.0
    atr_period:   int   = 14

    # Spread máximo permitido em pips
    max_spread_pips: float = 1.5

    # Janela de bloqueio antes/depois de notícias alto impacto (minutos)
    news_block_minutes_before: int = 30
    news_block_minutes_after:  int = 30

    # Sessões de trading permitidas (UTC)
    allowed_sessions: List[str] = field(default_factory=lambda: [
        "london",    # 07:00–16:00 UTC
        "new_york",  # 12:00–21:00 UTC
    ])

FILTERS = FilterConfig()

# -----------------------------------------------------------------------------
# SISTEMA — INTERVALOS E COMPORTAMENTO
# -----------------------------------------------------------------------------
@dataclass
class SystemConfig:
    # Intervalo entre ciclos do Scout (segundos) — corre no fecho de vela M15
    scout_interval_seconds: int = 60

    # Intervalo de monitorização de operações abertas (segundos)
    guardian_interval_seconds: int = 30

    # Timeout para resposta da API Claude (segundos)
    api_timeout_seconds: int = 30

    # Nº máximo de retries em caso de falha de API
    api_max_retries: int = 3

    # Pausa entre retries (segundos)
    api_retry_delay: int = 5

    # Modo demo (True = sem execução real de ordens)
    demo_mode: bool = True

    # Path da base de dados SQLite
    db_path: str = "alphaquant.db"

    # Path dos logs
    log_path: str = "logs/alphaquant.log"

    # Nível de log: DEBUG, INFO, WARNING, ERROR
    log_level: str = "INFO"

SYSTEM = SystemConfig()

# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# WHATSAPP (notificações via Twilio WhatsApp API)
# -----------------------------------------------------------------------------
# Twilio sandbox gratuito: https://www.twilio.com/whatsapp
# Setup: consola Twilio → Messaging → Try it out → Send a WhatsApp message
WHATSAPP_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
WHATSAPP_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
WHATSAPP_FROM        = os.getenv("WHATSAPP_FROM", "whatsapp:+14155238886")  # número Twilio sandbox
WHATSAPP_TO          = os.getenv("WHATSAPP_TO", "")   # ex: whatsapp:+351912345678
WHATSAPP_ENABLED     = bool(WHATSAPP_ACCOUNT_SID and WHATSAPP_AUTH_TOKEN and WHATSAPP_TO)

# -----------------------------------------------------------------------------
# VALIDAÇÃO NA INICIALIZAÇÃO
# -----------------------------------------------------------------------------
def validate_config() -> list[str]:
    """
    Valida configuração crítica antes de arrancar o sistema.
    Retorna lista de erros. Lista vazia = tudo OK.
    """
    errors = []

    if not SYSTEM.demo_mode:
        if MT5_LOGIN == 0:
            errors.append("MT5_LOGIN não configurado")
        if not MT5_PASSWORD:
            errors.append("MT5_PASSWORD não configurada")
        if not MT5_SERVER:
            errors.append("MT5_SERVER não configurado")
        if not ANTHROPIC_API_KEY:
            errors.append("ANTHROPIC_API_KEY não configurada")

    if RISK.risk_per_trade_pct > 2.0:
        errors.append(f"risk_per_trade_pct={RISK.risk_per_trade_pct}% é demasiado alto (máx 2%)")

    if RISK.max_daily_risk_pct > 5.0:
        errors.append(f"max_daily_risk_pct={RISK.max_daily_risk_pct}% é demasiado alto (máx 5%)")

    if RISK.min_rr_ratio < 1.5:
        errors.append(f"min_rr_ratio={RISK.min_rr_ratio} abaixo do mínimo seguro (1.5)")

    return errors
