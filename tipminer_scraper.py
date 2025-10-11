# tipminer_scraper.py
# Lê o histórico da TipMiner via Playwright (navegador headless)
import asyncio, re
from typing import Tuple, Optional
from playwright.async_api import async_playwright

TIPMINER_URL_DEFAULT = "https://www.tipminer.com/br/historico/jonbet/bac-bo"

# Regex para detectar o último lado vencedor
TM_PLAYER_RE = re.compile(r"(🔵|🟦|player|azul)\b", re.I)
TM_BANKER_RE = re.compile(r"(🔴|🟥|banker|vermelho)\b", re.I)

async def _extract_side_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    head = text[:100_000]
    has_p = bool(TM_PLAYER_RE.search(head))
    has_b = bool(TM_BANKER_RE.search(head))
    if has_p and not has_b:
        return "player"
    if has_b and not has_p:
        return "banker"
    mp = TM_PLAYER_RE.search(head)
    mb = TM_BANKER_RE.search(head)
    if mp and mb:
        return "player" if mp.start() < mb.start() else "banker"
    return None

async def get_tipminer_latest_side(
    url: str = TIPMINER_URL_DEFAULT,
    timeout_ms: int = 10_000,
    headless: bool = True
) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Retorna (side, "tipminer_playwright", trecho_html)
      - side: "player" | "banker" | None
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await ctx.set_extra_http_headers({"User-Agent": "Mozilla/5.0"})
            await page.goto(url, timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass
            raw = await page.evaluate("() => document.body && document.body.innerText || ''")
            side = await _extract_side_from_text(raw)
            return side, "tipminer_playwright", (raw or "")[:2000]
        finally:
            await browser.close()