# -*- coding: utf-8 -*-
"""
api_fanta.py
Cliente para ler o último resultado do Fan-Tan a partir da sua API.

ENV aceitas:
  API_URL  -> endpoint completo OU base /api-evolution (o código completa /Fan-Tan/result.json)
  API_USER -> (opcional) usuário p/ Basic Auth
  API_PASS -> (opcional) senha   p/ Basic Auth
  API_KEY  -> (opcional) Bearer token (Authorization: Bearer <API_KEY>)
  API_POLL_SECONDS -> (opcional) intervalo de polling (default 2.0s)
"""

import os, time, httpx
from typing import Optional, Tuple, Any, Dict

API_URL = (os.getenv("API_URL") or "").strip()
API_USER = (os.getenv("API_USER") or os.getenv("FANTA_API_USER") or "").strip()
API_PASS = (os.getenv("API_PASS") or os.getenv("FANTA_API_PASS") or "").strip()
API_KEY  = (os.getenv("API_KEY")  or "").strip()
API_POLL_SECONDS = float(os.getenv("API_POLL_SECONDS", "2"))

if not API_URL:
    raise RuntimeError("Defina API_URL (ex.: http://189.1.172.114:8080/api-evolution/Fan-Tan/result.json)")

# Normaliza: se for base /api-evolution, completa o caminho do JSON
if API_URL.endswith("/"):
    API_URL = API_URL[:-1]
if not API_URL.lower().endswith(".json"):
    API_URL = API_URL + "/Fan-Tan/result.json"

_headers: Dict[str, str] = {}
_auth: Optional[tuple] = None

if API_KEY:
    _headers["Authorization"] = f"Bearer {API_KEY}"
elif API_USER and API_PASS:
    # Basic Auth será passado como auth=(user, pass) no httpx.get
    _auth = (API_USER, API_PASS)

_last_tuple: Optional[Tuple[int, int]] = None  # (numero, ts epoch) p/ dedupe leve


def _coerce_1_4(x: Any) -> Optional[int]:
    """Converte valor para int 1..4, retorna None se não for válido."""
    try:
        n = int(str(x).strip())
        if 1 <= n <= 4:
            return n
    except Exception:
        pass
    return None


def parse_payload(payload: Any) -> Optional[int]:
    """
    Descobre o último número do JSON.
    Suporta:
      {"Fan Tan": ["2","3","1",...]}   -> pega o ÚLTIMO da lista
      {"ultimo":3}, {"result":3}, {"n":3}, {"last":3}, ...
      {"hist":[...]} (ou "history"/"resultados"/"ultimos"/"ultimas") -> último elemento
      Fallback: procura qualquer campo simples que seja 1..4
    """
    if not isinstance(payload, dict):
        return None

    # 1) Seu formato atual
    if "Fan Tan" in payload and isinstance(payload["Fan Tan"], (list, tuple)) and payload["Fan Tan"]:
        n = _coerce_1_4(payload["Fan Tan"][-1])
        if n is not None:
            return n

    # 2) Campos diretos comuns
    for k in ("ultimo", "result", "n", "last", "ultimoNumero", "ultimo_resultado"):
        if k in payload:
            n = _coerce_1_4(payload[k])
            if n is not None:
                return n

    # 3) Históricos comuns
    for k in ("hist", "history", "ultimos", "ultimas", "resultados"):
        if k in payload and isinstance(payload[k], (list, tuple)) and payload[k]:
            n = _coerce_1_4(payload[k][-1])
            if n is not None:
                return n

    # 4) Fallback: varre valores simples 1..4
    for v in payload.values():
        n = _coerce_1_4(v)
        if n is not None:
            return n

    return None


async def get_latest_result(client: Optional[httpx.AsyncClient] = None) -> Optional[Tuple[int, int]]:
    """
    Faz GET no endpoint e retorna (numero, ts_epoch).
    Retorna None se não conseguir extrair um número 1..4.
    """
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=8)
        close_client = True

    try:
        r = await client.get(API_URL, headers=_headers or None, auth=_auth, timeout=8)
        r.raise_for_status()
        data = r.json()
        n = parse_payload(data)
        if n is None:
            return None

        ts_epoch = int(time.time())
        # Sem dedupe rígido: cada resposta é tratada como novo observado
        return (n, ts_epoch)

    except Exception:
        return None
    finally:
        if close_client:
            await client.aclose()