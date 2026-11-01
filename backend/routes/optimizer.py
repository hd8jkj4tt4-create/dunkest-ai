from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from backend.database import connect
from backend.services.team_optimizer import optimize, DEFAULT_RULES

router = APIRouter(prefix="/api/optimizer", tags=["optimizer"])

class GenerateRequest(BaseModel):
    budget: float = Field(default=95, gt=0, le=200)
    include_coach: bool = True

@router.post("/generate")
def generate(request: GenerateRequest):
    with connect() as con:
        players = [dict(r) for r in con.execute("SELECT * FROM players").fetchall()]
        coaches = [dict(r) for r in con.execute("SELECT * FROM coaches").fetchall()]
    if not players: raise HTTPException(400, "Database vuoto: sincronizza i dati o importa un CSV.")
    try:
        rules = {**DEFAULT_RULES, "budget": request.budget, "include_coach": request.include_coach}
        # Without a synced coach listone the optimizer simply stays players-only.
        result = optimize(players, coaches=coaches if request.include_coach else None, rules=rules)
        result["budget"] = request.budget
        result["include_coach"] = bool(request.include_coach and result["coach"])
        return result
    except Exception as e:
        raise HTTPException(422, str(e))
