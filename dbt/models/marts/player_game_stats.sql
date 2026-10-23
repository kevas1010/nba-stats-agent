-- One row per player per game: box score plus shooting-efficiency and usage metrics.

with players as (
    select * from {{ ref('stg_player_game_logs') }}
),

teams as (
    select * from {{ ref('stg_team_game_logs') }}
)

select
    p.game_id,
    p.game_date,
    p.season,
    p.season_type,
    p.player_id,
    p.player_name,
    p.team_id,
    p.team,
    p.opponent,
    p.is_home,
    p.is_win,
    p.minutes,
    p.minutes > 0                                             as played,
    p.pts,
    p.reb,
    p.oreb,
    p.dreb,
    p.ast,
    p.stl,
    p.blk,
    p.tov,
    p.pf,
    p.fgm,
    p.fga,
    p.fg3m,
    p.fg3a,
    p.ftm,
    p.fta,
    p.plus_minus,
    p.pts + p.reb + p.ast                                     as pts_reb_ast,

    p.fgm / nullif(p.fga, 0)                                  as fg_pct,
    p.fg3m / nullif(p.fg3a, 0)                                as fg3_pct,
    p.ftm / nullif(p.fta, 0)                                  as ft_pct,
    (p.fgm + 0.5 * p.fg3m) / nullif(p.fga, 0)                 as efg_pct,
    p.pts / nullif(2 * (p.fga + 0.44 * p.fta), 0)             as ts_pct,

    -- Share of team possessions the player ended while on the floor.
    -- Player minutes are rounded by the API, so this is an approximation.
    (p.fga + 0.44 * p.fta + p.tov) * t.game_minutes
        / nullif(p.minutes * (t.fga + 0.44 * t.fta + t.tov), 0) as usage_rate,

    -- Hollinger's Game Score.
    p.pts + 0.4 * p.fgm - 0.7 * p.fga - 0.4 * (p.fta - p.ftm)
        + 0.7 * p.oreb + 0.3 * p.dreb + p.stl + 0.7 * p.ast
        + 0.7 * p.blk - 0.4 * p.pf - p.tov                    as game_score

from players p
join teams t
    on t.game_id = p.game_id
   and t.team_id = p.team_id
