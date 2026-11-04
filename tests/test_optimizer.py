from backend.services.team_optimizer import optimize

def test_optimizer_respects_budget_roles_and_maximizes_spend_on_tie():
    ps=[]
    for i in range(2): ps.append(dict(id=f"c{i}",name=f"C{i}",team=f"T{i}",role="C",price=5,projection=10,status="active"))
    for i in range(4): ps.append(dict(id=f"f{i}",name=f"F{i}",team=f"U{i}",role="F",price=5,projection=10,status="active"))
    for i in range(4): ps.append(dict(id=f"g{i}",name=f"G{i}",team=f"V{i}",role="G",price=5,projection=10,status="active"))
    # Add an equal-score expensive alternative; tie-break should use more credits.
    ps[0]["price"] = 8
    out=optimize(ps, rules={"budget":95,"roster_size":10,"roles":{"C":2,"F":4,"G":4},"max_per_team":3})
    assert len(out["players"]) == 10
    assert out["cost"] <= 95
    assert out["projected_points"] == 100
    assert out["cost"] == 53

def test_injured_players_are_excluded_from_team_picking():
    ps=[]
    for role,count in {"C":2,"F":4,"G":4}.items():
        for i in range(count):
            ps.append(dict(id=f"{role}{i}",name=f"{role}{i}",team=f"T{role}{i}",role=role,price=5,projection=8,status="active"))
    # Injured/out players with huge scores must never be selected.
    ps.append(dict(id="inj1",name="Injured Star",team="TX",role="G",price=30,projection=99,status="Out (injury)"))
    ps.append(dict(id="inj2",name="Injured Star 2",team="TY",role="F",price=30,projection=99,status="INJURED"))
    ps.append(dict(id="inj3",name="Suspended",team="TZ",role="C",price=30,projection=99,status="suspended"))
    out=optimize(ps, rules={"budget":95})
    ids={p["id"] for p in out["players"]}
    assert not {"inj1","inj2","inj3"} & ids
    assert out["players_considered"] == 10


def test_uses_average_when_projection_missing():
    ps=[]
    for role,count in {"C":2,"F":4,"G":4}.items():
        for i in range(count):
            ps.append(dict(id=f"{role}{i}",name=f"{role}{i}",team=f"T{role}{i}",role=role,price=5,projection=0,avg_points=8,last5=10,status="active"))
    out=optimize(ps)
    assert out["projected_points"] == 87
    assert "avg+last5 proxy" in out["score_basis"]


def test_dunkest_live_row_normalization_supports_public_table_fields():
    from backend.services.data_collector import normalize
    row = normalize({
        "id": 123, "first_name": "Test", "last_name": "Player",
        "team_code": "BOS", "position_id": 1, "cr": "8,5",
        "pdk_avg": "22.4", "gp": 3,
    })
    assert row["name"] == "Test Player"
    assert row["team"] == "BOS"
    assert row["role"] == "G"
    assert row["price"] == 8.5
    assert row["avg_points"] == 22.4


def test_pdk_from_avg_table_is_not_divided_by_games_played():
    from backend.services.data_collector import normalize
    # stats_type=avg returns pdk as a per-game average; dividing it by gp
    # would silently shrink every player's score.
    row = normalize({"id": 1, "first_name": "Shai", "last_name": "G",
                     "cr": "21.6", "pdk": "30.5", "gp": "4", "position": "G"})
    assert row["avg_points"] == 30.5


def test_cumulative_pdk_total_is_still_converted_to_average():
    from backend.services.data_collector import normalize
    row = normalize({"id": 2, "name": "Cum Player", "cr": 10, "position": "F",
                     "pdk_tot": "120", "gp": 4})
    assert row["avg_points"] == 30


def test_csv_avg_points_column_is_preserved():
    from backend.services.data_collector import import_csv_bytes
    csv = ("id,name,team,role,price,projection,avg_points,last5,status\n"
           "1,Test Player,BOS,G,8.5,0,22.4,20,active\n")
    rows = import_csv_bytes(csv.encode())
    assert rows[0]["avg_points"] == 22.4
    assert rows[0]["last5"] == 20
    assert rows[0]["source"] == "csv"


def test_budget_can_be_customized():
    ps=[]
    for role,count in {"C":2,"F":4,"G":4}.items():
        for i in range(count):
            ps.append(dict(id=f"{role}{i}",name=f"{role}{i}",team=f"T{role}{i}",role=role,price=5,projection=8,status="active"))
    out=optimize(ps, rules={"budget":50})
    assert out["cost"] == 50
    assert out["remaining"] == 0


def test_coach_shares_the_budget_and_exactly_one_is_picked():
    ps=[]
    for role,count in {"C":2,"F":4,"G":4}.items():
        for i in range(count):
            ps.append(dict(id=f"{role}{i}",name=f"{role}{i}",team=f"T{role}{i}",role=role,price=5,projection=8,status="active"))
    cs=[
        dict(id="coachA",name="Coach A",team="TA",price=5,projection=20),
        dict(id="coachB",name="Coach B",team="TB",price=1,projection=20),
    ]
    out=optimize(ps, coaches=cs, rules={"budget":53,"include_coach":True})
    assert out["coach"] is not None
    assert out["coach"]["id"] == "coachB"          # same score, cheaper -> more budget left for tie-break logic
    assert out["cost"] <= 53
    assert out["player_cost"] + out["coach_cost"] == out["cost"]
    assert out["projected_points"] == 80 + 20
    assert out["coaches_considered"] == 2


def test_expensive_coach_is_worth_it_when_it_scores_more():
    ps=[]
    for role,count in {"C":2,"F":4,"G":4}.items():
        for i in range(count):
            ps.append(dict(id=f"{role}{i}",name=f"{role}{i}",team=f"T{role}{i}",role=role,price=5,projection=8,status="active"))
    cs=[
        dict(id="cheap",name="Cheap",team="T1",price=1,projection=5),
        dict(id="star",name="Star",team="T2",price=6,projection=30),
    ]
    out=optimize(ps, coaches=cs, rules={"budget":56,"include_coach":True})
    assert out["coach"]["id"] == "star"
    assert out["cost"] == 50 + 6
    assert out["projected_points"] == 80 + 30


def test_optimizer_stays_players_only_without_coaches():
    ps=[]
    for role,count in {"C":2,"F":4,"G":4}.items():
        for i in range(count):
            ps.append(dict(id=f"{role}{i}",name=f"{role}{i}",team=f"T{role}{i}",role=role,price=5,projection=8,status="active"))
    out=optimize(ps, coaches=[], rules={"budget":50})
    assert out["coach"] is None
    assert out["coach_cost"] == 0
    assert out["projected_points"] == 80


def test_expected_coach_points_follows_dunkest_official_table():
    from backend.services.coach_collector import expected_coach_points
    # Undefeated team, close games (margin 1-5): win = +15 each game.
    assert expected_coach_points(1.0, 110, 108) == 15
    # Winless team, close games: loss = -7 each game.
    assert expected_coach_points(0.0, 110, 112) == -7
    # Blowout win (margin > 40) = +35, plus +5 score >=120 and +5 concede <=95.
    assert expected_coach_points(1.0, 125, 80) == 45
    # Average opponent: 50% win chance between +15 and -7.
    assert expected_coach_points(0.5, 110, 108) == 4


def test_coach_sources_are_merged_on_team_name():
    from backend.services.coach_collector import merge_sources
    listone=[{"name":"J.B. Bickerstaff","team":"Detroit Pistons","price":9.5},
             {"name":"Someone Else","team":"Fake Team","price":6.0}]
    espn={"Detroit Pistons":{"wins":5,"losses":1,"win_pct":0.8333,
                             "ppg_for":119.7,"ppg_against":117.8,"diff":1.9}}
    bbref={"Detroit Pistons":{"coach":"J.B. Bickerstaff","wins":5,"losses":1,
                              "career_wins":364,"career_losses":351}}
    rows=merge_sources(listone, espn, bbref)
    det=rows[0]
    assert det["price"] == 9.5
    assert det["wins"] == 5 and det["losses"] == 1
    assert det["career_wins"] == 364
    assert det["record_match"] is True
    assert "espn" in det["source"] and "basketball_reference" in det["source"]
    assert det["projection"] > 0
    # A coach whose team is missing from the results still keeps its price.
    assert rows[1]["price"] == 6.0


def test_injury_report_flags_exclude_players_from_picking(tmp_path, monkeypatch):
    import backend.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    from backend.routes.players import save, apply_injuries
    ps = []
    for role, count in {"C": 2, "F": 4, "G": 4}.items():
        for i in range(count):
            ps.append(dict(id=f"{role}{i}", name=f"{role}{i}", team=f"T{role}{i}", role=role,
                           price=5, projection=8, avg_points=0, last5=0, status="unknown", source="csv"))
    ps.append(dict(id="star", name="Star Player", team="BOS", role="G", price=10,
                   projection=50, avg_points=0, last5=0, status="unknown", source="csv"))
    save(ps)
    matched = apply_injuries([
        {"id": "999", "name": "Star Player", "team": "BOS", "status": "out",
         "detail": "Indisponibile fino al 6 Nov"},
    ])
    assert matched == 1
    with db.connect() as con:
        row = con.execute("SELECT injured, status, status_detail FROM players WHERE id='star'").fetchone()
    assert row["injured"] == 1 and row["status"] == "out"
    assert "Indisponibile" in row["status_detail"]
    with db.connect() as con:
        db_players = [dict(r) for r in con.execute("SELECT * FROM players").fetchall()]
    out = optimize(db_players, rules={"budget": 95})
    ids = {p["id"] for p in out["players"]}
    assert "star" not in ids
    assert out["players_considered"] == 10
    # A second sync without that injury clears the mark.
    apply_injuries([])
    with db.connect() as con:
        row = con.execute("SELECT injured, status FROM players WHERE id='star'").fetchone()
    assert row["injured"] == 0 and row["status"] == "unknown"


def test_injury_status_mapping_distinguishes_out_from_gtd():
    from backend.services.injury_collector import _status
    assert _status({"reason": "Anca", "description": "Indisponibile fino al 6 Nov"}) == "out"
    assert _status({"reason": "Riposo", "description": ""}) == "out"
    assert _status({"reason": "Caviglia", "description": "GTD: da valutare prima della partita"}) == "injured"
