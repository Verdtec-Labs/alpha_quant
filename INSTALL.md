# ALPHA-QUANT — Guia de Instalação e Arranque
## Operacional em conta demo hoje

---

## PRÉ-REQUISITOS

- Windows 10/11 (obrigatório para MetaTrader 5)
- Python 3.11+ instalado: https://python.org/downloads
- MetaTrader 5 instalado com conta demo activa
- Conta Anthropic com créditos: https://console.anthropic.com

---

## PASSO 1 — Instalar dependências

Abre o CMD ou PowerShell na pasta do projecto:

```cmd
pip install pandas numpy anthropic flask python-dotenv MetaTrader5
```

---

## PASSO 2 — Configurar credenciais

Copia `.env.example` para `.env` e preenche:

```env
MT5_LOGIN=12345678           ← número da tua conta demo MT5
MT5_PASSWORD=password123     ← palavra-passe da conta
MT5_SERVER=ICMarkets-Demo    ← Menu do MT5: Ficheiro → Login → ver nome do servidor
ANTHROPIC_API_KEY=sk-ant-... ← https://console.anthropic.com → API Keys
```

Como encontrar o nome do servidor MT5:
1. Abre o MetaTrader 5
2. Vai a Ficheiro → Login na conta de trading
3. O campo "Servidor" mostra o nome exacto

---

## PASSO 3 — Verificar tudo

```cmd
python run.py --check
```

Deves ver:
```
✓ MT5: OK
✓ CLAUDE: OK
⚠ WHATSAPP: NÃO CONFIGURADO (opcional)
→ Pronto para OPERAÇÃO REAL
```

---

## PASSO 4 — Arrancar em modo demo

```cmd
python run.py --demo
```

O sistema vai:
1. Ligar ao MetaTrader 5
2. Arrancar a dashboard em http://localhost:5000
3. Iniciar o Scout (análise de EUR/USD a cada vela M15)
4. Enviar alertas quando encontrar setup

Abre o browser em **http://localhost:5000** para ver a dashboard.

---

## PASSO 5 — Operação normal

Quando o Scout encontrar um setup:

1. Recebes notificação na dashboard (e WhatsApp se configurado)
2. Vês: direcção, entry, SL, TP, risco em euros, score de confluência
3. Clicas **SIM** ou **NÃO** em 90 segundos
4. O sistema executa (demo = só simula, sem ordem real)

---

## COMANDOS DISPONÍVEIS

```cmd
python run.py              → Sistema completo (demo_mode=True por default)
python run.py --demo       → Força modo demo explicitamente
python run.py --check      → Verifica ligações e sai
python run.py --dashboard  → Só dashboard, sem trading
python run.py --loglevel DEBUG  → Log detalhado para debug
```

---

## CONFIGURAÇÃO MT5 PARA CONTA DEMO

1. Abre MetaTrader 5
2. Ficheiro → Abrir conta
3. Escolhe o teu broker (ICMarkets, Pepperstone, XM, etc.)
4. Selecciona "Conta demo"
5. Preenche os dados → O servidor aparece automaticamente
6. Activa "Trading automático" (botão verde no topo do MT5)
7. Verifica: Ferramentas → Opções → Expert Advisors → "Permitir trading automático"

---

## CONFIGURAÇÃO WHATSAPP (via Twilio — gratuito)

O sistema usa a API oficial do WhatsApp Business via Twilio.

1. Regista em https://www.twilio.com (trial gratuito)
2. Dashboard Twilio → Messaging → Try it out → Send a WhatsApp message
3. Envia a mensagem de activação do sandbox pelo teu WhatsApp
4. Copia Account SID e Auth Token do dashboard
5. Preenche no `.env`:

```env
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
WHATSAPP_FROM=whatsapp:+14155238886
WHATSAPP_TO=whatsapp:+351912345678
```

Cada sinal enviado por WhatsApp inclui par, direcção, entry, SL, TP,
risco em euros, R:R, score e justificativa do Claude.


## FICHEIROS DO PROJECTO

```
alpha_quant/
├── run.py                ← ENTRY POINT — executa este
├── .env                  ← as tuas credenciais (não partilhar)
├── config.py             ← parâmetros de risco e sistema
├── mt5_connector.py      ← ligação ao MetaTrader 5
├── indicators.py         ← RSI, MA, ATR base
├── didi_indicators.py    ← Didi Index + Bollinger + MACD + Estocástico
├── supply_demand.py      ← detecção de zonas S&D institucionais
├── claude_analyst.py     ← prompts e validação anti-alucinação
├── risk_manager.py       ← envelope, kill switch, trailing, SQLite
├── orchestrator.py       ← loop 24/7 Scout + Guardian
├── dashboard_server.py   ← servidor Flask + SSE
├── dashboard_v2.html     ← interface visual
└── alphaquant.db         ← base de dados (criada automaticamente)
```

---

## PARÂMETROS DE RISCO (config.py)

| Parâmetro | Valor | Significado |
|-----------|-------|-------------|
| risk_per_trade_pct | 1.0% | €5 de risco por trade com €500 |
| max_daily_risk_pct | 3.0% | €15 de perda máxima por dia |
| min_rr_ratio | 1.8 | mínimo 1.8:1 de risco/recompensa |
| max_sl_pips | 25 | stop loss máximo permitido |
| breakeven_trigger_pct | 50% | move SL para entrada aos 50% do TP |
| trailing_distance_pips | 10 | distância do trailing stop |

---

## SOLUÇÃO DE PROBLEMAS

**"MT5 não disponível"**
→ Instala MetaTrader5: `pip install MetaTrader5`
→ Confirma que o MT5 está aberto e logado

**"Claude API erro"**
→ Verifica ANTHROPIC_API_KEY no .env
→ Confirma que tens créditos na conta: https://console.anthropic.com

**"Sem sinais gerados"**
→ Normal — o sistema é exigente. Score mínimo 7/10
→ Verifica se está numa kill zone activa (02:00-05:00 ou 07:00-10:00 UTC)
→ Ativa log DEBUG: `python run.py --loglevel DEBUG`

**"Kill switch activo"**
→ Perdeste 3% do saldo no dia
→ O sistema suspende automaticamente até ao dia seguinte
→ É uma funcionalidade de segurança, não um erro

**Dashboard não abre**
→ Instala Flask: `pip install flask`
→ Verifica se a porta 5000 está livre
→ Tenta: `python run.py --dashboard --port 8080`
