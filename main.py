# main.py
from fastapi import FastAPI
from . import api_fanta

app = FastAPI()

@app.get("/")
async def get_fantan_result():
    result = await api_fanta.get_latest_result()
    if result:
        return {"numero": result[0], "timestamp": result[1]}
    return {"erro": "Nenhum resultado válido encontrado"}

