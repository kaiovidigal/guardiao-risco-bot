# worker.py - Versão SOMENTE TELEGRAM (Removido Playwright/Scraping)
import os
import sqlite3
import re
from pyrogram import Client, filters
import logging
from time import sleep

# ====================================================================
# CONFIGURAÇÃO GERAL E LOGGING
# ====================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# VARIÁVEIS DE AMBIENTE (Busca no Render)
API_ID = int(os.environ.get("API_ID", 0)) 
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# IDs dos Canais (IDs de grupos/canais são negativos)
CANAL_ORIGEM_IDS = [-1003156785631, -1009998887776] # Adicione todos os IDs de origem
CANAL_DESTINO_ID = -1002796105884 # Canal onde o sinal filtrado será enviado
CANAL_FEEDBACK_ID = -1009990001112 # << NOVO: Canal/Grupo onde o resultado será postado.

# ====================================================================
# CONFIGURAÇÃO DE APRENDIZADO E FILTRAGEM
# ====================================================================
DB_NAME = 'double_jonbet_data.db'
MIN_JOGADAS_APRENDIZADO = 10
PERCENTUAL_MINIMO_CONFIANCA = 79.0 

# Variável de estado para armazenar o ÚLTIMO SINAL ENVIADO
# Usado para ligar o feedback (WIN/LOSS) ao sinal correspondente
LAST_SENT_SIGNAL = {"text": None, "timestamp": 0} 

# ====================================================================
# BANCO DE DADOS (SQLite) - Funções Inalteradas
# ====================================================================

def setup_db():
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
    cursor.execute("SELECT jogadas_analisadas, acertos_branco FROM sinais_performance WHERE sinal_original = ?", (sinal,))
    data = cursor.fetchone()
    if data:
        analisadas, acertos = data
        if analisadas > 0:
            confianca = (acertos / analisadas) * 100
            return analisadas, confianca
        return analisadas, 0.0
    cursor.execute("INSERT OR IGNORE INTO sinais_performance (sinal_original) VALUES (?)", (sinal,))
    conn.commit()
    return 0, 0.0

def deve_enviar_sinal(sinal):
    analisadas, confianca = get_performance(sinal)
    
    if analisadas < MIN_JOGADAS_APRENDIZADO: 
        return True, "APRENDIZADO"

    if confianca > PERCENTUAL_MINIMO_CONFIANCA:
        return True, "CONFIANÇA"
        
    return False, "BLOQUEIO"

def atualizar_performance(sinal, is_win):
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
# APLICAÇÃO TELEGRAM (Pyrogram)
# ====================================================================

app = Client(
    "double_bot_ia",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ----------------------------------------
# 1. MANIPULADOR DE SINAIS (ORIGEM)
# ----------------------------------------
@app.on_message(filters.chat(CANAL_ORIGEM_IDS) & filters.text)
async def processar_sinal(client, message):
    global LAST_SENT_SIGNAL
    sinal = message.text.strip()
    sinal_limpo = re.sub(r'#[0-9]+', '', sinal).strip()

    deve_enviar, modo = deve_enviar_sinal(sinal_limpo)
    analisadas, confianca = get_performance(sinal_limpo)
    
    logging.info(f"Sinal Recebido: '{sinal_limpo}'. Decisão: {modo} ({confianca:.2f}% de {analisadas}).")
    
    if deve_enviar:
        # Formata a mensagem para o BRANCO
        sinal_convertido = (
            f"⚠️ **SINAL EXCLUSIVO BRANCO ({modo})** ⚠️\n\n"
            f"🎯 JOGO: **Double JonBet**\n"
            f"🔥 FOCO TOTAL NO **BRANCO** 🔥\n\n"
            f"📊 Confiança: `{confianca:.2f}%` (Base: {analisadas} análises)\n"
            f"🔔 Sinal Original: {sinal_limpo}"
        )
        
        # Envia para o canal de destino
        await client.send_message(CANAL_DESTINO_ID, sinal_convertido)
        
        # Registra o último sinal enviado para que o feedback saiba qual atualizar
        LAST_SENT_SIGNAL["text"] = sinal_limpo
        LAST_SENT_SIGNAL["timestamp"] = time.time()
        logging.warning(f"Sinal ENVIADO: '{sinal_limpo}'. Esperando feedback em {CANAL_FEEDBACK_ID}.")
        
    else:
        logging.info(f"Sinal IGNORADO: '{sinal_limpo}'. Confiança: {confianca:.2f}%")


# ----------------------------------------
# 2. MANIPULADOR DE FEEDBACK (APRENDIZADO)
# ----------------------------------------
@app.on_message(filters.chat(CANAL_FEEDBACK_ID) & filters.text)
async def processar_feedback(client, message):
    global LAST_SENT_SIGNAL
    feedback_text = message.text.strip().upper()
    
    if LAST_SENT_SIGNAL["text"] is None:
        logging.warning("Feedback recebido, mas nenhum sinal recente foi enviado.")
        return

    # Lógica para detectar WIN ou LOSS (Adapte conforme o texto que você usará)
    is_win = "WIN" in feedback_text or "GREEN" in feedback_text
    is_loss = "LOSS" in feedback_text or "RED" in feedback_text
    
    if is_win or is_loss:
        # Encontramos o resultado para o último sinal enviado
        sinal_para_atualizar = LAST_SENT_SIGNAL["text"]
        
        # 1. Atualiza o DB
        atualizar_performance(sinal_para_atualizar, is_win)
        
        # 2. Envia a confirmação para o canal de destino
        resultado_msg = f"✅ **{feedback_text} NO BRANCO!**\nSinal: `{sinal_para_atualizar}`"
        await client.send_message(CANAL_DESTINO_ID, resultado_msg)
        
        # 3. Limpa o estado
        LAST_SENT_SIGNAL["text"] = None 
        logging.info("Estado de feedback limpo. Pronto para o próximo sinal.")
    else:
        logging.info("Feedback recebido, mas não é um WIN/LOSS reconhecido. Ignorando.")


# ====================================================================
# EXECUÇÃO PRINCIPAL
# ====================================================================
if __name__ == "__main__":
    logging.info("Iniciando Bot de Análise (Modo SOMENTE TELEGRAM)...")
    
    # Verifica chaves críticas
    if not API_ID or not API_HASH or not BOT_TOKEN:
        logging.critical("ERRO: Configure as Variáveis de Ambiente (API_ID, API_HASH, BOT_TOKEN).")
        exit(1)
        
    try:
        app.run()
    except Exception as e:
        logging.critical(f"ERRO CRÍTICO NA EXECUÇÃO PRINCIPAL: {e}")
    finally:
        if conn:
            conn.close()
            logging.info("Conexão com o Banco de Dados fechada.")

