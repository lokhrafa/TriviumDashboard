#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cliente propio del scanner público de TradingView — reemplaza la dependencia
externa `tradingview-ta`.

Por qué: `tradingview-ta` pide una lista de columnas fija que NO incluye
"ATR" ni "BB.basis" (línea media real de las Bollinger Bands). Por eso
trading_engine.fetch_analysis() llevaba desde siempre calculando el stop
loss con un ATR fijo (0.0030, el valor por defecto de `ind.get("ATR",
0.0030)`) en vez del real — los stops en vivo no se movían con la
volatilidad del mercado, a diferencia de backtest.py, que sí calcula el ATR
real (ver backtest_indicators.atr). "bb_middle" no llegaba a fallar igual
de silencioso porque trading_engine.py ya traía un fallback
((upper+lower)/2) que da el mismo número que BB.basis en la práctica.

La lógica de cómputo de recomendaciones (clase Compute, función calculate())
es una transcripción de tradingview_ta 3.3.0 (MIT license,
github.com/deathlyface/python-tradingview-ta), que a su vez es una
traducción del bundle JS público de TradingView (technicals.*.js). Se
mantiene la lista base de columnas y la lectura por posición fija tal cual
el original para que el conteo BUY/SELL/NEUTRAL (y por tanto score_direction
en trading_engine.py) siga siendo exactamente el mismo que ya se validó en
backtest — verificado columna a columna contra la librería original antes
de reemplazarla. Los indicadores nuevos (ATR, BB.basis) van SIEMPRE al
final de la lista para no correr los índices fijos del resto.

API expuesta — mismo contrato que tradingview_ta, así el resto del proyecto
(trading_engine.py, dashboard.py) no necesita más cambios que el import:
Interval, TA_Handler, Analysis, Recommendation.
"""

import requests


class Recommendation:
    buy = "BUY"
    strong_buy = "STRONG_BUY"
    sell = "SELL"
    strong_sell = "STRONG_SELL"
    neutral = "NEUTRAL"
    error = "ERROR"


class Interval:
    INTERVAL_1_MINUTE = "1m"
    INTERVAL_5_MINUTES = "5m"
    INTERVAL_15_MINUTES = "15m"
    INTERVAL_30_MINUTES = "30m"
    INTERVAL_1_HOUR = "1h"
    INTERVAL_2_HOURS = "2h"
    INTERVAL_4_HOURS = "4h"
    INTERVAL_1_DAY = "1d"
    INTERVAL_1_WEEK = "1W"
    INTERVAL_1_MONTH = "1M"


# Sufijo que TradingView espera para cada timeframe en el nombre de columna
# (ej. "RSI|240" = RSI en 4H). "1d" es el default del scanner: sin sufijo.
_INTERVAL_SUFFIX = {
    Interval.INTERVAL_1_MINUTE: "|1",
    Interval.INTERVAL_5_MINUTES: "|5",
    Interval.INTERVAL_15_MINUTES: "|15",
    Interval.INTERVAL_30_MINUTES: "|30",
    Interval.INTERVAL_1_HOUR: "|60",
    Interval.INTERVAL_2_HOURS: "|120",
    Interval.INTERVAL_4_HOURS: "|240",
    Interval.INTERVAL_1_WEEK: "|1W",
    Interval.INTERVAL_1_MONTH: "|1M",
    Interval.INTERVAL_1_DAY: "",
}

# Columnas base — MISMO orden e índices que tradingview_ta.TradingView.indicators
# (github.com/deathlyface/python-tradingview-ta, main.py). calculate() lee estas
# columnas por posición fija más abajo: no reordenar ni insertar en medio.
BASE_COLUMNS = [
    "Recommend.Other", "Recommend.All", "Recommend.MA", "RSI", "RSI[1]",
    "Stoch.K", "Stoch.D", "Stoch.K[1]", "Stoch.D[1]", "CCI20", "CCI20[1]",
    "ADX", "ADX+DI", "ADX-DI", "ADX+DI[1]", "ADX-DI[1]", "AO", "AO[1]",
    "Mom", "Mom[1]", "MACD.macd", "MACD.signal", "Rec.Stoch.RSI",
    "Stoch.RSI.K", "Rec.WR", "W.R", "Rec.BBPower", "BBPower", "Rec.UO",
    "UO", "close", "EMA5", "SMA5", "EMA10", "SMA10", "EMA20", "SMA20",
    "EMA30", "SMA30", "EMA50", "SMA50", "EMA100", "SMA100", "EMA200",
    "SMA200", "Rec.Ichimoku", "Ichimoku.BLine", "Rec.VWMA", "VWMA",
    "Rec.HullMA9", "HullMA9", "Pivot.M.Classic.S3", "Pivot.M.Classic.S2",
    "Pivot.M.Classic.S1", "Pivot.M.Classic.Middle", "Pivot.M.Classic.R1",
    "Pivot.M.Classic.R2", "Pivot.M.Classic.R3", "Pivot.M.Fibonacci.S3",
    "Pivot.M.Fibonacci.S2", "Pivot.M.Fibonacci.S1",
    "Pivot.M.Fibonacci.Middle", "Pivot.M.Fibonacci.R1",
    "Pivot.M.Fibonacci.R2", "Pivot.M.Fibonacci.R3",
    "Pivot.M.Camarilla.S3", "Pivot.M.Camarilla.S2", "Pivot.M.Camarilla.S1",
    "Pivot.M.Camarilla.Middle", "Pivot.M.Camarilla.R1",
    "Pivot.M.Camarilla.R2", "Pivot.M.Camarilla.R3", "Pivot.M.Woodie.S3",
    "Pivot.M.Woodie.S2", "Pivot.M.Woodie.S1", "Pivot.M.Woodie.Middle",
    "Pivot.M.Woodie.R1", "Pivot.M.Woodie.R2", "Pivot.M.Woodie.R3",
    "Pivot.M.Demark.S1", "Pivot.M.Demark.Middle", "Pivot.M.Demark.R1",
    "open", "P.SAR", "BB.lower", "BB.upper", "AO[2]", "volume", "change",
    "low", "high",
]

# Columnas propias, añadidas al final — arreglan el ATR fijo y le dan a
# bb_middle su valor real en vez del fallback (upper+lower)/2.
EXTRA_COLUMNS = ["ATR", "BB.basis"]

ALL_COLUMNS = BASE_COLUMNS + EXTRA_COLUMNS

_SCAN_URL = "https://scanner.tradingview.com/{screener}/scan"
_USER_AGENT = "tvm_client/1.0 (TvmTrading)"


class Analysis:
    """Resultado de un análisis — mismos atributos que tradingview_ta.Analysis."""
    def __init__(self):
        self.screener = ""
        self.exchange = ""
        self.symbol = ""
        self.interval = ""
        self.time = None
        self.summary = {}
        self.oscillators = {}
        self.moving_averages = {}
        self.indicators = {}


# ══════════════════════════════════════════════════════════════════
#  REGLAS DE RECOMENDACIÓN — transcripción de tradingview_ta.technicals.Compute
# ══════════════════════════════════════════════════════════════════

class Compute:
    def MA(ma, close):
        if ma < close:
            return Recommendation.buy
        elif ma > close:
            return Recommendation.sell
        else:
            return Recommendation.neutral

    def RSI(rsi, rsi1):
        if rsi < 30 and rsi1 < rsi:
            return Recommendation.buy
        elif rsi > 70 and rsi1 > rsi:
            return Recommendation.sell
        else:
            return Recommendation.neutral

    def Stoch(k, d, k1, d1):
        if k < 20 and d < 20 and k > d and k1 < d1:
            return Recommendation.buy
        elif k > 80 and d > 80 and k < d and k1 > d1:
            return Recommendation.sell
        else:
            return Recommendation.neutral

    def CCI20(cci20, cci201):
        if cci20 < -100 and cci20 > cci201:
            return Recommendation.buy
        elif cci20 > 100 and cci20 < cci201:
            return Recommendation.sell
        else:
            return Recommendation.neutral

    def ADX(adx, adxpdi, adxndi, adxpdi1, adxndi1):
        if adx > 20 and adxpdi1 < adxndi1 and adxpdi > adxndi:
            return Recommendation.buy
        elif adx > 20 and adxpdi1 > adxndi1 and adxpdi < adxndi:
            return Recommendation.sell
        else:
            return Recommendation.neutral

    def AO(ao, ao1, ao2):
        if (ao > 0 and ao1 < 0) or (ao > 0 and ao1 > 0 and ao > ao1 and ao2 > ao1):
            return Recommendation.buy
        elif (ao < 0 and ao1 > 0) or (ao < 0 and ao1 < 0 and ao < ao1 and ao2 < ao1):
            return Recommendation.sell
        else:
            return Recommendation.neutral

    def Mom(mom, mom1):
        if mom < mom1:
            return Recommendation.sell
        elif mom > mom1:
            return Recommendation.buy
        else:
            return Recommendation.neutral

    def MACD(macd, signal):
        if macd > signal:
            return Recommendation.buy
        elif macd < signal:
            return Recommendation.sell
        else:
            return Recommendation.neutral

    def BBBuy(close, bblower):
        if close < bblower:
            return Recommendation.buy
        else:
            return Recommendation.neutral

    def BBSell(close, bbupper):
        if close > bbupper:
            return Recommendation.sell
        else:
            return Recommendation.neutral

    def PSAR(psar, open):
        if psar < open:
            return Recommendation.buy
        elif psar > open:
            return Recommendation.sell
        else:
            return Recommendation.neutral

    def Recommend(value):
        if value >= -1 and value < -.5:
            return Recommendation.strong_sell
        elif value >= -.5 and value < -.1:
            return Recommendation.sell
        elif value >= -.1 and value <= .1:
            return Recommendation.neutral
        elif value > .1 and value <= .5:
            return Recommendation.buy
        elif value > .5 and value <= 1:
            return Recommendation.strong_buy
        else:
            return Recommendation.error

    def Simple(value):
        if value == -1:
            return Recommendation.sell
        elif value == 1:
            return Recommendation.buy
        else:
            return Recommendation.neutral


def calculate(indicators, indicators_key, screener, symbol, exchange, interval):
    """Computa BUY/SELL/NEUTRAL y la recomendación agregada a partir de los
    valores crudos del scanner. Transcripción posicional de
    tradingview_ta.main.calculate — ver cabecera del módulo."""
    oscillators_counter = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}
    ma_counter = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}
    computed_oscillators, computed_ma = {}, {}

    values = list(indicators.values())

    if None not in values[0:3]:
        recommend_oscillators = Compute.Recommend(values[0])
        recommend_summary = Compute.Recommend(values[1])
        recommend_moving_averages = Compute.Recommend(values[2])
    else:
        return None

    # OSCILADORES
    if None not in values[3:5]:
        computed_oscillators["RSI"] = Compute.RSI(values[3], values[4])
        oscillators_counter[computed_oscillators["RSI"]] += 1
    if None not in values[5:9]:
        computed_oscillators["STOCH.K"] = Compute.Stoch(values[5], values[6], values[7], values[8])
        oscillators_counter[computed_oscillators["STOCH.K"]] += 1
    if None not in values[9:11]:
        computed_oscillators["CCI"] = Compute.CCI20(values[9], values[10])
        oscillators_counter[computed_oscillators["CCI"]] += 1
    if None not in values[11:16]:
        computed_oscillators["ADX"] = Compute.ADX(values[11], values[12], values[13], values[14], values[15])
        oscillators_counter[computed_oscillators["ADX"]] += 1
    if None not in values[16:18] and values[86] is not None:
        computed_oscillators["AO"] = Compute.AO(values[16], values[17], values[86])
        oscillators_counter[computed_oscillators["AO"]] += 1
    if None not in values[18:20]:
        computed_oscillators["Mom"] = Compute.Mom(values[18], values[19])
        oscillators_counter[computed_oscillators["Mom"]] += 1
    if None not in values[20:22]:
        computed_oscillators["MACD"] = Compute.MACD(values[20], values[21])
        oscillators_counter[computed_oscillators["MACD"]] += 1
    if values[22] is not None:
        computed_oscillators["Stoch.RSI"] = Compute.Simple(values[22])
        oscillators_counter[computed_oscillators["Stoch.RSI"]] += 1
    if values[24] is not None:
        computed_oscillators["W%R"] = Compute.Simple(values[24])
        oscillators_counter[computed_oscillators["W%R"]] += 1
    if values[26] is not None:
        computed_oscillators["BBP"] = Compute.Simple(values[26])
        oscillators_counter[computed_oscillators["BBP"]] += 1
    if values[28] is not None:
        computed_oscillators["UO"] = Compute.Simple(values[28])
        oscillators_counter[computed_oscillators["UO"]] += 1

    # MEDIAS MÓVILES
    ma_list = ["EMA10", "SMA10", "EMA20", "SMA20", "EMA30", "SMA30",
               "EMA50", "SMA50", "EMA100", "SMA100", "EMA200", "SMA200"]
    close = values[30]
    ma_list_counter = 0
    for index in range(33, 45):
        if values[index] is not None and close is not None:
            computed_ma[ma_list[ma_list_counter]] = Compute.MA(values[index], close)
            ma_counter[computed_ma[ma_list[ma_list_counter]]] += 1
            ma_list_counter += 1

    if values[45] is not None:
        computed_ma["Ichimoku"] = Compute.Simple(values[45])
        ma_counter[computed_ma["Ichimoku"]] += 1
    if values[47] is not None:
        computed_ma["VWMA"] = Compute.Simple(values[47])
        ma_counter[computed_ma["VWMA"]] += 1
    if values[49] is not None:
        computed_ma["HullMA"] = Compute.Simple(values[49])
        ma_counter[computed_ma["HullMA"]] += 1

    analysis = Analysis()
    analysis.screener = screener
    analysis.exchange = exchange
    analysis.symbol = symbol
    analysis.interval = interval

    for key, val in zip(indicators_key, values):
        analysis.indicators[key] = val

    analysis.oscillators = {
        "RECOMMENDATION": recommend_oscillators,
        "BUY": oscillators_counter["BUY"], "SELL": oscillators_counter["SELL"],
        "NEUTRAL": oscillators_counter["NEUTRAL"], "COMPUTE": computed_oscillators,
    }
    analysis.moving_averages = {
        "RECOMMENDATION": recommend_moving_averages,
        "BUY": ma_counter["BUY"], "SELL": ma_counter["SELL"],
        "NEUTRAL": ma_counter["NEUTRAL"], "COMPUTE": computed_ma,
    }
    analysis.summary = {
        "RECOMMENDATION": recommend_summary,
        "BUY": oscillators_counter["BUY"] + ma_counter["BUY"],
        "SELL": oscillators_counter["SELL"] + ma_counter["SELL"],
        "NEUTRAL": oscillators_counter["NEUTRAL"] + ma_counter["NEUTRAL"],
    }

    return analysis


# ══════════════════════════════════════════════════════════════════
#  CAPA HTTP — habla directo con el scanner público de TradingView
# ══════════════════════════════════════════════════════════════════

class TA_Handler:
    """Mismo contrato público que tradingview_ta.TA_Handler: construir con
    symbol/screener/exchange/interval y llamar a get_analysis()."""

    def __init__(self, symbol="", screener="", exchange="", interval="",
                 timeout=None, proxies=None):
        self.symbol = symbol
        self.screener = screener
        self.exchange = exchange
        self.interval = interval
        self.timeout = timeout
        self.proxies = proxies
        self.indicators = ALL_COLUMNS.copy()

    def add_indicators(self, indicators):
        self.indicators = self.indicators + list(indicators)

    def get_indicators(self):
        if not self.screener or not isinstance(self.screener, str):
            raise Exception("Screener is empty or not valid.")
        if not self.exchange or not isinstance(self.exchange, str):
            raise Exception("Exchange is empty or not valid.")
        if not self.symbol or not isinstance(self.symbol, str):
            raise Exception("Symbol is empty or not valid.")

        suffix = _INTERVAL_SUFFIX.get(self.interval, "")
        columns = [c + suffix for c in self.indicators]
        ticker = f"{self.exchange}:{self.symbol}".upper()
        payload = {"symbols": {"tickers": [ticker], "query": {"types": []}}, "columns": columns}
        url = _SCAN_URL.format(screener=self.screener.lower())

        response = requests.post(url, json=payload, headers={"User-Agent": _USER_AGENT},
                                  timeout=self.timeout, proxies=self.proxies)
        if response.status_code != 200:
            raise Exception(
                f"Can't access TradingView's API. HTTP status code: {response.status_code}. "
                "Check for invalid symbol, exchange, or indicators.")

        result = response.json().get("data", [])
        if not result:
            raise Exception("Exchange or symbol not found.")

        return dict(zip(self.indicators, result[0]["d"]))

    def get_analysis(self):
        indicators = self.get_indicators()
        return calculate(indicators=indicators, indicators_key=self.indicators,
                          screener=self.screener, symbol=self.symbol,
                          exchange=self.exchange, interval=self.interval)
