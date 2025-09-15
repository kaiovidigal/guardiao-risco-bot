from fastapi import FastAPI
from api_fanta import get_latest_result

app = FastAPI(title="Guardião Risco Bot")

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/")
async def root():
    result = await get_latest_result()
    if result:
        numero, ts_epoch = result
        return {"numero": numero, "timestamp": ts_epoch}
    return {"erro": "Nenhum resultado válido encontrado"}
