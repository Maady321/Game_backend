# Real-Time Multiplayer Game Backend

A production-grade, horizontally scalable real-time multiplayer game server built with Python, FastAPI, WebSockets, Redis, and PostgreSQL.

## Architecture

```
Client → Nginx (sticky sessions) → FastAPI Instances ↔ Redis (coordination)
                                          ↓
                                    PostgreSQL (persistence)
```

### Key Design Decisions

| Component | Choice | Why |
|---|---|---|
| Transport | WebSockets | Bidirectional, low-latency, single persistent connection |
| Framework | FastAPI + uvicorn | Async-native, high performance, auto-docs |
| Coordination | Redis Pub/Sub | Sub-ms cross-instance messaging, atomic Lua scripts |
| Matchmaking | Redis Sorted Set + Lua | Atomic pair extraction, no double-matching |
| Game State | In-memory (hot) + Redis snapshots (warm) | Fast mutations, crash recovery |
| Persistence | PostgreSQL | ACID for match history, ELO, leaderboards |
| Auth | JWT (HS256) | Stateless, no DB lookup per WS message |

## Quick Start

### Prerequisites
- Python 3.11+
- Docker & Docker Compose
- Redis 7+
- PostgreSQL 16+

### Development (Local)

```bash
# 1. Install dependencies
pip install -e ".[dev]"

# 2. Start Redis and PostgreSQL
docker-compose -f docker/docker-compose.yml up -d postgres redis

# 3. Copy and configure environment
cp .env.example .env

# 4. Run the server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Production (Docker)

```bash
# Build and launch all services (2 game server replicas + nginx + redis + postgres)
cd docker
docker-compose up -d --build

# Scale to more instances
docker-compose up -d --scale game-server=4
```

## API Reference

### REST Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/register` | Create player account |
| POST | `/api/login` | Authenticate, get JWT |
| GET | `/api/profile/{id}` | Player stats |
| GET | `/api/leaderboard` | Global rankings |
| GET | `/api/health` | Health check |

### WebSocket Protocol

Connect: `ws://host/ws?token=<JWT>`

#### Client → Server Messages

```json
{"type": "find_match"}
{"type": "cancel_match"}
{"type": "game_action", "action": "place", "data": {"x": 3, "y": 5}, "sequence_number": 1}
{"type": "heartbeat_ping"}
```

#### Server → Client Messages

```json
{"type": "connected", "player_id": "...", "session_id": "..."}
{"type": "matchmaking_queued", "estimated_wait_sec": 5}
{"type": "match_found", "room_id": "...", "opponent_id": "...", "your_color": "player_1"}
{"type": "game_started", "room_id": "...", "initial_state": {...}, "your_turn": true}
{"type": "game_state_update", "state": {...}, "your_turn": false, "turn_number": 3}
{"type": "game_over", "winner_id": "...", "result": "win", "elo_delta": 15}
{"type": "error", "code": "NOT_YOUR_TURN", "message": "..."}
```

## Testing

```bash
# Unit tests (no Redis/Postgres needed — uses fakeredis)
pytest tests/ -v

# With coverage
pytest tests/ --cov=app --cov-report=html

# Load testing (requires locust)
# locust -f tests/locustfile.py --host ws://localhost:8000
```

## Project Structure

```
app/
├── main.py              # FastAPI app factory + lifespan
├── config.py            # Pydantic settings
├── dependencies.py      # Singleton DI container
├── core/
│   ├── ws_manager.py    # WebSocket connection manager
│   ├── session_manager.py  # Redis-backed sessions
│   ├── matchmaking.py   # Distributed matchmaking engine
│   ├── game_engine.py   # Server-authoritative game logic
│   └── redis_pubsub.py  # Cross-instance messaging
├── models/
│   ├── database.py      # SQLAlchemy ORM models
│   ├── schemas.py       # Pydantic message schemas
│   └── redis_models.py  # Redis data structures
├── api/
│   ├── ws_routes.py     # WebSocket endpoint
│   ├── rest_routes.py   # REST API endpoints
│   └── middleware.py    # Rate limiting
├── persistence/
│   ├── database.py      # Async engine setup
│   └── repositories.py  # Data access layer
└── utils/
    ├── auth.py          # JWT utilities
    ├── logging.py       # Structured logging
    └── constants.py     # Enums and Redis key patterns
```

## License

MIT
