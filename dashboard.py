#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DASHBOARD WEB LOCAL — los 5 sistemas de trading en un solo proceso.

Corre el análisis de AUD/USD, GBP/USD, EUR/USD, NZD/USD y US500 importando el
motor REAL (misma estrategia, cero duplicación de lógica) y sirve una página en
http://localhost:8877 con todo junto: precio, score, señales, órdenes
pendientes, trades activos, niveles SL/TP visualizados, curva de P&L combinada
e historial de score.

El US500 (S&P 500) es el quinto sistema y el primero que no es un par de
divisas: cotiza en puntos y no en pips, usa el VIX en vez del DXY como índice
de régimen, no tiene ventana de decisión el domingo y NO cuenta en la lectura
de exposición neta al dólar. Ver pair_configs.US500_CONFIG.

Ventajas sobre las ventanas CMD sueltas:
  - UNA sola cola de peticiones a TradingView con espaciado global (3s entre
    llamadas) — evita el rate-limit 429 que causaban varios procesos compitiendo.
  - El índice de régimen se consulta UNA vez por índice DISTINTO y por ciclo:
    2 peticiones (DXY + VIX) para los 5 sistemas, en vez de 5 idénticas.
  - Sin beeps ni pausas de consola: las alertas suenan y se ven en el navegador.

IMPORTANTE: no correr este dashboard Y las ventanas CMD individuales a la vez
— duplicaría las peticiones a TradingView y ambos escribirían los mismos CSV.
Es un reemplazo, no un complemento.

Solo librería estándar de Python + las dependencias que ya tenían los sistemas.
"""

import sys
import os
import json
import csv
import math
import time
import threading
import webbrowser
import hashlib
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from tvm_client import Interval

from trading_engine import (make_engine, _safe_float, _safe_upper, _trading_days_since,
                             fetch_news_calendar, DATA_DIR)
from pair_configs import AUD_CONFIG, GBP_CONFIG, EUR_CONFIG, NZD_CONFIG, US500_CONFIG
from publisher import publish_snapshot

aud = make_engine(AUD_CONFIG)
gbp = make_engine(GBP_CONFIG)
eur = make_engine(EUR_CONFIG)
nzd = make_engine(NZD_CONFIG)
us500 = make_engine(US500_CONFIG)   # quinto sistema -- índice, no par de divisas

PAIRS = [
    ("AUD/USD", aud),
    ("GBP/USD", gbp),
    ("EUR/USD", eur),
    ("NZD/USD", nzd),
    ("US500",   us500),
]

# Símbolo TradingView ("AUDUSD") -> (nombre para mostrar, instancia del motor).
# Usado por el endpoint /api/trade/<SIMBOLO>/close para resolver el par desde
# la URL sin depender del nombre con barra ("AUD/USD"), que no es válido en
# un path HTTP.
PAIRS_BY_SYMBOL = {mod.SYMBOL: (name, mod) for name, mod in PAIRS}

# Pares que en la práctica son la misma apuesta contra el dólar (ver plan,
# hallazgo H6): correlaciones documentadas en pair_configs.py
# (market_context_notes), NZD/USD-AUD/USD medida en el propio backtest
# (+0.81). Los límites de riesgo son por par -- nada impedía hoy tener las 4
# posiciones abiertas a la vez en la misma dirección sin que ningún control
# se disparara.
CORRELATED_GROUPS = [
    (("AUD/USD", "NZD/USD"), 0.85),
    (("EUR/USD", "GBP/USD"), 0.85),
]

# Máximo de posiciones (PENDIENTE+ACTIVA) simultáneas en la misma dirección
# dentro de TODA la cartera antes de avisar -- ver plan E2. Solo un aviso por
# ahora (no bloquea nada): bloquear señales por esto es un cambio de
# comportamiento de la estrategia, fuera del alcance de "mostrarlo en el
# dashboard".
MAX_SAME_DIRECTION_WARN = 2

# Transiciones de estado válidas para /api/trade/.../close -- lo único que
# antes requería abrir el CSV a mano mientras el proceso de análisis lo
# tenía abierto (ver plan, hallazgo H1/H12).
VALID_RESULTADOS = {"ACTIVA", "WIN", "LOSS", "BE", "CANCELADA"}

PORT = 8877
HTML_FILE = os.path.join(BASE_DIR, "dashboard.html")
SCORE_HISTORY_FILE = os.path.join(DATA_DIR, "score_history.csv")
SCORE_HISTORY_MAX_POINTS = 200      # puntos por par que se envían al navegador
FETCH_SPACING_SECONDS = 3.0         # espacio mínimo entre llamadas a TradingView
MANUAL_REFRESH_COOLDOWN = 120       # segundos entre refrescos manuales
TOTAL_CAPITAL = sum(mod.CAPITAL for _, mod in PAIRS)

# Histórico propio de precio + indicadores (ver plan, hallazgo H10 / Item 8):
# TradingView vía tvm_client es la única fuente y no se guarda ni una
# vela -- si cambia de formato o da 429 sostenido, no hay forma de
# reconstruir qué vio el sistema. Un archivo por par (no uno compartido con
# columna "par" como score_history.csv) porque cada par acumula a un ritmo
# distinto y este es bastante más ancho.
OHLC_HISTORY_MAX_POINTS = 300
OHLC_FIELDS = ["timestamp", "close", "ema20", "ema50", "ema200", "rsi", "adx", "atr",
               "macd", "macd_signal", "bb_upper", "bb_lower", "rec_w", "rec_d", "rec_4h",
               "dxy_rec", "score_b", "score_s"]

# Reglas de riesgo: idénticas en los 5 sistemas salvo las escalas del índice
# (ver min_sl_texto), se muestran una sola vez en el footer en vez de
# repetirlas en cada tarjeta.
RISK_RULES = {
    "capital_total":          TOTAL_CAPITAL,
    "capital_por_par":        aud.CAPITAL,
    "riesgo_pct":             aud.RISK_PCT,
    "riesgo_usd_por_par":     round(aud.RISK_USD, 2),
    "tp1_r":                  aud.TP1_R,
    "min_rr":                 aud.MIN_RR,
    "max_trades_dia":         aud.MAX_TRADES_DAY,
    "max_perdida_dia_pct":    aud.MAX_LOSS_DAY_PCT,
    "max_perdida_semana_pct": aud.MAX_LOSS_WK_PCT,
    "min_score":              aud.MIN_SCORE,
    "max_score":              aud.MAX_SCORE,
    "min_pips_sl":            aud.MIN_PIPS_SL,
    # Desde el quinto sistema el SL mínimo ya no es un único número: está en
    # pips para los pares y en puntos para el índice (ver IndexEngine).
    "min_sl_texto":           " · ".join(
        f"{n} {u}" for n, u in dict.fromkeys(
            (mod.MIN_PIPS_SL, mod.UNIT) for _, mod in PAIRS)),
    "news_window_hours":      aud.NEWS_WINDOW_HOURS,
    "reglas": [
        {"tipo": "NUNCA",   "texto": "mover el SL en dirección contraria a la operación"},
        {"tipo": "NUNCA",   "texto": "agregar posiciones a un trade perdedor (no \"promediar\")"},
        {"tipo": "NUNCA",   "texto": "operar durante noticias de alto impacto (NFP, RBA, RBNZ, BCE, BOE, Fed)"},
        {"tipo": "NUNCA",   "texto": "arriesgar más del 1% aunque \"tengas una corazonada\""},
        {"tipo": "SIEMPRE", "texto": "esperar cierre de vela diaria (el sistema lo aplica automático)"},
        {"tipo": "SIEMPRE", "texto": "llevar un diario de trading con cada entrada y salida"},
        {"tipo": "SIEMPRE", "texto": "cerrar el trade si el precio no avanza en 5 días"},
        {"tipo": "REGLA",   "texto": "Si pierdes 2 trades seguidos en un par → PARAR y analizar"},
        {"tipo": "REGLA",   "texto": "No operar lunes en la apertura ni viernes al cierre"},
        {"tipo": "REGLA",   "texto": "El tamaño del lote calculado es el MÁXIMO, nunca más"},
    ],
}

# ══════════════════════════════════════════════════════════════════
#  ESTADO COMPARTIDO
# ══════════════════════════════════════════════════════════════════

STATE_LOCK = threading.Lock()
STATE = {
    "started": None,
    "last_wave": None,
    "next_wave": None,
    "wave_running": False,
    "market": None,
    "window": None,
    "sessions": None,
    "total_capital": TOTAL_CAPITAL,
    "risk_rules": RISK_RULES,
    "pairs": {},
    "pnl": None,
    "portfolio": None,
    "score_history": {},
    "ohlc_history": {},
    "source_status": None,
}

REFRESH_EVENT = threading.Event()
_last_manual_refresh = 0.0

# Hilo del último publish_snapshot() disparado por run_wave() -- ver
# run_once_ci.py, que necesita esperarlo antes de salir del proceso.
LAST_PUBLISH_THREAD = None

_fetch_lock = threading.Lock()
_last_fetch_ts = 0.0

# Fiabilidad de la fuente de datos EN ESTA SESIÓN (ver plan, hallazgo H10 /
# Item 10) -- tvm_client es la única fuente y hasta ahora no había
# ninguna señal en el dashboard de si está fallando sistemáticamente, más
# allá del timestamp pequeño al pie de cada tarjeta.
SOURCE_STATUS_LOCK = threading.Lock()
SOURCE_STATUS = {"ok_count": 0, "fail_count": 0, "last_ok": None, "last_fail": None}


def _record_fetch_result(ok: bool):
    with SOURCE_STATUS_LOCK:
        if ok:
            SOURCE_STATUS["ok_count"] += 1
            SOURCE_STATUS["last_ok"] = now_iso()
        else:
            SOURCE_STATUS["fail_count"] += 1
            SOURCE_STATUS["last_fail"] = now_iso()

# Serializa TODA escritura a senales*.csv -- tanto la del ciclo de análisis
# (update_pending_order/log_signal) como la de los botones de estado del
# dashboard (close_trade). Sin esto, un clic del usuario y el hilo de
# análisis podrían escribir el mismo CSV a la vez -- _write_csv_atomic hace
# cada escritura individual atómica, pero no evita que una pise a la otra.
CSV_LOCK = threading.Lock()

_csv_mtimes = {}


def _utc_now_naive():
    """'Ahora' en UTC pero como datetime naive (sin tzinfo) -- comparable
    directo contra los timestamps parseados de los CSV (también naive, ver
    now_iso()) sin mezclar aware/naive. Explícito en vez de depender de que
    el reloj del sistema operativo que corre esto ya esté en UTC (cierto hoy
    en GitHub Actions, pero no si algún día vuelve a correr localmente en
    una laptop con otro huso horario -- ver banner de frescura en
    dashboard.html, que asume que estos timestamps SON UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_iso():
    return _utc_now_naive().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


def _spaced_call(fn, *args):
    """Serializa TODAS las llamadas a TradingView con un espaciado mínimo global."""
    global _last_fetch_ts
    with _fetch_lock:
        wait = FETCH_SPACING_SECONDS - (time.monotonic() - _last_fetch_ts)
        if wait > 0:
            time.sleep(wait)
        try:
            return fn(*args)
        finally:
            _last_fetch_ts = time.monotonic()


HISTORY_RETENTION_DAYS = 90   # ver plan, Item 10 -- score_history.csv crecía sin límite


def _purge_old_rows(path, timestamp_field="timestamp", max_age_days=HISTORY_RETENTION_DAYS):
    """
    Reescribe un CSV descartando filas más viejas que max_age_days -- se
    llama una vez al arrancar (no en cada escritura, para no pagar el coste
    de leer+filtrar+reescribir todo el archivo en cada ciclo). Antes
    score_history.csv crecía para siempre y se releía entero en cada
    arranque del dashboard.
    """
    if not os.path.exists(path):
        return
    cutoff = _utc_now_naive() - timedelta(days=max_age_days)
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
        kept = []
        for row in rows:
            try:
                ts = datetime.strptime(row.get(timestamp_field, ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                kept.append(row)   # fila con timestamp corrupto -- no descartar por las dudas
                continue
            if ts >= cutoff:
                kept.append(row)
        if len(kept) < len(rows) and fieldnames:
            with open(path + ".tmp", "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                w.writerows(kept)
            os.replace(path + ".tmp", path)
            log(f"{os.path.basename(path)}: purgadas {len(rows) - len(kept)} filas de más de {max_age_days} días")
    except Exception as e:
        log(f"No se pudo purgar {os.path.basename(path)}: {e}")


# ══════════════════════════════════════════════════════════════════
#  HISTORIAL DE SCORE (persistente entre reinicios)
# ══════════════════════════════════════════════════════════════════

def load_score_history():
    hist = {name: [] for name, _ in PAIRS}
    if not os.path.exists(SCORE_HISTORY_FILE):
        return hist
    try:
        with open(SCORE_HISTORY_FILE, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                par = row.get("par", "")
                if par in hist:
                    hist[par].append([
                        row.get("timestamp", ""),
                        _safe_float(row.get("score_b"), 0),
                        _safe_float(row.get("score_s"), 0),
                    ])
        for par in hist:
            hist[par] = hist[par][-SCORE_HISTORY_MAX_POINTS:]
    except Exception as e:
        log(f"No se pudo leer score_history.csv: {e}")
    return hist


def append_score_history(par, score_b, score_s, price):
    ts = now_iso()
    new_file = not os.path.exists(SCORE_HISTORY_FILE)
    try:
        with open(SCORE_HISTORY_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["timestamp", "par", "score_b", "score_s", "precio"])
            w.writerow([ts, par, score_b, score_s, f"{price:.5f}"])
    except Exception as e:
        log(f"No se pudo escribir score_history.csv: {e}")
    with STATE_LOCK:
        h = STATE["score_history"].setdefault(par, [])
        h.append([ts, score_b, score_s])
        if len(h) > SCORE_HISTORY_MAX_POINTS:
            del h[:len(h) - SCORE_HISTORY_MAX_POINTS]


# ══════════════════════════════════════════════════════════════════
#  HISTORIAL DE PRECIO + INDICADORES PROPIO (persistente, un CSV por par)
# ══════════════════════════════════════════════════════════════════

def _ohlc_file(mod):
    return os.path.join(DATA_DIR, f"ohlc_{mod.SYMBOL.lower()}.csv")


def load_ohlc_history(mod):
    path = _ohlc_file(mod)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        rows = rows[-OHLC_HISTORY_MAX_POINTS:]
        out = []
        for row in rows:
            r = {"timestamp": row.get("timestamp", "")}
            for k in OHLC_FIELDS[1:]:
                if k in ("rec_w", "rec_d", "rec_4h", "dxy_rec"):
                    r[k] = row.get(k, "")
                else:
                    r[k] = _safe_float(row.get(k), None)
            out.append(r)
        return out
    except Exception as e:
        log(f"No se pudo leer {os.path.basename(path)}: {e}")
        return []


def append_ohlc_snapshot(mod, weekly, daily, h4, dxy, score_b, score_s):
    """
    Guarda, una vez por ciclo, el precio y los indicadores TAL COMO los vio
    el sistema en ese momento (no OHLC real -- tvm_client no expone
    open/high/low, solo close + indicadores; ver limitación en el docstring
    de backtest.py). Es la única fuente propia y reproducible del sistema:
    hoy TradingView es la única fuente y no queda registro de qué vio si
    cambia de formato o da 429 sostenido (ver plan, hallazgo H10).
    """
    path = _ohlc_file(mod)
    row = {
        "timestamp": now_iso(),
        "close": round(daily["close"], 5),
        "ema20": round(daily["ema20"], 5),
        "ema50": round(daily["ema50"], 5),
        "ema200": round(daily["ema200"], 5),
        "rsi": round(daily["rsi"], 2),
        "adx": round(daily["adx"], 2),
        "atr": round(daily["atr"], 5),
        "macd": round(daily["macd"], 6),
        "macd_signal": round(daily["macd_signal"], 6),
        "bb_upper": round(daily["bb_upper"], 5),
        "bb_lower": round(daily["bb_lower"], 5),
        "rec_w": weekly["rec"], "rec_d": daily["rec"], "rec_4h": h4["rec"],
        "dxy_rec": dxy["rec"] if dxy else "",
        "score_b": score_b, "score_s": score_s,
    }
    new_file = not os.path.exists(path)
    try:
        with open(path, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OHLC_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log(f"No se pudo escribir {os.path.basename(path)}: {e}")
    with STATE_LOCK:
        h = STATE["ohlc_history"].setdefault(mod.SYMBOL, [])
        h.append(row)
        if len(h) > OHLC_HISTORY_MAX_POINTS:
            del h[:len(h) - OHLC_HISTORY_MAX_POINTS]


# ══════════════════════════════════════════════════════════════════
#  REGISTRO DE DECISIONES BLOQUEADAS (ver plan, F5 / Item 10)
#  Cuando una señal válida NO se registra (noticias / riesgo / ventana /
#  mercado / ya hay una orden pendiente), esa decisión se perdía -- no había
#  forma de saber después si los filtros estaban ahorrando pérdidas o
#  costando ganancias. Un archivo compartido (con columna "par"), igual que
#  score_history.csv, porque es una tabla de eventos, no una serie por par.
# ══════════════════════════════════════════════════════════════════

DECISIONS_LOG_FILE = os.path.join(DATA_DIR, "decisiones_bloqueadas.csv")
DECISIONS_FIELDS = ["timestamp", "par", "direccion", "score", "motivo"]


def log_blocked_decision(name, trade, alerts, risk_ok, market, window, pending):
    """Si hubo una señal técnicamente válida que NO se llegó a registrar,
    guarda por qué -- para poder revisar más adelante si el filtro que la
    bloqueó (noticias/riesgo/ventana/mercado/orden ya pendiente) ayudó o
    estorbó, en vez de perder esa información para siempre."""
    if not trade.get("valid"):
        return
    motivos = []
    if alerts:
        motivos.append("NOTICIAS")
    if not risk_ok:
        motivos.append("RIESGO")
    if market.get("block_new"):
        motivos.append("MERCADO")
    if not window.get("in_window"):
        motivos.append("VENTANA")
    if pending:
        motivos.append("ORDEN_PENDIENTE")
    if not motivos:
        return   # no estaba bloqueada -- se registró normalmente en senales*.csv
    new_file = not os.path.exists(DECISIONS_LOG_FILE)
    try:
        with open(DECISIONS_LOG_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=DECISIONS_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow({"timestamp": now_iso(), "par": name, "direccion": trade["direction"],
                       "score": trade["score"], "motivo": "+".join(motivos)})
    except Exception as e:
        log(f"No se pudo escribir decisiones_bloqueadas.csv: {e}")


# ══════════════════════════════════════════════════════════════════
#  P&L COMBINADO (leído de los 4 senales*.csv)
# ══════════════════════════════════════════════════════════════════

def _trade_usd(mod, row):
    """
    USD realizado de un trade cerrado: pips reales si están anotados, si no
    la aproximación. Para LOSS sin pips anotados, el riesgo REAL
    (riesgo_usd_real = lotes×pips_sl×valor_pip) en vez del nominal
    (riesgo_usd = 1% de CAPITAL) -- el nominal sobreestima el riesgo real
    hasta en un ~30% con este tamaño de cuenta por el redondeo de lotes
    (ver plan, hallazgo H2). riesgo_usd_real puede faltar en filas viejas
    (CSV escrito antes de este campo) -- ahí sí cae al nominal.

    pips_tp1_parcial (ver record_tp1_partial): si está presente, un 50% de
    la posición ya cerró en TP1 con esos pips y pips_resultado describe solo
    el OTRO 50% -- el resultado final (WIN/LOSS/BE) de la fila es el de esa
    segunda mitad, no el del trade completo. Sin esta ponderación, un trade
    que tocó TP1 y luego se detuvo en breakeven se contaba como $0 pese a
    tener medio lote de ganancia ya asegurada.
    """
    res = _safe_upper(row.get("resultado"))
    pips = _safe_float(row.get("pips_resultado"), 0)
    lots = _safe_float(row.get("lotes"), 0)
    tp1_parcial = row.get("pips_tp1_parcial")
    if tp1_parcial not in (None, "") and lots:
        pips_tp1 = _safe_float(tp1_parcial, 0)
        return (0.5 * pips_tp1 + 0.5 * pips) * lots * mod.USD_PER_PIP_STANDARD
    if pips and lots:
        return pips * lots * mod.USD_PER_PIP_STANDARD
    if res == "WIN":
        return _safe_float(row.get("ganancia_potencial_usd"), 0)
    if res == "LOSS":
        real = row.get("riesgo_usd_real")
        if real not in (None, ""):
            return -_safe_float(real, mod.RISK_USD)
        return -_safe_float(row.get("riesgo_usd"), mod.RISK_USD)
    return 0.0


def compute_pnl():
    closed = []
    per_pair = {}
    for name, mod in PAIRS:
        stats = {"total": 0.0, "wins": 0, "losses": 0, "be": 0, "abiertas": 0}
        try:
            if os.path.exists(mod.LOG_FILE):
                with open(mod.LOG_FILE, "r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        res = _safe_upper(row.get("resultado"))
                        if res in ("PENDIENTE", "ACTIVA"):
                            stats["abiertas"] += 1
                        elif res in ("WIN", "LOSS", "BE"):
                            usd = _trade_usd(mod, row)
                            fecha = row.get("fecha_cierre") or row.get("fecha") or ""
                            closed.append({"par": name, "fecha": fecha, "usd": usd,
                                           "resultado": res,
                                           "motivo": row.get("motivo_cierre", "")})
                            stats["total"] += usd
                            key = {"WIN": "wins", "LOSS": "losses", "BE": "be"}[res]
                            stats[key] += 1
        except Exception as e:
            log(f"Error leyendo {mod.LOG_FILE}: {e}")
        per_pair[name] = {k: (round(v, 2) if isinstance(v, float) else v)
                          for k, v in stats.items()}

    closed.sort(key=lambda t: t["fecha"])
    equity = float(TOTAL_CAPITAL)
    curve = [["inicio", equity]]
    for t in closed:
        equity += t["usd"]
        curve.append([t["fecha"], round(equity, 2)])

    n = len(closed)
    wins = sum(1 for t in closed if t["resultado"] == "WIN")
    total = sum(t["usd"] for t in closed)
    return {
        "curve": curve,
        "equity": round(equity, 2),
        "total": round(total, 2),
        "n_cerrados": n,
        "win_rate": round(wins / n * 100, 1) if n else None,
        "per_pair": per_pair,
        "recientes": closed[-8:][::-1],
    }


def compute_portfolio(pairs_state: dict) -> dict:
    """
    Vista de cartera: los 4 pares no son 4 riesgos independientes, son en
    gran parte la misma apuesta contra el dólar (ver plan, hallazgo H6).
    Agrega la exposición neta, el riesgo real abierto total, avisa cuando
    dos pares correlacionados quedan abiertos en la misma dirección a la vez,
    y resume por qué cada par no puede operar ahora mismo (noticias / riesgo
    / ventana / mercado) sin tener que desplegar cada tarjeta.

    market/window YA NO se reciben como parámetros globales: desde que el
    US500 tiene su propia ventana (sin domingo) y su propio calendario de
    mercado (festivos NYSE), "ventana_ok"/"mercado_ok" se leen del estado
    POR INSTRUMENTO (st["window_ok"]/st["market_ok"]) dentro de pairs_state,
    no de un único valor compartido.
    """
    exposure = {}
    for name, st in (pairs_state or {}).items():
        if not st or not st.get("ok"):
            continue
        if st.get("mode") == "ACTIVA" and st.get("trade"):
            t = st["trade"]
            exposure[name] = {"direction": t.get("direccion"), "status": "ACTIVA",
                              "riesgo_usd_real": t.get("riesgo_usd_real") or 0}
        elif st.get("mode") == "PENDIENTE" and st.get("order"):
            o = st["order"]
            exposure[name] = {"direction": o.get("direccion"), "status": "PENDIENTE",
                              "riesgo_usd_real": o.get("riesgo_usd_real") or 0}

    # LONG en un par XXX/USD = apuesta a que XXX sube = apuesta a que el USD
    # baja frente a esa divisa -- por eso más LONG abiertos = más CORTO neto
    # de dólar, no al revés.
    #
    # El quinto sistema (US500) NO entra en esta cuenta: un largo de índice no
    # es un corto de dólar (cfg.usd_exposure=False). Su riesgo sí suma al
    # total y su fila sí aparece en el grid -- lo único que no hace es
    # contaminar la lectura de exposición al dólar.
    usd_names = {name for name, mod in PAIRS if mod.cfg.usd_exposure}
    usd_exposure = {k: v for k, v in exposure.items() if k in usd_names}
    long_count = sum(1 for e in usd_exposure.values() if e["direction"] == "LONG")
    short_count = sum(1 for e in usd_exposure.values() if e["direction"] == "SHORT")
    if long_count > short_count:
        usd_net_direction, usd_net_count = "CORTO", long_count - short_count
    elif short_count > long_count:
        usd_net_direction, usd_net_count = "LARGO", short_count - long_count
    else:
        usd_net_direction, usd_net_count = "NEUTRAL", 0

    total_risk_usd = sum(e["riesgo_usd_real"] for e in exposure.values())
    total_risk_pct = round(total_risk_usd / TOTAL_CAPITAL * 100, 2) if TOTAL_CAPITAL else 0.0

    correlation_warnings = []
    for (pair_a, pair_b), corr in CORRELATED_GROUPS:
        ea, eb = exposure.get(pair_a), exposure.get(pair_b)
        if ea and eb and ea["direction"] == eb["direction"]:
            correlation_warnings.append({
                "pairs": [pair_a, pair_b],
                "direction": ea["direction"],
                "correlation": corr,
                "riesgo_usd_combinado": round(ea["riesgo_usd_real"] + eb["riesgo_usd_real"], 2),
            })

    same_direction_alert = exposure and max(long_count, short_count) >= MAX_SAME_DIRECTION_WARN

    grid = []
    for name, _ in PAIRS:
        st = (pairs_state or {}).get(name)
        if not st or not st.get("ok"):
            grid.append({"name": name, "ok": False})
            continue
        grid.append({
            "name": name,
            "ok": True,
            "noticias_ok": not st.get("news"),
            "riesgo_ok": st.get("risk_ok", True),
            # Por instrumento, no global: el US500 no tiene ventana el domingo
            # ni opera en festivos del NYSE, aunque el forex siga abierto.
            "ventana_ok": bool(st.get("window_ok")),
            "mercado_ok": bool(st.get("market_ok", True)),
        })

    return {
        "exposure": exposure,
        "long_count": long_count,
        "short_count": short_count,
        # long_count/short_count cuentan SOLO los instrumentos que expresan
        # una apuesta sobre el dólar. n_abiertas cuenta todo lo que hay vivo,
        # incluido el US500 -- son dos preguntas distintas y antes, con solo
        # pares de divisas, daban la misma respuesta.
        "n_abiertas": len(exposure),
        "usd_net_direction": usd_net_direction,
        "usd_net_count": usd_net_count,
        "total_risk_usd": round(total_risk_usd, 2),
        "total_risk_pct": total_risk_pct,
        "correlation_warnings": correlation_warnings,
        "same_direction_alert": bool(same_direction_alert),
        "grid": grid,
    }


def senales_mtimes():
    out = {}
    for _, mod in PAIRS:
        try:
            out[mod.LOG_FILE] = os.path.getmtime(mod.LOG_FILE)
        except OSError:
            out[mod.LOG_FILE] = None
    return out


def refresh_pnl_if_changed():
    global _csv_mtimes
    current = senales_mtimes()
    if current != _csv_mtimes:
        _csv_mtimes = current
        pnl = compute_pnl()
        with STATE_LOCK:
            STATE["pnl"] = pnl


# ══════════════════════════════════════════════════════════════════
#  VISTAS POR PAR (equivalentes a print_pending_order / print_active_trade)
# ══════════════════════════════════════════════════════════════════

def _order_levels(mod, row):
    sf = _safe_float
    return {
        "direccion":    row.get("direccion", ""),
        "entrada":      sf(row.get("entrada")),
        "sl":           sf(row.get("stop_loss")),
        "tp1":          sf(row.get("tp1"), 0),
        "tp2":          sf(row.get("take_profit")),
        "lotes":        row.get("lotes", ""),
        "fecha":        row.get("fecha", ""),
        "hora":         row.get("hora", ""),
        "tipo_entrada": row.get("tipo_entrada", ""),
        "precio_senal": sf(row.get("precio_senal"), 0),
        "pips_sl":      sf(row.get("pips_sl"), 0),
        "riesgo_usd":       sf(row.get("riesgo_usd"), mod.RISK_USD),
        "riesgo_usd_real":  sf(row.get("riesgo_usd_real"), 0) or sf(row.get("riesgo_usd"), mod.RISK_USD),
        # Recorrido persistido (0 mientras el CSV aún no lo tiene -- primer
        # ciclo del trade o CSV en formato viejo).
        "mfe_precio":   sf(row.get("mfe_precio"), 0),
        "mae_precio":   sf(row.get("mae_precio"), 0),
        # None si todavía no se cerró el 50% en TP1 (ver record_tp1_partial) --
        # distinto de 0.0, que sí sería un cierre parcial anotado en breakeven.
        "pips_tp1_parcial": sf(row.get("pips_tp1_parcial"), None) if row.get("pips_tp1_parcial") not in (None, "") else None,
    }


def _pullback_option(analysis):
    """Opción A (esperar el pullback) vs Opción B (entrar a mercado ahora),
    con los niveles concretos de la alternativa de mercado y qué recomienda
    el Estocástico 4H. None si esta señal no trae ese análisis."""
    pba = analysis.get("pb_analysis") if analysis else None
    if not pba:
        return None
    mpos = analysis.get("mkt_pos", {})
    return {
        "rec":       pba["rec"],           # PULLBACK | MERCADO | AMBAS
        "stoch":     round(pba["stoch"], 1),
        "msg":       pba["msg"],
        "mkt_entry": analysis.get("mkt_entry"),
        "mkt_sl":    analysis.get("mkt_sl"),
        "mkt_tp":    analysis.get("mkt_tp"),
        "mkt_lots":  mpos.get("lots"),
    }


def pending_view(mod, row, price, trade=None, daily=None, h4=None):
    """
    trade: la señal recién generada este ciclo (puede ser distinta dirección a la orden),
    solo se usa para avisar de una señal contraria.
    daily/h4: datos frescos de este ciclo -- se usan para el contexto Estocástico
    pullback-vs-mercado de la orden, INDEPENDIENTE de si `trade` califica como
    "valid" hoy (antes, si el score bajaba de un ciclo a otro, esta caja
    desaparecía del todo y la orden se veía como una espera obligatoria sin
    ningún contexto, aunque el Estocástico siguiera recomendando entrar ya).
    """
    v = _order_levels(mod, row)
    status = mod.check_order_status(row, price)
    dist_pips = round(abs(price - v["entrada"]) / mod.PIP_VALUE, 1)
    v.update({
        "estado":  status["estado"],
        "alerta":  status["alerta"],
        "detalle": status["detalle"],
        "dias":    status["dias"],
        "dist_pips": dist_pips,
        # Pullback estirado: la entrada límite quedó más lejos que el SL planeado
        "pullback_largo": (v["pips_sl"] > 0 and dist_pips > v["pips_sl"]
                            and status["estado"] in ("OK", "CERCA")),
        "contrary_signal": None,
        "pullback_vs_market": None,
    })
    if trade and trade.get("valid") and trade.get("direction") != v["direccion"]:
        v["contrary_signal"] = {"signal": trade["signal"], "score": trade["score"]}
    if v["tipo_entrada"] == "LIMITE" and daily and h4:
        v["pullback_vs_market"] = _pullback_option(mod.pullback_vs_market(v["direccion"], daily, h4))
    return v


def active_view(mod, row, price):
    """
    El recorrido de la posición (MFE/MAE, y los flags de nivel alcanzado que
    dependen de él) se lee del CSV, donde lo deja update_trade_tracking una vez
    por ciclo ANTES de llamar aquí -- esa función es la única que mira el
    high/low de la vela y la única con la guarda de contaminación del día de
    entrada. active_view solo lo consume; duplicar el cálculo de extremos aquí
    reintroducía el high pre-entrada que allí se descarta.
    El P&L flotante y el R actual sí son del close (métricas del "ahora").
    """
    v = _order_levels(mod, row)
    is_long = v["direccion"] == "LONG"
    sign = 1 if is_long else -1
    entry, sl, tp1, tp2 = v["entrada"], v["sl"], v["tp1"], v["tp2"]
    pnl_pips = (price - entry) / mod.PIP_VALUE * sign
    sl_pips = abs(entry - sl) / mod.PIP_VALUE
    tp_pips = abs(tp2 - entry) / mod.PIP_VALUE
    dist_tp = abs(tp2 - price) / mod.PIP_VALUE
    dist_sl = abs(price - sl) / mod.PIP_VALUE
    lots = _safe_float(v["lotes"], 0)

    # Recorrido: MFE/MAE persistido. Si aún no hay (primer ciclo antes de que
    # update_trade_tracking escriba, o refresco tras cierre manual), el punto
    # actual sirve de aproximación -- nunca peor que el comportamiento previo.
    #   LONG  -> fav = precio más alto alcanzado, adv = el más bajo
    #   SHORT -> fav = precio más bajo alcanzado, adv = el más alto
    fav = v["mfe_precio"] if v["mfe_precio"] > 0 else (max(entry, price) if is_long else min(entry, price))
    adv = v["mae_precio"] if v["mae_precio"] > 0 else (min(entry, price) if is_long else max(entry, price))

    mfe_pips = (fav - entry) / mod.PIP_VALUE * sign
    mae_pips = (adv - entry) / mod.PIP_VALUE * sign
    mfe_r = mfe_pips / sl_pips if sl_pips > 0 else 0.0
    mae_r = mae_pips / sl_pips if sl_pips > 0 else 0.0

    try:
        dias = _trading_days_since(datetime.strptime(v["fecha"], "%Y-%m-%d").date())
    except (ValueError, TypeError):
        dias = 0

    # Toques de nivel: contra el recorrido completo -> pegajosos de verdad.
    tp_tocado  = fav >= tp2 if is_long else fav <= tp2
    sl_tocado  = adv <= sl  if is_long else adv >= sl
    tp1_tocado = tp1 > 0 and (fav >= tp1 if is_long else fav <= tp1)
    r_multiple = pnl_pips / sl_pips if sl_pips > 0 else 0.0
    # ...y el mismo toque medido SOLO con el precio actual: distingue "el
    # precio está ahí ahora" (alerta en presente) de "llegó y retrocedió"
    # (nota de recorrido en pasado). tp1/be/+2R se comparan por múltiplo de R
    # en el frontend; el TP final es un nivel técnico, así que su flag en vivo
    # se calcula aquí.
    tp_tocado_vivo = price >= tp2 if is_long else price <= tp2
    sl_tocado_vivo = price <= sl  if is_long else price >= sl

    # Hito máximo alcanzado en todo el recorrido (+1R == TP1 con TP1_R=1.0).
    if tp2 and (fav >= tp2 if is_long else fav <= tp2):
        nivel_max = "TP2"
    elif mfe_r >= 2.0:
        nivel_max = "+2R"
    elif tp1_tocado or mfe_r >= 1.0:
        nivel_max = "TP1"
    else:
        nivel_max = ""

    # La regla de 5 días mira la situación ACTUAL (¿sigue estancado?), no el
    # recorrido: se calla solo si el precio está AHORA en zona de decisión.
    five_day = None
    if dias >= 5 and not (tp_tocado_vivo or sl_tocado_vivo):
        if dist_tp >= tp_pips:
            five_day = "CERRAR"
        else:
            five_day = "AVANZA"

    v.update({
        "dias":        dias,
        "pnl_pips":    round(pnl_pips, 1),
        "pnl_usd":     round(pnl_pips * lots * mod.USD_PER_PIP_STANDARD, 2),
        "sl_pips":     round(sl_pips, 1),
        "dist_tp":     round(dist_tp, 1),
        "dist_sl":     round(dist_sl, 1),
        "r_multiple":  round(r_multiple, 2),
        "tp_tocado":   tp_tocado,
        "tp_tocado_vivo": tp_tocado_vivo,
        "sl_tocado":   sl_tocado,
        "tp1_tocado":  tp1_tocado,
        "be_tocado":   mfe_r >= 1.0,
        "trail_tocado": mfe_r >= 2.0,
        "five_day":    five_day,
        # Recorrido de la posición (MFE/MAE) -- para la línea informativa,
        # separada de la alerta accionable en vivo.
        "mfe_pips":    round(mfe_pips, 1),
        "mfe_r":       round(mfe_r, 2),
        "mfe_precio":  round(fav, 5),
        "mae_pips":    round(mae_pips, 1),
        "mae_r":       round(mae_r, 2),
        "mae_precio":  round(adv, 5),
        "nivel_max":   nivel_max,
    })
    return v


def signal_view(mod, trade):
    pos = trade["pos"]
    return {
        "signal":       trade["signal"],
        "direction":    trade["direction"],
        "entry":        trade["entry"],
        "entry_type":   trade["entry_type"],
        "retrace_pips": trade["retrace_pips"],
        "current":      trade["current"],
        "sl":           trade["sl"],
        "tp1":          trade["tp1"],
        "tp2":          trade["tp"],
        "score":        trade["score"],
        "rr":           trade["rr"],
        "reasons":      trade["reasons"],
        "lots":         pos["lots"],
        "risk_usd":         pos["risk_usd"],       # nominal -- objetivo (1% de CAPITAL)
        "risk_usd_real":    pos["risk_usd_real"],  # real -- tras redondeo de lotes (ver H2/H3)
        "risk_pct_real":    round(pos["risk_usd_real"] / mod.CAPITAL * 100, 2) if mod.CAPITAL else 0,
        "risk_gap_pct":     round(abs(pos["risk_usd"] - pos["risk_usd_real"]) / pos["risk_usd"] * 100, 1)
                            if pos["risk_usd"] else 0,
        "gain_usd":     pos["gain_usd"],
        "pb":           trade.get("pb_analysis"),
        "pullback_vs_market": _pullback_option(trade) if trade["entry_type"] == "LIMITE" else None,
        "mgmt":         trade.get("mgmt"),
    }


def recent_signals(mod, limit=5):
    out = []
    try:
        if os.path.exists(mod.LOG_FILE):
            with open(mod.LOG_FILE, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            for row in rows[-limit:][::-1]:
                out.append({
                    "fecha":     row.get("fecha", ""),
                    "direccion": row.get("direccion", ""),
                    "resultado": row.get("resultado", ""),
                    "pips":      row.get("pips_resultado", ""),
                })
    except Exception:
        pass
    return out


# ══════════════════════════════════════════════════════════════════
#  UNA "OLA" DE ANÁLISIS (equivalente a main_cycle de los 4, en secuencia)
# ══════════════════════════════════════════════════════════════════

def build_pair_state(name, mod, dxy, news_events):
    # La ventana de decisión y el estado de mercado NO son comunes a todos:
    # un índice de contado no tiene vela de domingo (ver
    # PairConfig.trades_sunday) y el NYSE cierra festivos que el forex no
    # observa (ver PairConfig.respects_nyse_holidays /
    # TradingEngine.check_market_session) -- por eso se calculan aquí, por
    # instrumento, en vez de recibir el market/window global de run_wave()
    # (ese sigue existiendo, pero solo para la cabecera de la página).
    window = mod.check_signal_window()
    market_local = mod.check_market_session()
    st = {"name": name, "symbol": mod.SYMBOL, "capital": mod.CAPITAL, "ok": False, "error": None,
          "updated": now_iso(), "mode": "SIN DATOS", "new_signal": False,
          "unit": mod.UNIT, "decimals": mod.cfg.price_decimals,
          "window_ok": window["in_window"],
          "market_ok": not market_local["block_new"],
          "market_note": market_local["reason"] if market_local["block_new"] else None,
          "context_notes": mod.MARKET_CONTEXT_NOTES}

    weekly = _spaced_call(mod.fetch_analysis, "Semanal (W)", Interval.INTERVAL_1_WEEK)
    daily  = _spaced_call(mod.fetch_analysis, "Diario (D)", Interval.INTERVAL_1_DAY)
    h4     = _spaced_call(mod.fetch_analysis, "4 Horas (4H)", Interval.INTERVAL_4_HOURS)
    if not all([weekly, daily, h4]):
        st["error"] = "Sin datos de TradingView (rate limit o red) — se reintenta en el próximo ciclo"
        _record_fetch_result(False)
        return st

    _record_fetch_result(True)
    st["ok"] = True
    price = daily["close"]
    st["price"] = round(price, 5)
    def _tf_view(label, tf):
        return {"label": label, "rec": tf["rec"], "rsi": round(tf["rsi"], 1), "adx": round(tf["adx"], 1),
                "buy": tf["buy_count"], "sell": tf["sell_count"], "neutral": tf["neutral_count"]}
    st["tf"] = [_tf_view("W", weekly), _tf_view("D", daily), _tf_view("4H", h4)]
    st["dxy_rec"] = dxy["rec"] if dxy else None
    st["regime_symbol"] = mod.cfg.regime_symbol
    st["regime_penalises"] = mod.cfg.regime_penalises

    # Score de ambas direcciones (para las barras y el historial) -- las
    # razones (rb/rs) se exponen siempre en score_reasons_b/s, sin importar
    # el modo, para que el desglose del dashboard pueda explicar cualquier
    # tarjeta (incluida SIN SETUP, que antes se quedaba sin ninguna razón).
    sb, ss, rb, rs = mod.score_direction(weekly, daily, h4, dxy)
    st["score_b"], st["score_s"] = sb, ss
    st["score_reasons_b"], st["score_reasons_s"] = rb, rs
    append_score_history(name, sb, ss, price)
    append_ohlc_snapshot(mod, weekly, daily, h4, dxy, sb, ss)

    alerts = mod.filter_news_alerts(news_events)
    st["news"] = alerts
    risk_ok, risk_reasons = mod.check_risk_limits()
    st["risk_ok"] = risk_ok
    st["risk_reasons"] = risk_reasons

    pending = mod.get_pending_order()
    is_active = pending and _safe_upper(pending.get("resultado")) == "ACTIVA"

    if is_active:
        st["mode"] = "ACTIVA"
        # Registrar el recorrido (MFE/MAE) ANTES de construir la vista: si esta
        # ola alcanzó un extremo nuevo queda persistido en el CSV, y active_view
        # lo lee de la fila ya actualizada. Así una mecha al TP1 sobrevive al
        # cambio de día aunque el próximo ciclo la pille ya retrocedida.
        with CSV_LOCK:
            if mod.update_trade_tracking(daily, h4):
                pending = mod.get_pending_order() or pending
        st["trade"] = active_view(mod, pending, price)
        return st

    if pending:
        with CSV_LOCK:
            updated, _, _ = mod.update_pending_order(pending, daily)
        if updated:
            pending = mod.get_pending_order()

    trade = mod.generate_signal(weekly, daily, h4, dxy)
    can_trade = ((not alerts) and risk_ok and (not market_local["block_new"])
                 and window["in_window"])

    if trade.get("valid") and can_trade and not pending:
        with CSV_LOCK:
            logged = mod.log_signal(trade)
        if logged:
            st["new_signal"] = True
            pending = mod.get_pending_order()
            log(f"  🔔 {name}: SEÑAL NUEVA registrada ({trade['signal']}, score {trade['score']})")
    else:
        log_blocked_decision(name, trade, alerts, risk_ok, market_local, window, pending)

    if pending:
        st["mode"] = "PENDIENTE"
        st["order"] = pending_view(mod, pending, price, trade, daily, h4)
    elif trade.get("valid"):
        st["mode"] = "SEÑAL" if can_trade else "SEÑAL (referencia)"
        st["signal"] = signal_view(mod, trade)
    elif trade.get("ranging"):
        st["mode"] = "LATERAL"
        st["ranging_reasons"] = trade.get("ranging_reasons", [])
    else:
        st["mode"] = "SIN SETUP"
        st["zero_lots"] = trade.get("zero_lots", False)
        # Mensajes de los bloqueos duros (ADX débil, RSI extendido), armados
        # AQUÍ en vez de con un texto fijo en dashboard.html -- "bloqueada
        # aunque el score alcance el mínimo" es una afirmación que puede ser
        # FALSA (el score de ese lado puede no llegar de todos modos), y con
        # el texto fijo no había forma de saberlo sin leer el código. Caso
        # real que lo mostró (2026-09-15, NZD/USD): el score de venta nunca
        # pasó de 46/75 durante toda la caída, con o sin la puerta de RSI --
        # el cartel viejo igual decía "aunque el score alcance el mínimo".
        score_b, score_s = trade.get("score_b", 0), trade.get("score_s", 0)
        b_ok, s_ok = score_b >= mod.MIN_SCORE, score_s >= mod.MIN_SCORE

        # ADX débil bloquea las DOS direcciones a la vez -- un solo mensaje,
        # pero su contenido depende de si el score de cada lado llegaba o no
        # (4 casos posibles) para no afirmar "aunque el score alcance el
        # mínimo" cuando en realidad tampoco llegaba de ese lado.
        st["weak_trend_msg"] = None
        if trade.get("weak_trend", False):
            base = f"⚠️ ADX diario {daily['adx']:.1f} < {mod.MIN_ADX_TO_TRADE} — tendencia insuficiente"
            if b_ok and s_ok:
                st["weak_trend_msg"] = f"{base}, señal bloqueada aunque el score alcance el mínimo en las dos direcciones"
            elif b_ok:
                st["weak_trend_msg"] = f"{base}, la compra bloqueada aunque el score alcance el mínimo (la venta tampoco llegaba: {score_s}/{mod.MAX_SCORE})"
            elif s_ok:
                st["weak_trend_msg"] = f"{base}, la venta bloqueada aunque el score alcance el mínimo (la compra tampoco llegaba: {score_b}/{mod.MAX_SCORE})"
            else:
                st["weak_trend_msg"] = f"{base} en ninguna dirección (compra {score_b}/{mod.MAX_SCORE}, venta {score_s}/{mod.MAX_SCORE})"

        # RSI extendido bloquea UNA sola dirección (rsi>60 y rsi<40 nunca son
        # ciertos a la vez) -- mismo criterio, mensaje distinto según si el
        # score de ESE lado llegaba solo o no.
        st["block_short"] = trade.get("block_short", False)
        st["block_long_rsi_msg"] = None
        if trade.get("block_long_rsi", False):
            base = f"⚠️ RSI diario {daily['rsi']:.1f} > {mod.MAX_RSI_TO_TRADE} — precio ya extendido al alza"
            st["block_long_rsi_msg"] = (f"{base}, compra bloqueada aunque el score alcance el mínimo" if b_ok
                                         else f"{base} — y el score de compra tampoco llega ({score_b}/{mod.MAX_SCORE})")
        st["block_short_rsi_msg"] = None
        if trade.get("block_short_rsi", False):
            base = f"⚠️ RSI diario {daily['rsi']:.1f} < {100 - mod.MAX_RSI_TO_TRADE} — precio ya extendido a la baja"
            st["block_short_rsi_msg"] = (f"{base}, venta bloqueada aunque el score alcance el mínimo" if s_ok
                                          else f"{base} — y el score de venta tampoco llega ({score_s}/{mod.MAX_SCORE})")

    return st


# ══════════════════════════════════════════════════════════════════
#  ACCIONES DEL USUARIO — cerrar/transicionar un trade desde el dashboard
#  (antes esto exigía abrir el CSV a mano mientras el proceso de análisis
#  lo tenía abierto -- ver plan, hallazgo H1/H12)
# ══════════════════════════════════════════════════════════════════

def _refresh_pair_view_after_close(name, mod):
    """
    Reconstruye la vista (mode/order/trade) de un par tras cerrar/transicionar
    un trade, usando el último precio conocido -- sin volver a consultar
    TradingView. No genera una señal nueva (eso requeriría datos frescos de
    weekly/daily/h4); el próximo ciclo periódico se encarga de eso. El
    objetivo es solo que la tarjeta deje de mostrar una orden que el usuario
    ya cerró, sin esperar hasta 30 min al próximo ciclo.
    """
    with STATE_LOCK:
        st = STATE["pairs"].get(name)
        price = st.get("price") if st else None
    if not st or not price:
        return

    pending = mod.get_pending_order()
    is_active = pending and _safe_upper(pending.get("resultado")) == "ACTIVA"

    new_fields = {"updated": now_iso(), "signal": None, "order": None, "trade": None,
                  "ranging_reasons": None, "new_signal": False}
    if is_active:
        new_fields["mode"] = "ACTIVA"
        new_fields["trade"] = active_view(mod, pending, price)
    elif pending:
        new_fields["mode"] = "PENDIENTE"
        new_fields["order"] = pending_view(mod, pending, price, trade=None)
    else:
        new_fields["mode"] = "SIN SETUP"
    new_fields["recientes"] = recent_signals(mod)

    with STATE_LOCK:
        if name in STATE["pairs"]:
            STATE["pairs"][name].update(new_fields)
        pairs_snapshot = STATE["pairs"]
        STATE["portfolio"] = compute_portfolio(pairs_snapshot)


def close_trade_action(symbol: str, resultado: str, pips_resultado, motivo_cierre: str,
                        entrada_real=None):
    """
    Punto de entrada de POST /api/trade/<symbol>/close. Retorna (ok, error).
    resultado: ACTIVA (la orden límite se ejecutó en el bróker, sigue abierta)
               | WIN | LOSS | BE | CANCELADA (cierre definitivo).
    entrada_real: solo con resultado=ACTIVA -- precio real de fill en el
    bróker si difiere del nivel guardado (ver mod.close_trade).
    """
    if symbol not in PAIRS_BY_SYMBOL:
        return False, f"Par desconocido: {symbol}"
    name, mod = PAIRS_BY_SYMBOL[symbol]

    resultado = (resultado or "").upper()
    if resultado not in VALID_RESULTADOS:
        return False, f"resultado inválido: {resultado!r} (válidos: {sorted(VALID_RESULTADOS)})"

    pips = None
    if pips_resultado not in (None, ""):
        pips = _safe_float(pips_resultado, None)
        if pips is None:
            return False, "pips_resultado no es un número válido"

    entrada = None
    if entrada_real not in (None, ""):
        entrada = _safe_float(entrada_real, None)
        if entrada is None:
            return False, "entrada_real no es un número válido"

    with CSV_LOCK:
        updated = mod.close_trade(resultado, pips_resultado=pips,
                                  motivo_cierre=motivo_cierre or "MANUAL",
                                  entrada_real=entrada)
    if not updated:
        return False, "No había ninguna orden PENDIENTE/ACTIVA que actualizar"

    log(f"  ✍️  {name}: trade -> {resultado}"
        + (f" ({pips:+.1f} pips)" if pips is not None else "")
        + f" [{motivo_cierre or 'MANUAL'}] -- registrado desde el dashboard")

    global _csv_mtimes
    _csv_mtimes = senales_mtimes()
    pnl = compute_pnl()
    with STATE_LOCK:
        STATE["pnl"] = pnl
    _refresh_pair_view_after_close(name, mod)
    return True, None


def record_tp1_partial_action(symbol: str, pips_tp1):
    """
    Punto de entrada de POST /api/trade/<symbol>/partial. Anota que se cerró
    el 50% de la posición ACTIVA en TP1 (ver mod.record_tp1_partial) sin
    cerrar la fila -- el cierre final sigue haciéndose con /close, anotando
    solo los pips del 50% restante. Retorna (ok, error).
    """
    if symbol not in PAIRS_BY_SYMBOL:
        return False, f"Par desconocido: {symbol}"
    name, mod = PAIRS_BY_SYMBOL[symbol]

    pips = _safe_float(pips_tp1, None)
    if pips is None:
        return False, "pips_tp1 no es un número válido"

    with CSV_LOCK:
        updated = mod.record_tp1_partial(pips)
    if not updated:
        return False, "No había ninguna orden ACTIVA que actualizar"

    log(f"  ✍️  {name}: 50% cerrado en TP1 ({pips:+.1f} pips) -- registrado desde el dashboard")

    global _csv_mtimes
    _csv_mtimes = senales_mtimes()
    pnl = compute_pnl()
    with STATE_LOCK:
        STATE["pnl"] = pnl
    _refresh_pair_view_after_close(name, mod)
    return True, None


def run_wave():
    with STATE_LOCK:
        STATE["wave_running"] = True

    market = aud.check_market_session()
    window = aud.check_signal_window()
    log(f"Ciclo de análisis — mercado {'ABIERTO' if market['open'] else 'CERRADO'}, "
        f"ventana de decisión {'SÍ' if window['in_window'] else 'no'}")

    if market["open"]:
        # Índice de régimen: una consulta por índice DISTINTO, no por
        # instrumento -- los 4 pares comparten el DXY y el US500 usa el VIX,
        # así que son 2 peticiones por ciclo en vez de 5.
        regimes = {}
        for _, mod in PAIRS:
            key = mod.cfg.regime_symbol
            if key and key not in regimes:
                regimes[key] = _spaced_call(mod.fetch_regime_index)
        # Idem el calendario de Forex Factory: mismo JSON para todos --
        # antes cada build_pair_state lo volvía a descargar (una petición
        # idéntica por instrumento y ciclo, sin pasar por _spaced_call).
        news_events = fetch_news_calendar()
        new_pairs = {}
        for name, mod in PAIRS:
            try:
                dxy = regimes.get(mod.cfg.regime_symbol)
                new_pairs[name] = build_pair_state(name, mod, dxy, news_events)
                log(f"  {name}: {new_pairs[name]['mode']}"
                    + (f" (error: {new_pairs[name]['error']})" if new_pairs[name].get("error") else ""))
            except Exception as e:
                log(f"  {name}: ERROR inesperado — {e}")
                new_pairs[name] = {"name": name, "symbol": mod.SYMBOL, "capital": mod.CAPITAL, "ok": False,
                                   "error": f"Error inesperado: {e}", "updated": now_iso(),
                                   "mode": "ERROR", "new_signal": False}
        for name, mod in PAIRS:
            new_pairs[name]["recientes"] = recent_signals(mod)
    else:
        new_pairs = None   # conservar las tarjetas del último ciclo con datos

    pnl = compute_pnl()
    global _csv_mtimes
    _csv_mtimes = senales_mtimes()
    if new_pairs is not None:
        pairs_for_portfolio = new_pairs
    else:
        with STATE_LOCK:
            pairs_for_portfolio = STATE["pairs"]
    portfolio = compute_portfolio(pairs_for_portfolio)

    with STATE_LOCK:
        STATE["market"] = market
        STATE["window"] = {"in_window": window["in_window"],
                           "hours": round(window["hours"], 1),
                           "next_local": window["next_local"]}
        STATE["sessions"] = aud.active_sessions()
        if new_pairs is not None:
            STATE["pairs"] = new_pairs
        STATE["pnl"] = pnl
        STATE["portfolio"] = portfolio
        with SOURCE_STATUS_LOCK:
            STATE["source_status"] = dict(SOURCE_STATUS)
        STATE["last_wave"] = now_iso()
        STATE["wave_running"] = False

        # Copia saneada para el publicador de GitHub Pages (ver publisher.py)
        # -- _sanitize() reconstruye dicts/listas nuevos (solo reutiliza
        # escalares), así que esto es efectivamente una copia profunda segura
        # de pasar a otro hilo sin seguir sosteniendo STATE_LOCK.
        slim_for_publish = _sanitize({k: v for k, v in STATE.items()
                                       if k not in ("score_history", "ohlc_history")})
        score_history_for_publish = _sanitize(STATE["score_history"])
        ohlc_history_for_publish = _sanitize(STATE["ohlc_history"])

    # En un hilo aparte: la API de GitHub puede tardar unos segundos y no
    # tiene por qué retrasar el próximo ciclo de análisis si está lenta o caída.
    # Se guarda la referencia en LAST_PUBLISH_THREAD porque run_once_ci.py
    # (GitHub Actions, ver .github/workflows/) corre UN solo ciclo y termina
    # el proceso enseguida -- necesita poder esperar a que este hilo acabe
    # de subir antes de salir, o Actions lo mata a medio publicar.
    global LAST_PUBLISH_THREAD
    LAST_PUBLISH_THREAD = threading.Thread(
        target=publish_snapshot,
        args=(slim_for_publish, score_history_for_publish, ohlc_history_for_publish, HTML_FILE, log),
        daemon=True,
    )
    LAST_PUBLISH_THREAD.start()


def decide_interval():
    with STATE_LOCK:
        market = STATE.get("market") or {}
        window = STATE.get("window") or {}
        pairs = STATE.get("pairs") or {}
    if not market.get("open", True):
        return aud.IDLE_REFRESH_SECONDS
    busy = window.get("in_window") or any(
        p.get("mode") in ("PENDIENTE", "ACTIVA") for p in pairs.values())
    return aud.REFRESH_SECONDS if busy else aud.IDLE_REFRESH_SECONDS


def worker():
    while True:
        try:
            run_wave()
        except Exception as e:
            log(f"ERROR en el ciclo de análisis: {e}")
            with STATE_LOCK:
                STATE["wave_running"] = False
        interval = decide_interval()
        with STATE_LOCK:
            STATE["next_wave"] = (_utc_now_naive() + timedelta(seconds=interval)).strftime("%Y-%m-%d %H:%M:%S")
        log(f"Próximo ciclo en {interval // 60} min")
        REFRESH_EVENT.wait(timeout=interval)
        REFRESH_EVENT.clear()


# ══════════════════════════════════════════════════════════════════
#  SERVIDOR HTTP
# ══════════════════════════════════════════════════════════════════

def _sanitize(obj):
    """json no acepta inf/nan — limpiarlos antes de serializar."""
    if isinstance(obj, float):
        return None if (math.isinf(obj) or math.isnan(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass   # silenciar el log por defecto (una línea por request ensucia la consola)

    def _send(self, code, body, content_type):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json_with_etag(self, obj):
        """
        Para los endpoints de historial (score/ohlc): pueden pesar bastante
        y no cambian en cada sondeo de 5s (solo una vez por ciclo de
        análisis, cada 15-30 min) -- ETag deja que el navegador reciba 304
        y no vuelva a transferir el JSON completo si no cambió nada.
        """
        payload = json.dumps(_sanitize(obj), ensure_ascii=False, default=str)
        etag = '"' + hashlib.md5(payload.encode("utf-8")).hexdigest()[:16] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        data = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html")
            except OSError:
                self._send(500, "No se encontró dashboard.html", "text/plain")
        elif self.path == "/api/state":
            try:
                refresh_pnl_if_changed()
            except Exception:
                pass
            with STATE_LOCK:
                # score_history/ohlc_history van por endpoints separados con
                # ETag (ver abajo) -- son los campos más pesados de STATE y
                # casi nunca cambian entre sondeos de 5s, reenviarlos enteros
                # en cada /api/state era ancho de banda tirado.
                slim = {k: v for k, v in STATE.items() if k not in ("score_history", "ohlc_history")}
                payload = json.dumps(_sanitize(slim), ensure_ascii=False, default=str)
            self._send(200, payload, "application/json")
        elif self.path == "/api/score_history":
            with STATE_LOCK:
                data = STATE["score_history"]
            self._send_json_with_etag(data)
        elif self.path == "/api/ohlc_history":
            with STATE_LOCK:
                data = STATE["ohlc_history"]
            self._send_json_with_etag(data)
        else:
            self._send(404, "No encontrado", "text/plain")

    def do_POST(self):
        global _last_manual_refresh
        # Mitigación CSRF básica: un <form> HTML normal (el vector típico --
        # cualquier web que el usuario abra en la misma máquina podría apuntar
        # un <form method=POST> a localhost:8877 y marcar un trade como LOSS/
        # CANCELADA) NO puede fijar cabeceras personalizadas. Un fetch/XHR
        # cross-origin que sí intente fijarla dispara un preflight CORS que
        # este servidor no responde (sin Access-Control-Allow-*) -> el
        # navegador lo bloquea antes de que llegue aquí. Exigir esta cabecera
        # cierra ambos caminos sin necesitar sesión/token.
        if self.headers.get("X-Requested-With") != "tvm-dashboard-ui":
            self._send(403, json.dumps({"ok": False, "error": "Falta cabecera requerida"}), "application/json")
            return
        if self.path == "/api/refresh":
            with STATE_LOCK:
                busy = STATE["wave_running"]
            if busy:
                # Ya hay un ciclo en curso — los datos estarán frescos en breve,
                # encolar otro ciclo detrás solo duplicaría las consultas a TradingView.
                self._send(200, json.dumps({"ok": True, "ya_actualizando": True}), "application/json")
                return
            elapsed = time.monotonic() - _last_manual_refresh
            if elapsed < MANUAL_REFRESH_COOLDOWN:
                restante = int(MANUAL_REFRESH_COOLDOWN - elapsed)
                self._send(429, json.dumps({"ok": False, "espera": restante}), "application/json")
                return
            _last_manual_refresh = time.monotonic()
            REFRESH_EVENT.set()
            log("Refresco manual solicitado desde el navegador")
            self._send(200, json.dumps({"ok": True}), "application/json")
            return

        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "trade" and parts[3] == "close":
            symbol = parts[2].upper()
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "Body JSON inválido"}), "application/json")
                return
            ok, error = close_trade_action(
                symbol,
                body.get("resultado"),
                body.get("pips_resultado"),
                body.get("motivo_cierre"),
                body.get("entrada_real"),
            )
            code = 200 if ok else 400
            self._send(code, json.dumps({"ok": ok, "error": error}, ensure_ascii=False), "application/json")
            return

        if len(parts) == 4 and parts[0] == "api" and parts[1] == "trade" and parts[3] == "partial":
            symbol = parts[2].upper()
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "Body JSON inválido"}), "application/json")
                return
            ok, error = record_tp1_partial_action(symbol, body.get("pips_tp1"))
            code = 200 if ok else 400
            self._send(code, json.dumps({"ok": ok, "error": error}, ensure_ascii=False), "application/json")
            return

        self._send(404, "No encontrado", "text/plain")


def main():
    _purge_old_rows(SCORE_HISTORY_FILE)
    for _, mod in PAIRS:
        _purge_old_rows(_ohlc_file(mod))

    with STATE_LOCK:
        STATE["started"] = now_iso()
        STATE["score_history"] = load_score_history()
        STATE["ohlc_history"] = {mod.SYMBOL: load_ohlc_history(mod) for _, mod in PAIRS}
        STATE["pnl"] = compute_pnl()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    url = f"http://localhost:{PORT}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        log(f"No se pudo abrir el puerto {PORT}: {e}")
        log("¿Ya hay otro dashboard corriendo? Cierra esa ventana primero.")
        sys.exit(1)

    log("=" * 60)
    log(f"Dashboard corriendo en {url}")
    log("Los 5 sistemas se analizan en este único proceso.")
    log("NO abras las ventanas CMD individuales a la vez.")
    log("Ctrl+C para detener.")
    log("=" * 60)

    if os.environ.get("DASHBOARD_NO_BROWSER") != "1":
        threading.Timer(1.5, webbrowser.open, [url]).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Dashboard detenido. ¡Buen trading!")


if __name__ == "__main__":
    main()
