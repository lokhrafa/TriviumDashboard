#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CONFIGURACIÓN POR PAR — lo único que distingue a un par de otro.

Antes esto vivía duplicado dentro de 4 archivos de ~1550 líneas cada uno
(audusd/eurusd/gbpusd/nzdusd_trading_system.py), que solo diferían en estos
datos: símbolo, archivo de log, filtros de noticias y notas de contexto.
Todo lo demás (estrategia, gestión de riesgo, backtest) vive en
trading_engine.py y se ejecuta UNA sola vez, no cuatro.

Los parámetros de riesgo/tiempo (CAPITAL, RISK_PCT, MIN_SCORE, etc.) eran
también idénticos en los 4 archivos -- por eso quedan como constantes de
clase en TradingEngine y no aquí. Si algún día un par necesita un riesgo o
un score mínimo distinto, ese es el lugar natural para añadirlo por config
sin tocar el motor.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PairConfig:
    name: str                      # nombre para mostrar, ej. "AUD/USD"
    symbol: str                    # símbolo TradingView, ej. "AUDUSD"
    log_filename: str              # nombre del CSV de señales
    news_filters: list = field(default_factory=list)
    market_context_notes: list = field(default_factory=list)
    exchange: str = "FX_IDC"
    pip_value: float = 0.0001
    usd_per_pip_standard: float = 10.0
    price_decimals: int = 4        # decimales de cotización (4 estándar, 2 en pares JPY)
    # Screener del scanner de TradingView. Los 6 pares de divisas viven en
    # "forex"; un índice como el S&P 500 (SP:SPX) vive en "america" y los CFD
    # sintéticos (TVC:DXY, TVC:VIX) en "cfd". Antes estaba fijo a "forex"
    # dentro de fetch_analysis, lo que hacía imposible consultar nada que no
    # fuera un par de divisas.
    screener: str = "forex"
    # Unidad de precio para la interfaz: un índice se mueve en PUNTOS, no en
    # pips. Solo afecta a las etiquetas -- la aritmética usa pip_value.
    unit_label: str = "pips"
    # True cuando el USD es la divisa BASE del par (USD/CAD, USD/JPY) en vez
    # de la de cotización (EUR/USD, GBP/USD, AUD/USD, NZD/USD). En esos pares
    # el valor del pip en USD NO es una constante -- depende del tipo de
    # cambio del día (pip_value * 100_000 / precio). usd_per_pip_standard
    # arriba queda como valor de arranque nada más; backtest.py lo recalcula
    # cada día para estos pares (ver iter_daily_context). Ningún par en vivo
    # hoy tiene esto en True -- si alguno lo necesitara, trading_engine.py
    # también necesitaría ese recálculo dinámico antes de activarlo.
    usd_is_base: bool = False
    # ── Filtro de régimen (ver TradingEngine.fetch_regime_index) ──────────
    # Índice externo que penaliza el score cuando contradice la señal. Para
    # los pares de divisas es el dólar (DXY): dólar fuerte castiga compras.
    # Para el S&P 500 es el VIX, y la MISMA regla vale sin tocar el scoring:
    # "rec BUY del índice de régimen resta al score de compra" significa
    # miedo al alza -> no comprar, y "rec SELL resta a la venta" significa
    # mercado en calma -> no vender. regime_symbol=None desactiva el filtro.
    regime_symbol: str | None = "DXY"
    regime_screener: str = "cfd"
    regime_exchange: str = "TVC"
    regime_label: str = "Dólar (DXY)"   # cómo se nombra en el desglose del score
    # False = el índice se consulta y se MUESTRA, pero no resta puntos al
    # score. Se usa cuando el filtro no se sostiene en el backtest: es
    # preferible seguir viendo el dato en el dashboard a dejar de recogerlo.
    regime_penalises: bool = True
    # Serie diaria del índice de régimen para el backtest. El DXY no es
    # descargable directamente, así que se usa el ETF UUP como proxy; el VIX
    # sí lo es (CBOE, contrato 13455763).
    regime_backtest_file: str = "uup_daily_5y.json"
    # ── Costes de transacción para el backtest (ver backtest.apply_costs) ──
    # Los valores por defecto son los de forex, ya usados en las corridas
    # documentadas. Un CFD de índice cotiza sin comisión aparte y su coste de
    # mantenimiento no es un swap fijo por lote sino financiación sobre el
    # NOCIONAL de la posición (lotes x precio), que es un orden de magnitud
    # distinto: 0.11 lotes de US500 mueven $848 de nocional, mientras que
    # 0.11 lotes de forex mueven $11.000.
    bt_spread_units: float = 1.5             # pips/puntos de spread, ida y vuelta
    bt_commission_per_lot: float = 4.0       # USD por lote estándar
    bt_financing_annual_pct: float | None = None   # % anual sobre nocional; None = usar swap fijo
    # True cuando una posición LARGA en este instrumento equivale a una
    # apuesta CORTA contra el dólar (todo par XXX/USD). El dashboard usa esto
    # para su lectura de exposición neta al dólar: un largo de US500 no es un
    # corto de dólar y contaminaría el cómputo si contara igual.
    usd_exposure: bool = True
    # El domingo a las 17:00 NY el forex REABRE con una vela nueva, así que
    # la ventana de decisión es legítima. Un índice de contado no cotiza en
    # domingo: su última vela diaria sigue siendo la del viernes, y generar
    # una señal ahí sería decidir sobre datos rancios.
    trades_sunday: bool = True
    # True para instrumentos que cotizan solo en el NYSE (hoy: el US500). El
    # forex no observa los festivos del mercado de acciones (Thanksgiving, 4
    # de julio, Navidad...) y sigue las 24h -- así que TradingEngine.
    # check_market_session() solo consulta el calendario de festivos del
    # NYSE (ver is_us_market_holiday) para instrumentos marcados aquí.
    respects_nyse_holidays: bool = False
    # Puerta del lado corto: prohíbe vender mientras el cierre diario esté
    # POR ENCIMA de la EMA200 diaria. Existe por la deriva estructural alcista
    # de la renta variable -- sin ella, un corto del S&P se valida con el
    # precio sobre la EMA200 (17 de alineación + 11 de posición + 20 de RSI +
    # 15 de MACD = 63 >= MIN_SCORE), es decir apostando contra la tendencia
    # secular del índice. No aplica a divisas, que no tienen tal deriva.
    no_short_above_ema200: bool = False
    # Datos de backtest (ver backtest.py) -- nombres de los archivos de velas
    # descargados en backtest_data/. None si el par todavía no tiene datos.
    backtest_daily_file: str | None = None
    backtest_h4_file: str | None = None


AUD_CONFIG = PairConfig(
    name="AUD/USD",
    symbol="AUDUSD",
    log_filename="senales.csv",
    backtest_daily_file="daily_5y.json",
    backtest_h4_file="h4_2y.json",
    news_filters=[
        {
            "keywords":   ["Cash Rate", "Interest Rate", "RBA Rate Statement", "RBA"],
            "currencies": ["AUD"],
            "label":      "Decisión de tasas RBA",
        },
        {
            "keywords":   ["Non-Farm", "Nonfarm", "NFP"],
            "currencies": ["USD"],
            "label":      "NFP (Non-Farm Payrolls)",
        },
        {
            "keywords":   ["CPI", "Consumer Price", "Inflation"],
            "currencies": ["AUD", "USD"],
            "label":      "CPI / Inflación",
        },
        {
            "keywords":   ["GDP", "Gross Domestic"],
            "currencies": ["CNY"],
            "label":      "PIB de China",
        },
        {
            "keywords":   ["Powell", "FOMC", "Fed Chair", "Federal Reserve", "Fed Statement"],
            "currencies": ["USD"],
            "label":      "Discurso Fed / Powell",
        },
    ],
    market_context_notes=[
        "AUD correlaciona con commodities: oro, cobre y mineral de hierro (China)",
        "Noticias clave: Banco de la Reserva de Australia (RBA) y Reserva Federal (Fed)",
        "Mejor sesión: Apertura de Tokio (23:00 UTC) y solapamiento Londres/NY",
        "Evitar: Comunicados RBA, NFP americano, datos PIB de China",
        "Niveles psicológicos: 0.6000 | 0.6500 | 0.6800 | 0.7000 | 0.7500",
        "Rango diario promedio: 60–90 pips — ideal para swing trading",
        "Correlación con NZD/USD +0.85/0.90 -- ver aviso de cartera si ambos están abiertos a la vez",
    ],
)

EUR_CONFIG = PairConfig(
    name="EUR/USD",
    symbol="EURUSD",
    log_filename="senales_eurusd.csv",
    backtest_daily_file="eurusd_daily_5y.json",
    backtest_h4_file="eurusd_h4.json",
    news_filters=[
        {
            "keywords":   ["Main Refinancing", "ECB Interest Rate", "ECB Press Conference", "Deposit Facility Rate"],
            "currencies": ["EUR"],
            "label":      "Decisión de tasas BCE",
        },
        {
            "keywords":   ["Non-Farm", "Nonfarm", "NFP"],
            "currencies": ["USD"],
            "label":      "NFP (Non-Farm Payrolls)",
        },
        {
            "keywords":   ["CPI", "Consumer Price", "Inflation"],
            "currencies": ["EUR", "USD"],
            "label":      "CPI / Inflación",
        },
        {
            "keywords":   ["GDP", "Gross Domestic"],
            "currencies": ["EUR"],
            "label":      "PIB de la Eurozona",
        },
        {
            "keywords":   ["Powell", "FOMC", "Fed Chair", "Federal Reserve", "Fed Statement"],
            "currencies": ["USD"],
            "label":      "Discurso Fed / Powell",
        },
    ],
    market_context_notes=[
        "EUR correlaciona (inverso) con el índice DXY y con el diferencial de tasas BCE-Fed",
        "Noticias clave: Banco Central Europeo (BCE) y Reserva Federal (Fed)",
        "Mejor sesión: Apertura de Londres (07:00 UTC) y solapamiento Londres/NY",
        "Evitar: Comunicados BCE, NFP americano, IPC de EE.UU. y la Eurozona",
        "Niveles psicológicos: 1.0500 | 1.1000 | 1.1500 | 1.2000",
        "Rango diario promedio: 70–90 pips — ideal para swing trading",
        "Correlación con GBP/USD +0.80/0.90 -- ver aviso de cartera si ambos están abiertos a la vez",
    ],
)

GBP_CONFIG = PairConfig(
    name="GBP/USD",
    symbol="GBPUSD",
    log_filename="senales_gbpusd.csv",
    backtest_daily_file="gbpusd_daily_5y.json",
    backtest_h4_file="gbpusd_h4.json",
    news_filters=[
        {
            "keywords":   ["BOE Interest Rate", "Bank Rate", "BOE Rate Statement", "MPC"],
            "currencies": ["GBP"],
            "label":      "Decisión de tasas BOE",
        },
        {
            "keywords":   ["Non-Farm", "Nonfarm", "NFP"],
            "currencies": ["USD"],
            "label":      "NFP (Non-Farm Payrolls)",
        },
        {
            "keywords":   ["CPI", "Consumer Price", "Inflation"],
            "currencies": ["GBP", "USD"],
            "label":      "CPI / Inflación",
        },
        {
            "keywords":   ["GDP", "Gross Domestic"],
            "currencies": ["GBP"],
            "label":      "PIB del Reino Unido",
        },
        {
            "keywords":   ["Powell", "FOMC", "Fed Chair", "Federal Reserve", "Fed Statement"],
            "currencies": ["USD"],
            "label":      "Discurso Fed / Powell",
        },
    ],
    market_context_notes=[
        "GBP correlaciona con datos económicos del Reino Unido y el diferencial de tasas BOE-Fed",
        "Noticias clave: Banco de Inglaterra (BOE) y Reserva Federal (Fed)",
        "Mejor sesión: Apertura de Londres (07:00 UTC) y solapamiento Londres/NY",
        "Evitar: Comunicados BOE, NFP americano, IPC del Reino Unido y EE.UU.",
        "Niveles psicológicos: 1.2000 | 1.2500 | 1.3000 | 1.3500 | 1.4000",
        "Rango diario promedio: 90–110 pips — ideal para swing trading",
        "Correlación con EUR/USD +0.80/0.90 -- ver aviso de cartera si ambos están abiertos a la vez",
    ],
)

NZD_CONFIG = PairConfig(
    name="NZD/USD",
    symbol="NZDUSD",
    log_filename="senales_nzdusd.csv",
    backtest_daily_file="nzdusd_daily_5y.json",
    backtest_h4_file="nzdusd_h4.json",
    news_filters=[
        {
            "keywords":   ["RBNZ", "Official Cash Rate", "OCR"],
            "currencies": ["NZD"],
            "label":      "Decisión de tasas RBNZ",
        },
        {
            "keywords":   ["Non-Farm", "Nonfarm", "NFP"],
            "currencies": ["USD"],
            "label":      "NFP (Non-Farm Payrolls)",
        },
        {
            "keywords":   ["CPI", "Consumer Price", "Inflation"],
            "currencies": ["NZD", "USD"],
            "label":      "CPI / Inflación",
        },
        {
            "keywords":   ["GDP", "Gross Domestic"],
            "currencies": ["CNY"],
            "label":      "PIB de China",
        },
        {
            "keywords":   ["Powell", "FOMC", "Fed Chair", "Federal Reserve", "Fed Statement"],
            "currencies": ["USD"],
            "label":      "Discurso Fed / Powell",
        },
    ],
    market_context_notes=[
        "NZD correlaciona con commodities agrícolas/lácteos y con China, de forma parecida a AUD -- correlación real con AUD/USD +0.81 en backtest: alta, más redundancia de riesgo que EUR/USD o GBP/USD",
        "Noticias clave: Banco de la Reserva de Nueva Zelanda (RBNZ) y Reserva Federal (Fed)",
        "Mejor sesión: Apertura de Wellington/Sídney (21:00–22:00 UTC) y solapamiento Londres/NY",
        "Evitar: Comunicados RBNZ, NFP americano, datos PIB de China",
        "Niveles psicológicos: 0.5500 | 0.5800 | 0.6000 | 0.6200 | 0.6500",
        "Rango diario promedio: 55–70 pips — ideal para swing trading",
    ],
)

# Pares con datos de backtest ya descargados pero SIN sistema en vivo todavía
# (ver plan E6 -- candidatos para ampliar el universo hacia fuera del bloque
# AUD/NZD/EUR/GBP, que están altamente correlacionados entre sí frente al USD).
USDCAD_CONFIG = PairConfig(
    name="USD/CAD",
    symbol="USDCAD",
    log_filename="senales_usdcad.csv",
    usd_is_base=True,
    backtest_daily_file="usdcad_daily_5y.json",
    backtest_h4_file="usdcad_h4.json",
    news_filters=[],
    market_context_notes=[],
)

USDJPY_CONFIG = PairConfig(
    name="USD/JPY",
    symbol="USDJPY",
    log_filename="senales_usdjpy.csv",
    pip_value=0.01,       # JPY cotiza a 2 decimales -- 1 pip = 0.01, no 0.0001
    price_decimals=2,
    usd_is_base=True,
    backtest_daily_file="usdjpy_daily_5y.json",
    backtest_h4_file="usdjpy_h4.json",
    news_filters=[],
    market_context_notes=[],
)

# ══════════════════════════════════════════════════════════════════════
#  QUINTO SISTEMA — S&P 500 (no es un par de divisas)
# ══════════════════════════════════════════════════════════════════════
# Instrumento elegido en agosto de 2026 tras barrer 31 candidatos de seis
# clases de activo. Razones, por orden de peso:
#   1. Encaja el calendario del motor sin tocarlo: no cotiza en fin de semana
#      y su vela diaria ya está CERRADA (16:00 NY) cuando se abre la ventana
#      de decisión de las 17:00 -- mejor garantía que en forex.
#   2. Mejor granularidad de tamaño de toda la cartera: 10.9 escalones de
#      lote contra los ~1.2 que promedian los 4 pares en vivo.
#   3. Spread más barato del universo (~0.5% del riesgo por operación).
#   4. Correlación 0.16 con el bloque AUD/EUR/GBP/NZD, que correlaciona
#      0.70-0.76 entre sí: DILUYE la concentración de la cartera.
#
# Datos: SP:SPX (índice de contado). Verificado que el cierre de TradingView
# coincide al céntimo con el histórico de IBKR (CBOE, contrato 416904), así
# que backtest y producción leen la misma serie.
#
# Escalas: 1 "pip" = 1 PUNTO del índice y 1.00 lote = $1/punto (contrato 1,
# confirmado por el usuario; lote mínimo del bróker 0.01 = $0.01/punto).
# Con ATR diario ~70 pts el SL de 1.5xATR sale a ~105 pts, que a $12.50 de
# riesgo da 0.11 lotes y $11.58 de riesgo real -- 93% del objetivo.
US500_CONFIG = PairConfig(
    name="US500",
    symbol="SPX",
    exchange="SP",
    screener="america",
    log_filename="senales_us500.csv",
    pip_value=1.0,                 # 1 punto del índice
    usd_per_pip_standard=1.0,      # $1 por punto con 1.00 lote
    price_decimals=2,
    unit_label="puntos",
    regime_symbol="VIX",           # el miedo sustituye al dólar (ver PairConfig)
    regime_label="Volatilidad (VIX)",
    regime_backtest_file="vix_daily_5y.json",
    # El VIX se consulta y se muestra, pero NO resta puntos. Medido sobre el
    # backtest 2024-04/2026-07: con el filtro activo el sistema pasa de 60% a
    # 50% de aciertos, de +$37.40 a +$9.97 netos y de 2.00 a 1.20 de profit
    # factor -- y recorta las señales de 34 a 26 en un instrumento que ya
    # opera poco. La hipótesis de partida (miedo al alza -> no comprar) es
    # razonable pero no sobrevive a la medición, así que se conserva el dato
    # a la vista sin dejar que decida. Muestra pequeña (10 trades): revisar
    # cuando haya más histórico.
    regime_penalises=False,
    bt_spread_units=0.6,             # puntos: spread típico del US500 (0.4-1.0)
    bt_commission_per_lot=0.0,       # los CFD de índice cobran vía spread, sin comisión aparte
    bt_financing_annual_pct=6.5,     # ~SOFR + 2.5% sobre el nocional -- ESTIMACIÓN, confirmar con el bróker
    usd_exposure=False,            # un largo de US500 no es un corto de dólar
    trades_sunday=False,           # el índice de contado no tiene vela de domingo
    respects_nyse_holidays=True,   # el NYSE cierra ~9-10 días al año que el forex no observa
    no_short_above_ema200=True,    # deriva estructural alcista de la renta variable
    backtest_daily_file="us500_daily_5y.json",
    backtest_h4_file="us500_h4_2y.json",
    news_filters=[
        {
            "keywords":   ["FOMC", "Federal Funds", "Fed Interest Rate", "Fed Statement",
                           "Federal Reserve", "Fed Chair", "Powell"],
            "currencies": ["USD"],
            "label":      "Decisión / discurso de la Fed",
        },
        {
            "keywords":   ["Non-Farm", "Nonfarm", "NFP", "Unemployment Rate"],
            "currencies": ["USD"],
            "label":      "NFP (Non-Farm Payrolls)",
        },
        {
            "keywords":   ["CPI", "Consumer Price", "Inflation", "PCE"],
            "currencies": ["USD"],
            "label":      "CPI / PCE / Inflación",
        },
        {
            "keywords":   ["PPI", "Producer Price"],
            "currencies": ["USD"],
            "label":      "PPI (precios de producción)",
        },
        {
            "keywords":   ["GDP", "Gross Domestic"],
            "currencies": ["USD"],
            "label":      "PIB de EE.UU.",
        },
        {
            "keywords":   ["Retail Sales"],
            "currencies": ["USD"],
            "label":      "Ventas minoristas",
        },
    ],
    market_context_notes=[
        "US500 = S&P 500 de contado. Los datos vienen de SP:SPX, que solo cotiza 9:30–16:00 NY",
        "Noticias clave: TODAS son de EE.UU. (Fed, CPI, NFP, PIB) — no hay segundo banco central",
        "Filtro de régimen: VIX en vez de DXY. VIX al alza penaliza compras; VIX en calma penaliza ventas",
        "Solo se permiten VENTAS con el precio bajo la EMA200 diaria (deriva alcista estructural)",
        "RIESGO PROPIO DEL ÍNDICE: abre con hueco tras el fin de semana o un dato macro — el stop "
        "puede ejecutarse al otro lado y perder MÁS del 1% nominal. En forex esto casi no pasa",
        "Rango diario promedio: ~70 puntos de ATR — el SL de 1.5xATR sale sobre 105 puntos",
        "Correlación 0.16 con el bloque AUD/EUR/GBP/NZD — es el instrumento que MÁS diversifica",
    ],
)

ALL_LIVE_CONFIGS = [AUD_CONFIG, GBP_CONFIG, EUR_CONFIG, NZD_CONFIG, US500_CONFIG]
ALL_BACKTEST_CONFIGS = [AUD_CONFIG, EUR_CONFIG, GBP_CONFIG, NZD_CONFIG, USDCAD_CONFIG, USDJPY_CONFIG,
                        US500_CONFIG]
