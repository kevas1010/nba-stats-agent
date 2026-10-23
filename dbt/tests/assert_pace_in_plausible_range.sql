-- Modern NBA game pace sits roughly between 85 and 115 possessions per 48.
-- Anything outside 75–125 points to a bad join or a broken box score.

select game_id, team_id, pace
from {{ ref('team_pace') }}
where pace not between 75 and 125
