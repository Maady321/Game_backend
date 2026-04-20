--[[
Atomic matchmaking pair extraction.

This script is loaded into Redis and called via EVALSHA.
It runs atomically — no other command can interleave.

KEYS[1] = matchmaking queue (Sorted Set, score = ELO)
KEYS[2] = matchmaking timestamps (Hash, player_id → join_time)

ARGV[1] = base tolerance
ARGV[2] = expansion per second
ARGV[3] = max tolerance
ARGV[4] = current time

Returns: {player_a_id, player_a_elo, player_b_id, player_b_elo} or nil
]]

local queue_key = KEYS[1]
local timestamps_key = KEYS[2]
local base_tolerance = tonumber(ARGV[1])
local expansion_per_sec = tonumber(ARGV[2])
local max_tolerance = tonumber(ARGV[3])
local current_time = tonumber(ARGV[4])

local members = redis.call('ZRANGEBYSCORE', queue_key, '-inf', '+inf', 'WITHSCORES')
local count = #members / 2

if count < 2 then
    return nil
end

local players = {}
for i = 1, #members, 2 do
    local pid = members[i]
    local elo = tonumber(members[i + 1])
    local join_time = tonumber(redis.call('HGET', timestamps_key, pid) or current_time)
    local wait_seconds = current_time - join_time
    local tolerance = math.min(
        base_tolerance + (wait_seconds * expansion_per_sec),
        max_tolerance
    )
    table.insert(players, {pid = pid, elo = elo, tolerance = tolerance})
end

for i = 1, #players do
    for j = i + 1, #players do
        local elo_diff = math.abs(players[i].elo - players[j].elo)
        if elo_diff <= players[i].tolerance and elo_diff <= players[j].tolerance then
            redis.call('ZREM', queue_key, players[i].pid, players[j].pid)
            redis.call('HDEL', timestamps_key, players[i].pid, players[j].pid)
            return {
                players[i].pid, tostring(players[i].elo),
                players[j].pid, tostring(players[j].elo)
            }
        end
    end
end

return nil
