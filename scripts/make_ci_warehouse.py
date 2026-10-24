"""Build a small synthetic raw warehouse so CI can run `dbt build` without nba_api.

stats.nba.com blocks most cloud IPs, so CI can't ingest real data. These rows
are made up but internally consistent: player points sum to team points,
possessions give a realistic pace, and games fall on both sides of the
All-Star break, so every dbt data test exercises real logic.

    python scripts/make_ci_warehouse.py data/ci.duckdb
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

SEASON = "2024-25"
TEAMS = {"NYK": (1610612752, "New York Knicks"), "BOS": (1610612738, "Boston Celtics")}

# (game_id, date, home, away, season_type, home_pts, away_pts, minutes)
GAMES = [
    ("0022400100", "2024-11-12", "NYK", "BOS", "Regular Season", 112, 104, 240),
    ("0022400900", "2025-03-04", "BOS", "NYK", "Regular Season", 121, 118, 265),  # overtime, after the break
    ("0042400201", "2025-05-05", "BOS", "NYK", "Playoffs", 105, 108, 240),
]


def _team_line(pts: int, minutes: int) -> dict:
    # About 100 possessions per 48 minutes: fga - oreb + tov + 0.44 * fta.
    scale = minutes / 240
    fga, oreb, tov, fta = round(88 * scale), round(10 * scale), round(13 * scale), round(22 * scale)
    fg3m = 13
    ftm = round(fta * 0.8)
    fgm = (pts - ftm - fg3m) // 2  # 2*fgm + fg3m + ftm == pts when the parity works out
    ftm = pts - 2 * fgm - fg3m
    return dict(min=minutes, fgm=fgm, fga=fga, fg3m=fg3m, fg3a=36, ftm=ftm, fta=max(fta, ftm),
                oreb=oreb, dreb=34, reb=oreb + 34, ast=26, stl=7, blk=5, tov=tov, pf=19, pts=pts)


def _split(total: int, parts: int) -> list[int]:
    base = [total // parts] * parts
    for i in range(total % parts):
        base[i] += 1
    return base


def _play_by_play(team_rows: list[dict], player_rows: list[dict], now: datetime) -> list[dict]:
    """Shot events whose counts match each team's box score (fga, fgm, fg3m), plus a steal
    that shares its action_number with a turnover, as the real feed does."""
    events = []
    for game_id in dict.fromkeys(r["game_id"] for r in team_rows):
        lines = [r for r in team_rows if r["game_id"] == game_id]
        home = next(r for r in lines if "vs." in r["matchup"])
        score = {r["team_id"]: 0 for r in lines}
        action_id = 0
        for line in lines:
            shooters = [p for p in player_rows if p["game_id"] == game_id and p["team_id"] == line["team_id"]]
            # Made threes, missed threes, made twos, missed twos, in that order.
            shots = ([(3, True)] * line["fg3m"] + [(3, False)] * (line["fg3a"] - line["fg3m"])
                     + [(2, True)] * (line["fgm"] - line["fg3m"])
                     + [(2, False)] * (line["fga"] - line["fgm"] - (line["fg3a"] - line["fg3m"])))
            for i, (value, made) in enumerate(shots):
                action_id += 1
                shooter = shooters[i % len(shooters)]
                period = 1 + (i * 4) // len(shots)
                if made:
                    score[line["team_id"]] += value
                home_score = score[home["team_id"]]
                away_score = next(v for k, v in score.items() if k != home["team_id"])
                events.append(dict(
                    game_id=game_id, action_number=action_id, action_id=action_id,
                    clock=f"PT{11 - i % 12:02d}M{(i * 7) % 60:02d}.00S", period=period,
                    team_id=line["team_id"], team_tricode=line["team_abbreviation"],
                    person_id=shooter["player_id"], player_name=shooter["player_name"].split()[-1],
                    action_type="Made Shot" if made else "Missed Shot", sub_type="Jump Shot",
                    description="", is_field_goal=1, shot_result="Made" if made else "Missed",
                    shot_value=value, shot_distance=25 if value == 3 else 8, x_legacy=0, y_legacy=0,
                    score_home=str(home_score) if made else "", score_away=str(away_score) if made else "",
                    points_total=home_score + away_score, location="h" if line is home else "v",
                    season=line["season"], season_type=line["season_type"], ingested_at=now,
                ))
        # A turnover and the steal it caused share an action_number.
        for linked_id, action_type in ((action_id + 1, "Turnover"), (action_id + 2, "")):
            events.append(dict(events[-1], action_id=linked_id, action_number=action_id + 1,
                               action_type=action_type, is_field_goal=0, shot_result="", shot_value=0,
                               shot_distance=0, score_home="", score_away=""))
    return events


def build(path: Path) -> None:
    now = datetime.now(timezone.utc)
    team_rows, player_rows = [], []
    for game_id, day, home, away, season_type, home_pts, away_pts, minutes in GAMES:
        for team, opp, pts, at_home in ((home, away, home_pts, True), (away, home, away_pts, False)):
            team_id, team_name = TEAMS[team]
            line = _team_line(pts, minutes)
            matchup = f"{team} vs. {opp}" if at_home else f"{team} @ {opp}"
            won = pts > (away_pts if at_home else home_pts)
            team_rows.append(dict(
                season_id=f"2{SEASON[:4]}", team_id=team_id, team_abbreviation=team, team_name=team_name,
                game_id=game_id, game_date=day, matchup=matchup, wl="W" if won else "L",
                **line, fg_pct=line["fgm"] / line["fga"], fg3_pct=line["fg3m"] / line["fg3a"],
                ft_pct=line["ftm"] / line["fta"], plus_minus=pts - (away_pts if at_home else home_pts),
                season=SEASON, season_type=season_type, ingested_at=now,
            ))
            # Ten players; the counting stats split evenly so the sums match the team line.
            splits = {k: _split(line[k], 10) for k in ("fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb",
                                                         "dreb", "ast", "stl", "blk", "tov", "pf", "pts")}
            mins = _split(minutes, 10)
            for i in range(10):
                p = {k: v[i] for k, v in splits.items()}
                player_rows.append(dict(
                    season_id=f"2{SEASON[:4]}", player_id=team_id * 100 + i, player_name=f"{team} Player {i}",
                    team_id=team_id, team_abbreviation=team, team_name=team_name, game_id=game_id,
                    game_date=day, matchup=matchup, wl="W" if won else "L", min=mins[i], **p,
                    reb=p["oreb"] + p["dreb"], fg_pct=None, fg3_pct=None, ft_pct=None,
                    plus_minus=0, fantasy_pts=0.0, season=SEASON, season_type=season_type, ingested_at=now,
                ))

    schedule = pd.DataFrame([
        dict(game_id="0022600001", season_year="2026-27", game_time_utc=pd.Timestamp("2026-10-20 23:30", tz="UTC"),
             game_status=3, game_status_text="Final", game_label="", arena_name="Madison Square Garden",
             arena_city="New York", home_team_id=TEAMS["NYK"][0], home_team="NYK", home_score=110,
             away_team_id=TEAMS["BOS"][0], away_team="BOS", away_score=101, season="2026-27", ingested_at=now),
        dict(game_id="0022600050", season_year="2026-27", game_time_utc=pd.Timestamp("2026-11-02 00:00", tz="UTC"),
             game_status=1, game_status_text="7:00 pm ET", game_label="Emirates NBA Cup", arena_name="TD Garden",
             arena_city="Boston", home_team_id=TEAMS["BOS"][0], home_team="BOS", home_score=0,
             away_team_id=TEAMS["NYK"][0], away_team="NYK", away_score=0, season="2026-27", ingested_at=now),
    ])

    pbp = pd.DataFrame(_play_by_play(team_rows, player_rows, now))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE SCHEMA raw")
        for name, frame in (("team_game_logs", pd.DataFrame(team_rows)),
                            ("player_game_logs", pd.DataFrame(player_rows)), ("schedule", schedule),
                            ("play_by_play", pbp)):
            con.register("frame", frame)
            con.execute(f"CREATE TABLE raw.{name} AS SELECT * FROM frame")
            con.unregister("frame")
    print(f"wrote {path}: {len(team_rows)} team rows, {len(player_rows)} player rows, {len(schedule)} scheduled games")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "data/ci.duckdb"))
