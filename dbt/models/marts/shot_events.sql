-- One row per field goal attempt, with where it came from, when, and the game situation.

with shots as (
    select * from {{ ref('stg_play_by_play') }}
    where is_field_goal
),

teams as (
    select game_id, team_id, game_date, opponent, is_home
    from {{ ref('stg_team_game_logs') }}
),

-- Play-by-play only has last names ("Mobley"); box scores have full names.
players as (
    select game_id, player_id, player_name
    from {{ ref('stg_player_game_logs') }}
),

located as (
    select
        s.* exclude (player_name),
        coalesce(p.player_name, s.player_name) as player_name,
        t.game_date,
        t.opponent,
        t.is_home,
        case when s.shot_made then s.shot_value else 0 end as points,
        -- Score after the shot, from the shooting team's side.
        case when t.is_home then s.score_home else s.score_away end as team_score_after,
        case when t.is_home then s.score_away else s.score_home end as opp_score_after
    from shots s
    join teams t
        on t.game_id = s.game_id
       and t.team_id = s.team_id
    left join players p
        on p.game_id = s.game_id
       and p.player_id = s.player_id
)

select
    game_id,
    action_id,
    game_date,
    season,
    season_type,
    period,
    period > 4                                            as is_overtime,
    seconds_left_in_period,
    team_id,
    team,
    opponent,
    is_home,
    player_id,
    player_name,
    sub_type                                              as shot_type,
    shot_distance,
    x_legacy,
    y_legacy,
    shot_value = 3                                        as is_three,
    shot_made,
    points,
    -- Margin before the shot: positive when the shooting team led.
    (team_score_after - points) - opp_score_after         as margin_before,
    -- NBA "clutch": last five minutes of the 4th quarter or overtime, score within five.
    period >= 4
        and seconds_left_in_period <= 300
        and abs((team_score_after - points) - opp_score_after) <= 5 as is_clutch
from located
