-- Each team's final score in play-by-play must equal its box-score points.
-- Uses the feed's h/v side, not the box score's home flag, which is false for
-- both teams at neutral sites (this test caught exactly that).

with sides as (
    select
        game_id,
        team_id,
        mode(location) filter (where location in ('h', 'v')) = 'h' as scored_as_home
    from {{ ref('stg_play_by_play') }}
    where team_id is not null
    group by game_id, team_id
),

finals as (
    select game_id, max(score_home) as home_final, max(score_away) as away_final
    from {{ ref('stg_play_by_play') }}
    group by game_id
)

select b.game_id, b.team, b.pts,
       case when s.scored_as_home then f.home_final else f.away_final end as pbp_final
from {{ ref('stg_team_game_logs') }} b
join sides s on s.game_id = b.game_id and s.team_id = b.team_id
join finals f on f.game_id = b.game_id
where b.pts <> case when s.scored_as_home then f.home_final else f.away_final end
