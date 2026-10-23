-- One row per team per game: possessions, pace and ratings.
--
-- Possessions use the standard box-score estimate
--   FGA - OREB + TOV + 0.44 * FTA
-- averaged over both teams, so a game has one possession count and pace.

with teams as (
    select
        *,
        fga - oreb + tov + 0.44 * fta as est_possessions
    from {{ ref('stg_team_game_logs') }}
),

all_star as (
    select * from {{ ref('all_star_games') }}
),

games as (
    select
        t.*,
        o.team_id                     as opponent_id,
        o.pts                         as opp_pts,
        (t.est_possessions + o.est_possessions) / 2 as possessions
    from teams t
    join teams o
        on o.game_id = t.game_id
       and o.team_id <> t.team_id
)

select
    g.game_id,
    g.game_date,
    g.season,
    g.season_type,
    g.team_id,
    g.team,
    g.opponent_id,
    g.opponent,
    g.is_home,
    g.is_win,
    g.game_minutes,
    g.pts,
    g.opp_pts,
    g.possessions,
    -- Pace is possessions per 48 minutes, so overtime doesn't inflate it.
    48 * g.possessions / g.game_minutes          as pace,
    100 * g.pts / g.possessions                  as off_rating,
    100 * g.opp_pts / g.possessions              as def_rating,
    100 * (g.pts - g.opp_pts) / g.possessions    as net_rating,
    case
        when g.season_type = 'Playoffs' then 'playoffs'
        when a.all_star_date is null then null
        when g.game_date < a.all_star_date then 'pre_all_star'
        else 'post_all_star'
    end                                          as season_phase
from games g
left join all_star a
    on a.season = g.season
