# docs/

Living documentation.

| Folder | What goes here |
|---|---|
| `architecture/` | ADRs (architecture decision records) — small, dated, append-only |
| `runbooks/` | Operational guides (deploy, rollback, incident response) |
| `reference/` | Notes on legacy TradeMatrix data we may reuse, external API quirks, etc. |

## ADR convention
- One markdown file per decision: `adr-NNNN-<kebab-title>.md`
- Status: `proposed` → `accepted` / `rejected` / `superseded`
- Never edit a past ADR. Write a new one that supersedes it.

The first few decisions to capture (still TODO):
1. Why LangGraph over a plain asyncio state machine
2. Monorepo layout (pnpm workspaces + uv workspaces side-by-side)
3. Broker abstraction surface
4. PDT tracking strategy
5. Drawdown circuit-breaker state machine
