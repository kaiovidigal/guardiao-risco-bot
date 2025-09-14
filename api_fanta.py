# -*- coding: utf-8 -*-
"""
api_fanta.py
Cliente simples para ler o último resultado do Fan-Tan a partir da sua API.

Como configurar (ENV):
  API_URL  -> base ou endpoint completo.
              Exemplos aceitos:
                - http://189.1.172.114:8080/api-evolution/Fan-Tan/result.json
                - http://189.1.172.114:8080/api-evolution    (o código completa /Fan-Tan/result.json)
  API_KEY  -> (opcional) chave; se definida, vai em Authorization: Bearer <API_KEY>
  API_POLL_SECONDS -> (opcional) intervalo de polling (default 2.0s)
"""

import os, time, httpx
from typing import Optional, Tuple

API_URL = (os.getenv("API_URL") or "").strip()
API_KEY = (os.getenv("API_KEY") or "").strip()
API_POLL_SECONDS = float(os.getenv("API_POLL_SECONDS", "2"))

if not API_URL:
    raise RuntimeError("Defina API_URL (ex.: http://189.1.172.114:8080/api-evolution/Fan-Tan/result.json)")

# Normaliza: se for base /api-evolution, completa o caminho do JSON
if API_URL.endswith("/"):
    API_URL = API_URL[:-1]
if not API_URL.lower().endswith(".json"):
    # completa para o caminho padrão que você informou
    API_URL = API_URL + "/Fan-Tan/result.json"

_headers = {}
if API_KEY:
    _headers["Authorization"] = f"Bearer {API_KEY}"

_last_tuple: Optional[Tuple[int,  int]] = None  # (numero, ts epoch) p/ dedupe

def _coerce_int_1_4(x) -> Optional[int]:
    try:
        n = int(str(x).strip())
        if 1 <= n <= 4:
            return n
    except Exception:
        pass
    return None

def parse_payload(payload: dict) -> Optional[int]:
    """
    Tenta descobrir o último número do JSON.
    Suporta vários formatos comuns:
      {"ultimo":3}, {"result":3}, {"n":3}, {"ultimoNumero":3}, {"ultimo_resultado":3}
      {"hist":[...,3]} -> pega o último da lista
    """
    if not isinstance(payload, dict):
        return None

    # chaves diretas
    for k in ("ultimo", "result", "n", "ultimoNumero", "ultimo_resultado", "last"):
        if k in payload:
            n = _coerce_int_1_4(payload[k])
            if n is not None:
                return n

    # histórico padrão
    for k in ("hist", "history", "ultimos", "ultimas", "resultados"):
        if k in payload and isinstance(payload[k], (list, tuple)) and payload[k]:
            n = _coerce_int_1_4(payload[k][-1])
            if n is not None:
                return n

    # fallback: procura algum campo que pareça inteiro 1..4
    for v in payload.values():
        n = _coerce_int_1_4(v)
        if n is not None:
            return n

    return None

async def get_latest_result(client: Optional[httpx.AsyncClient]=None) -> Optional[Tuple[int, int]]:
    """
    Faz GET no endpoint e retorna (numero, ts_epoch) quando houver valor **novo**.
    Se não houver novo, retorna None.
    """
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=8)
        close_client = True

    try:
        r = await client.get(API_URL, headers=_headers)
        r.raise_for_status()
        data = r.json()
        n = parse_payload(data)
        if n is None:
            return None

        ts_epoch = int(time.time())
        global _last_tuple
        tup = (n, ts_epoch)

        # dedupe: se repetiu o mesmo número muito rápido, ainda assim retornamos,
        # porque cada rodada é um novo "observado". Caso queira dedupe por tempo,
        # comente a linha abaixo.
        _last_tuple = tup
        return tup
    except Exception:
        return None
    finally:
        if close_client:
            await client.aclose()
