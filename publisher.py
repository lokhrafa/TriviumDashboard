#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUBLICADOR A GITHUB PAGES — instantánea de solo lectura del dashboard para verlo
desde el celular (o cualquier sitio sin acceso a localhost:8877).

Sube 4 archivos a la carpeta `docs/` de un repo de GitHub (Pages configurado
para servir desde ahí) usando la API REST de Contents — no requiere `git`
instalado, solo `requests` (ya es dependencia del proyecto):

  docs/index.html            — copia de dashboard.html; solo se re-sube si
                                cambió su hash (el HTML casi nunca cambia).
  docs/state.json             — equivalente a /api/state (sin score/ohlc history).
  docs/score_history.json     — equivalente a /api/score_history.
  docs/ohlc_history.json      — equivalente a /api/ohlc_history, pero recortado
                                a OHLC_STATIC_MAX_POINTS por símbolo (el móvil
                                no necesita 300 puntos de historial por par).

Configuración por variables de entorno (ninguna se guarda en el código):
  TVM_GH_REPO    "usuario/repo"   -- obligatorio para publicar.
  TVM_GH_TOKEN   PAT fine-grained con permiso Contents:Read&Write en ese repo
                 -- obligatorio para publicar.
  TVM_GH_BRANCH  rama de Pages (default "main").
  TVM_GH_DOCS_PATH  carpeta dentro del repo (default "docs").

Si falta el repo o el token, publish_snapshot() no hace nada (deja un aviso
una sola vez en el log) -- el dashboard local sigue funcionando igual.
Cualquier error de red o de la API se atrapa y se loguea: nunca debe tumbar
el ciclo de análisis que lo llama.
"""

import base64
import hashlib
import json
import math
import os
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GH_REPO = os.environ.get("TVM_GH_REPO", "").strip()          # "usuario/repo"
GH_TOKEN = os.environ.get("TVM_GH_TOKEN", "").strip()
GH_BRANCH = os.environ.get("TVM_GH_BRANCH", "main").strip() or "main"
GH_DOCS_PATH = os.environ.get("TVM_GH_DOCS_PATH", "docs").strip().strip("/") or "docs"

# Cuánto historial OHLC va en la instantánea pública -- el dashboard local
# guarda hasta OHLC_HISTORY_MAX_POINTS (300) por símbolo; para el móvil con
# menos pantalla y para no engordar cada commit, se recorta más.
OHLC_STATIC_MAX_POINTS = 120

# Solo re-subir el HTML cuando cambia -- se cachea su sha256 en disco para
# que sobreviva a un reinicio del dashboard.
_HTML_HASH_FILE = os.path.join(BASE_DIR, "data", ".gh_pages_html_sha256")

API_ROOT = "https://api.github.com"
REQUEST_TIMEOUT = 20

_warned_no_config = False


def _sanitize(obj):
    """json no acepta inf/nan -- limpiarlos antes de serializar (igual que en dashboard.py)."""
    if isinstance(obj, float):
        return None if (math.isinf(obj) or math.isnan(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _trim_ohlc_for_static(ohlc_history):
    return {symbol: rows[-OHLC_STATIC_MAX_POINTS:] for symbol, rows in (ohlc_history or {}).items()}


class _GitHubPublishError(Exception):
    pass


class GitHubPublisher:
    """Sube archivos a docs/ de un repo vía la API de Contents (sin git local).

    Cachea el sha de cada archivo tras cada PUT exitoso para no tener que
    hacer un GET antes de cada escritura -- solo se re-consulta el sha si
    GitHub responde que el que teníamos ya no es el vigente (alguien más
    tocó el archivo, o es la primera vez que se sube en este proceso).
    """

    def __init__(self, repo, token, branch, docs_path, log_fn=print):
        self.repo = repo
        self.branch = branch
        self.docs_path = docs_path
        self.log = log_fn
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self._sha_cache = {}

    def _contents_url(self, rel_path):
        # docs_path="" permite reutilizar esta clase para subir a la RAÍZ del
        # repo (ver herramientas de bootstrap) en vez de siempre bajo docs/.
        full_path = f"{self.docs_path}/{rel_path}" if self.docs_path else rel_path
        return f"{API_ROOT}/repos/{self.repo}/contents/{full_path}"

    def _fetch_sha(self, rel_path):
        try:
            r = self._session.get(self._contents_url(rel_path), params={"ref": self.branch},
                                   timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            return None
        if r.status_code == 200:
            return r.json().get("sha")
        return None   # 404 -> el archivo no existe todavía, se crea sin sha

    def put_file(self, rel_path, content_bytes, message):
        """Devuelve (ok: bool, motivo_error: str|None). Nunca lanza."""
        b64 = base64.b64encode(content_bytes).decode("ascii")
        sha = self._sha_cache.get(rel_path)
        last_error = "sin intentos"
        for attempt in range(3):
            body = {"message": message, "content": b64, "branch": self.branch}
            if sha:
                body["sha"] = sha
            try:
                r = self._session.put(self._contents_url(rel_path), json=body, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                last_error = f"red: {e}"
                time.sleep(1.5 * (attempt + 1))
                continue

            if r.status_code in (200, 201):
                self._sha_cache[rel_path] = r.json().get("content", {}).get("sha")
                return True, None

            body_txt = r.text[:300]
            # 409 (conflicto) o 422 con "sha" en el mensaje -> nuestro sha
            # cacheado quedó viejo (alguien tocó el archivo por fuera, o es
            # la primera escritura de este proceso). Volvemos a pedirlo y
            # reintentamos.
            if r.status_code == 409 or (r.status_code == 422 and "sha" in body_txt.lower()):
                sha = self._fetch_sha(rel_path)
                last_error = f"{r.status_code}: sha desactualizado, reintentando"
                continue
            if r.status_code == 403 and "rate limit" in body_txt.lower():
                last_error = "403: rate limit de GitHub"
                time.sleep(2 ** attempt)
                continue
            # Otro error (403 permisos, 404 repo/rama inexistente, etc.) --
            # no tiene sentido reintentar igual.
            return False, f"{r.status_code}: {body_txt}"

        return False, last_error


def _get_publisher(log_fn):
    global _warned_no_config
    if not GH_REPO or not GH_TOKEN:
        if not _warned_no_config:
            log_fn("Publicador GitHub Pages desactivado -- faltan TVM_GH_REPO y/o TVM_GH_TOKEN "
                   "(variables de entorno). El dashboard local sigue funcionando normal.")
            _warned_no_config = True
        return None
    return GitHubPublisher(GH_REPO, GH_TOKEN, GH_BRANCH, GH_DOCS_PATH, log_fn=log_fn)


def _read_cached_html_hash():
    try:
        with open(_HTML_HASH_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _write_cached_html_hash(digest):
    try:
        os.makedirs(os.path.dirname(_HTML_HASH_FILE), exist_ok=True)
        with open(_HTML_HASH_FILE, "w", encoding="utf-8") as f:
            f.write(digest)
    except OSError:
        pass   # no es crítico -- en el peor caso se re-sube el HTML una vez de más


def publish_snapshot(state_slim, score_history, ohlc_history, html_path, log_fn=print):
    """Publica la instantánea actual en docs/ del repo configurado.

    state_slim: el dict que ya se sirve en /api/state (sin score/ohlc_history).
    score_history / ohlc_history: los dicts completos que dashboard.py guarda
        en STATE -- aquí se recorta ohlc_history para el móvil.
    html_path: ruta a dashboard.html; se sube a docs/index.html solo si cambió.

    Nunca lanza -- cualquier fallo se loguea y se ignora, el ciclo de
    análisis que llama a esto debe seguir funcionando igual.
    """
    try:
        pub = _get_publisher(log_fn)
        if pub is None:
            return

        ok_count, fail_count = 0, 0

        # 1) HTML -- solo si cambió desde la última publicación.
        try:
            with open(html_path, "rb") as f:
                html_bytes = f.read()
            digest = hashlib.sha256(html_bytes).hexdigest()
            if digest != _read_cached_html_hash():
                ok, err = pub.put_file("index.html", html_bytes, "Actualizar dashboard (docs/index.html)")
                if ok:
                    _write_cached_html_hash(digest)
                    ok_count += 1
                    log_fn("Publicador: docs/index.html actualizado en GitHub Pages.")
                else:
                    fail_count += 1
                    log_fn(f"Publicador: fallo al subir index.html -- {err}")
        except OSError as e:
            fail_count += 1
            log_fn(f"Publicador: no se pudo leer {html_path} -- {e}")

        # 2) Los 3 JSON de datos -- se re-suben en cada ciclo (contenido nuevo).
        payloads = {
            "state.json": _sanitize(state_slim),
            "score_history.json": _sanitize(score_history),
            "ohlc_history.json": _sanitize(_trim_ohlc_for_static(ohlc_history)),
        }
        for rel_path, payload in payloads.items():
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            ok, err = pub.put_file(rel_path, data, f"Actualizar {rel_path}")
            if ok:
                ok_count += 1
            else:
                fail_count += 1
                log_fn(f"Publicador: fallo al subir {rel_path} -- {err}")

        if fail_count:
            log_fn(f"Publicador: {ok_count} archivo(s) OK, {fail_count} fallo(s) en este ciclo.")
    except Exception as e:
        # Red de seguridad final -- un bug en el publicador jamás debe tumbar
        # el ciclo de análisis de trading que lo invoca.
        log_fn(f"Publicador: error inesperado (ignorado) -- {e}")
