with source as (
    select * from {{ source('raw', 'team_game_logs') }}
)

select
    game_id,
    team_id,
    team_abbreviation                     as team,
    team_name,
    season,
    season_type,
    cast(game_date as date)               as game_date,
    matchup like '%vs.%'                  as is_home,
    right(matchup, 3)                     as opponent,
    wl = 'W'                              as is_win,
    -- Team minutes are summed over all five players on the floor.
    min / 5                               as game_minutes,
    fgm,
    fga,
    fg3m,
    fg3a,
    ftm,
    fta,
    oreb,
    dreb,
    reb,
    ast,
    stl,
    blk,
    tov,
    pf,
    pts,
    plus_minus
from source
