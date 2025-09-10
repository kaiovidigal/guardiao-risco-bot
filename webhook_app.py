    candidates = [1,2,3,4] if ALLOW_OFFBASE else list(base)
    base_set = set(base)
    nb = max(1, len(base_set))
    no = max(1, len([c for c in candidates if c not in base_set]))

    for c in candidates:
        ng = ngram_backoff_score(tail, after_num, c)

        rowp = query_one("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pat_key, c))
        pw = rowp["wins"] if rowp else 0
        pl = rowp["losses"] if rowp else 0
        p_pat = laplace_ratio(pw, pl)

        p_str = 1/nb
        if strategy:
            rows = query_one("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (strategy, c))
            sw = rows["wins"] if rows else 0
            sl = rows["losses"] if rows else 0
            p_str = laplace_ratio(sw, sl)

        # prior: favorece quem está na base, mas reserva uma fração para off-base
        if ALLOW_OFFBASE:
            mass_off = max(0.0, min(0.9, OFFBASE_PRIOR_FRACTION))
            mass_in  = 1.0 - mass_off
            if c in base_set:
                prior = mass_in / nb
            else:
                prior = mass_off / no
        else:
            prior = 1.0/nb

        boost = 1.0
        if p_pat >= 0.60: boost = 1.05
        elif p_pat <= 0.40: boost = 0.97

        score = prior * ((ng or 1e-6) ** ALPHA) * (p_pat ** BETA) * (p_str ** GAMMA) * boost
        scores[c] = score