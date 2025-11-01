# worker.py
import os
import sqlite3
import re
import asyncio
from pyrogram import Client, filters
from playwright.async_api import async_playwright
import time
import logging

# ====================================================================
# CONFIGURAÇÃO GERAL E LOGGING
# ====================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# VARIÁVEIS DE AMBIENTE (Busca no Render)
API_ID = int(os.environ.get("API_ID", 0)) 
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# URLs e IDs dos Canais (IDs de grupos/canais são negativos)
# ADICIONE A URL DO SEU JOGO DOUBLE AQUI!
JOGO_URL = os.environ.get("JOGO_URL", "URL_DO_JOGO_DOUBLE_JONBET_AQUI")

# LISTA DE IDs dos Canais de ORIGEM (Onde os sinais chegam)
CANAL_ORIGEM_IDS = [-1003156785631, -1009998887776] # Adicione todos os IDs dos canais de sinal aqui

# ID do Canal de DESTINO (Onde o sinal filtrado será enviado)
CANAL_DESTINO_ID = -1002796105884

# ====================================================================
# CONFIGURAÇÃO DE APRENDIZADO E FILTRAGEM
# ====================================================================
DB_NAME = 'double_jonbet_data.db'
MIN_JOGADAS_APRENDIZADO = 10  # Mínimo de amostras para sair do modo APRENDIZADO
PERCENTUAL_MINIMO_CONFIANCA = 79.0 # Percentual mínimo para ENVIAR o sinal (após aprendizado)
TEMPO_ESPERA_RESULTADO = 40 # Segundos que o bot espera para buscar o resultado após o sinal

# SELETOR CSS: Ajuste este para o seletor do último resultado do DOUBLE (ex: o número ou cor)
RESULT_SELECTOR = ".last-roll-result" 

# ====================================================================
# BANCO DE DADOS (SQLite)
# ====================================================================

def setup_db():
    """Conecta e garante que a tabela de performance exista."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS sinais_performance (
        sinal_original TEXT PRIMARY KEY,
        jogadas_analisadas INTEGER DEFAULT 0,
        acertos_branco INTEGER DEFAULT 0
    )
    ''')
    conn.commit()
    return conn, cursor

conn, cursor = setup_db()

def get_performance(sinal):
    """Retorna a performance e a confiança de um sinal."""
    cursor.execute("SELECT jogadas_analisadas, acertos_branco FROM sinais_performance WHERE sinal_original = ?", (sinal,))
    data = cursor.fetchone()
    if data:
        analisadas, acertos = data
        if analisadas > 0:
            confianca = (acertos / analisadas) * 100
            return analisadas, confianca
        return analisadas, 0.0
    # Insere o sinal se for a primeira vez que é visto
    cursor.execute("INSERT OR IGNORE INTO sinais_performance (sinal_original) VALUES (?)", (sinal,))
    conn.commit()
    return 0, 0.0

def deve_enviar_sinal(sinal):
    """Lógica da 'IA' para decidir o envio (Aprendizado e Confiança > 79%)."""
    analisadas, confianca = get_performance(sinal)
    
    # 1. MODO APRENDIZADO
    if analisadas < MIN_JOGADAS_APRENDIZADO: 
        return True, "APRENDIZADO"

    # 2. MODO CONFIANÇA
    if confianca > PERCENTUAL_MINIMO_CONFIANCA:
        return True, "CONFIANÇA"
        
    # 3. MODO BLOQUEIO
    return False, "BLOQUEIO"

def atualizar_performance(sinal, is_win):
    """Atualiza o DB com o resultado da rodada."""
    novo_acerto = 1 if is_win else 0
    
    cursor.execute(f"""
    UPDATE sinais_performance SET 
        jogadas_analisadas = jogadas_analisadas + 1, 
        acertos_branco = acertos_branco + ?
    WHERE sinal_original = ?
    """, (novo_acerto, sinal))
    
    conn.commit()
    logging.info(f"DB Atualizado: {sinal} - WIN BRANCO: {is_win}")

# ====================================================================
# MÓDULO DE SCRAPING DE RESULTADOS (Playwright Assíncrono)
# ====================================================================

async def fetch_real_result():
    """Busca o resultado mais recente (Branco/Não Branco) via Playwright."""
    logging.info(f"Iniciando busca de resultado em: {JOGO_URL}")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                # Argumentos cruciais para rodar no Render (Docker)
                args=['--no-sandbox', '--disable-gpu'] 
            )
            page = await browser.new_page()
            
            await page.goto(JOGO_URL, wait_until="networkidle") 
            await page.wait_for_selector(RESULT_SELECTOR, timeout=20000)
            
            result_element = await page.locator(RESULT_SELECTOR).first
            result_text = await result_element.inner_text()
            
            await browser.close()

            # LÓGICA DE DETECÇÃO DO BRANCO: ADAPTE PARA O TEXTO/VALOR REAL DO SEU JOGO
            is_branco = "BRANCO" in result_text.upper() or "WHITE" in result_text.upper() 
            
            logging.info(f"Resultado do Double capturado: '{result_text}'. É Branco? {is_branco}")
            return is_branco, result_text
            
    except Exception as e:
        logging.error(f"Erro Crítico no Playwright ao buscar resultado: {e}")
        return False, "ERRO_DE_SCRAPING"

# ====================================================================
# APLICAÇÃO TELEGRAM (Pyrogram)
# ====================================================================

app = Client(
    "double_bot_ia",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@app.on_message(filters.chat(CANAL_ORIGEM_IDS) & filters.text)
async def processar_sinal(client, message):
    sinal = message.text.strip()
    
    # Limpeza básica do sinal (você pode ajustar este regex para remover números de rodada, etc.)
    sinal_limpo = re.sub(r'#[0-9]+', '', sinal).strip()

    # Obtém performance e decide
    deve_enviar, modo = deve_enviar_sinal(sinal_limpo)
    analisadas, confianca = get_performance(sinal_limpo)
    
    logging.info(f"Sinal Recebido: '{sinal_limpo}'. Decisão: {modo} ({confianca:.2f}% de {analisadas}).")
    
    if deve_enviar:
        # Formata a mensagem de saída
        sinal_convertido = (
            f"⚠️ **SINAL BRANCO DETECTADO ({modo})** ⚠️\n\n"
            f"🎯 JOGO: **Double JonBet**\n"
            f"🔥 FOCO TOTAL NO **BRANCO** 🔥\n\n"
            f"📊 Confiança: `{confianca:.2f}%` (Base: {analisadas} análises)\n"
            f"🔔 Sinal Original: {sinal_limpo}"
        )
        
        # Envia para o canal de destino
        await client.send_message(CANAL_DESTINO_ID, sinal_convertido)
        
        # --- ROTINA DE APRENDIZADO APÓS O ENVIO DO SINAL ---
        
        # 1. Espera o tempo da rodada
        logging.info(f"Aguardando {TEMPO_ESPERA_RESULTADO} segundos pelo resultado da rodada...")
        await asyncio.sleep(TEMPO_ESPERA_RESULTADO)

        # 2. Busca do Resultado (Web Scraping)
        is_win, resultado_lido = await fetch_real_result()
        
        # 3. Atualização do Aprendizado (a 'IA')
        atualizar_performance(sinal_limpo, is_win)
        
        # 4. Envia o resultado do acompanhamento para o canal de destino
        if is_win:
            resultado_msg = f"✅ **WIN BRANCO!** Resultado lido: `{resultado_lido}`"
        else:
            resultado_msg = f"❌ **LOSS.** Resultado lido: `{resultado_lido}`"
            
        await client.send_message(CANAL_DESTINO_ID, resultado_msg)
    
# ====================================================================
# EXECUÇÃO PRINCIPAL
# ====================================================================
if __name__ == "__main__":
    logging.info("Iniciando Bot de Análise...")
    
    # Verifica se as chaves críticas estão presentes
    if not API_ID or not API_HASH or not BOT_TOKEN or "URL_DO_JOGO" in JOGO_URL:
        logging.critical("ERRO: Configure as Variáveis de Ambiente (API_ID, API_HASH, BOT_TOKEN) e a JOGO_URL no Render ou no código.")
        # Se estiver no Render, ele pode falhar aqui, o que é o correto.
        exit(1)
        
    try:
        app.run()
    except Exception as e:
        logging.critical(f"ERRO CRÍTICO NA EXECUÇÃO PRINCIPAL: {e}")
    finally:
        if conn:
            conn.close()
            logging.info("Conexão com o Banco de Dados fechada.")
