# Railway checks — for the agent with account access

**Why this file exists:** the session that wrote it could not reach the
deployment. Its Railway connector saw exactly one workspace ("Amogh
Patil's Projects", 2 projects: `loving-generosity`, `fearless-elegance`)
and neither contained the trading service. There is no `pranjal
projects` workspace, no `tradematrix` project and no `autonomous`
environment visible to that token, and an OAuth connector only ever
returns variable **names**, never values.

So everything below is a check somebody with real access has to run.
Each one states what to run, what a good answer looks like, and what to
do when it is wrong.

Ordered by consequence. **§1 is an eligibility gate — if it is wrong,
nothing else matters.**

---

## 1. 🔴 Fresh Alpaca paper account (BLOCKING)

The hackathon rules, fetched 2026-09-02:

> *"For your final submission, create a brand-new Alpaca paper trading
> account dedicated to this hackathon. Projects run on an existing or
> reused account will not be eligible for judging."*
> *"Competition account starting balance must be set to $100,000."*

The account in use is a reused development account. **Its P&L cannot be
submitted.** This also means the ~-$800 currently showing is irrelevant
to judging — the submission runs on a fresh $100k account.

```bash
railway variables --service AutonomousTradeAgents | grep -i alpaca
```

**Good:** `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` belong to an account
created for this hackathon, funded at $100,000.

**If wrong:** create the account, generate paper keys, then

```bash
railway variables --service AutonomousTradeAgents \
  --set "ALPACA_API_KEY=PK..." --set "ALPACA_SECRET_KEY=..."
```

Then confirm the reconciler picked them up — `/health` plus one scan
cycle — before the market opens.

---

## 2. 🔴 Alpaca CLI actually running (eligibility)

The rule is *"projects must utilize either Alpaca's MCP server or its CLI
tools."* The `alpaca` binary is pinned, checksummed and installed in the
image (`apps/api/Dockerfile` stage 2), and `USE_ALPACA_CLI` now **defaults
ON** in code — so this should be live without any variable set. Confirm
it, because a silent failure here costs eligibility, not just a feature.

```bash
# 1. Is the flag explicitly disabled anywhere?
railway variables --service AutonomousTradeAgents | grep -i USE_ALPACA_CLI

# 2. Is the binary present in the running container?
railway run --service AutonomousTradeAgents -- alpaca version

# 3. Does the app actually route through it?
curl -s https://<app-domain>/scanner/status | jq '{marketOpenSource, alpacaCli}'
```

**Good:**
- No `USE_ALPACA_CLI=0` (unset is fine — the default is on).
- `alpaca version` prints a version and exits 0.
- `alpacaCli.available == true`, `alpacaCli.enabled == true`.
- After a scan has run, `marketOpenSource == "alpaca_cli"`.

**If wrong:**
- `alpacaCli.available == false` → the binary is missing from the image.
  Check the Dockerfile stage-2 build did not silently skip.
- `available` true but `marketOpenSource` is `"alpaca"` → the CLI ran and
  failed, falling back to REST. Check the logs for
  `alpaca_cli:` lines; **exit code 2 means an auth error**, i.e. the keys
  in the container are wrong or missing.
- `USE_ALPACA_CLI=0` is set → remove it, or
  `railway variables --service AutonomousTradeAgents --set "USE_ALPACA_CLI=1"`.

---

## 3. 🟠 Option quote timestamps and the data feed

Quote-freshness plumbing shipped **disabled** and cannot be safely turned
on without this measurement. Getting it wrong in either direction is
expensive: too tight refuses 100% of options trades, off entirely means
selecting strikes on possibly-stale greeks.

```bash
railway variables --service AutonomousTradeAgents | grep -i ALPACA_OPTIONS_FEED
```

**Which feed is in use?**

| `ALPACA_OPTIONS_FEED` | Meaning | Baseline quote age | Correct setting |
|---|---|---|---|
| unset or `indicative` | free tier, derived quotes, documented ~15-min delay | ~900s | `1800` |
| `opra` | real-time OPRA tape | seconds | `300` |

Then confirm timestamps actually arrive. Run one options council pass and
check that `ContractQuote.quote_ts` is populated — if it is `None` in
production, enabling the gate refuses **every** option, because an
unknown age fails closed by design.

**Only once both are confirmed:**

```bash
railway variables --service AutonomousTradeAgents \
  --set "OPTIONS_MAX_QUOTE_AGE_SECONDS=1800"   # 300 if on OPRA
```

Leave it at `0` (off) if you cannot verify. Off is the current behaviour
and is survivable; on-and-wrong is not.

---

## 4. 🟠 Risk profile and the options master switch

```bash
railway variables --service AutonomousTradeAgents \
  | grep -iE "RISK_PROFILE|ALLOW_OPTIONS|ALLOW_SHORTS"
```

**Good:**
- `ALLOW_OPTIONS=1` — options are a hard hackathon requirement and this
  fails **closed**. Unset means no options trading at all.
- `RISK_PROFILE=aggressive_paper` — otherwise the conservative defaults
  apply and the book is 5% aggregate, not 7.5%.

Under `aggressive_paper` the book is **5 concurrent option positions**
(7.5% aggregate ÷ 1.5% per position). That is deliberate and pinned by a
test; do not widen it without reading
`max_options_book_drawdown_pct`.

---

## 5. 🟡 LLM budget

```bash
railway variables --service AutonomousTradeAgents \
  | grep -iE "MAX_LLM|MAX_DAILY_LLM|OPTIONS_AGENT_MODEL|MIN_LLM_SCORE"
```

**Good / expected:**

| Variable | Value | Note |
|---|---|---|
| `MAX_DAILY_LLM_SPEND_USD` | `3.00` | hard ceiling, live-checked before each paid symbol |
| `MAX_LLM_SYMBOLS_PER_DAY` | `20` | |
| `MAX_LLM_SYMBOLS_PER_HOUR` | `4` | stops the day's budget going in the first 15 minutes |
| `OPTIONS_AGENT_MODEL` | unset | defaults to Sonnet; `haiku` forces the cheap model |
| `MIN_LLM_SCORE` | unset | **leave unset** — see §6 |

Expected spend is **~$0.80/day**, hard-capped at $3.00. Two days is ~$2
expected, $6 worst case. There is no need to downgrade the model to stay
under $10.

---

## 6. 🟡 The measurement that is actually outstanding

The single highest-value unknown. Two ranking signals were measured on
**synthetic** data only:

| Dataset | `score` spread | `conviction` spread |
|---|---|---|
| synthetic generator (300 symbols) | 0.0032 | 0.0179 |
| eval archetypes (100 cases) | 0.1946 | 0.3892 |

Both datasets are inadequate — the generator derives every feature from
one hash seed so nothing varies independently, and the archetypes are
hand-built and several saturate. **Neither settles whether real Alpaca
features spread these out.**

With live keys, run:

```bash
cd apps/agents && ../../.venv/bin/python -m tests.eval.run_eval --json
```

...and separately score ~200 real watchlist symbols through
`best_strategy`, printing `score` and `conviction` spread.

- **If the real spread is wide** (say >0.1 on conviction): ranking works,
  and `MIN_LLM_SCORE` becomes a usable dial. Set it so the top ~20 of a
  day's candidates clear it.
- **If it is still ~0.003:** ranking is near-arbitrary and `MIN_LLM_SCORE`
  is a switch, not a dial. Leave it unset and rely on the hard filters
  (chain depth, liquidity, spread) which have real dispersion.

Do not set `MIN_LLM_SCORE` before this measurement. A floor guessed too
high trades nothing, which is worse than spending a slot on a mediocre
setup.

---

## 7. 🟢 After the first option fill

One thing shipped in this session has **never executed against a real
broker**: the resting broker-side protective stop. Its order shape is
built from Alpaca's docs (`type` may be `stop`/`stop_limit` for
single-leg options) and has not been sent.

After the first option entry fills, check the logs:

```
option_stops: resting stop-limit on <OCC> — <n> @ stop $X / limit $Y
```

**Good:** that line appears, and the order shows in Alpaca as an open
stop-limit on the contract.

**If it does not appear:** the placement failed and was swallowed
deliberately — a stop that cannot be placed must never make a good fill
look like a failed one. Search for `option_stops: failed to place`. The
position still has the in-process software stop, so this degrades rather
than breaks, but the broker-side protection is not there.

---

## Summary table

| # | Check | Severity | Blocking? |
|---|---|---|---|
| 1 | Fresh $100k paper account | 🔴 | **Yes — eligibility** |
| 2 | Alpaca CLI running | 🔴 | **Yes — eligibility** |
| 3 | Options feed + quote timestamps | 🟠 | No, but gates §3 config |
| 4 | `ALLOW_OPTIONS`, `RISK_PROFILE` | 🟠 | Yes for options trading |
| 5 | LLM budget vars | 🟡 | No |
| 6 | Live dispersion measurement | 🟡 | No |
| 7 | First protective stop lands | 🟢 | No |
