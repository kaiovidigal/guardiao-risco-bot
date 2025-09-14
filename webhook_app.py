# =========================
# Fechamento de pendências (GREEN/RED observado)
# =========================
async def close_pending_with_result(n_observed: int, event_kind: str):
    try:
        rows = query_all("""
            SELECT id, created_at, strategy, suggested, stage, open, window_left,
                   seen_numbers, announced, source
            FROM pending_outcome
            WHERE open=1
            ORDER BY id ASC
        """)
        if not rows:
            return {"ok": True, "no_open": True}

        for r in rows:
            pid        = r["id"]
            suggested  = int(r["suggested"])
            stage      = int(r["stage"])
            left       = int(r["window_left"])
            src        = (r["source"] or "CHAN").upper()
            seen       = (r["seen_numbers"] or "").strip()
            # ✅ CORRIGIDO: sem barras invertidas nas aspas
            seen_new   = (seen + ("," if seen else "") + str(int(n_observed)))

            if int(n_observed) == suggested:
                # GREEN
                exec_write(
                    "UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                    (seen_new, pid)
                )

                # Sequência de conferência: últimos 2 observados + (hit)
                seq_txt = _fmt_seq_conferencia(seen, n_observed, suggested)

                if stage == 0:
                    # GREEN direto no G0
                    update_daily_score(0, True)
                    if src == "IA":
                        update_daily_score_ia(0, True)
                    await send_green_imediato(sugerido=suggested, stage_txt="G0", seq_txt=seq_txt)
                else:
                    # Recuperação oculta: converte o último LOSS em GREEN
                    _convert_last_loss_to_green()
                    if src == "IA":
                        _convert_last_loss_to_green_ia()
                    bump_recov_g1(suggested, True)

                try:
                    INTEL.stop_signal()
                except Exception:
                    pass

            else:
                # Não bateu
                if left > 1:
                    # avança estágio (G1->G2) mantendo pendência aberta
                    # ✅ CORRIGIDO: aspas triplas normais
                    exec_write("""
                        UPDATE pending_outcome
                           SET stage = stage + 1, window_left = window_left - 1, seen_numbers=?
                         WHERE id=?
                    """, (seen_new, pid))

                    if stage == 0:
                        # PRIMEIRO erro (G0): conta LOSS e anuncia com conferência
                        update_daily_score(0, False)
                        if src == "IA":
                            update_daily_score_ia(0, False)
                        seq_txt = _fmt_seq_conferencia(seen, n_observed, suggested)
                        await send_loss_imediato(sugerido=suggested, stage_txt="G0", obs=n_observed, seq_txt=seq_txt)
                        _ia_set_post_loss_block()
                        bump_recov_g1(suggested, False)
                else:
                    # pendência esgotada (já contabilizado loss no G0)
                    exec_write(
                        "UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                        (seen_new, pid)
                    )

        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}