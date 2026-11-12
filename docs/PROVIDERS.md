# LLM providers — routing, cost, and how to turn one on

Three providers behind one flag. Anthropic is the default; the other two
exist because the council's Sonnet calls were 88% of this account's LLM
spend ($9.49 of $10.74), which is what made the operator pull the API key
and stop the desk on 2026-09-11.

| Provider | `LLM_PROVIDER` | Key | 1M in + 200k out |
|---|---|---|---|
| Anthropic | *(unset)* | `ANTHROPIC_API_KEY` | **$6.000** |
| GLM (Z.ai) | `glm` | `GLM_API_KEY` *(or `ZAI_API_KEY`)* | **$2.28** glm-5.3 · **$0.25** glm-5.3-flash |
| TypeSafe Jev | `jev` | `TYPESAFE_API_KEY` | **$0.042** |

**Read this before reading the table as a recommendation.** The 6-year
backtest over 13,403 signals found `strategy_fit` has no edge at any
horizon (hit rates 49.3–50.9%, every |z| < 1.0). A cheaper provider makes
*experiments* cheaper. It does not make the system profitable, and nothing
below should be read as implying it does.

---

## Setting keys on Railway

```bash
railway variables --service AutonomousTradeAgents --set "GLM_API_KEY=..."
railway variables --service AutonomousTradeAgents --set "LLM_PROVIDER=glm"
```

Then verify **before** a scheduled pass discovers the problem at 09:30 with
an open position on the line:

```bash
railway run -s AutonomousTradeAgents python -m trading_agents.provider_check --live
```

Without `--live` it is pure configuration inspection: no network, no spend,
safe anywhere. It never prints a key, only whether one is present. With
`--live` it makes exactly one call against a fixed synthetic snapshot —
fixed on purpose, so a failing probe cannot be confused with the market
having moved.

A non-zero exit means the provider is not usable. Two distinct failures it
separates for you:

- **`mode MOCK` with `--live`** — the key is missing, blank, or a
  placeholder. The council would have run on canned responses.
- **`abstained True`** — the call or the parse failed. The snapshot is
  unambiguous, so "no view" here is a transport or contract fault, not a
  market read.
- **`no tool_use block came back`** (added 2026-09-23) — a second live call
  checks TOOL calling, which the options trade hop depends on. It prints
  `stop_reason` and `output_tokens`: `max_tokens` with no block means a
  reasoning model spent the budget thinking. If that happens, raise the
  token budget or disable thinking. Do not ship it and read the HOLDs as
  market judgement.

**Auth.** Z.ai documents this endpoint with `ANTHROPIC_AUTH_TOKEN`
(`Authorization: Bearer`). The client sends the GLM key as both
`Authorization: Bearer` and `X-Api-Key`, so either scheme works, and it
never falls back to an `ANTHROPIC_API_KEY` found in the environment.

**Use a pay-as-you-go API key, not the GLM Coding Plan.** Z.ai's
Claude-Code docs present this endpoint under the Coding Plan, which is
sold for coding tools. Check Z.ai's current terms before running a trading
desk on a Coding Plan key.

---

## Fallback: GLM unreachable -> Claude Sonnet 5.5

Added 2026-11-12 (operator: "switch the model to sonnet 5.5 when glm is
not reachable"). Before it, a Z.ai outage made every council node raise,
every node abstain, and the desk HOLD all day.

**It is armed by `ANTHROPIC_API_KEY` being present** while
`LLM_PROVIDER=glm`. That key was removed on 2026-09-11 to stop the spend,
so putting it back is the opt-in. With no Anthropic key nothing changes: a
GLM failure raises as it always did.

```bash
railway variables --service AutonomousTradeAgents --set "ANTHROPIC_API_KEY=..."
railway run -s AutonomousTradeAgents python -m trading_agents.provider_check --live --provider glm --fallback
```

`--fallback` skips GLM and runs both probes on the fallback model, which is
the exact path an outage takes. **It has never been run against a real
key.** Run it before relying on the fallback.

| What | Behaviour |
|---|---|
| Counts as unreachable | Connection failure, timeout, any 5xx, and 401/402/403/404/408/429 from Z.ai. |
| Does NOT fall back | 400, 413, 422. Those are our request being wrong, and would be wrong on any provider. |
| A missing `GLM_API_KEY` | Not "unreachable". It is a config fault and still resolves to MOCK (or raises under `AGENTS_REQUIRE_REAL_LLM`). |
| Breaker | After one failure, calls skip GLM for `LLM_FALLBACK_COOLDOWN_SECONDS` (default 300). Then one call probes GLM with no SDK retries. Process-wide, because `LLM()` is built per pass. |
| Model | `claude-sonnet-5-5` for every tier, fast tier included. `LLM_FALLBACK_MODEL` repoints it; add a `_PRICES` row for a new id. |
| Off switch | `LLM_FALLBACK_ENABLED=0`, without removing the key. |
| Where you see it | System health -> LLM cost reads "GLM unreachable (reason) - council on claude-sonnet-5-5". Ledger rows carry the Sonnet id and the Sonnet price. |

**Cost.** Sonnet 5.5 is $2 / $10 per million against glm-5.3's $1.40 /
$4.40 and glm-5.3-flash's $0.15 / $0.50. The fast tier (router, technical)
costs about 13x more per call while an outage lasts. `MAX_DAILY_LLM_SPEND_USD`
still applies, and the ledger bills the model that answered.

**The request is not the same request.** Sonnet 5.5 rejects a non-default
`temperature` with a 400 and thinks by default, billed against
`max_tokens`. `llm.request_shape` builds the fields per model: no
`temperature`, and `thinking: {"type": "between_tools"}` (its lowest
thinking setting) so the council's token budgets keep meaning what they
meant. `LLM_EFFORT` (low, medium, high; default medium) tunes it.

**A refusal is an abstain.** Sonnet 5.5 can decline a request (HTTP 200,
`stop_reason: "refusal"`). That comes back as an empty reply, which every
caller already turns into an abstain; the log names the category.
Anthropic's server-side refusal fallback is deliberately NOT enabled: it
needs a beta header this SDK version (0.109.1) predates, a wrong beta
header is a 400 on every call, and the two categories it retries (`cyber`,
`frontier_llm`) are not ones a market-data prompt trips.

**Also fixed here.** `Model.OPUS` is `claude-opus-4-7`, which rejects a
non-default temperature too. This file sent `temperature=0.0` to every
model, so `OPTIONS_AGENT_MODEL=opus` on the Anthropic provider failed
every Bull and Bear call. `request_shape` now omits it for that model.

---

## The two are not the same kind of integration

**GLM is a base-URL swap.** Z.ai serves the Anthropic wire protocol at
`https://api.z.ai/api/anthropic`, so the SDK, the tool-calling shape and
every caller are untouched. `complete()`, `complete_tools()` and the whole
council work as-is.

**Jev is not.** It is a *structured-decision* model: you declare typed
questions, it returns typed answers, and it generates **no prose at all**.
That is why its output is genuinely free — there is no generated text to
bill for. It therefore cannot serve `complete()` or the tool-calling path,
and routing a prose call to it would return nothing useful.

So Jev is reachable only through **`LLM.decide()`** — a provider-neutral
seam that returns the same `Decision` from all three providers:

```python
Decision(direction, conviction, confidence, thesis, provider, model, abstained)
```

On Jev that is one native typed call. On Anthropic and GLM it is a JSON
completion shaped to the same contract, so an A/B compares like with like
rather than comparing two different questions.

### What this means for the Bull/Bear agents

They are **not** switched to Jev, and switching them is a behaviour change
rather than a config change. They call `open_option_trade` as a tool and
write a `thesis`; Jev does neither.

But of that tool's seven required arguments:

| Argument | Where it comes from under Jev |
|---|---|
| `underlying`, `strategy` | already known before the call |
| `take_profit_pct`, `stop_loss_pct` | clamped by `effective_stop_loss_pct` regardless |
| `direction`, `conviction` | map exactly onto Jev's `choice` and `score` |
| `thesis` | **no equivalent** — audit trail, not a risk input |

Six of seven survive. That shape is arguably *better* than what we have
(CLAUDE.md §3: the model influences *how much*, never composes the order),
but it changes what the council does and so is not done behind a flag.

---

## Failure contract

Adopted deliberately, and the distinction is the point:

- **A model call or parse failure ABSTAINS.** `Decision.abstained` is True,
  direction is neutral. `decide()` never raises.
- **A data-layer failure PROPAGATES** from the feature layer and must never
  reach here. A broken snapshot silently becoming "no view" is how a bug
  turns into a quiet, permanent HOLD that nobody investigates.

A caller must treat `abstained` differently from a genuine neutral: one is
"the evidence says nothing", the other is "we never got an answer".

---

## Cost-ledger gotchas

**Jev rows are estimates, not receipts.** Jev's response carries no usage
block — verified against the reference integration, which reads none
because there is none to read. Input tokens are approximated from the
serialised request at ~4 chars/token. Every other provider's rows are
exact. At $0.042/M the absolute error is cents on a year of trading, but it
is an estimate and the ledger must not imply otherwise.

**An unpriced model now warns.** It used to fall through to Sonnet pricing
in silence, which is the worst possible direction for the error: every
provider we add is added *because* it is cheaper, so an unpriced cheap
model reports up to 140x its real cost and the ledger says the migration
saved nothing. This bit Jev — its id is `jev-1.13.0` while the price row
said `jev-1.13`.

It warns rather than raises: a pricing gap is an accounting bug, and an
accounting bug must never halt a live council mid-pass. **If you repoint a
GLM tier with `GLM_MODEL_*`, add a price row for the new id.**

---

## Model ids drift

Defaults as of 2026-09-23: reasoning tier (`Model.SONNET`/`Model.OPUS`) ->
`glm-5.3` ($1.40/$4.40 per M), fast tier (`Model.HAIKU`) -> `glm-5.3-flash`
($0.15/$0.50). Price rows exist for those and for `glm-4.7`, `glm-4.6`,
`glm-4.5-air`, all at Z.ai's published list prices.
`test_every_default_glm_tier_is_priced` fails if a default ever lacks a row.

Z.ai revs GLM faster than we deploy. Repoint a tier from Railway rather
than shipping a code change, and add a price row for any new id:

```bash
railway variables --service AutonomousTradeAgents --set "GLM_MODEL_SONNET=glm-4.7"
```

Jev is four days into early access as of 2026-09-19. Its specs may move.

---

## What is verified, and what is not

**Verified offline:** request shape, response contract, retry on
`{429,500,502,503,504,529}`, redirects refused, echoed keys redacted,
non-JSON handled, abstain on every failure path, pricing, and that Jev and
the prose providers produce the same `Decision`.

**NOT verified:** the fallback against a real Anthropic key (offline it
is covered by 34 tests against the SDK's own exception classes); and a
live round trip against either endpoint, including
whether `glm-5.3`/`glm-5.3-flash` are served on the Anthropic-compatible
path and whether GLM tool calling returns `tool_use` blocks. There is no
`GLM_API_KEY` or `TYPESAFE_API_KEY` on this machine. `provider_check
--live` is the thing that closes that gap, and it has never been run
against a real key.
