# -*- coding: utf-8 -*-
# Guardião - utilitário de BACKUP
# Este app NÃO interfere no webhook. Apenas permite baixar/restaurar o banco.

import os
import shutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

app = FastAPI(title="Guardiao Backup Tool", version="1.0.0")

# Caminho padrão do banco no Guardião
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
BACKUP_KEY = os.getenv("BACKUP_KEY", "meusegredo123").strip()

@app.get("/")
async def root():
    return {"ok": True, "detail": "Use /download?key=SUA_KEY ou /restore?key=SUA_KEY"}

@app.get("/download")
async def download(key: str):
    if key != BACKUP_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not os.path.exists(DB_PATH):
        raise HTTPException(status_code=404, detail="Banco não encontrado")
    backup_file = "/tmp/backup_guardiao.db"
    shutil.copy2(DB_PATH, backup_file)
    return FileResponse(backup_file, filename="backup_guardiao.db")

@app.get("/restore")
async def restore(key: str):
    if key != BACKUP_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    upload_file = "/tmp/backup_guardiao.db"
    if not os.path.exists(upload_file):
        raise HTTPException(status_code=404, detail="Arquivo backup_guardiao.db não encontrado em /tmp")
    shutil.copy2(upload_file, DB_PATH)
    return {"ok": True, "restored_to": DB_PATH}