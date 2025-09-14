def _extract_num_from_api(payload: dict) -> Optional[int]:
    # Tenta vários caminhos comuns
    candidates = [
        ("numero",),
        ("result","numero"),
        ("result","n"),
        ("ultimo","numero"),
        ("ultimo","saida"),
        ("last","number"),
        ("data","numero"),
    ]
    for path in candidates:
        cur = payload
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok:
            try:
                n = int(str(cur).strip())
                if n in (1,2,3,4):
                    return n
            except Exception:
                pass
    # Fallback: regex no JSON inteiro
    try:
        m = re.search(r'\b([1-4])\b', json.dumps(payload))
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None