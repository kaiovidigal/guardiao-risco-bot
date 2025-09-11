# ===== Fases adaptativas por amostra + desempenho (com amortecimento) =====
# Limiares de amostra
_S_A_MAX = 20_000     # Fase A até 20k amostras
_S_B_MAX = 100_000    # Fase B até 100k amostras; >= isso considera C (se performar)

# Limiares de acurácia (IA) na janela de 7 dias
_ACC_TO_B = 0.55      # precisa >=55% para subir/ficar em B
_ACC_TO_C = 0.58      # precisa >=58% para subir/ficar em C
_ACC_FALL_B = 0.52    # se cair <52%, rebaixa de B
_ACC_FALL_C = 0.55    # se cair <55%, rebaixa de C

# Anti-churn: tempo mínimo na fase antes de permitir troca (em segundos)
_PHASE_DWELL = 60 * 60  # 1h

# Estado em memória da fase
_phase_name: str = "A"
_phase_since_ts: int = 0

def _current_samples() -> int:
    row = query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    return int((row["s"] or 0) if row else 0)

def _decide_phase(samples:int, acc7:float) -> str:
    """Decisão 'crua' (sem dwell) com base em amostra + acurácia IA(7d)."""
    if samples < _S_A_MAX:
        # Ainda muito cedo -> A
        return "A"

    if samples < _S_B_MAX:
        # Janela média -> A se acc ruim, senão B
        return "B" if acc7 >= _ACC_TO_B else "A"

    # Amostra grande -> tenta C se acc muito boa, senão B ou A
    if acc7 >= _ACC_TO_C:
        return "C"
    if acc7 >= _ACC_TO_B:
        return "B"
    # Se caiu muito, volta pra A para recuperar base com mais volume
    return "A" if acc7 < _ACC_FALL_B else "B"

def _adaptive_phase() -> str:
    """Fase com amortecimento (dwell) para evitar trocar o tempo todo."""
    global _phase_name, _phase_since_ts
    s = _current_samples()
    _g, _l, acc7 = _ia_acc_days(7)

    # Proposta de fase
    target = _decide_phase(s, acc7)

    # Dwell: se acabou de trocar, respeita janela mínima
    now = now_ts()
    if _phase_since_ts == 0:
        _phase_name, _phase_since_ts = target, now
        return _phase_name

    # Se mesma fase, só atualiza tempo
    if target == _phase_name:
        return _phase_name

    # Se deseja SUBIR de fase, precisa cumprir dwell
    if target > _phase_name:
        if now - _phase_since_ts < _PHASE_DWELL:
            return _phase_name  # segura mais um pouco
        _phase_name, _phase_since_ts = target, now
        return _phase_name

    # Se deseja DESCER:
    #  - C -> B se acc7 < ACC_FALL_C
    #  - B -> A se acc7 < ACC_FALL_B
    if _phase_name == "C" and acc7 < _ACC_FALL_C:
        _phase_name, _phase_since_ts = "B", now
        return _phase_name
    if _phase_name == "B" and acc7 < _ACC_FALL_B:
        _phase_name, _phase_since_ts = "A", now
        return _phase_name

    # Caso contrário, respeita dwell também para quedas “leves”
    if now - _phase_since_ts < _PHASE_DWELL:
        return _phase_name

    _phase_name, _phase_since_ts = target, now
    return _phase_name

def _eff() -> dict:
    """Parâmetros efetivos da fase atual."""
    p = _adaptive_phase()
    if p == "A":
        return {
            "PHASE": "A",
            "MAX_CONCURRENT_PENDINGS": 2,
            "IA2_MAX_PER_HOUR": 30,
            "IA2_MIN_SECONDS_BETWEEN_FIRE": 7,
            "IA2_COOLDOWN_AFTER_LOSS": 10,
            "IA2_TIER_STRICT": 0.59,
            "IA2_DELTA_GAP": 0.04,   # flex: 0.55 se gap ≥ 0.08
            "IA2_GAP_SAFETY": IA2_GAP_SAFETY,
            "MIN_CONF_G0": 0.52,
            "MIN_GAP_G0": 0.035,
        }
    elif p == "B":
        return {
            "PHASE": "B",
            "MAX_CONCURRENT_PENDINGS": 2,
            "IA2_MAX_PER_HOUR": 20,
            "IA2_MIN_SECONDS_BETWEEN_FIRE": 10,
            "IA2_COOLDOWN_AFTER_LOSS": 12,
            "IA2_TIER_STRICT": 0.60,
            "IA2_DELTA_GAP": 0.04,
            "IA2_GAP_SAFETY": IA2_GAP_SAFETY,
            "MIN_CONF_G0": 0.53,
            "MIN_GAP_G0": 0.035,
        }
    else:
        return {
            "PHASE": "C",
            "MAX_CONCURRENT_PENDINGS": 1,
            "IA2_MAX_PER_HOUR": 12,
            "IA2_MIN_SECONDS_BETWEEN_FIRE": 12,
            "IA2_COOLDOWN_AFTER_LOSS": 15,
            "IA2_TIER_STRICT": 0.62,
            "IA2_DELTA_GAP": 0.03,
            "IA2_GAP_SAFETY": IA2_GAP_SAFETY,
            "MIN_CONF_G0": 0.55,
            "MIN_GAP_G0": 0.040,
        }