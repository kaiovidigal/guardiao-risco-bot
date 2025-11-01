# worker.py
from pyrogram import Client, filters
import sqlite3
import re
import os
from time import sleep

# ====================================================================
# CONFIGURAÇÃO DE SEGURANÇA (Usando Variáveis de Ambiente)
# ====================================================================
# Render usa variáveis de ambiente. Defina estas no seu dashboard do Render!
API_ID = int(os.environ.get("API_ID", 0)) 
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# IDs dos Canais (Defina no Render ou diretamente aqui)
# Os IDs devem ser definidos como inteiros, usando valores negativos para canais
CANAL_ORIGEM_ID = -1003156785631 
CANAL_DESTINO_ID = -1002796105884

# ====================================================================
# BANCO DE DADOS (DB)
# ====================================================================
DB_NAME = 'double_jonbet_data.db'

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

# ====================================================================
# MÓDULO DE ANÁLISE E DECISÃO (A 'IA' de Filtragem)
# ====================================================================

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
    
    # MODO APRENDIZADO (Libera tudo para coletar dados)
    if analisadas < 10: 
        return True, "APRENDIZADO"

    # MODO CONFIANÇA (Só envia se a confiança for alta)
    if confianca > 79.0:
        return True, "CONFIANÇA"
        
    # MODO BLOQUEIO
    return False, "BLOQUEIO"

def atualizar_performance(sinal, is_win):
    """ATENÇÃO: ESTA FUNÇÃO PRECISA SER CHAMADA COM O RESULTADO REAL."""
    # A implementação completa do bot exige saber o resultado da rodada.
    # Por enquanto, mantemos a função para futuras chamadas.
    
    novo_acerto = 1 if is_win else 0
    
    cursor.execute(f"""
    UPDATE sinais_performance SET 
        jogadas_analisadas = jogadas_analisadas + 1, 
        acertos_branco = acertos_branco + ?
    WHERE sinal_original = ?
    """, (novo_acerto, sinal))
    
    conn.commit()
    print(f"DB Atualizado: {sinal} - WIN: {is_win}")
    
# ====================================================================
# APLICAÇÃO TELEGRAM (Pyrogram)
# ====================================================================

# Inicialização do cliente Pyrogram
app = Client(
    "double_bot_ia",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@app.on_message(filters.chat(CANAL_ORIGEM_ID) & filters.text)
async def processar_sinal(client, message):
    sinal = message.text.strip()
    
    # Limpeza básica do sinal (você pode ajustar este regex)
    sinal_limpo = re.sub(r'#[0-9]+', '', sinal).strip()

    # Obtém performance e decide
    deve_enviar, modo = deve_enviar_sinal(sinal_limpo)
    analisadas, confianca = get_performance(sinal_limpo)
    
    if deve_enviar:
        # Formata a mensagem de saída
        sinal_convertido = (
            f"⚠️ **SINAL BRANCO DETECTADO ({modo})** ⚠️\n\n"
            f"🎯 JOGO: **Double JonBet**\n"
            f"🔥 FOCO TOTAL NO **BRANCO** 🔥\n\n"
            f"📊 Confiança: `{confianca:.2f}%` (Base: {analisadas} análises)"
        )
        
        # Envia para o canal de destino
        await client.send_message(CANAL_DESTINO_ID, sinal_convertido)
        
        print(f"Sinal ENVIADO: {sinal_limpo} - Modo: {modo}")
    else:
        print(f"Sinal IGNORADO: {sinal_limpo} - Confiança baixa: {confianca:.2f}%")

# ====================================================================
# EXECUÇÃO PRINCIPAL
# ====================================================================
if __name__ == "__main__":
    print("Iniciando Bot de Análise...")
    print(f"API ID: {API_ID}, Token Length: {len(BOT_TOKEN)}")
    
    # O Pyrogram rodará em modo bloqueante (executa o bot 24/7)
    # Coloque o bot em um loop seguro para evitar que o Render o encerre
    try:
        app.run()
    except Exception as e:
        print(f"Erro Crítico na Execução: {e}")
    finally:
        if conn:
            conn.close()
