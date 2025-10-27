# worker.py
# Código Python FINAL para monitoramento de sinais do Crazy Time usando Playwright NATIVO.

from playwright.sync_api import sync_playwright
import time
import logging

# ====================================================================
# CONFIGURAÇÃO
# ====================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
URL = "https://gamblingcounting.com/VAVADA" 

# SELETOR CSS: AJUSTE ESTE SELETOR para a área do histórico
RESULT_SELECTOR = ".history-container" 

# ====================================================================
# FUNÇÕES DO BOT
# ====================================================================

def fetch_signals(page):
    """Navega até o site e extrai os últimos sinais usando Playwright."""
    try:
        logging.info(f"Navegando para: {URL}")
        
        # Navega para a URL e espera até que a rede esteja inativa (carregamento completo)
        page.goto(URL, wait_until="networkidle") 
        
        # Espera que o elemento de histórico apareça (Timeout de 20 segundos)
        page.wait_for_selector(RESULT_SELECTOR, timeout=20000)
        
        # Extrai todo o texto da área do histórico
        history_text = page.locator(RESULT_SELECTOR).inner_text()
        
        # Lógica de extração de sinais: Transforma o texto em uma lista de strings
        signals_raw = history_text.split() 
        signals = [s for s in signals_raw if s]
        
        if not signals:
            logging.warning("Não conseguiu extrair sinais válidos. Verifique o seletor ou a lógica de extração.")
            return []

        logging.info(f"Total de sinais capturados: {len(signals)}. Últimos: {signals[:5]}")
        return signals

    except Exception as e:
        logging.error(f"Erro ao capturar sinais: {e}")
        return []

def filter_and_alert(signals):
    """Sua lógica de filtragem e alerta (a lógica é a mesma)."""
    if not signals:
        return

    logging.info("Iniciando a lógica de filtragem de sinais...")
    
    # --- EXEMPLO DE LÓGICA DE FILTRAGEM ---
    recent_signals = signals[:5] 
    contagem_do_1 = recent_signals.count('1') 

    if contagem_do_1 >= 4:
        logging.warning(f"*** SINAL DETECTADO ***: '1' apareceu {contagem_do_1} vezes em 5 rodadas.")
    else:
        logging.info("Nenhum padrão de sinal detectado na última análise.")
    # --------------------------------------


# ====================================================================
# LÓGICA DE EXECUÇÃO PRINCIPAL DO BOT (Playwright)
# ====================================================================
if __name__ == "__main__":
    logging.info("Iniciando o bot usando Playwright NATIVO...")
    try:
        # Inicia o Playwright, o navegador e uma página de uma vez
        with sync_playwright() as p:
            # Lança o Chromium no modo headless (necessário para o Render)
            browser = p.chromium.launch(
                headless=True, 
                args=['--no-sandbox', '--disable-gpu'] # Args de ambiente Docker
            )
            page = browser.new_page()

            # Loop principal: verifica, filtra e espera
            while True:
                signals = fetch_signals(page)
                filter_and_alert(signals)
                
                logging.info("Aguardando 60 segundos para a próxima verificação...")
                time.sleep(60)
            
            browser.close() # Fecha o navegador se o loop for interrompido
            
    except Exception as e:
        logging.error(f"ERRO CRÍTICO NO LOOP PRINCIPAL DO PLAYWRIGHT: {e}")
