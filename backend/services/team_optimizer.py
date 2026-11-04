"""
Roster optimizer: points-first, budget-efficient integer optimization.
Projection is preferred. When unavailable, the available fantasy-points average
and recent form are used as a transparent proxy (not a true forecast).
"""
from ortools.linear_solver import pywraplp

DEFAULT_RULES = {
    "budget": 95.0,
    "roster_size": 10,
    "roles": {"C": 2, "F": 4, "G": 4},
    "max_per_team": 2,
    "time_limit_ms": 20000,
}

# Players use 3-letter codes (BOS) while coaches carry the full name
# (Boston Celtics): both must map to one key so the optimizer recognises the
# franchise. The per-team limit applies to the 10 players only; the coach is
# excluded (2 players + 1 coach from the same team is allowed).
TEAM_ALIASES = {
    "ATLANTA HAWKS": "ATL", "BOSTON CELTICS": "BOS", "BROOKLYN NETS": "BKN",
    "CHARLOTTE HORNETS": "CHA", "CHICAGO BULLS": "CHI", "CLEVELAND CAVALIERS": "CLE",
    "DALLAS MAVERICKS": "DAL", "DENVER NUGGETS": "DEN", "DETROIT PISTONS": "DET",
    "GOLDEN STATE WARRIORS": "GSW", "HOUSTON ROCKETS": "HOU", "INDIANA PACERS": "IND",
    "LOS ANGELES CLIPPERS": "LAC", "LOS ANGELES LAKERS": "LAL", "MEMPHIS GRIZZLIES": "MEM",
    "MIAMI HEAT": "MIA", "MILWAUKEE BUCKS": "MIL", "MINNESOTA TIMBERWOLVES": "MIN",
    "NEW ORLEANS PELICANS": "NOP", "NEW YORK KNICKS": "NYK", "OKLAHOMA CITY THUNDER": "OKC",
    "ORLANDO MAGIC": "ORL", "PHILADELPHIA 76ERS": "PHI", "PHOENIX SUNS": "PHX",
    "PORTLAND TRAIL BLAZERS": "POR", "SACRAMENTO KINGS": "SAC", "SAN ANTONIO SPURS": "SAS",
    "TORONTO RAPTORS": "TOR", "UTAH JAZZ": "UTA", "WASHINGTON WIZARDS": "WAS",
    # Alternate abbreviations used by other sources.
    "BRK": "BKN", "CHO": "CHA", "PHO": "PHX", "NO": "NOP", "GS": "GSW",
}

def _team_key(value):
    s = str(value or "").strip().upper()
    return TEAM_ALIASES.get(s, s)

def _number(value):
    try:
        n = float(value or 0)
        return n if n == n and abs(n) != float("inf") else 0.0
    except (TypeError, ValueError):
        return 0.0

def _score(p):
    projection = _number(p.get("projection"))
    avg = _number(p.get("avg_points"))
    last5 = _number(p.get("last5"))
    if projection > 0:
        return projection, "projection"
    # Do not silently discard a player simply because a forecast is absent.
    if avg > 0 and last5 > 0:
        return 0.65 * avg + 0.35 * last5, "avg+last5 proxy"
    if last5 > 0:
        return last5, "last5 proxy"
    if avg > 0:
        return avg, "average proxy"
    return 0.0, "no scoring data"

# Statuses that make a player unavailable: injured/out players never enter the
# team picking phase. Matching is case-insensitive and substring-based so
# values like "Out (injury)", "injured", "ruled out" are all caught.
UNAVAILABLE_TOKENS = (
    "injur", "out", "ruled out", "inactive", "suspend",
    "not available", "did not play", "dnp", "ir",
)

def _unavailable(status) -> bool:
    s = str(status or "unknown").strip().lower()
    if not s or s in {"unknown", "active", "available", "probable", "questionable", "day-to-day", "gtc"}:
        return False
    return any(t in s for t in UNAVAILABLE_TOKENS)

def _eligible_coaches(coaches):
    eligible = []
    for c in coaches or []:
        price = _number(c.get("price"))
        if price <= 0 or not str(c.get("id", "")).strip():
            continue
        row = dict(c)
        row["_price"] = price
        row["_score"] = _number(c.get("projection"))
        eligible.append(row)
    ids = [str(c["id"]) for c in eligible]
    if len(set(ids)) != len(ids):
        raise ValueError("ID allenatori duplicati: sincronizza di nuovo gli allenatori.")
    return eligible


def optimize(players, coaches=None, rules=None):
    rules = {**DEFAULT_RULES, **(rules or {})}
    roles = rules["roles"]
    eligible = []
    for p in players:
        # Injured/out players never enter the team picking phase.
        if p.get("injured") in (1, True, "1") or _unavailable(p.get("status")):
            continue
        price = _number(p.get("price"))
        role = str(p.get("role", "")).strip().upper()
        if price <= 0 or role not in roles:
            continue
        row = dict(p)
        row["_price"] = price
        row["_score"], row["_score_basis"] = _score(p)
        eligible.append(row)

    # Player IDs must be unique for the binary decision variables.
    ids = [str(p.get("id", "")) for p in eligible]
    if not all(ids) or len(set(ids)) != len(ids):
        raise ValueError("ID giocatori mancanti o duplicati: correggi il CSV/database.")
    if len(eligible) < int(rules["roster_size"]):
        raise ValueError("Giocatori eleggibili insufficienti per completare il roster.")

    # The coach shares the same credit budget with the 10 players (Dunkest rule).
    coach_pool = _eligible_coaches(coaches) if rules.get("include_coach", True) else []

    solver = pywraplp.Solver.CreateSolver("SCIP")
    if not solver:
        raise RuntimeError("Solver OR-Tools non disponibile.")
    solver.SetTimeLimit(int(rules.get("time_limit_ms", 20000)))
    x = {str(p["id"]): solver.BoolVar(f"x_{i}") for i, p in enumerate(eligible)}
    solver.Add(solver.Sum(x[str(p["id"])] for p in eligible) == int(rules["roster_size"]))
    for role, count in roles.items():
        solver.Add(solver.Sum(x[str(p["id"])] for p in eligible if str(p["role"]).upper() == role) == int(count))
    y = {str(c["id"]): solver.BoolVar(f"y_{i}") for i, c in enumerate(coach_pool)}
    if coach_pool:
        solver.Add(solver.Sum(y[str(c["id"])] for c in coach_pool) == 1)

    # Max players per NBA franchise. The coach does NOT count towards this
    # limit: 2 players + the coach of the same team is a valid combination.
    player_team = {str(p["id"]): _team_key(p.get("team")) for p in eligible}
    for team in set(player_team.values()) - {""}:
        members = [x[pid] for pid, t in player_team.items() if t == team]
        if members:
            solver.Add(solver.Sum(members) <= int(rules["max_per_team"]))

    player_cost = solver.Sum(x[str(p["id"])] * p["_price"] for p in eligible)
    coach_cost = solver.Sum(y[str(c["id"])] * c["_price"] for c in coach_pool)
    solver.Add(player_cost + coach_cost <= float(rules["budget"]))

    score_expr = solver.Sum(x[str(p["id"])] * p["_score"] for p in eligible) + \
        solver.Sum(y[str(c["id"])] * c["_score"] for c in coach_pool)
    cost_expr = player_cost + coach_cost

    # Phase 1: maximize fantasy output. Phase 2: among equally scoring
    # solutions, spend as much of the available budget as possible.
    solver.Maximize(score_expr)
    status = solver.Solve()
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        raise ValueError("Nessuna squadra valida: controlla dati, ruoli, budget e vincoli.")
    best_score = score_expr.solution_value()
    first_status = status
    solver.Add(score_expr >= best_score - 1e-7)
    solver.Maximize(cost_expr)
    second_status = solver.Solve()
    if second_status in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        status = second_status
    else:
        status = first_status

    chosen = [p for p in eligible if x[str(p["id"])].solution_value() > .5]
    chosen_coach = next((c for c in coach_pool if y[str(c["id"])].solution_value() > .5), None)
    player_cost_total = sum(p["_price"] for p in chosen)
    coach_cost_total = chosen_coach["_price"] if chosen_coach else 0.0
    total_cost = player_cost_total + coach_cost_total
    player_score = sum(p["_score"] for p in chosen)
    coach_score = chosen_coach["_score"] if chosen_coach else 0.0
    bases = sorted({p["_score_basis"] for p in chosen})
    return {
        "players": [{k: v for k, v in p.items() if not k.startswith("_")} for p in chosen],
        "coach": {k: v for k, v in chosen_coach.items() if not k.startswith("_")} if chosen_coach else None,
        "player_cost": round(player_cost_total, 2),
        "coach_cost": round(coach_cost_total, 2),
        "cost": round(total_cost, 2),
        "remaining": round(float(rules["budget"]) - total_cost, 2),
        "budget_used_pct": round(100 * total_cost / float(rules["budget"]), 1),
        "projected_points": round(player_score + coach_score, 2),
        "player_points": round(player_score, 2),
        "coach_points": round(coach_score, 2),
        "score_basis": bases,
        "coach_basis": "Punti attesi allenatore: media live Dunkest (60%) + modello regolamentare (40%), con coefficiente avversario calcolato sulla quotazione reale dell'allenatore avversario." if chosen_coach else None,
        "status": "OPTIMAL" if status == pywraplp.Solver.OPTIMAL else "FEASIBLE",
        "players_considered": len(eligible),
        "coaches_considered": len(coach_pool),
        "optimization": "Punteggio massimo (giocatori + allenatore); a parità di punteggio, budget utilizzato massimo.",
        "warning": "La soluzione è ottima rispetto ai dati disponibili e ai vincoli impostati. Se mancano proiezioni, usa medie/forma come proxy: non sono previsioni garantite."
    }
