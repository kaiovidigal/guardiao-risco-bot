def derive_our_result_from_source_flow(text:str, fonte_side:str|None, chosen_side:str|None):
    """
    - Se fonte foi para G1/G2: igual ao fonte = LOSS; oposto = GREEN
    - Se fonte G0:
        -> igual ao fonte = GREEN
        -> oposto = EMPATE (GREEN também)
    """
    if not fonte_side or not chosen_side:
        return None
    low = (text or "").lower()

    # Caso fonte vá pro Gale (G1/G2)
    if SOURCE_WENT_GALE_RE.search(low):
        return "R" if (chosen_side == fonte_side) else "G"

    # Caso fonte acerte de primeira (G0)
    if SOURCE_G0_GREEN_RE.search(low):
        return "G"  # IA acerta se igual ao fonte ou se for oposta (empate)

    return None