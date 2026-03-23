#!/usr/bin/env python3
# =============================================================================
# ALPHA-QUANT · watchdog.py
# Sistema de monitorização e auto-restart
#
# Lógica:
#   · Corre num processo separado do sistema principal
#   · Monitoriza um ficheiro heartbeat actualizado pelo orquestrador
#   · Se o heartbeat parar → reinicia o sistema automaticamente
#   · Envia alerta WhatsApp em caso de crash ou restart
#   · Regista todos os crashes com stack trace para análise
#   · Limita restarts (max 5 em 1h) para evitar loops infinitos
# =============================================================================

import json
import logging
import os
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

HEARTBEAT_FILE  = Path("watchdog_heartbeat.json")
WATCHDOG_LOG    = Path("logs/watchdog.log")
MAX_RESTARTS_PER_HOUR = 5
CHECK_INTERVAL  = 30   # segundos entre verificações
HEARTBEAT_TTL   = 120  # segundos sem heartbeat = sistema morto


class Watchdog:
    """
    Processo independente que vigia o sistema principal.
    Deve ser iniciado separadamente: python watchdog.py
    """

    def __init__(self):
        self._restarts: list[float] = []
        self._running = True
        self._process: subprocess.Popen = None

    # ─── ARRANQUE ─────────────────────────────────────────────────────────────

    def start(self, script: str = "run.py", args: list = None):
        """
        Arranca o watchdog. Inicia o sistema e monitoriza-o.
        """
        os.makedirs("logs", exist_ok=True)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-8s | WATCHDOG | %(message)s",
            datefmt="%H:%M:%S",
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler(WATCHDOG_LOG, encoding="utf-8"),
            ]
        )

        logger.info("="*50)
        logger.info("  ALPHA-QUANT WATCHDOG INICIADO")
        logger.info("="*50)
        logger.info(f"Script: {script}")
        logger.info(f"Heartbeat TTL: {HEARTBEAT_TTL}s")
        logger.info(f"Max restarts/hora: {MAX_RESTARTS_PER_HOUR}")

        self._send_whatsapp("Watchdog iniciado — sistema Alpha-Quant a arrancar")

        cmd = [sys.executable, script] + (args or [])
        self._start_process(cmd)

        try:
            self._monitor_loop(cmd)
        except KeyboardInterrupt:
            logger.info("Watchdog a terminar (Ctrl+C)")
            self._stop_process()
            self._send_whatsapp("Watchdog terminado pelo utilizador")

    def _start_process(self, cmd: list):
        """Inicia o processo do sistema principal."""
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            logger.info(f"Processo iniciado: PID {self._process.pid}")

            # Thread para capturar output do processo filho
            threading.Thread(
                target=self._capture_output,
                daemon=True
            ).start()

        except Exception as e:
            logger.error(f"Falha ao iniciar processo: {e}")

    def _stop_process(self):
        """Para o processo principal graciosamente."""
        if self._process and self._process.poll() is None:
            logger.info("A terminar processo principal...")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def _capture_output(self):
        """Captura e regista o output do processo filho."""
        if not self._process:
            return
        for line in iter(self._process.stdout.readline, ""):
            if line.strip():
                print(f"[SYS] {line.rstrip()}")

    # ─── LOOP DE MONITORIZAÇÃO ────────────────────────────────────────────────

    def _monitor_loop(self, cmd: list):
        """Loop principal de monitorização."""
        while self._running:
            time.sleep(CHECK_INTERVAL)

            if not self._running:
                break

            # Verifica se o processo ainda está vivo
            if self._process and self._process.poll() is not None:
                exit_code = self._process.returncode
                logger.error(f"Processo terminou inesperadamente! Exit code: {exit_code}")
                self._handle_crash(cmd, exit_code)
                continue

            # Verifica o heartbeat
            hb_status = self._check_heartbeat()
            if not hb_status["alive"]:
                logger.error(f"Heartbeat morto: {hb_status['reason']}")
                self._stop_process()
                self._handle_crash(cmd, -1, reason=hb_status["reason"])

    def _handle_crash(self, cmd: list, exit_code: int, reason: str = ""):
        """Gere um crash — decide se reinicia ou desiste."""
        now = time.time()

        # Limpa restarts com mais de 1 hora
        self._restarts = [t for t in self._restarts if now - t < 3600]

        if len(self._restarts) >= MAX_RESTARTS_PER_HOUR:
            msg = (
                f"SISTEMA DESACTIVADO — {MAX_RESTARTS_PER_HOUR} restarts "
                f"na última hora. Intervenção manual necessária."
            )
            logger.critical(msg)
            self._send_whatsapp(f"ALERTA CRÍTICO: {msg}")
            self._running = False
            return

        self._restarts.append(now)
        restart_num = len(self._restarts)

        msg = (
            f"Crash detectado (exit={exit_code}). "
            f"A reiniciar ({restart_num}/{MAX_RESTARTS_PER_HOUR})... "
            f"{reason or ''}"
        )
        logger.warning(msg)
        self._send_whatsapp(f"⚠ {msg}")

        # Espera antes de reiniciar (backoff exponencial)
        wait = min(30 * (2 ** (restart_num - 1)), 300)
        logger.info(f"A aguardar {wait}s antes de reiniciar...")
        time.sleep(wait)

        logger.info("A reiniciar o sistema...")
        self._start_process(cmd)
        self._send_whatsapp(f"✓ Sistema reiniciado (tentativa {restart_num})")

    # ─── HEARTBEAT ────────────────────────────────────────────────────────────

    def _check_heartbeat(self) -> dict:
        """Verifica se o sistema principal está a enviar heartbeat."""
        if not HEARTBEAT_FILE.exists():
            return {"alive": False, "reason": "ficheiro heartbeat não encontrado"}

        try:
            data = json.loads(HEARTBEAT_FILE.read_text())
            last_beat = data.get("timestamp", 0)
            age = time.time() - last_beat

            if age > HEARTBEAT_TTL:
                return {
                    "alive": False,
                    "reason": f"último heartbeat há {age:.0f}s (limite {HEARTBEAT_TTL}s)"
                }

            return {"alive": True, "age_seconds": age}

        except Exception as e:
            return {"alive": False, "reason": f"erro a ler heartbeat: {e}"}

    # ─── WHATSAPP ─────────────────────────────────────────────────────────────

    def _send_whatsapp(self, message: str):
        """Envia alerta WhatsApp via Twilio (se configurado)."""
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        sid    = os.getenv("TWILIO_ACCOUNT_SID", "")
        token  = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_n = os.getenv("WHATSAPP_FROM", "whatsapp:+14155238886")
        to_n   = os.getenv("WHATSAPP_TO", "")

        if not sid or not token or not to_n:
            return

        try:
            import urllib.request, urllib.parse, base64
            url  = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
            body = urllib.parse.urlencode({
                "From": from_n,
                "To":   to_n,
                "Body": f"Alpha-Quant Watchdog\n{message}",
            }).encode()
            credentials = base64.b64encode(f"{sid}:{token}".encode()).decode()
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type":  "application/x-www-form-urlencoded",
            })
            urllib.request.urlopen(req, timeout=8)
        except Exception as e:
            logger.debug(f"WhatsApp falhou: {e}")


# =============================================================================
# HEARTBEAT WRITER — usado pelo orquestrador
# =============================================================================

class HeartbeatWriter:
    """
    Mixin para o orquestrador — escreve heartbeat regularmente.
    O watchdog lê este ficheiro para confirmar que o sistema está vivo.
    """

    def __init__(self):
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_running = False

    def start_heartbeat(self):
        """Inicia a thread de heartbeat."""
        self._hb_running = True
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="heartbeat",
        )
        self._hb_thread.start()
        logger.info("Heartbeat writer iniciado")

    def stop_heartbeat(self):
        """Para a thread de heartbeat."""
        self._hb_running = False

    def _heartbeat_loop(self):
        """Escreve heartbeat a cada 30 segundos."""
        while self._hb_running:
            try:
                data = {
                    "timestamp": time.time(),
                    "time_utc": datetime.now(tz=timezone.utc).isoformat(),
                    "pid": os.getpid(),
                }
                HEARTBEAT_FILE.write_text(json.dumps(data))
            except Exception as e:
                logger.warning(f"Heartbeat write falhou: {e}")
            time.sleep(30)


# =============================================================================
# SISTEMA DE HEALTH CHECK
# =============================================================================

class SystemHealthChecker:
    """
    Verifica a saúde de todos os componentes do sistema.
    Usado pelo watchdog e pela dashboard.
    """

    @staticmethod
    def check_all() -> dict:
        """Verifica todos os componentes."""
        results = {}

        # MT5
        results["mt5"] = SystemHealthChecker._check_mt5()

        # Claude API
        results["claude_api"] = SystemHealthChecker._check_anthropic()

        # Base de dados
        results["database"] = SystemHealthChecker._check_database()

        # Memória do sistema
        results["memory"] = SystemHealthChecker._check_memory()

        # Disco
        results["disk"] = SystemHealthChecker._check_disk()

        # Heartbeat
        results["heartbeat"] = SystemHealthChecker._check_heartbeat_file()

        # Status global
        critical_fail = any(
            v.get("status") == "ERROR"
            for k, v in results.items()
            if k in ("mt5", "database")
        )
        results["overall"] = "ERROR" if critical_fail else "OK"

        return results

    @staticmethod
    def _check_mt5() -> dict:
        try:
            import MetaTrader5 as mt5
            if mt5.terminal_info() is not None:
                return {"status": "OK", "detail": "Terminal conectado"}
            return {"status": "WARNING", "detail": "Terminal não conectado"}
        except ImportError:
            return {"status": "SIMULATION", "detail": "MT5 não instalado — modo demo"}
        except Exception as e:
            return {"status": "ERROR", "detail": str(e)}

    @staticmethod
    def _check_anthropic() -> dict:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return {"status": "SIMULATION", "detail": "API key não configurada"}
        try:
            import anthropic
            return {"status": "OK", "detail": "API key configurada"}
        except ImportError:
            return {"status": "WARNING", "detail": "anthropic não instalado"}

    @staticmethod
    def _check_database() -> dict:
        try:
            import sqlite3
            conn = sqlite3.connect("alphaquant.db", timeout=3)
            conn.execute("SELECT COUNT(*) FROM trades")
            count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            conn.close()
            return {"status": "OK", "detail": f"{count} trades registados"}
        except Exception as e:
            return {"status": "ERROR", "detail": str(e)}

    @staticmethod
    def _check_memory() -> dict:
        try:
            import psutil
            mem = psutil.virtual_memory()
            pct = mem.percent
            status = "OK" if pct < 80 else ("WARNING" if pct < 90 else "ERROR")
            return {"status": status, "detail": f"{pct:.1f}% usado"}
        except ImportError:
            return {"status": "UNKNOWN", "detail": "psutil não instalado"}

    @staticmethod
    def _check_disk() -> dict:
        try:
            import shutil
            total, used, free = shutil.disk_usage(".")
            free_gb = free / (1024**3)
            status = "OK" if free_gb > 1 else ("WARNING" if free_gb > 0.5 else "ERROR")
            return {"status": status, "detail": f"{free_gb:.1f} GB livres"}
        except Exception as e:
            return {"status": "UNKNOWN", "detail": str(e)}

    @staticmethod
    def _check_heartbeat_file() -> dict:
        if not HEARTBEAT_FILE.exists():
            return {"status": "WARNING", "detail": "Sem heartbeat (sistema ainda não iniciado?)"}
        try:
            data = json.loads(HEARTBEAT_FILE.read_text())
            age  = time.time() - data.get("timestamp", 0)
            if age > HEARTBEAT_TTL:
                return {"status": "ERROR", "detail": f"Último heartbeat há {age:.0f}s"}
            return {"status": "OK", "detail": f"Último heartbeat há {age:.0f}s"}
        except Exception as e:
            return {"status": "ERROR", "detail": str(e)}


# Importação opcional usada em SystemHealthChecker
from typing import Optional


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Alpha-Quant Watchdog")
    parser.add_argument("--script", default="run.py", help="Script a vigiar")
    parser.add_argument("--args",   nargs="*", default=["--demo"], help="Argumentos")
    args = parser.parse_args()

    wd = Watchdog()
    wd.start(script=args.script, args=args.args)
