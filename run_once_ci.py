#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Punto de entrada para GitHub Actions (ver .github/workflows/analysis-cycle.yml).

dashboard.py normal es un proceso de vida larga: un hilo hace el loop de
análisis cada 15-30 min y otro sirve el HTTP local para siempre. En GitHub
Actions cada ejecución es una VM efímera que se levanta, corre algo, y se
apaga -- no tiene sentido (ni sirve) levantar un servidor ahí. Este script
hace UN solo ciclo de análisis (equivalente a lo que hace main() al arrancar
+ una vuelta de worker()) y termina.

Reutiliza dashboard.py tal cual -- ninguna lógica de análisis se duplica.
Lo único propio de este script es: sin servidor, sin loop, y esperar a que
el hilo de publish_snapshot() (disparado dentro de run_wave) termine antes
de salir, porque si no Actions mata el proceso a mitad de subir los JSON.

El estado entre corridas (qué par tiene una orden pendiente/activa, historial
de señales, etc.) vive en data/*.csv -- el workflow hace checkout del repo
antes de correr esto y commitea data/ de vuelta después, así que cada
ejecución continúa donde quedó la anterior.
"""

import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import dashboard as d  # noqa: E402


def main():
    d._purge_old_rows(d.SCORE_HISTORY_FILE)
    for _, mod in d.PAIRS:
        d._purge_old_rows(d._ohlc_file(mod))

    with d.STATE_LOCK:
        d.STATE["started"] = d.now_iso()
        d.STATE["score_history"] = d.load_score_history()
        d.STATE["ohlc_history"] = {mod.SYMBOL: d.load_ohlc_history(mod) for _, mod in d.PAIRS}
        d.STATE["pnl"] = d.compute_pnl()

    d.log("=== Ciclo único (GitHub Actions) ===")
    d.run_wave()
    d.log("Ciclo terminado -- esperando a que termine de publicar...")

    if d.LAST_PUBLISH_THREAD is not None:
        d.LAST_PUBLISH_THREAD.join(timeout=90)
        if d.LAST_PUBLISH_THREAD.is_alive():
            d.log("El publicador no terminó en 90s -- se sigue el proceso igual "
                  "(GitHub Actions lo cortará, revisar el log del próximo ciclo).")

    d.log("Listo.")


if __name__ == "__main__":
    main()
