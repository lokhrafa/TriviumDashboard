#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MOTOR DE SWING TRADING — EMA Pullback + RSI + MACD (Multi-Timeframe)
Un solo motor, instanciado una vez por par vía TradingEngine(config).

Antes esta lógica vivía duplicada en 4 archivos de ~1550 líneas
(audusd/eurusd/gbpusd/nzdusd_trading_system.py) que solo diferían en ~60
líneas (símbolo, archivo de log, filtros de noticias, notas de contexto).
Consecuencia práctica de la duplicación: cualquier corrección había que
aplicarla y verificarla 4 veces, y backtest.py solo importaba el módulo de
AUD/USD -- lo que se cambiara en los otros 3 nunca se backtesteaba.

Ahora los 4 archivos *_trading_system.py son lanzadores CLI de ~10 líneas
(ver el final de este módulo) y dashboard.py / backtest.py instancian
TradingEngine directamente con la config de cada par (pair_configs.py).

Librería TradingView: tvm_client (cliente propio, no oficial, solo lectura --
ver tvm_client.py; reemplazó a tradingview-ta porque esa librería no exponía
ATR ni BB.basis, ver fetch_analysis más abajo).
"""

import sys
import os

# Forzar UTF-8 en Windows (necesario para caracteres de cuadros y emojis)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    # Habilitar ANSI nativo en consola Windows 10/11
    os.system("")

from tvm_client import TA_Handler, Interval
from colorama import Fore, Style, init
from tabulate import tabulate
from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo
import urllib.request
import json
import csv
import math
import time
import random

from pair_configs import PairConfig

init(autoreset=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Datos "en vivo" (señales, histórico de score/OHLC, decisiones bloqueadas) --
# separados de los .py y de backtest_data/ (velas históricas para backtest)
# para que la raíz del proyecto no se llene de CSV. Creada si no existe para
# que un checkout limpio no falle al escribir la primera señal.
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# Pivotes clásicos mensuales que expone TradingView (niveles de estructura) —
# idénticos para los 4 pares, no depende de la config.
PIVOT_KEYS = [
    "Pivot.M.Classic.S3", "Pivot.M.Classic.S2", "Pivot.M.Classic.S1",
    "Pivot.M.Classic.Middle",
    "Pivot.M.Classic.R1", "Pivot.M.Classic.R2", "Pivot.M.Classic.R3",
]

# Columnas del CSV. Al cerrar un trade, además de resultado (WIN/LOSS/BE) se
# anotan: fecha_cierre, pips_resultado (+/-) y motivo_cierre (TP/SL/5DIAS/MANUAL)
#
# pips_tp1_parcial: pips ganados en el 50% de la posición que se cerró al
# tocar TP1 (ver record_tp1_partial), anotado ANTES del cierre final -- la
# fila sigue en resultado=ACTIVA. Cuando está presente, pips_resultado del
# cierre final describe solo el OTRO 50% (el que queda corriendo tras mover
# el SL a breakeven), no el trade completo: un trade que llega a TP1 y luego
# se detiene en breakeven no es "BE" a efectos de P&L -- ya hay una ganancia
# real bloqueada en la primera mitad que un solo pips_resultado no puede
# expresar. compute_pnl (dashboard.py) pondera ambas mitades al 50/50.
#
# mfe_* / mae_* = recorrido de la posición mientras estuvo abierta, lo rellena
# TradingEngine.update_trade_tracking una vez por ciclo (ver
# dashboard.build_pair_state). MFE = Maximum Favorable Excursion (mejor precio
# a favor que llegó a alcanzarse), MAE = Maximum Adverse Excursion (peor en
# contra). Solo se mueven en su sentido, nunca retroceden: así una mecha
# puntual al TP1 queda registrada aunque el precio vuelva y el trade cierre
# después en BE o LOSS -- antes ese toque no dejaba ningún rastro.
CSV_HEADER = ["fecha", "hora", "direccion", "entrada", "tipo_entrada",
              "stop_loss", "tp1", "take_profit", "pips_sl", "lotes", "score",
              "riesgo_usd", "riesgo_usd_real", "ganancia_potencial_usd", "precio_senal",
              "resultado", "fecha_cierre", "pips_resultado", "motivo_cierre",
              "pips_tp1_parcial",
              "mfe_precio", "mfe_pips", "mae_precio", "mae_pips"]

# (nombre, zona horaria, hora local de apertura, hora local de cierre)
TRADING_SESSIONS = [
    ("Sídney",     "Australia/Sydney", 7, 16),
    ("Tokio",      "Asia/Tokyo",       9, 18),
    ("Londres",    "Europe/London",    8, 17),
    ("Nueva York", "America/New_York", 8, 17),
]


# ══════════════════════════════════════════════════════════════════
#  FUNCIONES AUXILIARES SIN ESTADO (no dependen del par -- compartidas)
# ══════════════════════════════════════════════════════════════════

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def _safe_float(value, default: float = 0.0) -> float:
    """Convierte a float sin lanzar excepción (celdas vacías o corruptas del CSV)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_upper(value) -> str:
    """.upper() sin lanzar excepción cuando el valor es None -- csv.DictReader
    rellena con None (no con "") las columnas que faltan en una fila más corta
    que el header (CSV editado a mano y guardado con una columna de menos).
    row.get("campo", "") NO cubre ese caso: la clave existe, así que .get()
    devuelve ese None en vez del default."""
    return (value or "").upper()


def _is_valid_order_row(row: dict) -> bool:
    """
    Fila de orden PENDIENTE/ACTIVA con los campos mínimos usables: niveles
    numéricos positivos y fecha/hora/dirección presentes. Compartida por
    get_pending_order (que lee) y update_pending_order/close_trade (que
    escriben) -- antes cada una decidía la validez por su cuenta: si la fila
    más reciente estaba corrupta, get_pending_order la saltaba y devolvía una
    fila anterior, pero update_pending_order/close_trade seguían escribiendo
    en la fila corrupta más reciente -- el dashboard mostraba una orden y el
    botón WIN/LOSS modificaba otra.
    """
    return not (_safe_float(row.get("entrada"), -1) <= 0
                or _safe_float(row.get("stop_loss"), -1) <= 0
                or _safe_float(row.get("take_profit"), -1) <= 0
                or not row.get("fecha")
                or not row.get("hora")
                or row.get("direccion") not in ("LONG", "SHORT"))


def _trading_days_since(start_date, today=None) -> int:
    """
    Cuenta días DE TRADING (lunes a viernes) transcurridos desde start_date
    hasta hoy. El mercado forex está cerrado el sábado y la mayor parte del
    domingo (reabre 17:00 NY) -- esas horas no deberían contar para reglas
    basadas en "días" como la de los 5 días o el vencimiento de órdenes
    pendientes, o se dispararían antes de lo previsto solo por un fin de
    semana de por medio. Coherente con el backtest: sus datos históricos solo
    tenían velas de lunes a viernes, así que el "5 días" ahí validado ya era,
    de hecho, días de trading, no días calendario.
    """
    today = today or datetime.now().date()
    days = 0
    d = start_date
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:   # lunes=0 ... viernes=4
            days += 1
    return days


def _beep(times: int = 3):
    """Alerta sonora en la consola."""
    for _ in range(times):
        print("\a", end="", flush=True)
        time.sleep(0.4)


def _write_csv_atomic(path: str, fieldnames: list, rows: list):
    """Escribe el CSV en un temporal y lo reemplaza — el historial nunca queda a medias."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def check_market_session(now_utc: datetime | None = None) -> dict:
    """
    Estado del mercado Forex en hora de Nueva York (ajusta sola verano/invierno):
      - Cerrado: viernes 17:00 NY → domingo 17:00 NY (datos congelados, no analizar)
      - Precaución (regla: no operar lunes en la apertura ni viernes al cierre):
        últimas 2h antes del cierre del viernes y primeras 3h tras la apertura del domingo
    Antes usaba una hora UTC fija (22:00) que solo era correcta en invierno NY;
    en verano (NY = UTC-4) el cierre real cae una hora antes, a las 21:00 UTC.
    Pair-independiente: el calendario del mercado es el mismo para los 4 pares.
    """
    try:
        NY = ZoneInfo("America/New_York")
    except Exception:
        NY = timezone(timedelta(hours=-4))   # sin tzdata: aproximar (horario de verano NY)

    now = (now_utc or datetime.now(timezone.utc)).astimezone(NY)
    wd, hour = now.weekday(), now.hour   # lunes=0 ... domingo=6

    # Próxima reapertura (domingo 17:00 NY) — para mostrar en el aviso de cierre
    days_ahead = (6 - wd) % 7
    reopen = (now + timedelta(days=days_ahead)).replace(hour=17, minute=0, second=0, microsecond=0)
    if reopen < now:
        reopen += timedelta(days=7)
    reopen_local = reopen.astimezone().strftime("%A %H:%M")

    if (wd == 4 and hour >= 17) or wd == 5 or (wd == 6 and hour < 17):
        return {"open": False, "block_new": True, "reopen_local": reopen_local,
                "reason": "Mercado Forex CERRADO (fin de semana) — cerrado desde el viernes 17:00 hora NY"}
    if wd == 4 and hour >= 15:
        return {"open": True, "block_new": True, "reopen_local": "",
                "reason": "Cierre del viernes (últimas 2h) — regla: no abrir operaciones nuevas"}
    if wd == 6 and hour < 20:
        return {"open": True, "block_new": True, "reopen_local": "",
                "reason": "Apertura del domingo — regla: esperar a que el mercado defina dirección"}
    return {"open": True, "block_new": False, "reopen_local": "", "reason": ""}


def _easter_sunday(year: int) -> date:
    """Domingo de Pascua (algoritmo de Meeus/Jones/Butcher, calendario
    gregoriano) -- necesario porque el Good Friday del NYSE es el único
    festivo de fecha móvil. Verificado contra 2025/2026/2027 (20 abr / 5 abr
    / 28 mar, fechas públicas conocidas)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-ésimo `weekday` (0=lunes) del mes."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Último `weekday` del mes."""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = nxt - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _observed(d: date) -> date:
    """Si el festivo cae en fin de semana, el NYSE observa el hábil más
    cercano (sábado -> viernes anterior, domingo -> lunes siguiente)."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nyse_holidays_for_year(year: int) -> set[date]:
    """
    Festivos del NYSE (mercado de ACCIONES) en un año -- día en que SP:SPX no
    genera vela nueva aunque el forex, con su calendario semanal aparte
    (check_market_session), siga abierto. Reglas estándar del NYSE, no una
    lista fija que hay que actualizar cada año: Año Nuevo, MLK (3er lunes de
    enero), Presidents Day (3er lunes de febrero), Good Friday (viernes antes
    de Pascua), Memorial Day (último lunes de mayo), Juneteenth (19 de junio,
    festivo del NYSE desde 2022), Independencia (4 de julio), Labor Day (1er
    lunes de septiembre), Thanksgiving (4º jueves de noviembre) y Navidad.
    """
    easter = _easter_sunday(year)
    days = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        easter - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    return days


_NYSE_HOLIDAY_CACHE: dict[int, set[date]] = {}


def is_us_market_holiday(d: date) -> bool:
    """
    True si el NYSE no opera ese día. Consulta el año de `d` y los dos
    colindantes -- un festivo "observado" puede caer al otro lado de un 31 de
    diciembre. El único caso real es Año Nuevo: si el 1 de enero del año
    SIGUIENTE cae en sábado, se observa el viernes 31 de diciembre de este
    año (ej. Año Nuevo 2028, sábado, observado el 31-dic-2027) -- por eso
    hace falta año+1, no año-1 (verificado con un test que falló en la
    primera versión de esta función, que solo miraba hacia atrás).
    """
    for year in (d.year - 1, d.year, d.year + 1):
        if year not in _NYSE_HOLIDAY_CACHE:
            _NYSE_HOLIDAY_CACHE[year] = _nyse_holidays_for_year(year)
        if d in _NYSE_HOLIDAY_CACHE[year]:
            return True
    return False


def active_sessions(now_utc: datetime | None = None) -> str:
    """
    Sesiones de trading activas ahora, calculadas en la hora LOCAL de cada plaza
    (Sídney/Londres/NY ajustan solas su horario de verano; Tokio no tiene DST).
    Pair-independiente.
    """
    now = now_utc or datetime.now(timezone.utc)
    ses = []
    for name, tz_name, start_h, end_h in TRADING_SESSIONS:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            continue   # sin tzdata: omitir esa plaza en vez de adivinar mal
        local_hour = now.astimezone(tz).hour
        if start_h <= local_hour < end_h:
            ses.append(name)
    return " + ".join(ses) if ses else "Entre sesiones"


def fetch_news_calendar() -> list | None:
    """
    Descarga el calendario semanal de Forex Factory -- pair-independiente
    (mismo JSON para los 4 pares). None si falla la red; el llamador decide
    qué hacer (TradingEngine.filter_news_alerts trata None como "sin
    noticias", no bloquea el sistema por un fallo de red).
    """
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
#  MOTOR — UNA INSTANCIA POR PAR
# ══════════════════════════════════════════════════════════════════

class TradingEngine:
    """
    Motor de señales para un par de divisas. Instanciar con la PairConfig del
    par (ver pair_configs.py); todo lo que sigue era antes 4 copias casi
    idénticas de estas mismas constantes y funciones.
    """

    # ── Parámetros de riesgo/tiempo -- idénticos en los 4 pares actuales.
    # Constantes de CLASE (no de instancia) para que un futuro par que
    # necesite un valor distinto pueda sobreescribirlas sin duplicar el motor.
    RISK_PCT         = 1.0        # % de riesgo por operación
    MIN_RR           = 1.2        # R:B mínimo garantizado al TP final (TP2) — filtro real
    TP1_R            = 1.0        # TP1: cerrar 50% de la posición a este múltiplo de R
    FALLBACK_RR      = 1.5        # TP2 fijo si no hay nivel técnico disponible
    # Backtest 2026-07-11 (IDEALPRO, 2024-04/2026-07): vs TP1=1.5R/MIN_RR=2.0 original
    # (43% WR, +$251, PF 1.79), este punto medio da ~52% WR, +$211-230, PF 1.53-1.66
    # — más trades ganadores, algo menos de ganancia total. Elegido por el usuario
    # deliberadamente sobre la variante de máxima expectancia.
    TP_LEVEL_BUFFER  = 10         # Pips antes del nivel técnico para colocar el TP2
    # Puntuación mínima para señal válida. Recalibrado 2026-08-08 al fusionar
    # el score (ver plan Item 9): el máximo teórico bajó de 100 a 75 (se quitó
    # el bonus de ADX de +10 y se fusionaron las dos secciones de tendencia de
    # 30+25=55 a 40 pts). Dejar MIN_SCORE=60 sin cambios con el nuevo máximo
    # de 75 exigía 80% de los puntos en vez de 60% -- probado en barrido sobre
    # los 6 pares (backtest, 2024-04/2026-07): colapsó a 27 trades totales
    # (antes ~150-180), la estrategia casi dejó de operar. Barrido de
    # candidatos (n_trades / win rate / R promedio / P&L neto agregado):
    #   45 -> 128 / 30.5% / +0.19R / +$163   50 -> 95 / 35.8% / +0.28R / +$174
    #   48 -> 108 / 35.2% / +0.26R / +$191   52 -> 88 / 35.2% / +0.25R / +$140
    #   55 ->  76 / 39.5% / +0.31R / +$185   60 -> 27 / 40.7% / +0.35R /  +$80
    # 50 elegido: número redondo dentro de la zona productiva (48-55), sin
    # tomar el punto exacto de mejor $ (48) para no sobreajustar a una
    # diferencia de ruido entre valores vecinos.
    MIN_SCORE        = 50
    # Máximo teórico del score (ver score_direction: 40 tendencia + 20 RSI +
    # 15 MACD). El dashboard y el CLI lo usan para dibujar la barra/umbral a
    # escala real en vez de asumir una escala fija de 100 puntos que ya no
    # es el máximo desde que se fusionó el score (ver comentario de MIN_SCORE).
    MAX_SCORE        = 75
    REFRESH_SECONDS  = 900        # Segundos entre actualizaciones (15 min -- antes 5 min; se subió 2026-07-13
                                  # tras varios rate-limit 429 de TradingView tras un dia de mucho uso/pruebas)
    IDLE_REFRESH_SECONDS = 1800   # Refresco fuera de la ventana de decisión sin órdenes que vigilar (30 min)
    SIGNAL_WINDOW_HOURS  = 4      # Horas tras el cierre diario (17:00 NY) en las que se generan señales nuevas
    MAX_TRADES_DAY   = 2          # Máximo de operaciones por día
    MAX_LOSS_DAY_PCT = 3.0        # Pérdida máxima diaria (%)
    MAX_LOSS_WK_PCT  = 6.0        # Pérdida máxima semanal (%)
    MIN_PIPS_SL      = 30         # Stop Loss mínimo en pips (evitar ruido)
    ATR_MULTIPLIER   = 1.5        # Multiplicador ATR para Stop Loss dinámico
    PULLBACK_BUFFER  = 15         # Pips antes de EMA20 para entrar (pullback puede no llegar exacto)
    PENDING_EXPIRY_DAYS = 5       # Días que espera la orden límite antes de VENCIDA (backtest: 3d daba
                                  # peor P&L/profit factor que 5d — más días da tiempo a un pullback
                                  # real sin ya ser un setup obsoleto; 7d en cambio empeora)
    CAPITAL          = 1_250      # Capital por par en USD (1/4 de $5,000 -- 4 sistemas corriendo a la vez)
    MIN_ADX_TO_TRADE = 22         # ADX diario mínimo para generar CUALQUIER señal, sin importar el
                                  # score (ver plan Item 9 / hallazgo H8). Antes el ADX solo sumaba
                                  # puntos al lado que ya iba ganando -- filtro binario, independiente
                                  # del score y del detector de rango existente (detect_ranging_market,
                                  # que combina ADX+Bollinger+pendiente de EMAs) -- este es un segundo
                                  # control, más simple y directo, no un reemplazo de aquel.

    # Umbrales del detector de mercado lateral (ver detect_ranging_market).
    # Estaban escritos como literales dentro de la función; se sacan aquí con
    # los MISMOS valores para que un instrumento de otra clase de activo pueda
    # sobreescribirlos sin duplicar el detector. No son universales: medido el
    # 2026-08-19, la anchura de Bollinger diaria es 2.6-2.8% en forex pero
    # 8.26% en el S&P 500, y la separación EMA20-EMA50 es de ~22 pips en
    # AUD/USD contra ~115 puntos en el índice. Con los valores de forex, dos
    # de los tres filtros del detector NUNCA se dispararían en un índice y el
    # detector se degradaría en silencio a "solo ADX".
    BB_WIDTH_TIGHT   = 0.010      # anchura Bollinger que cuenta como rango claro (+3)
    BB_WIDTH_NARROW  = 0.015      # ... y como posible rango (+1)
    EMA_GAP_FLAT     = 15         # separación EMA20-EMA50 (en pips/puntos): medias planas (+3)
    EMA_GAP_NEAR     = 30         # ... tendencia débil (+1)
    # Umbrales de ADX del propio detector de rango (distintos de
    # MIN_ADX_TO_TRADE, que es el filtro binario de generate_signal). Se
    # usan también en score_direction para la penalización simétrica -5.
    RANGE_ADX_NO_TREND  = 20      # ADX diario bajo este valor: sin tendencia en ningún TF (+3)
    RANGE_ADX_WEAK      = 25      # ADX diario bajo este valor: tendencia débil (+1) / penalización -5
    RANGE_ADX_H4_STRONG = 30      # ADX 4H igual o sobre este valor: contrapesa el ADX diario bajo

    NEWS_WINDOW_HOURS = 4         # Horas ANTES de una noticia en las que no se abre operación
    NEWS_RECENT_HOURS = 1         # Horas DESPUÉS de una noticia en las que sigue bloqueado operar

    def __init__(self, config: PairConfig):
        self.cfg = config
        self.NAME     = config.name
        self.SYMBOL   = config.symbol
        self.EXCHANGE = config.exchange
        self.PIP_VALUE = config.pip_value
        self.USD_PER_PIP_STANDARD = config.usd_per_pip_standard
        self.UNIT = config.unit_label        # "pips" o "puntos" -- solo etiquetas
        self.NEWS_FILTERS = config.news_filters
        self.MARKET_CONTEXT_NOTES = config.market_context_notes
        self.LOG_FILE = os.path.join(DATA_DIR, config.log_filename)
        self.RISK_USD = self.CAPITAL * (self.RISK_PCT / 100)   # $12.50 por trade (capital ya repartido 1/4)

    def __repr__(self):
        return f"TradingEngine({self.NAME})"

    # ══════════════════════════════════════════════════════════════
    #  DATOS DE MERCADO
    # ══════════════════════════════════════════════════════════════

    def filter_news_alerts(self, events: list | None) -> list[dict]:
        """
        Filtra el calendario YA DESCARGADO (ver fetch_news_calendar, módulo)
        para las noticias de alto impacto relevantes a ESTE par, dentro de la
        ventana de bloqueo. events=None (falló la descarga) -> sin noticias,
        no bloquear el sistema por un fallo de red.
        """
        if not events:
            return []
        try:
            now          = datetime.now(timezone.utc)
            window_start = now - timedelta(hours=self.NEWS_RECENT_HOURS)   # noticias recientes también bloquean
            window_end   = now + timedelta(hours=self.NEWS_WINDOW_HOURS)
            alerts       = []
            seen         = set()   # el calendario suele listar la misma decisión en varias filas (Cash Rate, Rate Statement, Press Conference)

            # La API de Forex Factory devuelve horas en Eastern Time (ET)
            ET = ZoneInfo("America/New_York")

            for event in events:
                # Parsear fecha — asignar timezone ET si viene sin ella
                try:
                    event_dt = datetime.fromisoformat(event["date"])
                    if event_dt.tzinfo is None:
                        event_dt = event_dt.replace(tzinfo=ET)
                except (ValueError, KeyError):
                    continue

                # Noticias próximas (4h) o muy recientes (1h) — ambas bloquean
                if not (window_start <= event_dt <= window_end):
                    continue

                # Solo impacto alto
                if event.get("impact", "") != "High":
                    continue

                title   = event.get("title", "")
                country = event.get("country", "")

                for f in self.NEWS_FILTERS:
                    if country in f["currencies"] and any(kw.lower() in title.lower() for kw in f["keywords"]):
                        dedup_key = (f["label"], event_dt.astimezone().strftime("%H:%M"))
                        if dedup_key in seen:
                            break
                        seen.add(dedup_key)
                        delta_min = int((event_dt - now).total_seconds() // 60)
                        alerts.append({
                            "label":   f["label"],
                            "title":   title,
                            "country": country,
                            "when":    f"en {delta_min} min" if delta_min >= 0 else f"hace {-delta_min} min",
                            "time_utc": event_dt.strftime("%H:%M UTC"),
                            "time_local": event_dt.astimezone().strftime("%H:%M"),
                        })
                        break

            return alerts

        except Exception:
            return []  # Fila de evento corrupta o similar -- no bloquear el sistema

    def fetch_news_alerts(self) -> list[dict]:
        """
        Descarga el calendario de Forex Factory y lo filtra para este par --
        usado por el CLI (main_cycle), que no comparte la descarga entre
        pares. dashboard.py, con los 4 pares en un solo proceso, llama
        fetch_news_calendar() UNA vez por ciclo (módulo, pair-independiente)
        y filter_news_alerts() una vez por par sobre ese mismo resultado --
        antes cada par volvía a descargar el MISMO JSON, 4 peticiones
        idénticas por ciclo sin espaciado (fuera de _spaced_call).
        """
        return self.filter_news_alerts(fetch_news_calendar())

    def fetch_analysis(self, interval_label: str, interval_const) -> dict | None:
        """Obtiene análisis técnico de TradingView. Reintenta hasta 3 veces ante error 429."""
        for attempt in range(3):
            try:
                handler = TA_Handler(
                    symbol=self.SYMBOL,
                    screener=self.cfg.screener,
                    exchange=self.EXCHANGE,
                    interval=interval_const,
                    timeout=15,
                )
                a = handler.get_analysis()
                ind = a.indicators
                return {
                    "label":         interval_label,
                    "rec":           a.summary["RECOMMENDATION"],
                    "buy_count":     a.summary.get("BUY", 0),
                    "sell_count":    a.summary.get("SELL", 0),
                    "neutral_count": a.summary.get("NEUTRAL", 0),
                    "close":         ind.get("close", 0),
                    # high/low de la vela EN CURSO del intervalo pedido -- en la
                    # diaria acumulan el rango del día hasta ahora. Sirven para
                    # detectar que el precio TOCÓ un nivel (TP1/TP2/SL) entre dos
                    # ciclos de 15 min aunque el close ya haya retrocedido: con
                    # solo close, una mecha rápida al TP1 no dejaba rastro.
                    "high":          ind.get("high", ind.get("close", 0)),
                    "low":           ind.get("low", ind.get("close", 0)),
                    "rsi":           ind.get("RSI", 50),
                    "macd":          ind.get("MACD.macd", 0),
                    "macd_signal":   ind.get("MACD.signal", 0),
                    "ema20":         ind.get("EMA20", 0),
                    "ema50":         ind.get("EMA50", 0),
                    "ema200":        ind.get("EMA200", 0),
                    "atr":           ind.get("ATR", 0.0030),
                    "bb_upper":      ind.get("BB.upper", 0),
                    "bb_lower":      ind.get("BB.lower", 0),
                    "bb_middle":     ind.get("BB.basis", 0),
                    "stoch_k":       ind.get("Stoch.K", 50),
                    "stoch_d":       ind.get("Stoch.D", 50),
                    "adx":           ind.get("ADX", 0),
                    "pivots":        [ind.get(k, 0) for k in PIVOT_KEYS],
                }
            except Exception as e:
                msg = str(e)
                if "429" in msg and attempt < 2:
                    wait = 30 * (attempt + 1)  # 30s, luego 60s
                    print(f"{Fore.YELLOW}  Rate limit en {interval_label} — esperando {wait}s antes de reintentar...{Style.RESET_ALL}")
                    time.sleep(wait)
                else:
                    print(f"{Fore.RED}  Error al obtener datos ({interval_label}): {e}{Style.RESET_ALL}")
                    return None

    def fetch_regime_index(self) -> dict | None:
        """
        Recomendación diaria del ÍNDICE DE RÉGIMEN de este instrumento, que es
        lo que consume el filtro de correlación de score_direction():
          - Pares de divisas -> DXY (índice del dólar).
          - S&P 500          -> VIX (índice de volatilidad).
        Un instrumento con regime_symbol=None no usa el filtro.

        No depende de qué instrumento lo pida, solo de la config: instrumentos
        que comparten índice comparten también la consulta (ver dashboard.py,
        que la hace una sola vez por índice y ciclo). No es crítico: si falla
        la consulta, el sistema sigue sin este filtro.
        """
        if not self.cfg.regime_symbol:
            return None
        try:
            handler = TA_Handler(symbol=self.cfg.regime_symbol,
                                 screener=self.cfg.regime_screener,
                                 exchange=self.cfg.regime_exchange,
                                 interval=Interval.INTERVAL_1_DAY, timeout=15)
            a = handler.get_analysis()
            return {"rec": a.summary["RECOMMENDATION"]}
        except Exception:
            return None

    # Nombre anterior, de cuando el único índice de régimen posible era el
    # dólar. Se conserva para no romper llamadas externas.
    fetch_usd_index = fetch_regime_index

    # ══════════════════════════════════════════════════════════════
    #  TAMAÑO DE POSICIÓN
    # ══════════════════════════════════════════════════════════════

    def calc_position_size(self, entry: float, stop_loss: float, reward_r: float | None = None) -> dict:
        """
        Calcula el tamaño de lote basado en capital, riesgo % y el valor del pip.
        reward_r = múltiplo de R esperado (para mostrar la ganancia potencial).

        risk_usd (nominal) es SIEMPRE self.RISK_USD -- el objetivo de riesgo.
        risk_usd_real es lotes×pips_sl×valor_pip -- lo que el redondeo de lotes
        realmente arriesga, que puede quedar hasta ~50% por debajo del nominal
        con este tamaño de cuenta (ver plan, hallazgo H2/H3). Se exponen ambos:
        quien consuma este dict decide cuál usar para medir riesgo real.
        """
        if reward_r is None:
            reward_r = self.MIN_RR
        pips_at_risk = abs(entry - stop_loss) / self.PIP_VALUE
        if pips_at_risk <= 0:
            pips_at_risk = self.MIN_PIPS_SL
        # floor, no round: redondear al alza pondría el riesgo por encima del 1%
        lots = math.floor(self.RISK_USD / (pips_at_risk * self.USD_PER_PIP_STANDARD) * 100) / 100
        risk_usd_real = lots * pips_at_risk * self.USD_PER_PIP_STANDARD
        potential_gain = self.RISK_USD * reward_r   # nominal, igual que el sistema original
        return {
            "lots":          lots,
            "mini_lots":     round(lots * 10, 1),
            "micro_lots":    round(lots * 100, 0),
            "pips_sl":       round(pips_at_risk, 1),
            "risk_usd":      self.RISK_USD,          # nominal (objetivo, 1% de CAPITAL)
            "risk_usd_real": round(risk_usd_real, 2),  # real, tras redondeo de lotes
            "gain_usd":      round(potential_gain, 2),
        }

    # ══════════════════════════════════════════════════════════════
    #  MOTOR DE SEÑALES — ESTRATEGIA EMA PULLBACK + RSI + MACD
    # ══════════════════════════════════════════════════════════════

    def score_direction(self, weekly: dict, daily: dict, h4: dict,
                        dxy: dict | None = None) -> tuple[int, int, list, list]:
        """
        Puntúa la dirección alcista y bajista usando:
          1. TENDENCIA (40 pts: 25 alineación multi-timeframe + 15 posición
             precio vs EMAs) -- ver plan, hallazgo H7 y plan Item 9. Antes
             eran dos secciones separadas de 30+25=55 pts que en la práctica
             medían casi lo mismo (¿hay tendencia?), contando la misma
             evidencia dos veces. Fusionadas en un solo factor, con menos
             peso combinado que la suma original.
          2. RSI diario (20 pts) -- sin cambios
          3. MACD diario (15 pts) -- sin cambios
          4. Filtro de correlación DXY: penaliza -10/-20 pts si el dólar contradice
        El ADX ya NO puntúa aquí (antes sumaba 10 pts SOLO al lado que ya
        iba ganando, sin aportar información nueva sobre si conviene operar
        -- ver plan, hallazgo H8). Ahora es un filtro binario independiente
        en generate_signal() (self.MIN_ADX_TO_TRADE): con tendencia débil,
        NINGUNA dirección genera señal, sin importar el score.
        """
        score_buy  = 0
        score_sell = 0
        reasons_b  = []
        reasons_s  = []

        # Cada categoría añade SIEMPRE una línea a reasons_b/reasons_s, incluso
        # cuando aporta +0 pts -- para que el desglose del dashboard explique
        # no solo de dónde vienen los puntos ganados sino también qué le falta
        # al score para llegar a MIN_SCORE (antes las ramas en 0 pts se
        # quedaban mudas y "por qué tiene tan pocos puntos" no se podía
        # responder sin leer este código).

        # ── 1. TENDENCIA (40 pts: 25 alineación MTF + 15 posición vs EMAs) ──
        tfs = [weekly["rec"], daily["rec"], h4["rec"]]
        buys_tf  = sum(1 for r in tfs if "BUY"  in r)
        sells_tf = sum(1 for r in tfs if "SELL" in r)

        if buys_tf == 3:
            score_buy += 25
            reasons_b.append("[+25/25] Tendencia ALCISTA en los 3 timeframes (W/D/4H)")
        elif buys_tf == 2:
            score_buy += 17
            reasons_b.append(f"[+17/25] Tendencia alcista en {buys_tf}/3 timeframes")
        else:
            reasons_b.append(f"[+0/25] Solo {buys_tf}/3 timeframes alcistas (hacen falta 2 o más)")
        if sells_tf == 3:
            score_sell += 25
            reasons_s.append("[+25/25] Tendencia BAJISTA en los 3 timeframes (W/D/4H)")
        elif sells_tf == 2:
            score_sell += 17
            reasons_s.append(f"[+17/25] Tendencia bajista en {sells_tf}/3 timeframes")
        else:
            reasons_s.append(f"[+0/25] Solo {sells_tf}/3 timeframes bajistas (hacen falta 2 o más)")

        close  = daily["close"]
        ema20  = daily["ema20"]
        ema50  = daily["ema50"]
        ema200 = daily["ema200"]

        if close > ema20 > ema50 > ema200:
            score_buy += 15
            reasons_b.append("[+15/15] Precio > EMA20 > EMA50 > EMA200 (alineación alcista perfecta)")
        elif close > ema50 > ema200:
            score_buy += 11
            reasons_b.append("[+11/15] Precio > EMA50 > EMA200 — tendencia alcista activa")
        elif close > ema200 and close > ema50:
            score_buy += 6
            reasons_b.append("[+6/15] Precio sobre EMA200 — zona alcista de largo plazo")
        elif close > ema20 > ema50:
            score_buy += 3
            reasons_b.append("[+3/15] Precio > EMA20 > EMA50 — tendencia alcista de corto plazo (aún bajo EMA200)")
        else:
            reasons_b.append("[+0/15] Precio no está alineado sobre las EMAs (20/50/200)")

        if close < ema20 < ema50 < ema200:
            score_sell += 15
            reasons_s.append("[+15/15] Precio < EMA20 < EMA50 < EMA200 (alineación bajista perfecta)")
        elif close < ema50 < ema200:
            score_sell += 11
            reasons_s.append("[+11/15] Precio < EMA50 < EMA200 — tendencia bajista activa")
        elif close < ema200 and close < ema50:
            score_sell += 6
            reasons_s.append("[+6/15] Precio bajo EMA200 — zona bajista de largo plazo")
        elif close < ema20 < ema50:
            score_sell += 3
            reasons_s.append("[+3/15] Precio < EMA20 < EMA50 — tendencia bajista de corto plazo (aún sobre EMA200)")
        else:
            reasons_s.append("[+0/15] Precio no está alineado bajo las EMAs (20/50/200)")

        # ── 2. RSI DIARIO (20 pts) ────────────────────────────────────
        # BUY y SELL se evalúan en bloques separados: RSI 45-55 puede
        # aportar puntos a AMBAS direcciones (zona neutral, dos opciones).
        rsi = daily["rsi"]
        if 35 <= rsi <= 55:
            score_buy += 20
            reasons_b.append(f"[+20/20] RSI diario en zona ideal de compra en pullback ({rsi:.1f})")
        elif 20 <= rsi < 35:
            score_buy += 10
            reasons_b.append(f"[+10/20] RSI sobrevendido — posible rebote ({rsi:.1f})")
        else:
            reasons_b.append(f"[+0/20] RSI diario ({rsi:.1f}) fuera de zona de compra (ideal 35-55)")

        if 45 <= rsi <= 65:
            score_sell += 20
            reasons_s.append(f"[+20/20] RSI diario en zona ideal de venta en pullback ({rsi:.1f})")
        elif rsi > 75:
            score_sell += 12
            reasons_s.append(f"[+12/20] RSI sobrecomprado — presión vendedora ({rsi:.1f})")
        else:
            reasons_s.append(f"[+0/20] RSI diario ({rsi:.1f}) fuera de zona de venta (ideal 45-65)")

        # ── 3. MACD DIARIO (15 pts) ───────────────────────────────────
        macd  = daily["macd"]
        macd_sig = daily["macd_signal"]
        histogram = macd - macd_sig

        if macd > macd_sig and macd > 0:
            score_buy += 15
            reasons_b.append(f"[+15/15] MACD alcista sobre cero y sobre señal (hist: {histogram:.5f})")
        elif macd > macd_sig and macd <= 0:
            score_buy += 8
            reasons_b.append(f"[+8/15] MACD cruzando al alza (aún bajo cero) (hist: {histogram:.5f})")
        else:
            reasons_b.append(f"[+0/15] MACD no está en cruce alcista (hist: {histogram:.5f})")
        if macd < macd_sig and macd < 0:
            score_sell += 15
            reasons_s.append(f"[+15/15] MACD bajista bajo cero y bajo señal (hist: {histogram:.5f})")
        elif macd < macd_sig and macd >= 0:
            score_sell += 8
            reasons_s.append(f"[+8/15] MACD cruzando a la baja (aún sobre cero) (hist: {histogram:.5f})")
        else:
            reasons_s.append(f"[+0/15] MACD no está en cruce bajista (hist: {histogram:.5f})")

        # ── ADX: ya no suma puntos (ver docstring) -- solo un descuento
        # simétrico (no premia a nadie) cuando la tendencia diaria es débil.
        # El bloqueo duro vive en generate_signal() (MIN_ADX_TO_TRADE).
        if daily["adx"] < self.RANGE_ADX_WEAK:
            score_buy  = max(0, score_buy  - 5)
            score_sell = max(0, score_sell - 5)
            nota_adx = f"[-5] ADX diario débil ({daily['adx']:.1f} < {self.RANGE_ADX_WEAK}) — penalización simétrica"
            reasons_b.append(nota_adx)
            reasons_s.append(nota_adx)

        # ── 4. FILTRO DE CORRELACIÓN — ÍNDICE DE RÉGIMEN ─────────────
        # El parámetro se llama `dxy` por historia: hoy es el índice que la
        # config del instrumento designe (ver fetch_regime_index).
        #
        # Todos los PARES del sistema son en gran parte una apuesta contra el
        # USD: si el dólar está fuerte no conviene comprar, y viceversa.
        #
        # La MISMA regla vale para el S&P 500 con el VIX, sin tocar una línea
        # de este bloque: "rec BUY del índice de régimen resta al score de
        # compra" se lee ahí como miedo al alza -> no comprar, y "rec SELL
        # resta a la venta" como mercado en calma -> no vender contra él.
        # Por eso el quinto sistema cambia de símbolo, no de código.
        #
        # Backtest 2026-08-08 sobre 6 pares (177 trades sin filtro vs 160 con
        # filtro, ver backtest_results_summary.json / plan Item 7): con solo
        # AUD/USD (n=25) el filtro parecía dañino (52%->35% WR, -78% de P&L).
        # Con la muestra ampliada a 6 pares el filtro resulta neutral/positivo
        # en agregado (+$146 sin filtro vs +$197 con filtro, netos de costes),
        # mejora 4 de 6 pares y solo perjudica claramente a AUD/USD. Se
        # mantiene ACTIVO para los 6 -- una excepción solo para AUD/USD
        # significaría ajustar un parámetro sobre el mismo n=25 que ya se
        # había señalado como insuficiente (ver plan, Parte 5).
        if dxy and self.cfg.regime_penalises:
            rec_usd = dxy["rec"]
            etiqueta = self.cfg.regime_label
            if "BUY" in rec_usd:
                pen = 20 if "STRONG" in rec_usd else 10
                score_buy = max(0, score_buy - pen)
                reasons_b.append(f"[-{pen}] {etiqueta} {'muy ' if pen == 20 else ''}alcista — resta {pen} pts a la compra")
            elif "SELL" in rec_usd:
                pen = 20 if "STRONG" in rec_usd else 10
                score_sell = max(0, score_sell - pen)
                reasons_s.append(f"[-{pen}] {etiqueta} {'muy ' if pen == 20 else ''}bajista — resta {pen} pts a la venta")

        return score_buy, score_sell, reasons_b, reasons_s

    def detect_ranging_market(self, daily: dict, h4: dict) -> tuple[bool, list[str]]:
        """
        Detecta mercado lateral usando 3 filtros combinados.
        El ADX del 4H actúa como contrapeso: si el 4H tiene tendencia fuerte,
        no se bloquea aunque el daily ADX esté bajo (el daily tarda más en reaccionar).
        Retorna (is_ranging, razones).
        """
        ranging_score = 0
        reasons = []

        # ── 1. ADX — sin tendencia ────────────────────────────────────
        adx    = daily["adx"]
        adx_h4 = h4["adx"]

        if adx < self.RANGE_ADX_NO_TREND:
            if adx_h4 >= self.RANGE_ADX_H4_STRONG:
                # 4H tiene tendencia fuerte — el daily solo está rezagado, no es rango
                reasons.append(f"ADX diario = {adx:.1f} (bajo) pero ADX 4H = {adx_h4:.1f} — tendencia activa en 4H, no es rango")
            else:
                ranging_score += 3
                reasons.append(f"ADX diario = {adx:.1f} y ADX 4H = {adx_h4:.1f} — sin tendencia en ningun timeframe")
        elif adx < self.RANGE_ADX_WEAK:
            if adx_h4 < self.RANGE_ADX_WEAK:
                ranging_score += 1
                reasons.append(f"ADX diario = {adx:.1f} y ADX 4H = {adx_h4:.1f} — tendencia debil en ambos timeframes")

        # ── 2. Bollinger Bands contraídas (squeeze) ───────────────────
        bb_upper = daily["bb_upper"]
        bb_lower = daily["bb_lower"]
        bb_mid   = daily["bb_middle"] if daily["bb_middle"] > 0 else (bb_upper + bb_lower) / 2
        if bb_mid > 0 and bb_upper > bb_lower:
            bb_width = (bb_upper - bb_lower) / bb_mid
            if bb_width < self.BB_WIDTH_TIGHT:
                ranging_score += 3
                reasons.append(f"Bollinger Bands muy contraidas ({bb_width*100:.2f}%) — precio comprimido en rango")
            elif bb_width < self.BB_WIDTH_NARROW:
                ranging_score += 1
                reasons.append(f"Bollinger Bands estrechas ({bb_width*100:.2f}%) — posible rango")

        # ── 3. EMA20 y EMA50 planas — sin pendiente ───────────────────
        # Solo aplica si ADX < RANGE_ADX_WEAK: EMAs cercanas con ADX alto = cruce de tendencia, no rango
        ema_gap_pips = abs(daily["ema20"] - daily["ema50"]) / self.PIP_VALUE
        if adx < self.RANGE_ADX_WEAK:
            if ema_gap_pips < self.EMA_GAP_FLAT:
                ranging_score += 3
                reasons.append(f"EMA20 y EMA50 casi iguales ({ema_gap_pips:.0f} {self.UNIT}) — medias planas, sin tendencia")
            elif ema_gap_pips < self.EMA_GAP_NEAR:
                ranging_score += 1
                reasons.append(f"EMA20 y EMA50 muy cercanas ({ema_gap_pips:.0f} {self.UNIT}) — tendencia debil")

        # Mercado lateral si acumula 3+ puntos de ranging
        return ranging_score >= 3, reasons

    def calc_pullback_entry(self, direction: str, daily: dict) -> dict:
        """
        Calcula el nivel de entrada en PULLBACK (retroceso a la EMA20).
        En vez de entrar al precio de mercado (caro, extendido), espera a que
        el precio retroceda a la EMA20 con una orden LÍMITE — mejor precio, mejor R:R.

        Retorna: entry (nivel), entry_type ("LIMITE" o "MERCADO") y los pips
        que el precio debe retroceder para activar la orden.
        """
        close  = daily["close"]
        ema20  = daily["ema20"]
        buf    = self.PULLBACK_BUFFER * self.PIP_VALUE   # buffer en precio

        if direction == "LONG":
            # Pullback = caída hacia EMA20. Entramos BUFFER pips ANTES de llegar a EMA20.
            # Una Buy Limit solo es válida POR DEBAJO del precio actual: si el precio ya
            # está dentro de la zona de entrada, la orden correcta es a MERCADO.
            limit_entry = round(ema20 + buf, 5)
            if close > limit_entry + self.PIP_VALUE:
                entry      = limit_entry
                entry_type = "LIMITE"
            else:
                entry      = close
                entry_type = "MERCADO"
        else:  # SHORT
            # Pullback = subida hacia EMA20. Sell Limit solo es válida POR ENCIMA del precio.
            limit_entry = round(ema20 - buf, 5)
            if close < limit_entry - self.PIP_VALUE:
                entry      = limit_entry
                entry_type = "LIMITE"
            else:
                entry      = close
                entry_type = "MERCADO"

        retrace_pips = abs(close - entry) / self.PIP_VALUE
        return {
            "entry":        round(entry, 5),
            "entry_type":   entry_type,
            "retrace_pips": round(retrace_pips, 1),
            "current":      round(close, 5),
        }

    def find_structure_levels(self, daily: dict) -> list[float]:
        """
        Niveles técnicos donde el precio suele frenar:
          - Pivotes clásicos mensuales de TradingView (S3..R3)
          - Números redondos cada 50 pips (0.6400, 0.6450, ...)

        Para los 4 pares en vivo (todos cotizan a 4 decimales, 1 pip =
        0.0001) el paso de 0.0050 y el redondeo a 4 decimales quedan
        hardcodeados, byte-idénticos al código original -- ya verificados
        por backtest de regresión (500 escenarios aleatorios + reproducción
        exacta contra datos reales). Pares con otra escala (p.ej. USD/JPY,
        1 pip = 0.01, cotiza a 2 decimales) usan la rama derivada de
        self.PIP_VALUE -- nunca comparten código con la rama ya validada,
        para que ampliar a un par nuevo no pueda alterar el comportamiento
        de los 4 que ya están en producción.
        """
        levels = set()
        for v in daily.get("pivots", []):
            if v and v > 0:
                levels.add(round(v, 5))
        if self.PIP_VALUE == 0.0001:
            base = math.floor(daily["close"] / 0.0050) * 0.0050
            for i in range(-6, 8):                  # ~±300-400 pips alrededor del precio
                levels.add(round(base + i * 0.0050, 4))
        else:
            step = 50 * self.PIP_VALUE
            decimals = self.cfg.price_decimals
            base = math.floor(daily["close"] / step) * step
            for i in range(-6, 8):
                levels.add(round(base + i * step, decimals))
        return sorted(levels)

    def calc_take_profits(self, direction: str, entry: float, sl: float, daily: dict) -> dict:
        """
        Take profits escalonados:
          - TP1 = TP1_R → cerrar 50% de la posición (y mover SL a breakeven)
          - TP2 = primer nivel técnico (pivote / número redondo) más allá de MIN_RR,
                  colocado unos pips ANTES del nivel (el precio suele frenar ahí).
                  Sin nivel disponible → FALLBACK_RR fijo.
        El R:B final queda garantizado ≥ MIN_RR por construcción.
        Avisa si hay un nivel técnico en el camino antes del TP1 (posible freno).
        """
        risk    = abs(entry - sl)
        buf     = self.TP_LEVEL_BUFFER * self.PIP_VALUE
        tol     = self.PIP_VALUE / 2          # tolerancia de medio pip (ruido de coma flotante)
        is_long = direction == "LONG"
        levels  = self.find_structure_levels(daily)

        if is_long:
            tp1        = round(entry + self.TP1_R * risk, 5)
            min_tp2    = entry + self.MIN_RR * risk + buf
            candidates = [lv for lv in levels if lv >= min_tp2 - tol]
            tp2        = round(min(candidates) - buf, 5) if candidates else round(entry + self.FALLBACK_RR * risk, 5)
            en_camino  = [lv for lv in levels if entry + 5 * self.PIP_VALUE < lv < tp1]
            obstaculo  = min(en_camino) if en_camino else None
        else:
            tp1        = round(entry - self.TP1_R * risk, 5)
            min_tp2    = entry - self.MIN_RR * risk - buf
            candidates = [lv for lv in levels if lv <= min_tp2 + tol]
            tp2        = round(max(candidates) + buf, 5) if candidates else round(entry - self.FALLBACK_RR * risk, 5)
            en_camino  = [lv for lv in levels if tp1 < lv < entry - 5 * self.PIP_VALUE]
            obstaculo  = max(en_camino) if en_camino else None

        warning = (f"Nivel técnico en {obstaculo:.5f} antes del TP1 — el precio puede frenar ahí"
                   if obstaculo else "")

        return {
            "tp1":     tp1,
            "tp2":     tp2,
            "rr2":     round(abs(tp2 - entry) / risk, 2) if risk > 0 else 0.0,
            "warning": warning,
        }

    def build_management(self, direction: str, entry: float, sl: float, daily: dict) -> dict:
        """
        Calcula los niveles de gestión activa del trade:
          - Breakeven / salida parcial al alcanzar +1R
          - Trailing stop al alcanzar +2R
        """
        risk = abs(entry - sl)
        if direction == "LONG":
            be_trigger      = round(entry + risk, 5)        # +1R
            trail_trigger   = round(entry + risk * 2, 5)    # +2R
        else:
            be_trigger      = round(entry - risk, 5)
            trail_trigger   = round(entry - risk * 2, 5)

        return {
            "be_trigger":    be_trigger,
            "trail_trigger": trail_trigger,
            "be_price":      round(entry, 5),               # SL se mueve a la entrada
            "trail_ema":     round(daily["ema20"], 5),      # luego trailing bajo EMA20
        }

    # ══════════════════════════════════════════════════════════════
    #  REGISTRO DE SEÑALES (senales*.csv)
    # ══════════════════════════════════════════════════════════════

    def get_pending_order(self) -> dict | None:
        """
        Lee el CSV del par y retorna la orden más reciente con estado PENDIENTE o ACTIVA.
        Retorna None si no hay ninguna, o si la última fue CANCELADA/WIN/LOSS/BE.
        """
        if not os.path.exists(self.LOG_FILE):
            return None
        try:
            with open(self.LOG_FILE, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            for row in reversed(rows):
                if _safe_upper(row.get("resultado")) in ("PENDIENTE", "ACTIVA"):
                    # Ignorar filas corruptas para no tumbar el loop (niveles no
                    # numéricos, o fecha/hora/dirección ausentes -- fecha=None
                    # revienta strptime más adelante con TypeError, que algunos
                    # except solo capturan ValueError). Mismo criterio que
                    # update_pending_order/close_trade (_is_valid_order_row) para
                    # que los tres operen siempre sobre la MISMA fila.
                    if not _is_valid_order_row(row):
                        continue
                    return row
        except Exception:
            pass
        return None

    def update_pending_order(self, order: dict, daily: dict) -> tuple[bool, float, float]:
        """
        Actualiza entry, SL, TP y lotes de la orden pendiente si la EMA20 se movió > 1 pip.
        precio_senal NO se toca — sigue siendo el precio original para detectar oportunidad perdida.
        Retorna (actualizado, entrada_anterior, entrada_nueva).
        """
        direction    = order["direccion"]
        stored_entry = _safe_float(order["entrada"])

        # Una orden MERCADO representa una entrada inmediata al precio de la
        # señal, no una espera de pullback -- no se debe "perseguir" el precio
        # recalculando su nivel en cada ciclo (bug: eso convertía en silencio
        # una entrada a mercado en una orden límite distinta antes de que el
        # usuario llegara a confirmarla en el bróker).
        if _safe_upper(order.get("tipo_entrada")) != "LIMITE":
            return False, stored_entry, stored_entry

        # Si la orden ya fue alcanzada, invalidada, vencida o perdió su ventana, NO moverla:
        # recolocar la entrada en esos casos "persigue" al precio y anula la invalidación
        status = self.check_order_status(order, daily["close"])
        if status["estado"] not in ("OK", "CERCA"):
            return False, stored_entry, stored_entry

        pb = self.calc_pullback_entry(direction, daily)
        if pb["entry_type"] != "LIMITE":
            # El precio ya está en la zona de entrada — no hay orden límite que recolocar
            return False, stored_entry, stored_entry
        new_entry = pb["entry"]

        if abs(new_entry - stored_entry) < self.PIP_VALUE:
            return False, stored_entry, stored_entry

        atr         = daily["atr"] if daily["atr"] > 0 else 0.0030
        sl_distance = max(atr * self.ATR_MULTIPLIER, self.MIN_PIPS_SL * self.PIP_VALUE)

        if direction == "LONG":
            new_sl = round(new_entry - sl_distance, 5)
        else:
            new_sl = round(new_entry + sl_distance, 5)

        tps        = self.calc_take_profits(direction, new_entry, new_sl, daily)
        new_tp     = tps["tp2"]
        expected_r = 0.5 * self.TP1_R + 0.5 * tps["rr2"]
        new_pos    = self.calc_position_size(new_entry, new_sl, expected_r)

        if not os.path.exists(self.LOG_FILE):
            return False, stored_entry, stored_entry

        try:
            with open(self.LOG_FILE, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))

            updated = False
            for row in reversed(rows):
                if _safe_upper(row.get("resultado")) == "PENDIENTE" and _is_valid_order_row(row):
                    row["entrada"]                = f"{new_entry:.5f}"
                    row["stop_loss"]              = f"{new_sl:.5f}"
                    row["tp1"]                    = f"{tps['tp1']:.5f}"
                    row["take_profit"]            = f"{new_tp:.5f}"
                    row["pips_sl"]                = str(new_pos["pips_sl"])
                    row["lotes"]                  = str(new_pos["lots"])
                    row["riesgo_usd_real"]        = f"{new_pos['risk_usd_real']:.2f}"
                    row["ganancia_potencial_usd"] = f"{new_pos['gain_usd']:.2f}"
                    updated = True
                    break

            if updated:
                # Siempre con el header actual — migra filas de formato viejo al vuelo
                _write_csv_atomic(self.LOG_FILE, CSV_HEADER, rows)

            return updated, stored_entry, new_entry

        except Exception as e:
            print(f"{Fore.RED}  Error actualizando señal: {e}{Style.RESET_ALL}")
            return False, stored_entry, stored_entry

    def check_order_status(self, order: dict, current_price: float) -> dict:
        """
        Evalúa si la orden pendiente sigue válida o debe cancelarse.
        Retorna un dict con: estado, alerta, detalle.
        Estados: OK | CERCA | ACTIVADA | VENCIDA | PERDIDA | INVALIDADA

        Lógica de dirección:
          LONG  (Buy Limit):  esperamos que el precio BAJE hasta la entrada.
                              SL está DEBAJO de la entrada.
                              Invalidada si precio cae bajo el SL sin tocar la entrada.
          SHORT (Sell Limit): esperamos que el precio SUBA hasta la entrada.
                              SL está ENCIMA de la entrada.
                              Invalidada si precio sube sobre el SL sin tocar la entrada.
        """
        direction = order["direccion"]
        entry     = _safe_float(order["entrada"])
        sl        = _safe_float(order["stop_loss"])
        is_long   = direction == "LONG"
        risk_pips = abs(entry - sl) / self.PIP_VALUE   # 1R en pips

        # ── Días transcurridos ────────────────────────────────────────
        try:
            dias = _trading_days_since(datetime.strptime(order["fecha"], "%Y-%m-%d").date())
        except Exception:
            dias = 0

        # ── Órdenes a MERCADO: la entrada YA es (aprox) el precio de la señal,
        # no hay pullback que esperar. Las reglas de abajo son todas para una
        # orden LÍMITE que aún no se llenó (INVALIDADA si rompe el SL sin
        # tocar la entrada, PERDIDA si el precio avanza 1R sin activarse,
        # "faltan X pips para activar"): aplicadas a una orden a MERCADO
        # producían mensajes al revés -- un trade GANADOR (+1R a favor sin que
        # el usuario aún confirmara ejecución) se mostraba como "OPORTUNIDAD
        # PERDIDA -- considera cancelar", y se rotulaba "COMPRA/VENTA LÍMITE"
        # aunque nunca hubo límite que colocar.
        if _safe_upper(order.get("tipo_entrada")) != "LIMITE":
            price_vs_sl = (current_price <= sl) if is_long else (current_price >= sl)
            if price_vs_sl:
                return {
                    "estado":  "INVALIDADA",
                    "color":   Fore.RED,
                    "emoji":   "🚫",
                    "alerta":  "PRECIO YA EN TU STOP LOSS — si ejecutaste esta entrada a mercado, ciérrala y marca LOSS",
                    "detalle": "Si todavía NO la ejecutaste en el bróker, esta señal ya no es válida — no la abras.",
                    "dias":    dias,
                }
            if dias >= self.PENDING_EXPIRY_DAYS:
                return {
                    "estado":  "VENCIDA",
                    "color":   Fore.YELLOW,
                    "emoji":   "⏰",
                    "alerta":  f"SEÑAL SIN CONFIRMAR — {dias} dias sin marcarla ACTIVA",
                    "detalle": "Si ya ejecutaste esta operación a mercado en el bróker, márcala ACTIVA. Si no, esta señal ya no es válida.",
                    "dias":    dias,
                }
            return {
                "estado":  "ACTIVADA",
                "color":   Fore.GREEN,
                "emoji":   "✅",
                "alerta":  "ORDEN A MERCADO — confirma la ejecución en el bróker y marca ACTIVA",
                "detalle": "",
                "dias":    dias,
            }

        # ── Cálculos según dirección (resto: orden LÍMITE) ─────────────
        if is_long:
            # LONG Buy Limit: esperamos que el precio BAJE hasta entry.
            # dist positivo = precio sobre entry = sigue esperando la caída.
            dist_pips   = (current_price - entry) / self.PIP_VALUE
            price_vs_sl = current_price <= sl
        else:
            # SHORT Sell Limit: esperamos que el precio SUBA hasta entry.
            # dist positivo = precio bajo entry = sigue esperando el rebote.
            dist_pips   = (entry - current_price) / self.PIP_VALUE
            price_vs_sl = current_price >= sl

        # ── Oportunidad perdida: desde el precio al momento de la señal ──
        # Si el mercado se movió 1R en nuestra dirección sin tocarnos, el setup caducó.
        precio_senal = _safe_float(order.get("precio_senal", ""), 0)
        if precio_senal > 0:
            # LONG: oportunidad perdida si precio subió 1R desde la señal sin bajar a entry
            # SHORT: oportunidad perdida si precio bajó 1R desde la señal sin subir a entry
            moved_in_trade = ((current_price - precio_senal) / self.PIP_VALUE if is_long
                              else (precio_senal - current_price) / self.PIP_VALUE)
        else:
            moved_in_trade = 0  # señal antigua sin precio_senal → omitir este chequeo

        # ── Evaluación de estado (orden de prioridad) ─────────────────
        if price_vs_sl:
            return {
                "estado":  "INVALIDADA",
                "color":   Fore.RED,
                "emoji":   "🚫",
                "alerta":  "SETUP INVALIDADO — el precio rompió el Stop Loss",
                "detalle": "El mercado fue en dirección contraria al setup. Cancela la orden.",
                "dias":    dias,
            }

        if dias >= self.PENDING_EXPIRY_DAYS:
            return {
                "estado":  "VENCIDA",
                "color":   Fore.YELLOW,
                "emoji":   "⏰",
                "alerta":  f"ORDEN VENCIDA — {dias} dias sin activarse",
                "detalle": f"Han pasado {self.PENDING_EXPIRY_DAYS}+ dias sin que el precio llegue a tu entrada. Considera cancelar.",
                "dias":    dias,
            }

        if moved_in_trade >= risk_pips:
            return {
                "estado":  "PERDIDA",
                "color":   Fore.YELLOW,
                "emoji":   "⚠️",
                "alerta":  f"OPORTUNIDAD PERDIDA — el precio se movio {moved_in_trade:.0f} pips sin activarte",
                "detalle": f"El movimiento ya recorrio 1R ({risk_pips:.0f} pips) sin tocar tu entrada. Considera cancelar.",
                "dias":    dias,
            }

        if dist_pips <= 0:
            return {"estado": "ACTIVADA", "color": Fore.GREEN,  "emoji": "✅",
                    "alerta": "PRECIO EN ZONA — verificar ejecucion en broker", "detalle": "", "dias": dias}
        if dist_pips <= 10:
            return {"estado": "CERCA",    "color": Fore.YELLOW, "emoji": "⚡",
                    "alerta": f"MUY CERCA — {dist_pips:.1f} pips para activar", "detalle": "", "dias": dias}

        return {"estado": "OK", "color": Fore.CYAN, "emoji": "📍",
                "alerta": f"Faltan {dist_pips:.1f} pips para activar", "detalle": "", "dias": dias}

    def log_signal(self, trade: dict) -> bool:
        """
        Guarda la señal en el CSV del par. Reglas:
        - No escribe si ya hay PENDIENTE hoy en la misma dirección.
        - Sí escribe si la señal del día estaba CANCELADA/WIN/LOSS (el usuario quiere nueva).
        - Siempre reescribe con el header actual (migra formato viejo automáticamente).
        Retorna True si se registró una señal nueva.
        """
        now   = datetime.now()
        fecha = now.strftime("%Y-%m-%d")
        hora  = now.strftime("%H:%M")
        pos   = trade["pos"]

        # Leer filas existentes (DictReader maneja cualquier formato de header)
        existing = []
        if os.path.exists(self.LOG_FILE):
            try:
                with open(self.LOG_FILE, "r", encoding="utf-8", newline="") as f:
                    existing = list(csv.DictReader(f))
            except Exception:
                pass

        # Bloquear solo si hay PENDIENTE hoy en esta dirección
        # CANCELADA/WIN/LOSS permiten registrar una señal nueva el mismo día
        for row in existing:
            if (row.get("fecha") == fecha
                    and row.get("direccion") == trade["direction"]
                    and _safe_upper(row.get("resultado")) == "PENDIENTE"):
                return False

        # Migrar filas viejas sin precio_senal / riesgo_usd_real
        for row in existing:
            row.setdefault("precio_senal", "")
            row.setdefault("riesgo_usd_real", "")

        new_row = {
            "fecha":                  fecha,
            "hora":                   hora,
            "direccion":              trade["direction"],
            "entrada":                f"{trade['entry']:.5f}",
            "tipo_entrada":           trade["entry_type"],
            "stop_loss":              f"{trade['sl']:.5f}",
            "tp1":                    f"{trade['tp1']:.5f}",
            "take_profit":            f"{trade['tp']:.5f}",
            "pips_sl":                str(pos["pips_sl"]),
            "lotes":                  str(pos["lots"]),
            "score":                  str(trade["score"]),
            "riesgo_usd":             f"{pos['risk_usd']:.2f}",
            "riesgo_usd_real":        f"{pos['risk_usd_real']:.2f}",
            "ganancia_potencial_usd": f"{pos['gain_usd']:.2f}",
            "precio_senal":           f"{trade['current']:.5f}",
            "resultado":              "PENDIENTE",
            "fecha_cierre":           "",
            "pips_resultado":         "",
            "motivo_cierre":          "",
        }

        try:
            _write_csv_atomic(self.LOG_FILE, CSV_HEADER, existing + [new_row])
            return True
        except Exception as e:
            print(f"{Fore.RED}  No se pudo guardar la señal en CSV: {e}{Style.RESET_ALL}")
            return False

    def record_tp1_partial(self, pips: float) -> bool:
        """
        Anota que se cerró el 50% de la posición ACTIVA al tocar TP1 (ver
        CSV_HEADER: pips_tp1_parcial), SIN cerrar la fila -- sigue en
        resultado=ACTIVA hasta que la otra mitad cierre (TP final/BE/SL) y se
        llame a close_trade normalmente. Ese cierre final debe anotar solo los
        pips del 50% restante, no el trade completo: compute_pnl pondera
        ambas mitades al 50/50 usando este campo.

        Solo aplica a una orden ya ACTIVA (una PENDIENTE no puede haber
        tocado TP1 -- no hay posición abierta todavía). Retorna True si se
        actualizó una fila.
        """
        if not os.path.exists(self.LOG_FILE):
            return False
        try:
            with open(self.LOG_FILE, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            return False

        updated = False
        for row in reversed(rows):
            if _safe_upper(row.get("resultado")) == "ACTIVA" and _is_valid_order_row(row):
                row["pips_tp1_parcial"] = f"{pips:+.1f}"
                updated = True
                break

        if updated:
            _write_csv_atomic(self.LOG_FILE, CSV_HEADER, rows)
        return updated

    def close_trade(self, resultado: str, pips_resultado: float | None = None,
                     motivo_cierre: str = "MANUAL", fecha_cierre: str | None = None,
                     entrada_real: float | None = None) -> bool:
        """
        Cierra (o transiciona) la orden PENDIENTE/ACTIVA más reciente del CSV:
        resultado en WIN/LOSS/BE/CANCELADA marca cierre; resultado=ACTIVA marca
        que la orden límite se ejecutó en el bróker (sigue abierta, ahora activa).
        Usado por el dashboard (ver Item 3) para dejar de requerir edición manual
        del CSV mientras el proceso de análisis lo tiene abierto.

        entrada_real: solo aplica con resultado=ACTIVA. Precio real de fill en
        el bróker cuando difiere del nivel guardado (el precio se movió entre
        que se mostró la señal y que el usuario confirmó la ejecución). Se
        desplaza entrada/SL/TP1/TP en bloque por la misma distancia -- se
        conserva el mismo riesgo en pips y el R:R original, solo se recentra
        el plan sobre el precio en el que realmente se entró.

        Al pasar de PENDIENTE a ACTIVA también se reescribe fecha/hora al
        momento real de ejecución -- no al de la señal. El contador de "días"
        de una posición ACTIVA (regla de los 5 días, límite de riesgo diario)
        mide tiempo con riesgo real tomado, no tiempo esperando una entrada.
        Retorna True si se actualizó una fila.
        """
        if not os.path.exists(self.LOG_FILE):
            return False
        try:
            with open(self.LOG_FILE, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            return False

        updated = False
        for row in reversed(rows):
            if _safe_upper(row.get("resultado")) in ("PENDIENTE", "ACTIVA") and _is_valid_order_row(row):
                was_pendiente = _safe_upper(row.get("resultado")) == "PENDIENTE"
                if resultado == "ACTIVA" and entrada_real is not None:
                    entrada_guardada = _safe_float(row.get("entrada"))
                    delta = round(entrada_real - entrada_guardada, 5)
                    if delta:
                        row["entrada"]     = f"{entrada_real:.5f}"
                        row["stop_loss"]   = f"{_safe_float(row.get('stop_loss')) + delta:.5f}"
                        if _safe_float(row.get("tp1"), 0) > 0:
                            row["tp1"] = f"{_safe_float(row.get('tp1')) + delta:.5f}"
                        row["take_profit"] = f"{_safe_float(row.get('take_profit')) + delta:.5f}"
                if resultado == "ACTIVA" and was_pendiente:
                    now = datetime.now()
                    row["fecha"] = now.strftime("%Y-%m-%d")
                    row["hora"]  = now.strftime("%H:%M")
                    # Recorrido en limpio: la posición empieza a contar desde el
                    # fill. Si esta fila ya traía mfe/mae (se reactivó una orden
                    # vieja) esos valores son de otra vida.
                    row["mfe_precio"] = row["mfe_pips"] = ""
                    row["mae_precio"] = row["mae_pips"] = ""
                row["resultado"] = resultado
                if resultado != "ACTIVA":
                    row["fecha_cierre"] = fecha_cierre or datetime.now().strftime("%Y-%m-%d")
                    if pips_resultado is not None:
                        row["pips_resultado"] = f"{pips_resultado:+.1f}"
                    row["motivo_cierre"] = motivo_cierre
                updated = True
                break

        if updated:
            for r in rows:
                r.setdefault("riesgo_usd_real", "")
            _write_csv_atomic(self.LOG_FILE, CSV_HEADER, rows)
        return updated

    def update_trade_tracking(self, daily: dict, h4: dict | None = None) -> bool:
        """
        Actualiza el recorrido (MFE/MAE) de la posición ACTIVA más reciente del
        CSV. Se llama una vez por ciclo mientras hay un trade abierto, ANTES de
        construir la vista -- así la tarjeta lee un nivel ya persistido.

        Detección del extremo de cada ciclo:
          - Primeras ~20h desde el fill: SOLO el close. La vela diaria en curso
            incluye precio de ANTES de la entrada y, con entrada LIMITE (que
            espera un retroceso), el máximo previo al fill suele estar por
            encima de la entrada e incluso del TP1 -- high/low ahí darían un
            MFE falso ("TP1 alcanzado" nada más activarse). 20h garantizan que
            ya se ha formado una vela diaria íntegramente posterior a la
            entrada, sea cual sea el desfase sesión-hora local.
          - A partir de ahí: high/low completos de la diaria y la 4H.

        mfe_precio solo avanza a favor y mae_precio solo en contra: nunca
        retroceden, así un toque puntual del TP1 no se pierde al cambiar de día.
        Devuelve True si se escribió el CSV.
        """
        if not os.path.exists(self.LOG_FILE):
            return False
        try:
            with open(self.LOG_FILE, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            return False

        target = next((r for r in reversed(rows)
                       if _safe_upper(r.get("resultado")) == "ACTIVA" and _is_valid_order_row(r)), None)
        if target is None:
            return False

        entry = _safe_float(target.get("entrada"))
        if entry <= 0:
            return False
        is_long = target.get("direccion") == "LONG"
        sign    = 1 if is_long else -1
        close   = _safe_float(daily.get("close"), 0) or entry

        try:
            activada = datetime.strptime(f"{target.get('fecha')} {target.get('hora')}",
                                         "%Y-%m-%d %H:%M")
            fill_reciente = (datetime.now() - activada) < timedelta(hours=20)
        except (ValueError, TypeError):
            fill_reciente = True   # sin fecha/hora fiable -> lo prudente es close
        if fill_reciente:
            cand_fav = cand_adv = close
        else:
            highs = [_safe_float(daily.get("high"), close)] + (
                [_safe_float(h4.get("high"), close)] if h4 else [])
            lows  = [_safe_float(daily.get("low"),  close)] + (
                [_safe_float(h4.get("low"),  close)] if h4 else [])
            cand_fav = max(highs) if is_long else min(lows)
            cand_adv = min(lows)  if is_long else max(highs)
        # el close siempre es un punto realmente alcanzado este ciclo
        cand_fav = max(cand_fav, close) if is_long else min(cand_fav, close)
        cand_adv = min(cand_adv, close) if is_long else max(cand_adv, close)

        had_mfe  = target.get("mfe_precio") not in (None, "")
        had_mae  = target.get("mae_precio") not in (None, "")
        prev_mfe = _safe_float(target.get("mfe_precio"), 0) or entry
        prev_mae = _safe_float(target.get("mae_precio"), 0) or entry
        new_mfe  = max(prev_mfe, cand_fav) if is_long else min(prev_mfe, cand_fav)
        new_mae  = min(prev_mae, cand_adv) if is_long else max(prev_mae, cand_adv)

        tol = self.PIP_VALUE / 10   # cambios sub-décima de pip no valen una reescritura
        if had_mfe and had_mae and abs(new_mfe - prev_mfe) < tol and abs(new_mae - prev_mae) < tol:
            return False

        target["mfe_precio"] = f"{new_mfe:.5f}"
        target["mfe_pips"]   = f"{(new_mfe - entry) / self.PIP_VALUE * sign:+.1f}"
        target["mae_precio"] = f"{new_mae:.5f}"
        target["mae_pips"]   = f"{(new_mae - entry) / self.PIP_VALUE * sign:+.1f}"

        for r in rows:
            r.setdefault("riesgo_usd_real", "")
        try:
            _write_csv_atomic(self.LOG_FILE, CSV_HEADER, rows)
            return True
        except Exception as e:
            print(f"{Fore.RED}  No se pudo guardar el recorrido MFE/MAE: {e}{Style.RESET_ALL}")
            return False

    def check_risk_limits(self) -> tuple[bool, list[str]]:
        """
        Aplica los límites de riesgo REALES leyendo el CSV del par:
          - Máximo de trades por día (PENDIENTE/ACTIVA/WIN/LOSS cuentan; CANCELADA no)
          - Pérdida máxima diaria  (suma del riesgo real de los LOSS de hoy)
          - Pérdida máxima semanal (suma del riesgo real de los LOSS de la semana ISO)
          - 2 pérdidas seguidas con la última hoy → parar el resto del día
        Retorna (puede_operar, razones_de_bloqueo).
        """
        if not os.path.exists(self.LOG_FILE):
            return True, []
        try:
            with open(self.LOG_FILE, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception:
            return True, []

        hoy      = datetime.now().date()
        week_now = hoy.isocalendar()[:2]     # (año, semana ISO)

        trades_hoy   = 0
        loss_dia_usd = 0.0
        loss_sem_usd = 0.0
        closed_seq   = []                    # (resultado, fecha) de trades cerrados, en orden

        for row in rows:
            res = _safe_upper(row.get("resultado"))
            try:
                fecha = datetime.strptime(row.get("fecha", ""), "%Y-%m-%d").date()
            except ValueError:
                continue

            if res in ("PENDIENTE", "ACTIVA", "WIN", "LOSS", "BE") and fecha == hoy:
                trades_hoy += 1

            if res == "LOSS":
                # Pérdida real si el usuario anotó pips_resultado; si no, el riesgo
                # REAL calculado al abrir el trade (lotes×pips_sl×valor_pip, no el
                # nominal de 1% -- ver plan hallazgo H2: el nominal sobreestima el
                # riesgo real hasta en un ~30% con este tamaño de cuenta).
                pips = _safe_float(row.get("pips_resultado"), 0)
                lots = _safe_float(row.get("lotes"), 0)
                if pips and lots:
                    riesgo = abs(pips) * lots * self.USD_PER_PIP_STANDARD
                else:
                    riesgo = _safe_float(row.get("riesgo_usd_real"), None)
                    if riesgo is None:
                        riesgo = _safe_float(row.get("riesgo_usd"), self.RISK_USD)
                if fecha == hoy:
                    loss_dia_usd += riesgo
                if fecha.isocalendar()[:2] == week_now:
                    loss_sem_usd += riesgo

            if res in ("WIN", "LOSS"):
                closed_seq.append((res, fecha))

        max_dia = self.CAPITAL * self.MAX_LOSS_DAY_PCT / 100
        max_sem = self.CAPITAL * self.MAX_LOSS_WK_PCT / 100
        reasons = []

        if trades_hoy >= self.MAX_TRADES_DAY:
            reasons.append(f"Ya hay {trades_hoy} operaciones hoy (máximo {self.MAX_TRADES_DAY}) — no abrir más hasta mañana")
        if loss_dia_usd >= max_dia:
            reasons.append(f"Pérdida de hoy: ${loss_dia_usd:.0f} (límite diario ${max_dia:.0f}) — PARAR hasta mañana")
        if loss_sem_usd >= max_sem:
            reasons.append(f"Pérdida semanal: ${loss_sem_usd:.0f} (límite ${max_sem:.0f}) — parar y revisar la estrategia")
        if (len(closed_seq) >= 2
                and closed_seq[-1][0] == "LOSS" and closed_seq[-2][0] == "LOSS"
                and closed_seq[-1][1] == hoy):
            reasons.append("2 pérdidas seguidas (la última hoy) — PARAR por hoy y analizar qué falló")

        return len(reasons) == 0, reasons

    # ══════════════════════════════════════════════════════════════
    #  CALENDARIO (pair-independiente, pero expuesto como método para
    #  mantener la misma convención de llamada que el resto del motor)
    # ══════════════════════════════════════════════════════════════

    def check_market_session(self, now_utc: datetime | None = None) -> dict:
        """
        Delega al calendario semanal de forex (módulo, compartido por los 5
        sistemas) y, SOLO para instrumentos con respects_nyse_holidays=True
        (hoy: US500), añade el calendario de festivos del NYSE encima.

        Necesario porque check_market_session() de módulo no sabe que el
        mercado de acciones cierra ~9-10 días al año (Thanksgiving, 4 de
        julio, Navidad...) en los que el forex sigue cotizando -- sin esto,
        SP:SPX no genera vela nueva ese día pero el sistema trataría el
        cierre "17:00 NY" de siempre como si fuera una vela fresca, y podría
        registrar una señal basada en datos idénticos a los de ayer.
        """
        session = check_market_session(now_utc)
        if session["open"] and self.cfg.respects_nyse_holidays:
            try:
                NY = ZoneInfo("America/New_York")
            except Exception:
                NY = timezone(timedelta(hours=-4))
            hoy_ny = (now_utc or datetime.now(timezone.utc)).astimezone(NY).date()
            if is_us_market_holiday(hoy_ny):
                return {**session, "block_new": True,
                        "reason": "Festivo del NYSE — el mercado de acciones no opera hoy "
                                  "(el forex sigue abierto, pero SP:SPX no genera vela nueva)"}
        return session

    def active_sessions(self, now_utc: datetime | None = None) -> str:
        return active_sessions(now_utc)

    def check_signal_window(self, now_utc: datetime | None = None) -> dict:
        """
        Ventana de decisión tras el cierre/reapertura de la vela diaria de Forex
        (17:00 Nueva York). Recién cerrada la vela, los indicadores diarios
        (RSI/MACD/EMAs) son definitivos; a mitad del día siguiente son
        provisionales — una señal generada con la vela a medio formar puede dejar
        de existir al cierre (señal falsa). Fuera de la ventana el sistema solo
        vigila órdenes pendientes y trades activos.

        El domingo 17:00 NY el mercado REABRE tras el fin de semana — se trata
        igual que cualquier otro cierre diario porque es la primera vela nueva de
        la semana y da la primera oportunidad de señal, justo en la apertura de la
        sesión asiática del lunes. El bloqueo de las primeras horas tras la
        reapertura ya lo cubre check_market_session() (apertura del domingo).
        """
        try:
            NY = ZoneInfo("America/New_York")
        except Exception:
            NY = timezone(timedelta(hours=-4))   # sin tzdata: aproximar (horario de verano NY)

        now = (now_utc or datetime.now(timezone.utc)).astimezone(NY)

        last_close = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if now < last_close:
            last_close -= timedelta(days=1)

        hours_since = (now - last_close).total_seconds() / 3600
        next_close = last_close + timedelta(days=1)

        # Instrumentos sin vela de domingo (índices de contado): a las 17:00
        # del domingo NO hay vela nueva -- la última sigue siendo la del
        # viernes, que este mismo sistema ya evaluó. Abrir la ventana ahí
        # sería decidir dos veces sobre los mismos datos, ya rancios.
        in_window = hours_since <= self.SIGNAL_WINDOW_HOURS
        if in_window and not self.cfg.trades_sunday and last_close.weekday() == 6:
            in_window = False

        return {
            "in_window":  in_window,
            "hours":      hours_since,
            "next_local": next_close.astimezone().strftime("%H:%M"),
        }

    # ══════════════════════════════════════════════════════════════
    #  GENERACIÓN DE SEÑAL
    # ══════════════════════════════════════════════════════════════

    def _stoch_pullback_read(self, direction: str, stoch_h4: float) -> dict:
        """Analiza si el Estocástico 4H sugiere pullback próximo o entrada directa."""
        if direction == "LONG":
            # Esperamos que el precio BAJE a EMA20 → stoch overbought confirma caída próxima
            if stoch_h4 > 70:
                return {"rec": "PULLBACK", "stoch": stoch_h4,
                        "msg": f"Estocástico 4H sobrecomprado ({stoch_h4:.1f}) — caída hacia EMA20 probable"}
            elif stoch_h4 > 45:
                return {"rec": "AMBAS",   "stoch": stoch_h4,
                        "msg": f"Estocástico 4H neutro ({stoch_h4:.1f}) — pullback incierto"}
            else:
                return {"rec": "MERCADO", "stoch": stoch_h4,
                        "msg": f"Estocástico 4H bajo ({stoch_h4:.1f}) — precio puede subir sin caer a EMA20"}
        else:  # SHORT
            # Esperamos que el precio SUBA a EMA20 → stoch oversold confirma rebote próximo
            if stoch_h4 < 30:
                return {"rec": "PULLBACK", "stoch": stoch_h4,
                        "msg": f"Estocástico 4H sobrevendido ({stoch_h4:.1f}) — rebote hacia EMA20 probable"}
            elif stoch_h4 < 55:
                return {"rec": "AMBAS",   "stoch": stoch_h4,
                        "msg": f"Estocástico 4H neutro ({stoch_h4:.1f}) — pullback incierto"}
            else:
                return {"rec": "MERCADO", "stoch": stoch_h4,
                        "msg": f"Estocástico 4H alto ({stoch_h4:.1f}) — precio puede seguir bajando sin rebotar"}

    def pullback_vs_market(self, direction: str, daily: dict, h4: dict) -> dict:
        """
        Compara pullback vs entrada a mercado para `direction` usando el
        Estocástico 4H y calcula la alternativa de mercado (nivel, SL, TP,
        lotes). No depende de MIN_SCORE: se usa tanto para una señal nueva
        como para dar contexto a una orden PENDIENTE ya registrada aunque el
        score de hoy ya no alcance el mínimo -- antes, si el score bajaba de
        un ciclo a otro, esta caja desaparecía por completo del dashboard y
        la orden pendiente se veía como una espera obligatoria sin contexto.
        """
        pba = self._stoch_pullback_read(direction, h4["stoch_k"])
        atr = daily["atr"] if daily["atr"] > 0 else 0.0030
        sl_distance = max(atr * self.ATR_MULTIPLIER, self.MIN_PIPS_SL * self.PIP_VALUE)
        close = daily["close"]
        sign  = -1 if direction == "LONG" else 1
        mkt_sl  = round(close + sign * sl_distance, 5)
        mkt_tps = self.calc_take_profits(direction, close, mkt_sl, daily)
        expected_r = 0.5 * self.TP1_R + 0.5 * mkt_tps["rr2"]
        return {
            "pb_analysis": pba,
            "mkt_entry":   round(close, 5),
            "mkt_sl":      mkt_sl,
            "mkt_tp":      mkt_tps["tp2"],
            "mkt_pos":     self.calc_position_size(close, mkt_sl, expected_r),
        }

    def generate_signal(self, weekly: dict, daily: dict, h4: dict, dxy: dict | None = None) -> dict:
        """Genera la señal de trading final con niveles y gestión de posición."""

        # Bloqueo duro: verificar mercado lateral antes de calcular scores
        is_ranging, ranging_reasons = self.detect_ranging_market(daily, h4)
        if is_ranging:
            return {
                "signal":          "LATERAL",
                "valid":           False,
                "ranging":         True,
                "ranging_reasons": ranging_reasons,
                "score_b":         0,
                "score_s":         0,
            }

        score_b, score_s, reasons_b, reasons_s = self.score_direction(weekly, daily, h4, dxy)

        # Filtro binario de ADX (ver plan Item 9 / hallazgo H8): con tendencia
        # débil, NINGUNA dirección genera señal aunque el score alcance el
        # mínimo -- el score se sigue calculando (para mostrarlo) pero
        # "valid" queda forzado a False más abajo.
        weak_trend = daily["adx"] < self.MIN_ADX_TO_TRADE
        if weak_trend:
            nota = f"⚠ ADX diario {daily['adx']:.1f} < {self.MIN_ADX_TO_TRADE} — tendencia insuficiente, señal bloqueada aunque el score alcance el mínimo"
            reasons_b.append(nota)
            reasons_s.append(nota)

        # Puerta del lado corto (solo instrumentos con no_short_above_ema200):
        # la renta variable tiene deriva estructural alcista, así que vender
        # con el precio SOBRE la EMA200 diaria es apostar contra la tendencia
        # secular del índice. Es un bloqueo binario, independiente del score,
        # igual que el filtro de ADX -- no una penalización de puntos.
        block_short = (self.cfg.no_short_above_ema200
                       and daily["close"] > daily["ema200"])
        if block_short:
            reasons_s.append(f"⚠ Precio ({daily['close']:.2f}) sobre la EMA200 diaria "
                             f"({daily['ema200']:.2f}) — no se abren ventas por encima de la "
                             f"EMA200 en un índice de renta variable (deriva alcista estructural)")

        atr = daily["atr"] if daily["atr"] > 0 else 0.0030

        # Stop Loss dinámico: ATR × multiplicador, mínimo MIN_PIPS_SL pips
        sl_distance = max(atr * self.ATR_MULTIPLIER, self.MIN_PIPS_SL * self.PIP_VALUE)

        if score_b >= self.MIN_SCORE and score_b > score_s and not weak_trend:
            pb    = self.calc_pullback_entry("LONG", daily)
            entry = pb["entry"]
            sl    = round(entry - sl_distance, 5)
            tps   = self.calc_take_profits("LONG", entry, sl, daily)
            expected_r = 0.5 * self.TP1_R + 0.5 * tps["rr2"]   # 50% cierra en TP1, 50% en TP2
            pos   = self.calc_position_size(entry, sl, expected_r)
            # SL amplio (ATR alto) + capital pequeño -> el redondeo a la baja de
            # lotes (calc_position_size) puede dar 0.00 -- una señal "operable"
            # con 0 lotes arriesga $0 y gana $0 pero se registraba igual como
            # válida. Bloquear aquí en vez de dejar que cada consumidor (CSV,
            # dashboard, CLI) tenga que acordarse de comprobarlo por su cuenta.
            if pos["lots"] <= 0:
                reasons_b.append(f"⚠ SL de {pos['pips_sl']:.0f} pips es demasiado amplio para el "
                                  f"riesgo de ${self.RISK_USD:.2f} — el lote calculado redondea a 0.00, señal bloqueada")
                return {"signal": "NEUTRAL", "valid": False, "score_b": score_b, "score_s": score_s,
                        "weak_trend": False, "zero_lots": True}
            mgmt  = self.build_management("LONG", entry, sl, daily)
            pvm   = self.pullback_vs_market("LONG", daily, h4)
            if tps["warning"]:
                reasons_b.append("⚠ " + tps["warning"])
            return {
                "signal":       "COMPRA",
                "direction":    "LONG",
                "entry":        entry,
                "entry_type":   pb["entry_type"],
                "retrace_pips": pb["retrace_pips"],
                "current":      pb["current"],
                "sl":           sl,
                "tp1":          tps["tp1"],
                "tp":           tps["tp2"],
                "score":        score_b,
                "reasons":      reasons_b,
                "pos":          pos,
                "mgmt":         mgmt,
                "rr":           tps["rr2"],
                "valid":        True,
                **pvm,
            }

        elif score_s >= self.MIN_SCORE and score_s > score_b and not weak_trend and not block_short:
            pb    = self.calc_pullback_entry("SHORT", daily)
            entry = pb["entry"]
            sl    = round(entry + sl_distance, 5)
            tps   = self.calc_take_profits("SHORT", entry, sl, daily)
            expected_r = 0.5 * self.TP1_R + 0.5 * tps["rr2"]
            pos   = self.calc_position_size(entry, sl, expected_r)
            if pos["lots"] <= 0:
                reasons_s.append(f"⚠ SL de {pos['pips_sl']:.0f} pips es demasiado amplio para el "
                                  f"riesgo de ${self.RISK_USD:.2f} — el lote calculado redondea a 0.00, señal bloqueada")
                return {"signal": "NEUTRAL", "valid": False, "score_b": score_b, "score_s": score_s,
                        "weak_trend": False, "zero_lots": True}
            mgmt  = self.build_management("SHORT", entry, sl, daily)
            pvm   = self.pullback_vs_market("SHORT", daily, h4)
            if tps["warning"]:
                reasons_s.append("⚠ " + tps["warning"])
            return {
                "signal":       "VENTA",
                "direction":    "SHORT",
                "entry":        entry,
                "entry_type":   pb["entry_type"],
                "retrace_pips": pb["retrace_pips"],
                "current":      pb["current"],
                "sl":           sl,
                "tp1":          tps["tp1"],
                "tp":           tps["tp2"],
                "score":        score_s,
                "reasons":      reasons_s,
                "pos":          pos,
                "mgmt":         mgmt,
                "rr":           tps["rr2"],
                "valid":        True,
                **pvm,
            }

        return {
            "signal":      "NEUTRAL",
            "valid":       False,
            "score_b":     score_b,
            "score_s":     score_s,
            "weak_trend":  weak_trend,
            "block_short": block_short,
        }

    # ══════════════════════════════════════════════════════════════
    #  IMPRESIÓN — SOLO para el modo CLI (python <par>_trading_system.py)
    # ══════════════════════════════════════════════════════════════

    def print_news_alerts(self, alerts: list[dict]) -> bool:
        """Muestra alertas de noticias. Retorna True si hay noticias próximas."""
        if not alerts:
            print(f"\n  {Fore.GREEN}✅ Sin noticias críticas en las próximas {self.NEWS_WINDOW_HOURS}h — OK para operar{Style.RESET_ALL}")
            return False

        print(f"\n{Fore.RED}╔{'═'*63}╗")
        print(f"║{'  ⚠️  ALERTA — NOTICIAS DE ALTO IMPACTO PRÓXIMAS':^63}║")
        print(f"╚{'═'*63}╝{Style.RESET_ALL}")

        for a in alerts:
            print(f"  {Fore.RED}🔴 {a['label']} ({a['country']}) — {a['time_local']} hora local ({a['when']})  /  {a['time_utc']}{Style.RESET_ALL}")

        print(f"\n  {Fore.YELLOW}→ NO ABRIR operaciones nuevas hasta 30-60 min después del evento{Style.RESET_ALL}")
        print(f"  {Fore.YELLOW}→ Si ya tienes una operación abierta, verifica tu SL y considera cerrar antes{Style.RESET_ALL}")
        return True

    def print_header(self, market: dict | None = None):
        now    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        sesion = "Mercado cerrado" if (market and not market["open"]) else self.active_sessions()
        print(f"\n{Fore.CYAN}╔{'═'*63}╗")
        print(f"║{('  🤖 SISTEMA DE SWING TRADING — ' + self.NAME):^63}║")
        print(f"║{f'  Capital: ${self.CAPITAL:,} USD  |  Riesgo: {self.RISK_PCT:.0f}% = ${self.RISK_USD:.0f} por trade':^63}║")
        print(f"║{('  ' + now + '  |  ' + sesion):^63}║")
        print(f"╚{'═'*63}╝{Style.RESET_ALL}")

    def print_risk_rules(self):
        print(f"\n{Fore.CYAN}┌{'─'*63}┐")
        print(f"│{'  📋 REGLAS DE GESTIÓN DE RIESGO':^63}│")
        print(f"└{'─'*63}┘{Style.RESET_ALL}")

        reglas_tabla = [
            ["💰 Capital total",          f"${self.CAPITAL:,} USD"],
            ["⚠️  Riesgo máx por trade",   f"{self.RISK_PCT}% = ${self.RISK_USD:.0f} USD"],
            ["📊 Take profits",            f"TP1: cerrar 50% en {self.TP1_R}R  |  TP2: nivel técnico ≥ {self.MIN_RR}R"],
            ["🚫 Pérdida máx diaria",      f"{self.MAX_LOSS_DAY_PCT}% = ${self.CAPITAL*self.MAX_LOSS_DAY_PCT/100:.0f} → CERRAR todo y parar"],
            ["📅 Pérdida máx semanal",     f"{self.MAX_LOSS_WK_PCT}% = ${self.CAPITAL*self.MAX_LOSS_WK_PCT/100:.0f} → Revisar estrategia"],
            ["📈 Trades máx por día",      f"{self.MAX_TRADES_DAY} operaciones (evitar el overtrading)"],
            ["🎯 Score mínimo señal",       f"{self.MIN_SCORE}/{self.MAX_SCORE} puntos"],
            ["📏 SL mínimo",               f"{self.MIN_PIPS_SL} pips (filtrar ruido del mercado)"],
        ]
        print(tabulate(reglas_tabla, tablefmt="rounded_outline",
                       headers=["Parámetro", "Valor"]))

        print(f"\n{Fore.YELLOW}  REGLAS INQUEBRANTABLES:{Style.RESET_ALL}")
        reglas = [
            ("NUNCA",   "mover el SL en dirección contraria a la operación"),
            ("NUNCA",   "agregar posiciones a un trade perdedor (no 'promediar')"),
            ("NUNCA",   "operar durante noticias de alto impacto (NFP, RBA, Fed)"),
            ("NUNCA",   "arriesgar más del 1% aunque 'tengas una corazonada'"),
            ("SIEMPRE", "esperar cierre de vela diaria (el sistema lo aplica automático)"),
            ("SIEMPRE", "llevar un diario de trading con cada entrada y salida"),
            ("SIEMPRE", "cerrar el trade si el precio no avanza en 5 días"),
            ("REGLA",   "Si pierdes 2 trades seguidos → PARAR y analizar"),
            ("REGLA",   "No operar lunes en la apertura ni viernes al cierre"),
            ("REGLA",   "El tamaño del lote calculado es el MÁXIMO, nunca más"),
        ]
        colors = {
            "NUNCA":   Fore.RED,
            "SIEMPRE": Fore.GREEN,
            "REGLA":   Fore.YELLOW,
        }
        for tipo, desc in reglas:
            c = colors.get(tipo, Fore.WHITE)
            print(f"  {c}[{tipo:7}]{Style.RESET_ALL} {desc}")

    def print_market_context(self):
        print(f"\n{Fore.CYAN}  CONTEXTO DEL PAR {self.NAME}:{Style.RESET_ALL}")
        for nota in self.MARKET_CONTEXT_NOTES:
            print(f"  {Fore.WHITE}• {nota}")

    def print_timeframe_summary(self, weekly: dict, daily: dict, h4: dict):
        print(f"\n{Fore.CYAN}  ANÁLISIS MULTI-TIMEFRAME:{Style.RESET_ALL}")
        rows = []
        for tf in [weekly, daily, h4]:
            if not tf:
                continue
            rec_color = (Fore.GREEN if "BUY" in tf["rec"]
                         else Fore.RED if "SELL" in tf["rec"]
                         else Fore.YELLOW)
            rec_str = f"{rec_color}{tf['rec']}{Style.RESET_ALL}"
            rows.append([
                tf["label"],
                rec_str,
                f"{tf['rsi']:.1f}",
                f"{tf['adx']:.1f}",
                f"{tf['buy_count']}↑ / {tf['sell_count']}↓ / {tf['neutral_count']}→",
            ])
        print(tabulate(rows,
                       headers=["Timeframe", "TV Recomendación", "RSI", "ADX", "Indicadores"],
                       tablefmt="rounded_outline"))

    def print_pending_order(self, order: dict, current_price: float, trade: dict | None = None):
        """Muestra la orden límite pendiente con alertas automáticas de validez."""
        direction = order["direccion"]
        entry     = _safe_float(order["entrada"])
        sl        = _safe_float(order["stop_loss"])
        tp        = _safe_float(order["take_profit"])
        lotes     = order["lotes"]
        fecha     = order.get("fecha") or "?"    # nunca None: se concatena en texto más abajo
        hora      = order.get("hora") or "?"
        is_long   = direction == "LONG"
        es_limite = _safe_upper(order.get("tipo_entrada")) == "LIMITE"
        if es_limite:
            tipo = "COMPRA LÍMITE" if is_long else "VENTA LÍMITE"
        else:
            tipo = "COMPRA A MERCADO" if is_long else "VENTA A MERCADO"
        emoji_dir = "🟢" if is_long else "🔴"

        status = self.check_order_status(order, current_price)
        c      = status["color"]

        print(f"\n{c}╔{'═'*63}╗")
        print(f"║{('  ' + emoji_dir + '  ORDEN PENDIENTE: ' + tipo + '  —  ' + self.NAME):^63}║")
        dias_str = str(status["dias"])
        print(f"║{'  Registrada el ' + fecha + ' a las ' + hora + '  (' + dias_str + ' dias)':^63}║")
        print(f"╚{'═'*63}╝{Style.RESET_ALL}")

        pips_sl_val  = _safe_float(order.get("pips_sl", 0))
        dist_to_entry = abs(current_price - entry) / self.PIP_VALUE

        tp1_val = _safe_float(order.get("tp1", 0))
        rows = [
            ["Dirección",     f"{tipo}"],
            ["Entrada",       f"{entry:.5f}"],
            ["Precio actual", f"{current_price:.5f}  ({dist_to_entry:.1f} pips de distancia)"],
            ["Stop Loss",     f"{sl:.5f}  ({pips_sl_val:.0f} pips)"],
        ]
        if tp1_val > 0:
            rows.append(["TP1 (50%)", f"{tp1_val:.5f}"])
        rows.append(["TP2 / final", f"{tp:.5f}"])
        rows.append(["Lotes", f"{lotes}"])
        print(tabulate(rows, tablefmt="rounded_outline"))

        print(f"\n  {status['color']}{status['emoji']}  {status['alerta']}{Style.RESET_ALL}")
        if status["detalle"]:
            print(f"  {status['color']}   {status['detalle']}{Style.RESET_ALL}")

        if trade and trade.get("valid") and trade.get("direction") != direction:
            print(f"\n  {Fore.RED}⚠️  NUEVA SEÑAL {trade['signal']} CONTRARIA a esta orden pendiente "
                  f"(score {trade['score']}/{self.MAX_SCORE}) — el mercado cambió de dirección, considera cancelar.{Style.RESET_ALL}")

        print(f"\n  {Fore.YELLOW}→ En {os.path.basename(self.LOG_FILE)}: CANCELADA para ignorar | ACTIVA cuando el broker la ejecute{Style.RESET_ALL}")

    def print_active_trade(self, order: dict, current_price: float):
        """Muestra el estado del trade mientras está en curso (resultado=ACTIVA)."""
        direction = order["direccion"]
        entry     = _safe_float(order["entrada"])
        sl        = _safe_float(order["stop_loss"])
        tp1       = _safe_float(order.get("tp1", 0))
        tp2       = _safe_float(order["take_profit"])
        lots      = _safe_float(order["lotes"], 0)
        is_long   = direction == "LONG"
        sign      = 1 if is_long else -1
        tipo      = "COMPRA (LONG)" if is_long else "VENTA (SHORT)"
        emoji_dir = "🟢" if is_long else "🔴"

        pnl_pips = (current_price - entry) / self.PIP_VALUE * sign
        pnl_usd  = pnl_pips * lots * self.USD_PER_PIP_STANDARD
        sl_pips  = abs(entry - sl) / self.PIP_VALUE
        r_multiple = pnl_pips / sl_pips if sl_pips > 0 else 0.0

        c = Fore.GREEN if pnl_pips >= 0 else Fore.RED
        print(f"\n{c}╔{'═'*63}╗")
        print(f"║{('  ' + emoji_dir + '  TRADE EN CURSO: ' + tipo + '  —  ' + self.NAME):^63}║")
        print(f"╚{'═'*63}╝{Style.RESET_ALL}")

        rows = [
            ["Entrada",       f"{entry:.5f}"],
            ["Precio actual", f"{current_price:.5f}"],
            ["P&L flotante",  f"{pnl_pips:+.1f} pips  (${pnl_usd:+.2f})"],
            ["R actual",      f"{r_multiple:+.2f}R"],
            ["Stop Loss",     f"{sl:.5f}"],
            ["TP1 (50%)",     f"{tp1:.5f}" if tp1 > 0 else "—"],
            ["TP2 / final",   f"{tp2:.5f}"],
            ["Lotes",         f"{lots}"],
        ]
        print(tabulate(rows, tablefmt="rounded_outline"))

        if (current_price >= tp2 if is_long else current_price <= tp2):
            print(f"\n  {Fore.GREEN}🎉 TP FINAL alcanzado — verifica el broker y marca WIN en el CSV{Style.RESET_ALL}")
        elif (current_price <= sl if is_long else current_price >= sl):
            print(f"\n  {Fore.RED}🛑 STOP LOSS alcanzado — verifica el broker y marca LOSS en el CSV{Style.RESET_ALL}")
        elif r_multiple >= 2.0:
            print(f"\n  {Fore.GREEN}📈  +2R ALCANZADO — activa el trailing: sube el SL siguiendo la EMA20{Style.RESET_ALL}")
        elif tp1 > 0 and (current_price >= tp1 if is_long else current_price <= tp1):
            print(f"\n  {Fore.GREEN}🥇 TP1 alcanzado — cierra el 50% y mueve el SL a la entrada{Style.RESET_ALL}")
        elif r_multiple >= 1.0:
            print(f"\n  {Fore.GREEN}🔒 +1R — mueve el SL a tu entrada (trade sin riesgo){Style.RESET_ALL}")

    def print_signal(self, trade: dict, pause: bool = True) -> bool:
        """Muestra la señal. Retorna True si pausó esperando confirmación del usuario."""
        if not trade["valid"]:

            # ── Mercado lateral detectado ─────────────────────────────
            if trade.get("ranging"):
                print(f"\n{Fore.MAGENTA}╔{'═'*63}╗")
                print(f"║{'  🚫  MERCADO LATERAL — SEÑALES BLOQUEADAS':^63}║")
                print(f"╚{'═'*63}╝{Style.RESET_ALL}")
                print(f"\n  {Fore.MAGENTA}Razones del bloqueo:{Style.RESET_ALL}")
                for r in trade["ranging_reasons"]:
                    print(f"  {Fore.MAGENTA}  ✗ {r}{Style.RESET_ALL}")
                print(f"\n  {Fore.YELLOW}→ En mercado lateral el precio hace falsos breakouts — NO operar{Style.RESET_ALL}")
                print(f"  {Fore.YELLOW}→ Esperar que el ADX supere 25 y las Bollinger Bands se expandan{Style.RESET_ALL}")
                print(f"  {Fore.YELLOW}→ La señal se desbloqueará automáticamente cuando el mercado salga del rango{Style.RESET_ALL}")
                return False

            # ── Sin setup suficiente ──────────────────────────────────
            sb = trade["score_b"]
            ss = trade["score_s"]
            blocks_b = round(min(sb, self.MAX_SCORE) / self.MAX_SCORE * 20)
            blocks_s = round(min(ss, self.MAX_SCORE) / self.MAX_SCORE * 20)
            bar_b = "█" * blocks_b + "░" * (20 - blocks_b)
            bar_s = "█" * blocks_s + "░" * (20 - blocks_s)
            print(f"\n{Fore.YELLOW}╔{'═'*63}╗")
            print(f"║{'  ⏳  SIN SETUP — ESPERAR MEJOR OPORTUNIDAD':^63}║")
            print(f"╚{'═'*63}╝{Style.RESET_ALL}")
            print(f"  Score Compra: {Fore.GREEN}{bar_b}{Style.RESET_ALL} {sb}/{self.MAX_SCORE}")
            print(f"  Score Venta:  {Fore.RED}{bar_s}{Style.RESET_ALL} {ss}/{self.MAX_SCORE}")
            if trade.get("zero_lots"):
                print(f"  {Fore.YELLOW}→ SL demasiado amplio para el riesgo de ${self.RISK_USD:.2f} — el lote calculado redondea a 0.00, señal bloqueada{Style.RESET_ALL}")
            elif trade.get("weak_trend"):
                print(f"  {Fore.YELLOW}→ ADX diario por debajo de {self.MIN_ADX_TO_TRADE} — tendencia insuficiente, bloqueado aunque el score alcance el mínimo{Style.RESET_ALL}")
            elif sb == ss and sb >= self.MIN_SCORE:
                # Empate exacto con ambos por encima del mínimo: no es que falten puntos,
                # es que no hay una dirección claramente dominante — aclarar para no confundir.
                print(f"  {Fore.YELLOW}→ Empate en {sb} pts — el sistema no opera sin una dirección claramente dominante{Style.RESET_ALL}")
            else:
                print(f"  {Fore.YELLOW}→ Se necesitan ≥{self.MIN_SCORE} pts para una señal válida{Style.RESET_ALL}")
            return False

        is_long = trade["direction"] == "LONG"
        c    = Fore.GREEN if is_long else Fore.RED
        emoji = "🟢" if is_long else "🔴"
        tipo  = "COMPRA (LONG)" if is_long else "VENTA (SHORT)"

        print(f"\n{c}╔{'═'*63}╗")
        print(f"║{('  ' + emoji + '  SEÑAL: ' + tipo + '  —  ' + self.NAME):^63}║")
        print(f"╚{'═'*63}╝{Style.RESET_ALL}")

        pos = trade["pos"]

        # ── Tabla de precios ──────────────────────────────────────────
        print(f"\n  {Fore.CYAN}NIVELES DE PRECIO:{Style.RESET_ALL}")
        if trade["entry_type"] == "LIMITE":
            accion_orden = "compra" if is_long else "venta"
            entry_label  = f"🎯 Entrada LÍMITE ({accion_orden} en pullback)"
            entry_val    = (f"{trade['entry']:.5f}  "
                            f"(precio actual {trade['current']:.5f} → esperar {trade['retrace_pips']:.0f} pips de retroceso)")
        else:
            entry_label  = "🎯 Entrada a MERCADO (precio ya en pullback)"
            entry_val    = f"{trade['entry']:.5f}"
        tp1_pips = abs(trade["tp1"] - trade["entry"]) / self.PIP_VALUE
        tp2_pips = abs(trade["tp"] - trade["entry"]) / self.PIP_VALUE
        price_rows = [
            [entry_label,                     entry_val],
            ["🛑 Stop Loss",                  f"{trade['sl']:.5f}  ({pos['pips_sl']} pips)"],
            ["🥇 TP1 — cerrar 50%",           f"{trade['tp1']:.5f}  ({tp1_pips:.0f} pips = {self.TP1_R}R)"],
            ["✅ TP2 — cierre final",         f"{trade['tp']:.5f}  ({tp2_pips:.0f} pips, en nivel técnico)"],
            ["📐 R:B al TP final",            f"  1:{trade['rr']}"],
        ]
        print(tabulate(price_rows, tablefmt="rounded_outline"))

        # ── Análisis pullback vs entrada directa ─────────────────────
        pba = trade.get("pb_analysis")
        if pba and trade["entry_type"] == "LIMITE":
            rec   = pba["rec"]
            mpos  = trade["mkt_pos"]
            rec_color = Fore.GREEN if rec == "PULLBACK" else Fore.YELLOW if rec == "AMBAS" else Fore.CYAN

            print(f"\n  {Fore.CYAN}¿PULLBACK O ENTRADA DIRECTA? — Estocástico 4H: {pba['stoch']:.1f}{Style.RESET_ALL}")
            print(f"  {rec_color}  → {pba['msg']}{Style.RESET_ALL}")

            opt_a_label = "⭐ Opción A — PULLBACK (RECOMENDADA)" if rec == "PULLBACK" else "   Opción A — PULLBACK"
            opt_b_label = "⭐ Opción B — MERCADO ahora (RECOMENDADA)" if rec == "MERCADO" else "   Opción B — MERCADO ahora"

            opts = [
                [opt_a_label,
                 f"Esperar que precio llegue a {trade['entry']:.5f} (EMA20)\n"
                 f"     SL {trade['sl']:.5f}  |  TP {trade['tp']:.5f}  |  {pos['lots']} lotes"],
                [opt_b_label,
                 f"Entrar ahora en {trade['mkt_entry']:.5f}\n"
                 f"     SL {trade['mkt_sl']:.5f}  |  TP {trade['mkt_tp']:.5f}  |  {mpos['lots']} lotes"],
            ]
            if rec == "AMBAS":
                opts[0][0] = "   Opción A — PULLBACK (ambas válidas)"
                opts[1][0] = "   Opción B — MERCADO ahora (ambas válidas)"
            print(tabulate(opts, tablefmt="rounded_outline"))

        # ── Tabla de posición ─────────────────────────────────────────
        print(f"\n  {Fore.CYAN}TAMAÑO DE POSICIÓN — CÁLCULO CON {self.RISK_PCT:.0f}% RIESGO:{Style.RESET_ALL}")
        pos_rows = [
            ["💼 Lotes estándar",   f"{pos['lots']}  lotes"],
            ["💼 Mini lotes (x10)", f"{pos['mini_lots']}  mini lotes"],
            ["💼 Micro lotes (x100)",f"{pos['micro_lots']:.0f}  micro lotes"],
            ["⚠️  Riesgo objetivo",  f"${pos['risk_usd']:.2f}  ({self.RISK_PCT:.0f}% de ${self.CAPITAL:,})"],
            ["⚠️  Riesgo real",      f"${pos['risk_usd_real']:.2f}  (tras redondeo de lotes)"],
            ["💰 Ganancia potencial", f"${pos['gain_usd']:.2f}  (si TP1 y TP2 se alcanzan)"],
        ]
        print(tabulate(pos_rows, tablefmt="rounded_outline"))

        # ── Razones ───────────────────────────────────────────────────
        print(f"\n  {Fore.CYAN}RAZONES DE LA SEÑAL (Score: {trade['score']}/{self.MAX_SCORE}):{Style.RESET_ALL}")
        for r in trade["reasons"]:
            print(f"  {c}  ✓ {r}{Style.RESET_ALL}")

        # ── Gestión activa del trade ──────────────────────────────────
        mgmt = trade["mgmt"]
        print(f"\n  {Fore.CYAN}GESTIÓN DEL TRADE (proteger ganancia y dejar correr):{Style.RESET_ALL}")
        mgmt_rows = [
            [f"1️⃣  Cuando el precio llegue a {mgmt['be_trigger']:.5f} (+1R)",
             "Mover SL a tu entrada (breakeven) — el trade queda sin riesgo"],
            [f"2️⃣  En TP1 {trade['tp1']:.5f} (+{self.TP1_R}R)",
             "Cerrar 50% de la posición (ganancia parcial asegurada)"],
            [f"3️⃣  Cuando el precio llegue a {mgmt['trail_trigger']:.5f} (+2R)",
             f"Activar trailing: mover SL siguiendo la EMA20 ({mgmt['trail_ema']:.5f})"],
            ["4️⃣  Resto de la posición",
             f"Dejar correr hasta TP2 {trade['tp']:.5f} o hasta que toque el trailing"],
        ]
        print(tabulate(mgmt_rows, tablefmt="rounded_outline"))
        print(f"  {Fore.WHITE}  → El breakeven + parcial convierte un ganador en 'trade sin riesgo'{Style.RESET_ALL}")

        # ── Instrucciones paso a paso ─────────────────────────────────
        print(f"\n  {Fore.YELLOW}⚡ PASOS PARA EJECUTAR LA OPERACIÓN:{Style.RESET_ALL}")
        if trade["entry_type"] == "LIMITE":
            accion = ("Colocar orden LÍMITE de COMPRA (Buy Limit)" if is_long
                      else "Colocar orden LÍMITE de VENTA (Sell Limit)")
            entry_step = f"{accion} en {trade['entry']:.5f} con {pos['lots']} lotes (NO al mercado)"
        else:
            accion = "Abrir orden de COMPRA (Buy)" if is_long else "Abrir orden de VENTA (Sell)"
            entry_step = f"{accion} a mercado con {pos['lots']} lotes en {self.NAME}"
        steps = [
            f"Verificar que no hay noticias de alto impacto en las próximas 4 horas",
            entry_step,
            f"Colocar Stop Loss en {trade['sl']:.5f} (INMEDIATAMENTE al abrir)",
            f"Colocar TP del 50% en {trade['tp1']:.5f} (TP1) y el resto en {trade['tp']:.5f} (TP2)",
            f"Seguir el plan de GESTIÓN de arriba — breakeven en +1R",
            f"Anotar en tu diario: entrada, SL, TP, razón de la operación",
        ]
        for i, step in enumerate(steps, 1):
            print(f"  {Fore.WHITE}{i}. {step}")

        print(f"\n  {Fore.RED}  ⚠️  NUNCA abrir esta operación sin el SL colocado{Style.RESET_ALL}")

        if not pause:
            # Señal solo de referencia (noticias / mercado bloqueado) — sin beeps ni pausa
            return False

        _beep(3)

        print(f"\n{c}{'═'*65}")
        print(f"  🔔  SEÑAL ACTIVA — El sistema está en PAUSA")
        print(f"  Presiona Enter cuando hayas revisado la señal para continuar el monitoreo...")
        print(f"{'═'*65}{Style.RESET_ALL}")
        try:
            input()
        except EOFError:
            pass   # ejecución sin consola interactiva — continuar sin bloquear
        return True

    # ══════════════════════════════════════════════════════════════
    #  PUNTO DE ENTRADA — SOLO para el modo CLI
    # ══════════════════════════════════════════════════════════════

    def main_cycle(self):
        """Ejecuta un ciclo completo de análisis y señal."""
        clear_screen()
        market = self.check_market_session()
        self.print_header(market)
        self.print_risk_rules()

        # ── Mercado cerrado (fin de semana) → no analizar datos congelados ──
        if not market["open"]:
            print(f"\n  {Fore.MAGENTA}🌙 {market['reason']}{Style.RESET_ALL}")
            print(f"  {Fore.MAGENTA}   El análisis se reanuda automáticamente el {market['reopen_local']} hora local.{Style.RESET_ALL}")
            print(f"\n{Fore.CYAN}  Próxima verificación en 30 min — Ctrl+C para salir{Style.RESET_ALL}\n")
            time.sleep(1800 + random.uniform(0, 60))
            return

        print(f"\n{Fore.WHITE}  Conectando con TradingView y Forex Factory...{Style.RESET_ALL}")

        weekly = self.fetch_analysis("Semanal (W)",   Interval.INTERVAL_1_WEEK)
        time.sleep(3)
        daily  = self.fetch_analysis("Diario (D)",    Interval.INTERVAL_1_DAY)
        time.sleep(3)
        h4     = self.fetch_analysis("4 Horas (4H)",  Interval.INTERVAL_4_HOURS)

        if not all([weekly, daily, h4]):
            print(f"{Fore.RED}  Error: no se pudieron obtener todos los datos. Reintentando en 30s...{Style.RESET_ALL}")
            time.sleep(30)
            return

        time.sleep(3)
        dxy = self.fetch_usd_index()

        self.print_timeframe_summary(weekly, daily, h4)

        # ── Filtro de correlación con el dólar ────────────────────────
        if dxy:
            rec_usd = dxy["rec"]
            contexto = (f"viento en CONTRA para compras de {self.NAME}" if "BUY" in rec_usd
                        else f"viento a FAVOR de compras de {self.NAME}" if "SELL" in rec_usd
                        else "neutral")
            print(f"\n  {Fore.CYAN}Filtro USD — Índice del dólar (DXY) diario: {rec_usd}  → {contexto}{Style.RESET_ALL}")
        else:
            print(f"\n  {Fore.YELLOW}Filtro USD (DXY) no disponible en este ciclo — señal sin filtro de correlación{Style.RESET_ALL}")

        # ── Verificación de noticias ──────────────────────────────────
        alerts   = self.fetch_news_alerts()
        has_news = self.print_news_alerts(alerts)

        # ── Límites de riesgo (trades/día, pérdidas, rachas) ──────────
        risk_ok, risk_reasons = self.check_risk_limits()
        if not risk_ok:
            print(f"\n{Fore.RED}╔{'═'*63}╗")
            print(f"║{'  🛑  LÍMITES DE RIESGO ALCANZADOS — NO OPERAR':^63}║")
            print(f"╚{'═'*63}╝{Style.RESET_ALL}")
            for r in risk_reasons:
                print(f"  {Fore.RED}✗ {r}{Style.RESET_ALL}")

        # ── Ventana de precaución (viernes al cierre / lunes en la apertura) ──
        if market["block_new"]:
            print(f"\n  {Fore.YELLOW}🕐 {market['reason']}{Style.RESET_ALL}")

        # ── Ventana de decisión: señales solo con la vela diaria CERRADA ──
        window = self.check_signal_window()
        if not window["in_window"]:
            print(f"\n  {Fore.YELLOW}🕯️  Fuera de la ventana de decisión — la vela diaria cerró hace "
                  f"{window['hours']:.0f}h y sus indicadores ya son provisionales.{Style.RESET_ALL}")
            print(f"  {Fore.YELLOW}   Señales nuevas: tras el próximo cierre diario (~{window['next_local']} hora local). "
                  f"Órdenes y trades se siguen vigilando.{Style.RESET_ALL}")

        can_trade     = not has_news and risk_ok and not market["block_new"] and window["in_window"]
        current_price = daily["close"]

        # ── Orden pendiente o activa del CSV ─────────────────────────
        pending = self.get_pending_order()
        is_active = pending and _safe_upper(pending.get("resultado")) == "ACTIVA"

        # Trade en curso → mostrar P&L y no tocar nada más
        if is_active:
            self.print_active_trade(pending, current_price)
            self.print_market_context()
            print(f"\n{Fore.CYAN}  Próxima actualización en {self.REFRESH_SECONDS}s — Ctrl+C para salir{Style.RESET_ALL}\n")
            time.sleep(self.REFRESH_SECONDS + random.uniform(0, 45))
            return

        if pending:
            updated, old_entry, new_entry = self.update_pending_order(pending, daily)
            if updated:
                pips_moved  = abs(new_entry - old_entry) / self.PIP_VALUE
                arrow       = "↓" if new_entry < old_entry else "↑"
                print(f"\n  {Fore.CYAN}⚡ Señal actualizada: EMA20 se movió {pips_moved:.0f} pips {arrow}"
                      f"  ({old_entry:.5f} → {new_entry:.5f}){Style.RESET_ALL}")
                pending = self.get_pending_order()

        trade = self.generate_signal(weekly, daily, h4, dxy)

        # Registrar señal válida nueva (solo si se puede operar y no hay ya una pendiente)
        if trade["valid"] and can_trade and not pending:
            if self.log_signal(trade):
                print(f"\n  {Fore.GREEN}📝 Señal registrada en {os.path.basename(self.LOG_FILE)} (resultado: PENDIENTE){Style.RESET_ALL}")

        # Mostrar orden pendiente si existe (tiene prioridad sobre nueva señal)
        if pending:
            self.print_pending_order(pending, current_price, trade)
            self.print_market_context()
            print(f"\n{Fore.CYAN}  Próxima actualización en {self.REFRESH_SECONDS}s — Ctrl+C para salir{Style.RESET_ALL}\n")
            time.sleep(self.REFRESH_SECONDS + random.uniform(0, 45))
            return

        if trade["valid"] and not can_trade:
            print(f"\n{Fore.YELLOW}  ─── Señal técnica (solo referencia — NO operar ahora) ───{Style.RESET_ALL}")
            paused = self.print_signal(trade, pause=False)
        else:
            paused = self.print_signal(trade)

        self.print_market_context()

        # Esperar el intervalo normal salvo que print_signal ya pausara con el usuario.
        # Sin órdenes que vigilar y fuera de la ventana de decisión → refresco lento
        # (menos carga a la API; no hay nada que pueda cambiar la decisión hasta el cierre)
        if not paused:
            wait = self.REFRESH_SECONDS if window["in_window"] else self.IDLE_REFRESH_SECONDS
            wait += random.uniform(0, 45)
            print(f"\n{Fore.CYAN}  Próxima actualización en {wait // 60:.0f} min — Ctrl+C para salir{Style.RESET_ALL}\n")
            time.sleep(wait)

    def main(self):
        print(f"{Fore.CYAN}  Iniciando sistema de trading {self.NAME}...{Style.RESET_ALL}")
        try:
            while True:
                self.main_cycle()
        except KeyboardInterrupt:
            print(f"\n{Fore.YELLOW}  Sistema detenido. ¡Buen trading y gestiona bien el riesgo!{Style.RESET_ALL}\n")


# ══════════════════════════════════════════════════════════════════
#  MOTOR PARA ÍNDICES — solo constantes de escala
# ══════════════════════════════════════════════════════════════════

class IndexEngine(TradingEngine):
    """
    El mismo motor, con las constantes que están expresadas en pips de forex
    traducidas a PUNTOS de índice. No redefine ni una sola función: toda la
    diferencia de comportamiento del US500 vive en su PairConfig (screener,
    índice de régimen, puerta del corto, domingo) o en estos números.

    La traducción se hizo por dos vías independientes que dan el mismo
    resultado, tomando AUD/USD a 0.6500 con ATR diario de 60 pips como
    referencia y el S&P 500 a 7708 con ATR diario de 70.18 puntos (medido el
    2026-08-19):

        constante          forex     % del precio   x ATR    índice
        MIN_PIPS_SL        30 pips      0.46%        0.50     35 pts
        PULLBACK_BUFFER    15 pips      0.23%        0.25     18 pts
        TP_LEVEL_BUFFER    10 pips      0.15%        0.17     12 pts

    La regla de tres queda confirmada por los datos: sobre el periodo del
    backtest (2024-04/2026-07) el ATR diario MEDIANO es 62.5 pips en los 4
    pares y 70.77 puntos en el S&P 500 -- un factor de 1.13, que aplicado a
    30/15/10 da 34/17/11. Los valores de abajo son ese resultado redondeado.

    MIN_ADX_TO_TRADE y los umbrales del detector de rango NO se traducen así:
    un porcentaje no dice nada sobre dónde cae un ADX de 22 en la distribución
    de cada instrumento. Se calibran preguntando QUÉ FRACCIÓN DE DÍAS descarta
    cada umbral en forex, y tomando el valor del S&P 500 que descarta esa
    misma fracción. Medido sobre las series diarias reales del periodo:

        umbral                 forex   descarta   equivalente US500
        MIN_ADX_TO_TRADE         22      42.0%          21.2
        BB_WIDTH_TIGHT        0.010       0.0%         0.0223
        BB_WIDTH_NARROW       0.015       4.9%         0.0276
        EMA_GAP_FLAT             15      18.6%           45
        EMA_GAP_NEAR             30      31.9%           64
        RANGE_ADX_NO_TREND      20       33.6%          19.4
        RANGE_ADX_WEAK          25       53.2%          23.3
        RANGE_ADX_H4_STRONG     30       70.2%          27.4

    Conclusiones que NO se podían adivinar sin medir:

    1. MIN_ADX_TO_TRADE=22 ya está bien calibrado para el índice (equivale a
       21.2) y por eso NO se sobreescribe aquí. La sospecha inicial -- "un
       umbral de ADX de forex matará los buenos setups del índice" -- resultó
       falsa: descarta el 46.1% de los días del S&P frente al 42.0% de los
       pares, prácticamente lo mismo.
    2. La separación EMA20-EMA50 sí estaba mal por un factor de 2.5. La regla
       de tres habría dado 17/34 puntos, que solo descartan el 5.1%/9.8% de
       los días en vez del 18.6%/31.9% que descartan en forex. Los valores
       correctos son 45 y 64.
    3. Los tres umbrales de ADX del DETECTOR DE RANGO (RANGE_ADX_*, distintos
       de MIN_ADX_TO_TRADE -- viven en detect_ranging_market/score_direction,
       no en generate_signal) se quedaron sin recalibrar en la primera pasada
       de este archivo, pese a documentarse como "todo el detector calibrado
       por percentil". El desajuste crece con el umbral (+2.7pp en 20, +6.1pp
       en 25, +10.7pp en 30) -- el más desviado era el "escape" de ADX 4H
       fuerte (30), lo que hacía el bloqueo por rango más agresivo de lo
       previsto en el índice. Corregido aquí con los mismos valores de la
       tabla de arriba, redondeados: 19/23/27.
    """
    MIN_PIPS_SL     = 35      # puntos del índice
    PULLBACK_BUFFER = 18
    TP_LEVEL_BUFFER = 12

    BB_WIDTH_TIGHT  = 0.022
    BB_WIDTH_NARROW = 0.028
    EMA_GAP_FLAT    = 45      # puntos del índice
    EMA_GAP_NEAR    = 64

    RANGE_ADX_NO_TREND  = 19
    RANGE_ADX_WEAK      = 23
    RANGE_ADX_H4_STRONG = 27


def make_engine(config):
    """
    Construye el motor que corresponde a la clase de activo de `config`.
    Un instrumento cuyo "pip" es una unidad entera de cotización (pip_value
    de 1.0) es un índice; cualquier otra cosa es un par de divisas.
    Usarlo en vez de TradingEngine(cfg) directamente para que dashboard.py,
    backtest.py y los lanzadores no tengan que saber de esta distinción.
    """
    return (IndexEngine if config.pip_value >= 1.0 else TradingEngine)(config)
