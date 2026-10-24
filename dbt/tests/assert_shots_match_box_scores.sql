-- Field goals counted from play-by-play must equal the official box score for
-- every team-game that has play-by-play: attempts, makes and made threes.

with from_pbp as (
    select
        game_id,
        team_id,
        count(*)                                  as fga,
        count(*) filter (where shot_made)         as fgm,
        count(*) filter (where shot_made and is_three) as fg3m
    from {{ ref('shot_events') }}
    group by game_id, team_id
)

select b.game_id, b.team, b.fga, p.fga as pbp_fga, b.fgm, p.fgm as pbp_fgm, b.fg3m, p.fg3m as pbp_fg3m
from {{ ref('stg_team_game_logs') }} b
join from_pbp p
    on p.game_id = b.game_id
   and p.team_id = b.team_id
where p.fga <> b.fga or p.fgm <> b.fgm or p.fg3m <> b.fg3m
