--[[
Atomic room ownership claim.

Used when an instance detects an orphaned room (owner instance's
heartbeat expired) and wants to claim ownership.

KEYS[1] = room key (Hash)
ARGV[1] = claiming instance ID
ARGV[2] = expected dead instance ID

Returns: 1 if claimed, 0 if already claimed by someone else
]]

local room_key = KEYS[1]
local claiming_instance = ARGV[1]
local dead_instance = ARGV[2]

local current_owner = redis.call('HGET', room_key, 'owner_instance_id')

-- Only claim if the current owner is the dead instance
if current_owner == dead_instance then
    redis.call('HSET', room_key, 'owner_instance_id', claiming_instance)
    return 1
end

return 0
