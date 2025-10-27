# worker.py (Playwright NATIVO)

# Substitua os imports do Selenium/UC por este:
from playwright.sync_api import sync_playwright
import time
import logging

# ====================================================================
# CONFIGURAÇÃO
# ====================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
URL = "https://gamblingcounting.com/VAVADA" # URL do jogo
# SELETOR CSS: Ajuste o seletor para pegar a área do histórico
RESULT_SELECTOR = ".history-container .result-row" 

# ====================================================================
# FUNÇÕES DO BOT
# ====================================================================

def fetch_signals(page):
    """Navega até o site e extrai os últimos sinais usando Playwright."""
    try:
        logging.info(f"Navegando para: {URL}")
        # Usa o método nativo do Playwright para navegar
        page.goto(URL, wait_until="networkidle") 
        
        # Espera que o elemento de histórico apareça
        page.wait_for_selector(RESULT_SELECTOR, timeout=20000)
        
        # Extrai os elementos
        results = page.locator(RESULT_SELECTOR).all_text_contents()
        
        if not results:
            logging.warning("Não encontrou resultados. Verifique o seletor CSS.")
            return []

        # A extração de texto bruto é mais simples no Playwright
        signals = [text.strip() for text in results if text.strip()]
                
        logging.info(f"Total de sinais capturados: {len(signals)}. Últimos: {signals[:5]}")
        return signals

    except Exception as e:
        logging.error(f"Erro ao capturar sinais: {e}")
        return []

def filter_and_alert(signals):
    """Sua lógica de filtragem e alerta (esta lógica é a mesma)."""
    if not signals:
        return

    logging.info("Iniciando a lógica de filtragem de sinais...")
    
    recent_signals = signals[:5] 
    contagem_do_1 = recent_signals.count('1') 

    if contagem_do_1 >= 4:
        logging.warning(f"*** SINAL DETECTADO ***: '1' apareceu {contagem_do_1} vezes em 5 rodadas.")
        # EX: enviar_alerta_telegram("SINAL DE ENTRADA DETECTADO!")
    else:
        logging.info("Nenhum padrão de sinal detectado na última análise.")


# ====================================================================
# LÓGICA DE EXECUÇÃO PRINCIPAL DO BOT (Playwright)
# ====================================================================
if __name__ == "__main__":
    logging.info("Iniciando o bot usando Playwright...")
    try:
        # Inicia o Playwright, o navegador e uma página de uma vez
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, 
                args=['--no-sandbox', '--disable-gpu']
            )
            page = browser.new_page()

            # Loop principal: verifica, filtra e espera
            while True:
                signals = fetch_signals(page)
                filter_and_alert(signals)
                
                logging.info("Aguardando 60 segundos para a próxima verificação...")
                time.sleep(60)

    except Exception as e:
        logging.error(f"ERRO CRÍTICO NO LOOP PRINCIPAL DO PLAYWRIGHT: {e}")
