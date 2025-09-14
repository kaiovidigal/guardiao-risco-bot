# api_fanta.py
# Cliente simples para ler o último número do Fan-Tan via JSON
import os
import requests
from typing import Optional, Tuple, Any

API_URL  = os.getenv("FANTA_API_URL", "http://189.1.172.114:8080/api-evolution/Fan-Tan/result.json")
API_USER = os.getenv("FANTA_API_USER", "Kaio")
API_PASS = os.getenv("FANTA_API_PASS", "Kaio")

# guarda o último "id" lido para não repetir fechamento
_last_round_id: Optional[str] = None

def _extract_number_and_id(data: Any) -> Tuple[Optional[int], Optional[str]]:
    """
    Tenta achar (numero, round_id) em formatos comuns.
    Ajuste aqui se seu JSON tiver chaves diferentes.
    """
    # exemplos comuns:
    # {"round": 12345, "result": 3}
    # {"last": {"id": "1694700001", "number": 2}}
    # {"id":"...","value":4}
    # {"result":"3"}  ← string
    # {"results":[..., {"id": "...", "n": 1}]}  ← lista, pega o último

    if isinstance(data, dict):
        # candidato a round/id
        rid = (
            data.get("round")
            or data.get("id")
            or (data.get("last") or {}).get("id")
        )
        # candidato a número
        n = (
            data.get("result")
            or data.get("number")
            or data.get("value")
            or (data.get("last") or {}).get("result")
            or (data.get("last") or {}).get("number")
        )
        try:
            n = int(n) if n is not None else None
        except Exception:
            n = None

        # se vier lista em "results" / "history", pega o último
        for key in ("results", "history", "items"):
            if key in data and isinstance(data[key], list) and data[key]:
                last = data[key][-1]
                lrid = last.get("id") or last.get("round") or rid
                ln = last.get("n") or last.get("result") or last.get("number")
                try:
                    ln = int(ln) if ln is not None else None
                except Exception:
                    ln = None
                if ln in (1, 2, 3, 4):
                    return ln, str(lrid) if lrid is not None else rid

        if n in (1, 2, 3, 4):
            return n, str(rid) if rid is not None else None

    # fallback: tenta varrer um dígito 1–4 na string do JSON
    try:
        import re, json
        s = json.dumps(data)
        m = re.search(r'[^0-9](1|2|3|4)[^0-9]', s)
        if m:
            return int(m.group(1)), None
    except Exception:
        pass
    return None, None

def get_new_number() -> Optional[Tuple[int, Optional[str]]]:
    """
    Consulta a API. Se houver um novo resultado (round diferente do último),
    retorna (numero, round_id). Se nada novo, retorna None.
    """
    global _last_round_id
    r = requests.get(API_URL, auth=(API_USER, API_PASS), timeout=8)
    r.raise_for_status()
    data = r.json()
    n, rid = _extract_number_and_id(data)
    if n not in (1, 2, 3, 4):
        return None
    # se a API não tiver id, considera sempre "novo"
    if rid is None:
        return (n, None)
    # dedup por id de rodada
    if _last_round_id == str(rid):
        return None
    _last_round_id = str(rid)
    return (n, str(rid))
