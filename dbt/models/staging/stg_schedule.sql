with source as (
    select * from {{ source('raw', 'schedule') }}
)

select
    game_id,
    season,
    game_time_utc,
    cast(timezone('America/New_York', game_time_utc) as date) as game_date_et,
    case game_status
        when 1 then 'scheduled'
        when 2 then 'live'
        when 3 then 'final'
    end                                   as status,
    nullif(trim(game_label), '')          as game_label,
    game_label = 'Preseason'              as is_preseason,
    arena_name,
    arena_city,
    home_team_id,
    home_team,
    away_team_id,
    away_team,
    -- The API reports 0-0 for games that haven't tipped off.
    case when game_status > 1 then home_score end as home_score,
    case when game_status > 1 then away_score end as away_score
from source
