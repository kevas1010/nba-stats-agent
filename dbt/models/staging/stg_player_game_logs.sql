with source as (
    select * from {{ source('raw', 'player_game_logs') }}
)

select
    game_id,
    player_id,
    player_name,
    team_id,
    team_abbreviation                     as team,
    season,
    season_type,
    cast(game_date as date)               as game_date,
    matchup like '%vs.%'                  as is_home,
    right(matchup, 3)                     as opponent,
    wl = 'W'                              as is_win,
    min                                   as minutes,
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
