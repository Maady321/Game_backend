"""
Distributed Matchmaking Engine.

Algorithm: ELO-based priority queue with progressive tolerance relaxation.

How it works:
1. Player requests a match → added to Redis Sorted Set with score = ELO.
2. A periodic matching loop (runs on every instance) attempts to pair players.
3. Pairing uses a Lua script for atomicity: finds two players within ELO
   tolerance and removes them in a single operation. No other instance can
   grab the same players.
4. Tolerance widens over wait time: a player who's waited 30 seconds will
   accept opponents ±400 ELO instead of the initial ±100. This prevents
   high/low-rated players from waiting forever.
5. On match, the instance that won the Lua pop creates the room, registers
   it in Redis, and notifies both players via Pub/Sub.

Why Lua scripts for matchmaking?
- Redis is single-threaded. A Lua script executes atomically — while it
  runs, no other command can interleave. This gives us a distributed lock
  without actually using SETNX/Redlock patterns (which are harder to get
  right and have failure modes).
- Without atomicity: Instance A reads player X, Instance B reads player X,
  both try to match X → double-booking. Lua prevents this.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.core.redis_pubsub import RedisPubSubCoordinator
from app.core.ws_manager import ConnectionManager
from app.models.redis_models import PubSubMessage, RedisRoom
from app.models.schemas import MatchCancelledResponse, MatchFoundResponse, MatchmakingQueuedResponse
from app.utils.constants import RedisKey, RoomStatus, WSMessageType
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Lua script: atomically find and pop a pair of players within ELO tolerance.
# Returns: [player_a_id, player_a_elo, player_b_id, player_b_elo] or nil
MATCHMAKING_LUA = """
local queue_key = KEYS[1]
local timestamps_key = KEYS[2]
local base_tolerance = tonumber(ARGV[1])
local expansion_per_sec = tonumber(ARGV[2])
local max_tolerance = tonumber(ARGV[3])
local current_time = tonumber(ARGV[4])

-- Get all members sorted by ELO (score)
local members = redis.call('ZRANGEBYSCORE', queue_key, '-inf', '+inf', 'WITHSCORES')
local count = #members / 2

if count < 2 then
    return nil
end

-- Build player list: {id, elo, wait_time, tolerance}
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

-- Try to find a compatible pair (greedy: first valid pair wins)
for i = 1, #players do
    for j = i + 1, #players do
        local elo_diff = math.abs(players[i].elo - players[j].elo)
        -- Both players must accept the ELO difference
        if elo_diff <= players[i].tolerance and elo_diff <= players[j].tolerance then
            -- Atomically remove both from queue
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
"""


class MatchmakingService:
    """Distributed matchmaking engine using Redis Sorted Sets + Lua."""

    def __init__(
        self,
        redis_pool: aioredis.Redis,
        connection_manager: ConnectionManager,
        pubsub: RedisPubSubCoordinator,
    ) -> None:
        self._redis = redis_pool
        self._conn_manager = connection_manager
        self._pubsub = pubsub
        self._settings = get_settings()
        self._matching_task: asyncio.Task[None] | None = None
        self._running = False
        self._lua_sha: str | None = None

    async def start(self) -> None:
        """Start the periodic matching loop.

        Loads the Lua script into Redis (SCRIPT LOAD) for performance —
        subsequent calls use EVALSHA instead of sending the full script.
        """
        self._lua_sha = await self._redis.script_load(MATCHMAKING_LUA)
        self._running = True
        self._matching_task = asyncio.create_task(
            self._matching_loop(), name="matchmaking_loop"
        )
        logger.info("matchmaking_service_started")

    async def stop(self) -> None:
        """Stop the matching loop."""
        self._running = False
        if self._matching_task and not self._matching_task.done():
            self._matching_task.cancel()
            try:
                await self._matching_task
            except asyncio.CancelledError:
                pass
        logger.info("matchmaking_service_stopped")

    async def enqueue_player(
        self, player_id: str, elo_rating: int
    ) -> None:
        """Add a player to the matchmaking queue.

        Uses a pipeline to atomically add to the sorted set AND record
        the join timestamp (used for tolerance expansion).
        """
        pipe = self._redis.pipeline()
        pipe.zadd(RedisKey.MATCHMAKING_QUEUE, {player_id: elo_rating})
        pipe.hset(RedisKey.MATCHMAKING_TIMESTAMPS, player_id, str(time.time()))
        await pipe.execute()

        # Get queue position for the player
        rank = await self._redis.zrank(RedisKey.MATCHMAKING_QUEUE, player_id)
        queue_size = await self._redis.zcard(RedisKey.MATCHMAKING_QUEUE)

        logger.info(
            "player_enqueued",
            player_id=player_id,
            elo=elo_rating,
            queue_position=rank,
            queue_size=queue_size,
        )

        # Notify the player they're in queue
        await self._conn_manager.send_personal(
            player_id,
            MatchmakingQueuedResponse(
                queue_position=rank,
                estimated_wait_sec=self._estimate_wait(queue_size),
            ).model_dump(),
        )

    async def dequeue_player(self, player_id: str) -> bool:
        """Remove a player from the matchmaking queue.

        Returns True if the player was in the queue, False otherwise.
        """
        pipe = self._redis.pipeline()
        pipe.zrem(RedisKey.MATCHMAKING_QUEUE, player_id)
        pipe.hdel(RedisKey.MATCHMAKING_TIMESTAMPS, player_id)
        results = await pipe.execute()

        removed = results[0] > 0
        if removed:
            logger.info("player_dequeued", player_id=player_id)
            await self._conn_manager.send_personal(
                player_id,
                MatchCancelledResponse().model_dump(),
            )
        return removed

    async def _matching_loop(self) -> None:
        """Periodic loop that attempts to form matches.

        Runs at the configured interval. Each iteration calls the Lua
        script to atomically find and pop one pair. Continues until no
        more pairs can be formed.

        Multiple instances run this loop simultaneously. The Lua script's
        atomicity ensures no double-matching.
        """
        interval = self._settings.matchmaking_interval_ms / 1000.0

        while self._running:
            try:
                # Keep matching until no more pairs found
                while self._running:
                    pair = await self._try_match()
                    if pair is None:
                        break
                    await self._create_match(*pair)

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("matchmaking_loop_error", error=str(exc))
                await asyncio.sleep(interval)

    async def _try_match(
        self,
    ) -> tuple[str, int, str, int] | None:
        """Execute the matchmaking Lua script.

        Returns (player_a_id, player_a_elo, player_b_id, player_b_elo)
        or None if no valid pair exists.
        """
        if self._lua_sha is None:
            return None

        try:
            result = await self._redis.evalsha(
                self._lua_sha,
                2,  # Number of KEYS
                RedisKey.MATCHMAKING_QUEUE,
                RedisKey.MATCHMAKING_TIMESTAMPS,
                self._settings.elo_tolerance_base,
                self._settings.elo_tolerance_expansion_per_sec,
                self._settings.elo_tolerance_max,
                time.time(),
            )
        except aioredis.exceptions.NoScriptError:
            # Script was evicted from Redis cache — reload it
            self._lua_sha = await self._redis.script_load(MATCHMAKING_LUA)
            return None

        if result is None:
            return None

        # Lua returns list of bytes
        player_a_id = result[0].decode() if isinstance(result[0], bytes) else result[0]
        player_a_elo = int(result[1].decode() if isinstance(result[1], bytes) else result[1])
        player_b_id = result[2].decode() if isinstance(result[2], bytes) else result[2]
        player_b_elo = int(result[3].decode() if isinstance(result[3], bytes) else result[3])

        logger.info(
            "match_found",
            player_a=player_a_id,
            player_a_elo=player_a_elo,
            player_b=player_b_id,
            player_b_elo=player_b_elo,
            elo_diff=abs(player_a_elo - player_b_elo),
        )

        return player_a_id, player_a_elo, player_b_id, player_b_elo

    async def _create_match(
        self,
        player_a_id: str,
        player_a_elo: int,
        player_b_id: str,
        player_b_elo: int,
    ) -> str:
        """Create a room for the matched pair and notify both players.

        The instance that executes this becomes the room owner (it won
        the Lua script race, so it's the natural owner).
        """
        room_id = f"room-{uuid.uuid4().hex[:12]}"
        settings = self._settings

        # Register room in Redis
        room = RedisRoom(
            room_id=room_id,
            owner_instance_id=settings.instance_id,
            player_ids=[player_a_id, player_b_id],
            status=RoomStatus.FULL,
        )
        key = RedisKey.room(room_id)
        await self._redis.hset(key, mapping=room.to_dict())

        # Assign colors/sides randomly
        import random
        if random.random() > 0.5:
            player_a_color, player_b_color = "player_1", "player_2"
        else:
            player_a_color, player_b_color = "player_2", "player_1"

        # Notify Player A
        msg_a = MatchFoundResponse(
            room_id=room_id,
            opponent_id=player_b_id,
            opponent_username=player_b_id,  # Will be enriched by session data
            opponent_elo=player_b_elo,
            your_color=player_a_color,
        )

        # Notify Player B
        msg_b = MatchFoundResponse(
            room_id=room_id,
            opponent_id=player_a_id,
            opponent_username=player_a_id,
            opponent_elo=player_a_elo,
            your_color=player_b_color,
        )

        # Try local delivery first, fall back to Pub/Sub for cross-instance
        sent_a = await self._conn_manager.send_personal(player_a_id, msg_a.model_dump())
        sent_b = await self._conn_manager.send_personal(player_b_id, msg_b.model_dump())

        # If a player is on another instance, publish via Pub/Sub
        if not sent_a:
            await self._pubsub.publish_to_player(
                player_a_id,
                PubSubMessage(
                    event="match_found",
                    room_id=room_id,
                    sender_instance_id=settings.instance_id,
                    payload=msg_a.model_dump(),
                ),
            )

        if not sent_b:
            await self._pubsub.publish_to_player(
                player_b_id,
                PubSubMessage(
                    event="match_found",
                    room_id=room_id,
                    sender_instance_id=settings.instance_id,
                    payload=msg_b.model_dump(),
                ),
            )

        logger.info(
            "room_created",
            room_id=room_id,
            player_a=player_a_id,
            player_b=player_b_id,
            owner_instance=settings.instance_id,
        )

        from app.dependencies import get_game_engine
        game_engine = get_game_engine()
        await game_engine.create_game(
            room_id=room_id,
            player_ids=[player_a_id, player_b_id],
            player_elos={player_a_id: player_a_elo, player_b_id: player_b_elo}
        )

        return room_id

    def _estimate_wait(self, queue_size: int) -> int:
        """Rough wait time estimate based on queue size."""
        if queue_size >= 2:
            return 5
        elif queue_size == 1:
            return 15
        return 30

    async def get_queue_size(self) -> int:
        """Get current matchmaking queue size."""
        return await self._redis.zcard(RedisKey.MATCHMAKING_QUEUE)
