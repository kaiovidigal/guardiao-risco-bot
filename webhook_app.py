 -*- coding: utf-8 -*-
"""
Wrapper de ultra precisão (~90%) para o seu webhook_app existente.

- NÃO altera seu DB/rotas/parsers; apenas aplica patches em tempo de import
- IA (FIRE/GREEN/LOSS) sai EXCLUSIVAMENTE no IA_CHANNEL
- Thresholds bem rígidos → menos sinais, maior acurácia
- Debounce de GREEN/LOSS para evitar eco do Telegram
"""

import os
import time
import asyncio

import webhook_app as base

# ========= Logs úteis =========
print("🔧 Precisão 90% wrapper ativo")
print("🔍 Lendo de PUBLIC_CHANNEL:", os.getenv("PUBLIC_CHANNEL", "(env não definido)"))
print("📡 Enviando IA para IA_CHANNEL:", os.getenv("IA_CHANNEL", "(env não definido)"))

# ========= 1) Thresholds de 90% =========
try:
    # G0 do canal
    base.MIN_SAMPLES = max(getattr(base, 'MIN_SAMPLES', 1500), 2500)  # exige mais dados
    base.GAP_MIN     = 0.08
    base.MIN_CONF_G0 = 0.78   # pode subir para 0.80 se quiser ainda mais rígido
    base.MIN_GAP_G0  = 0.080

    # IA — hiper seletiva
    base.IA2_TIER_STRICT             = 0.82
    base.IA2_DELTA_GAP               = 0.01
    base.IA2_GAP_SAFETY              = 0.10
    base.IA2_MAX_PER_HOUR            = 8
    base.IA2_MIN_SECONDS_BETWEEN_FIRE= 6
    base.IA2_COOLDOWN_AFTER_LOSS     = 25
except Exception as e:
    print("[wrapper90] falha aplicando thresholds:", e)

# ========= 2) Envio dedicado para IA =========
IA_CHANNEL = os.getenv("IA_CHANNEL", "").strip()
async def send_ia_text_only(text: str, parse: str = "HTML"):
    if not IA_CHANNEL:
        return
    try:
        await base.tg_send_text(str(IA_CHANNEL), text, parse)
    except Exception as e:
        print(f"[IA-OUT] erro ao enviar: {e}")

# patch do sinal IA
if hasattr(base, "ia2_send_signal"):
    async def ia2_send_signal_patched(best:int, conf:float, tail_len:int, mode:str):
        txt = (
            f"🤖 <b>{getattr(base, 'SELF_LABEL_IA', 'Tiro seco por IA')} [{mode}]</b>\n"
            f"🎯 Número seco (G0): <b>{best}</b>\n"
            f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{tail_len}</b>"
        )
        await send_ia_text_only(txt)
    base.ia2_send_signal = ia2_send_signal_patched

# ========= 3) Debounce de GREEN/LOSS =========
_DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "1.2"))
_last_obs_ts = 0.0
if hasattr(base, "close_pending_with_result"):
    _orig_close = base.close_pending_with_result
    async def close_pending_with_result_patched(n_observed: int, event_kind: str):
        global _last_obs_ts
        now = time.time()
        if now - _last_obs_ts < _DEBOUNCE_SECONDS:
            return {"ok": True, "debounced": True}
        _last_obs_ts = now
        res = await _orig_close(n_observed, event_kind)
        # notifica IA se a pendência fechada pertencia à IA
        try:
            row = base.query_one("""
                SELECT suggested, stage, source
                  FROM pending_outcome
                 WHERE open=0
              ORDER BY id DESC LIMIT 1
            """)
            if row and (row["source"] or "").upper() == "IA":
                stage_txt = "G0" if int(row["stage"]) == 0 else f"G{int(row['stage'])}"
                msg = "✅ <b>GREEN</b>" if event_kind == "GREEN" else "❌ <b>LOSS</b>"
                await send_ia_text_only(f"{msg} em <b>{stage_txt}</b> — Número: <b>{int(row['suggested'])}</b>")
        except Exception as e:
            print(f"[IA-POST] erro: {e}")
        return res
    base.close_pending_with_result = close_pending_with_result_patched

# ========= 4) Exporta o app =========
app = base.app
print("[wrapper90] carregado com sucesso.")
