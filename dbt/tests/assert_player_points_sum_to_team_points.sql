-- Every team's points in a game should equal the sum of its players' points.

with player_totals as (
    select game_id, team_id, sum(pts) as player_pts
    from {{ ref('player_game_stats') }}
    group by game_id, team_id
)

select t.game_id, t.team_id, t.pts, p.player_pts
from {{ ref('stg_team_game_logs') }} t
left join player_totals p
    on p.game_id = t.game_id
   and p.team_id = t.team_id
where p.player_pts is distinct from t.pts
