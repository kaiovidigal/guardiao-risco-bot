from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class ForceFireReq(BaseModel):
    pattern: str
    conf: float

@router.post("/force_fire")
async def force_fire(req: ForceFireReq):
    return {
        "ok": True,
        "forced_pattern": req.pattern,
        "forced_conf": req.conf
    }