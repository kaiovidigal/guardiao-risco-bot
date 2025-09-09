import os, re, json, asyncio, httpx
from typing import Optional, List
from playwright.async_api import async_playwright, Page, Frame

TARGET_URL      = os.getenv("TARGET_URL", "https://blaze.bet.br/pt/games/fan-tan")
WEBHOOK_BASE    = os.getenv("WEBHOOK_BASE", "")
INGEST_TOKEN    = os.getenv("INGEST_TOKEN", "")
POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "1"))

# Você pode deixar em branco; usaremos candidatos abaixo.
LAST_RESULT_SELECTOR = os.getenv("LAST_RESULT_SELECTOR", "").strip()

# Lista de seletores candidatos (ajustáveis com o tempo).
# Ideia: procurar número atual e/ou o "último da lista de históricos".
CANDIDATES: List[str] = [
    # exemplos genéricos (ajuste se encontrar melhor no DevTools):
    "div.last-result, span.last-result, div.result, span.result",
    "[class*=last] [class*=result]",
    "[data-result], [data-testid*=result]",

    # histórico: pegue o MAIS à esquerda / topo (depende do layout)
    "ul[class*=history] li:first-child",
    "div[class*=history] div:first-child",
    "div.history div.item:first-child",
]

# regex para extrair 1..4 em strings
NUM_RE = re.compile(r"\b([1-4])\b")

last_sent: Optional[int] = None

async def post_number(n: int):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{WEBHOOK_BASE}/ingest/number",
            headers={"X-Ingest-Token": INGEST_TOKEN, "Content-Type": "application/json"},
            content=json.dumps({"number": n})
        )
        r.raise_for_status()

def extract_num(text: str) -> Optional[int]:
    if not text:
        return None
    m = NUM_RE.search(text)
    if m:
        return int(m.group(1))
    return None

async def try_selectors_in(page_like, where: str="page") -> Optional[int]:
    """Tenta todos os seletores candidatos e devolve o primeiro número válido encontrado."""
    global last_sent
    if LAST_RESULT_SELECTOR:
        try:
            el = await page_like.query_selector(LAST_RESULT_SELECTOR)
            if el:
                txt = (await el.inner_text()).strip()
                n = extract_num(txt)
                if n in (1,2,3,4):
                    print(f"[match:{where}] {LAST_RESULT_SELECTOR} -> {txt} -> {n}")
                    return n
        except Exception as e:
            print(f"[warn:{where}] selector (LAST_RESULT_SELECTOR) falhou:", e)

    for sel in CANDIDATES:
        try:
            el = await page_like.query_selector(sel)
            if not el:
                continue
            txt = (await el.inner_text()).strip()
            n = extract_num(txt)
            if n in (1,2,3,4):
                print(f"[match:{where}] {sel} -> {txt} -> {n}")
                return n
        except Exception as e:
            # só loga e segue tentando os outros
            pass
    return None

async def scan_all_frames(page: Page) -> Optional[int]:
    """Procura em todos os frames/iframes também (Blaze costuma usar iframes)."""
    # tenta no root primeiro
    n = await try_selectors_in(page, where="root")
    if n:
        return n
    # tenta em frames
    for fr in page.frames:
        try:
            n = await try_selectors_in(fr, where=f"frame:{fr.name or fr.url[:40]}")
            if n:
                return n
        except Exception:
            continue
    return None

async def main():
    global last_sent
    if not WEBHOOK_BASE or not INGEST_TOKEN:
        print("❌ Configure WEBHOOK_BASE e INGEST_TOKEN nas variáveis do Render.")
        return

    async with async_playwright() as p:
        # Use Chromium headless
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()
        print("Abrindo:", TARGET_URL)
        await page.goto(TARGET_URL, wait_until="load", timeout=60000)

        # Se houver login na operadora, você pode precisar automatizar aqui:
        # await page.fill("input[name=email]", "SEU_EMAIL")
        # await page.fill("input[name=password]", "SUA_SENHA")
        # await page.click("button[type=submit]")
        # await page.wait_for_timeout(5000)

        print("Watcher online. Observando a cada", POLL_INTERVAL_S, "s.")
        while True:
            try:
                n = await scan_all_frames(page)
                if n in (1,2,3,4) and n != last_sent:
                    await post_number(n)
                    last_sent = n
                    print("✅ Enviado número:", n)
                await asyncio.sleep(POLL_INTERVAL_S)
            except Exception as e:
                print("Watcher erro:", e)
                await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
