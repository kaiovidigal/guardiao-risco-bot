# worker.py - Versão FINAL COM APRENDIZADO e Persistência de Disco
import os
import sqlite3
import re
from pyrogram import Client, filters
import logging
import time

# ====================================================================
# CONFIGURAÇÃO GERAL E LOGGING
# ====================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# VARIÁVEIS DE AMBIENTE (Busca no Render)
API_ID = int(os.environ.get("API_ID", 0)) 
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# IDs dos Canais (IDs de grupos/canais são negativos)
# ATENÇÃO: SUBSTITUA OS PLACEHOLDERS PELOS SEUS IDs REAIS
CANAL_ORIGEM_IDS = [-1003156785631, -1009998887776] # Adicione todos os IDs de origem
CANAL_DESTINO_ID = -1002796105884 # Canal onde o sinal filtrado será enviado
CANAL_FEEDBACK_ID = -1009990001112 # Canal/Grupo onde o resultado (WIN/LOSS) será postado.

# ====================================================================
# CONFIGURAÇÃO DE APRENDIZADO E FILTRAGEM
# ====================================================================
MIN_JOGADAS_APRENDIZADO = 10
PERCENTUAL_MINIMO_CONFIANCA = 79.0 

# Variável de estado para armazenar o ÚLTIMO SINAL ENVIADO
LAST_SENT_SIGNAL = {"text": None, "timestamp": 0} 

# ====================================================================
# BANCO DE DADOS (SQLite) E PERSISTÊNCIA DE DISCO NO RENDER
# ====================================================================

# Caminho para o Disco Persistente no Render.
# EXIGE que o disco seja configurado no Render com o Mount Path: /var/data
DB_MOUNT_PATH = os.environ.get("DB_MOUNT_PATH", "/var/data") 
DB_NAME = os.path.join(DB_MOUNT_PATH, 'double_jonbet_data.db') 

def setup_db():
    """Conecta e garante que a tabela de performance exista."""
    # Garante que o diretório exista antes de criar o arquivo DB
    os.makedirs(DB_MOUNT_PATH, exist_ok=True)
    
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
    logging.info(f"DB configurado em: {DB_NAME}")
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
    
    # 1. MODO APRENDIZADO: Envia sempre se não tiver amostras suficientes
    if analisadas < MIN_JOGADAS_APRENDIZADO: 
        return True, "APRENDIZADO"

    # 2. MODO CONFIANÇA: Envia somente se a confiança for atingida
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
    sinal_limpo = re.sub(r'#[0-9]+', '', sinal).strip() # Limpa o sinal
    
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
        
        # Registra o último sinal enviado para o sistema de feedback
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
    
    # Verifica se há um sinal pendente para atualização
    if LAST_SENT_SIGNAL["text"] is None:
        logging.warning("Feedback recebido, mas nenhum sinal recente foi enviado.")
        return

    # Lógica para detectar WIN ou LOSS (o que for 'WIN'/'GREEN' é contado como acerto)
    is_win = "WIN" in feedback_text or "GREEN" in feedback_text
    is_loss = "LOSS" in feedback_text or "RED" in feedback_text or "NO WIN" in feedback_text
    
    if is_win or is_loss:
        sinal_para_atualizar = LAST_SENT_SIGNAL["text"]
        
        # 1. Atualiza o DB
        atualizar_performance(sinal_para_atualizar, is_win)
        
        # 2. Envia a confirmação para o canal de destino
        if is_win:
            resultado_msg = f"✅ **WIN BRANCO!** Feedback: `{feedback_text}`"
        else:
            resultado_msg = f"❌ **LOSS BRANCO.** Feedback: `{feedback_text}`"
            
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
    logging.info("Iniciando Bot de Análise (Modo COM APRENDIZADO)...")
    
    if not API_ID or not API_HASH or not BOT_TOKEN:
        logging.critical("ERRO: Configure as Variáveis de Ambiente (API_ID, API_HASH, BOT_TOKEN).")
        exit(1)
        
    try:
        app.run()
    except Exception as e:
        logging.critical(f"ERRO CRÍTICO NA EXECUÇÃO PRINCIPAL: {e}")
    finally:
        # Garante que a conexão com o DB seja fechada antes de o processo terminar
        if conn:
            conn.close()
            logging.info("Conexão com o Banco de Dados fechada.")
