-- One row per play-by-play event. Scores are only reported on scoring plays,
-- so they're carried forward to every event; the clock becomes seconds left.
--
-- action_id is the event key and runs in game order. action_number is not
-- unique: linked events (a steal and the turnover it caused, a block and the
-- missed shot) share one, with the second carrying a blank action type.

with source as (
    select * from {{ source('raw', 'play_by_play') }}
),

parsed as (
    select
        game_id,
        action_id,
        action_number,
        season,
        season_type,
        period,
        -- 'PT11M41.00S' -> 701.0
        cast(regexp_extract(clock, 'PT(\d+)M', 1) as integer) * 60
            + cast(regexp_extract(clock, 'M([\d.]+)S', 1) as double)  as seconds_left_in_period,
        nullif(team_id, 0)                                             as team_id,
        nullif(team_tricode, '')                                       as team,
        nullif(person_id, 0)                                           as player_id,
        nullif(player_name, '')                                        as player_name,
        nullif(action_type, '')                                        as action_type,
        nullif(sub_type, '')                                           as sub_type,
        description,
        is_field_goal = 1                                              as is_field_goal,
        case when is_field_goal = 1 then shot_result = 'Made' end      as shot_made,
        case when is_field_goal = 1 then shot_value end                as shot_value,
        case when is_field_goal = 1 then shot_distance end             as shot_distance,
        x_legacy,
        y_legacy,
        try_cast(nullif(score_home, '') as integer)                    as reported_score_home,
        try_cast(nullif(score_away, '') as integer)                    as reported_score_away
    from source
)

select
    * exclude (reported_score_home, reported_score_away),
    coalesce(last_value(reported_score_home ignore nulls) over running, 0) as score_home,
    coalesce(last_value(reported_score_away ignore nulls) over running, 0) as score_away
from parsed
window running as (
    partition by game_id order by action_id
    rows between unbounded preceding and current row
)
