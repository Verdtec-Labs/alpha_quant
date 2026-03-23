# =============================================================================
# ALPHA-QUANT · news_calendar.py
# Calendário económico em tempo real
# Fontes: ForexFactory (scraping) + Investing.com API + cache local
#
# Lógica:
#   1. Tenta ForexFactory JSON (melhor fonte, gratuita)
#   2. Fallback para Investing.com RSS
#   3. Cache de 30 min para não bater nas APIs constantemente
#   4. Expõe: is_safe_to_trade(symbol) → bool + razão
# =============================================================================

import json
import logging
import threading
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class NewsEvent:
    title:      str
    currency:   str          # "USD", "EUR", "GBP", etc.
    impact:     str          # "HIGH", "MEDIUM", "LOW"
    datetime_utc: datetime
    actual:     str = ""     # valor publicado
    forecast:   str = ""     # previsão
    previous:   str = ""     # valor anterior
    source:     str = "forexfactory"

    @property
    def is_high_impact(self) -> bool:
        return self.impact == "HIGH"

    @property
    def minutes_until(self) -> float:
        """Minutos até ao evento (negativo = já passou)."""
        now = datetime.now(tz=timezone.utc)
        return (self.datetime_utc - now).total_seconds() / 60

    @property
    def is_imminent(self) -> bool:
        """Evento nas próximas 30 minutos ou últimos 30 minutos."""
        mins = self.minutes_until
        return -30 <= mins <= 30


class NewsCalendar:
    """
    Calendário económico com cache e múltiplas fontes.
    Thread-safe — pode ser chamado de qualquer thread.
    """

    # Pares de moedas e as suas moedas relevantes
    SYMBOL_CURRENCIES = {
        "EURUSD": ["EUR", "USD"],
        "GBPUSD": ["GBP", "USD"],
        "USDJPY": ["USD", "JPY"],
        "EURJPY": ["EUR", "JPY"],
        "AUDUSD": ["AUD", "USD"],
        "USDCAD": ["USD", "CAD"],
        "USDCHF": ["USD", "CHF"],
        "EURGBP": ["EUR", "GBP"],
        "XAUUSD": ["USD"],          # Ouro reage principalmente ao USD
        "BTCUSD": ["USD"],          # Cripto reage ao USD e sentiment
        "US30":   ["USD"],
        "NAS100": ["USD"],
        "GER40":  ["EUR"],
    }

    # Notícias de ultra-alto impacto que bloqueiam TODOS os instrumentos
    GLOBAL_BLOCKERS = [
        "Federal Reserve", "FOMC", "Fed Rate",
        "ECB Rate", "Bank of England", "BOE Rate",
        "NFP", "Non-Farm", "Payroll",
        "CPI", "Inflation",
        "GDP", "Gross Domestic",
    ]

    def __init__(self, block_before_min: int = 30, block_after_min: int = 30):
        self.block_before = block_before_min
        self.block_after  = block_after_min
        self._events:    list[NewsEvent] = []
        self._lock       = threading.Lock()
        self._last_fetch = 0.0
        self._cache_ttl  = 1800  # 30 minutos
        self._fetch_errors = 0

        # Semana actual em cache (para ForexFactory)
        self._cached_week: Optional[str] = None

    # ─── API PÚBLICA ──────────────────────────────────────────────────────────

    def is_safe_to_trade(
        self, symbol: str, check_minutes_ahead: int = 0
    ) -> tuple[bool, str]:
        """
        Verifica se é seguro entrar numa operação agora (ou em X minutos).

        Retorna (True, "") se seguro, ou (False, razão) se não seguro.
        """
        self._refresh_if_needed()

        currencies = self.SYMBOL_CURRENCIES.get(
            symbol.upper(),
            self._infer_currencies(symbol)
        )

        with self._lock:
            for event in self._events:
                if not event.is_high_impact:
                    continue

                mins = event.minutes_until - check_minutes_ahead

                # Janela de bloqueio: 30 min antes + 30 min depois
                if -(self.block_after) <= mins <= self.block_before:

                    # Verifica se afecta este instrumento
                    affects = (
                        event.currency in currencies or
                        any(g.lower() in event.title.lower() for g in self.GLOBAL_BLOCKERS)
                    )

                    if affects:
                        direction = "em" if mins >= 0 else "há"
                        abs_mins  = abs(int(mins))
                        return (
                            False,
                            f"Notícia ALTO impacto {direction} {abs_mins}min: "
                            f"{event.title} ({event.currency}) — bloqueado"
                        )

        return True, ""

    def get_upcoming_events(
        self, hours_ahead: int = 8, high_only: bool = False
    ) -> list[NewsEvent]:
        """Retorna eventos nas próximas X horas."""
        self._refresh_if_needed()
        now = datetime.now(tz=timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)

        with self._lock:
            events = [
                e for e in self._events
                if now <= e.datetime_utc <= cutoff
                and (not high_only or e.is_high_impact)
            ]
        return sorted(events, key=lambda e: e.datetime_utc)

    def get_next_high_impact(self, symbol: str) -> Optional[NewsEvent]:
        """Retorna a próxima notícia de alto impacto para um símbolo."""
        currencies = self.SYMBOL_CURRENCIES.get(symbol.upper(), ["USD"])
        upcoming   = self.get_upcoming_events(hours_ahead=24, high_only=True)
        for e in upcoming:
            if e.currency in currencies:
                return e
        return None

    def force_refresh(self):
        """Força actualização imediata do calendário."""
        self._last_fetch = 0.0
        self._refresh_if_needed()

    # ─── FETCH ────────────────────────────────────────────────────────────────

    def _refresh_if_needed(self):
        """Actualiza o cache se expirou."""
        if time.time() - self._last_fetch < self._cache_ttl:
            return
        self._fetch()

    def _fetch(self):
        """Tenta buscar eventos — ForexFactory primeiro, depois fallback."""
        logger.info("A actualizar calendário económico...")

        events = self._fetch_forexfactory()

        if not events:
            logger.warning("ForexFactory falhou — a tentar fallback...")
            events = self._fetch_investing_rss()

        if not events:
            logger.warning("Todas as fontes falharam — a usar eventos de emergência")
            events = self._emergency_events()
            self._fetch_errors += 1
        else:
            self._fetch_errors = 0

        with self._lock:
            self._events = events
            self._last_fetch = time.time()

        logger.info(f"Calendário actualizado: {len(events)} eventos")

    def _fetch_forexfactory(self) -> list[NewsEvent]:
        """
        Busca o calendário semanal da ForexFactory.
        URL: https://nfs.faireconomy.media/ff_calendar_thisweek.json
        Esta é a API JSON não-oficial mas estável da ForexFactory.
        """
        try:
            url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 AlphaQuant/1.0",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            events = []
            for item in data:
                try:
                    # ForexFactory formato: {"title":"...", "country":"...",
                    #   "date":"...", "time":"...", "impact":"High/Medium/Low",
                    #   "forecast":"...", "previous":"..."}
                    impact_map = {
                        "High":   "HIGH",
                        "Medium": "MEDIUM",
                        "Low":    "LOW",
                        "Non-Economic": "LOW",
                    }
                    impact = impact_map.get(item.get("impact", "Low"), "LOW")

                    # Parseia a data/hora
                    date_str = item.get("date", "")
                    time_str = item.get("time", "00:00am")
                    dt = self._parse_ff_datetime(date_str, time_str)
                    if dt is None:
                        continue

                    # Mapeia país para moeda
                    currency = self._country_to_currency(item.get("country", ""))

                    events.append(NewsEvent(
                        title=item.get("title", "Unknown"),
                        currency=currency,
                        impact=impact,
                        datetime_utc=dt,
                        actual=item.get("actual", ""),
                        forecast=item.get("forecast", ""),
                        previous=item.get("previous", ""),
                        source="forexfactory",
                    ))
                except Exception as e:
                    logger.debug(f"Erro a parsear evento FF: {e}")
                    continue

            logger.info(f"ForexFactory: {len(events)} eventos carregados")
            return events

        except Exception as e:
            logger.warning(f"ForexFactory erro: {e}")
            return []

    def _fetch_investing_rss(self) -> list[NewsEvent]:
        """
        Fallback: RSS do Investing.com para notícias de alto impacto.
        Menos preciso nos horários mas melhor que nada.
        """
        try:
            url = "https://www.investing.com/rss/news_25.rss"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 AlphaQuant/1.0",
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                content = resp.read().decode("utf-8", errors="replace")

            events = []
            import re
            items = re.findall(r"<item>(.*?)</item>", content, re.DOTALL)

            now = datetime.now(tz=timezone.utc)
            for item in items[:20]:
                title_m = re.search(r"<title>(.*?)</title>", item)
                date_m  = re.search(r"<pubDate>(.*?)</pubDate>", item)

                if not title_m:
                    continue

                title = title_m.group(1).strip()
                impact = self._infer_impact_from_title(title)

                dt = now + timedelta(hours=1)  # fallback: assume 1h no futuro
                if date_m:
                    try:
                        from email.utils import parsedate_to_datetime
                        dt = parsedate_to_datetime(date_m.group(1))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

                currency = self._infer_currency_from_title(title)

                events.append(NewsEvent(
                    title=title[:100],
                    currency=currency,
                    impact=impact,
                    datetime_utc=dt,
                    source="investing_rss",
                ))

            return events

        except Exception as e:
            logger.warning(f"Investing.com RSS erro: {e}")
            return []

    def _emergency_events(self) -> list[NewsEvent]:
        """
        Quando todas as fontes falham, cria um calendário de emergência
        com os horários típicos de alto impacto para a semana.
        Melhor do que nada — protege nos horários mais prováveis.
        """
        now  = datetime.now(tz=timezone.utc)
        today = now.date()
        events = []

        # Horários típicos de alto impacto em UTC
        typical_events = [
            # USD — sessão americana
            ("13:30", "USD", "US Economic Data (scheduled)", "HIGH"),
            ("15:00", "USD", "US Economic Data (afternoon)", "HIGH"),
            ("18:00", "USD", "Fed Speaker / FOMC Minutes", "MEDIUM"),
            # EUR — sessão europeia
            ("08:00", "EUR", "EUR Economic Data", "MEDIUM"),
            ("09:00", "EUR", "EUR Economic Data", "HIGH"),
            ("10:00", "EUR", "EUR Economic Data", "MEDIUM"),
            # GBP
            ("07:00", "GBP", "GBP Economic Data", "MEDIUM"),
            ("09:30", "GBP", "GBP Economic Data", "HIGH"),
        ]

        for time_str, currency, title, impact in typical_events:
            h, m = map(int, time_str.split(":"))
            dt = datetime(today.year, today.month, today.day, h, m,
                         tzinfo=timezone.utc)
            if dt < now:
                dt += timedelta(days=1)

            events.append(NewsEvent(
                title=title, currency=currency,
                impact=impact, datetime_utc=dt,
                source="emergency_schedule",
            ))

        logger.warning(f"Usando calendário de emergência ({len(events)} eventos)")
        return events

    # ─── UTILITÁRIOS ──────────────────────────────────────────────────────────

    def _parse_ff_datetime(
        self, date_str: str, time_str: str
    ) -> Optional[datetime]:
        """Parseia data/hora da ForexFactory (formato americano)."""
        try:
            # Formato FF: "2024-03-20" e "8:30am"
            if not date_str:
                return None

            # Normaliza hora
            time_str = time_str.strip().lower()
            if not time_str or time_str in ("all day", "tentative", ""):
                time_str = "12:00am"

            # Parse
            dt_str = f"{date_str} {time_str}"
            for fmt in [
                "%Y-%m-%d %I:%M%p",
                "%Y-%m-%d %H:%M",
                "%m/%d/%Y %I:%M%p",
            ]:
                try:
                    dt = datetime.strptime(dt_str, fmt)
                    return dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue

            return None
        except Exception:
            return None

    def _country_to_currency(self, country: str) -> str:
        """Converte país para código de moeda."""
        mapping = {
            "USD": "USD", "US": "USD", "United States": "USD",
            "EUR": "EUR", "EU": "EUR", "Eurozone": "EUR", "Europe": "EUR",
            "GBP": "GBP", "UK": "GBP", "United Kingdom": "GBP",
            "JPY": "JPY", "Japan": "JPY",
            "AUD": "AUD", "Australia": "AUD",
            "CAD": "CAD", "Canada": "CAD",
            "CHF": "CHF", "Switzerland": "CHF",
            "NZD": "NZD", "New Zealand": "NZD",
            "CNY": "CNY", "China": "CNY",
        }
        return mapping.get(country, country[:3].upper() if country else "USD")

    def _infer_currencies(self, symbol: str) -> list[str]:
        """Infere moedas de um símbolo não catalogado."""
        sym = symbol.upper()
        currencies = []
        known = ["USD","EUR","GBP","JPY","AUD","CAD","CHF","NZD","CNY"]
        for c in known:
            if c in sym:
                currencies.append(c)
        return currencies or ["USD"]

    def _infer_impact_from_title(self, title: str) -> str:
        """Infere impacto pelo título da notícia."""
        title_lower = title.lower()
        high_keywords = [
            "nfp", "non-farm", "payroll", "fomc", "fed rate", "ecb rate",
            "boe rate", "cpi", "inflation", "gdp", "interest rate",
            "employment", "unemployment", "retail sales",
        ]
        medium_keywords = [
            "pmi", "ism", "trade balance", "current account",
            "housing", "consumer confidence", "manufacturing",
        ]
        if any(k in title_lower for k in high_keywords):
            return "HIGH"
        if any(k in title_lower for k in medium_keywords):
            return "MEDIUM"
        return "LOW"

    def _infer_currency_from_title(self, title: str) -> str:
        """Infere moeda pelo título da notícia."""
        title_upper = title.upper()
        pairs = [
            ("US", "USD"), ("FEDERAL", "USD"), ("FED", "USD"),
            ("ECB", "EUR"), ("EUROZONE", "EUR"), ("EUROPE", "EUR"),
            ("BOE", "GBP"), ("BRITAIN", "GBP"), ("UK", "GBP"),
            ("BOJ", "JPY"), ("JAPAN", "JPY"),
            ("RBA", "AUD"), ("AUSTRALIA", "AUD"),
            ("BOC", "CAD"), ("CANADA", "CAD"),
        ]
        for keyword, currency in pairs:
            if keyword in title_upper:
                return currency
        return "USD"

    # ─── RESUMO ───────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Resumo do estado do calendário para a dashboard."""
        self._refresh_if_needed()
        upcoming = self.get_upcoming_events(hours_ahead=4, high_only=True)
        next_event = upcoming[0] if upcoming else None

        return {
            "total_events_cached": len(self._events),
            "high_impact_next_4h": len(upcoming),
            "next_high_impact": {
                "title":    next_event.title if next_event else None,
                "currency": next_event.currency if next_event else None,
                "minutes":  round(next_event.minutes_until) if next_event else None,
                "time_utc": next_event.datetime_utc.strftime("%H:%M UTC") if next_event else None,
            } if next_event else None,
            "fetch_errors": self._fetch_errors,
            "last_updated": datetime.fromtimestamp(
                self._last_fetch, tz=timezone.utc
            ).strftime("%H:%M UTC") if self._last_fetch else "nunca",
        }


# ─── INSTÂNCIA GLOBAL ─────────────────────────────────────────────────────────
calendar = NewsCalendar(block_before_min=30, block_after_min=30)
