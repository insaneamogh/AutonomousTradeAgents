# LLM providers — routing, cost, and how to turn one on

Three providers behind one flag. Anthropic is the default; the other two
exist because the council's Sonnet calls were 88% of this account's LLM
spend ($9.49 of $10.74), which is what made the operator pull the API key
and stop the desk on 2026-09-11.

| Provider | `LLM_PROVIDER` | Key | 1M in + 200k out |
|---|---|---|---|
| Anthropic | *(unset)* | `ANTHROPIC_API_KEY` | **$6.000** |
| GLM (Z.ai) | `glm` | `GLM_API_KEY` *(or `ZAI_API_KEY`)* | **$0.778** |
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

Z.ai revs GLM faster than we deploy — their docs already advertise GLM-5.3
while our defaults are `glm-4.6` / `glm-4.5-air`. The defaults stay where
they are because those are the ids the cost ledger has price rows for.
Repoint a tier from Railway rather than shipping a code change:

```bash
railway variables --service AutonomousTradeAgents --set "GLM_MODEL_SONNET=glm-5.3"
```

Jev is four days into early access as of 2026-09-19. Its specs may move.

---

## What is verified, and what is not

**Verified offline:** request shape, response contract, retry on
`{429,500,502,503,504,529}`, redirects refused, echoed keys redacted,
non-JSON handled, abstain on every failure path, pricing, and that Jev and
the prose providers produce the same `Decision`.

**NOT verified:** a live round trip against either endpoint. There is no
`GLM_API_KEY` or `TYPESAFE_API_KEY` on this machine. `provider_check
--live` is the thing that closes that gap, and it has never been run
against a real key.
