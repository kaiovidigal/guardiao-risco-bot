# main.py
from fastapi import FastAPI
from . import api_fanta  # Importa seu módulo api_fanta.py

app = FastAPI()

@app.get("/")
async def get_fantan_result():
    """
    Endpoint que retorna o último resultado do Fan-Tan.
    """
    result = await api_fanta.get_latest_result()
    if result:
        return {"numero": result[0], "timestamp": result[1]}
    return {"erro": "Nenhum resultado válido encontrado"}

