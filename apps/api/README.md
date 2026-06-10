# apps/api

FastAPI gateway. Thin layer — handles auth, request validation, and exposes the REST surface the mobile app talks to. Heavy lifting lives in `packages/engine` and `apps/agents`.

## Phase 0
- `GET /health` only.

## Dev
```bash
uv run --package api uvicorn app.main:app --reload --port 8000
```

## Coming
- Auth router (JWT issue/refresh, biometric session binding)
- Broker connect router (Alpaca OAuth handoff)
- Orders / positions / portfolio routers
- Agent decision feed (websocket + REST)
- Approval endpoints
