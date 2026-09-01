# Shorts and bearish scenarios — where they actually stand, for the next model

**Written after a live investigation on 2026-09-01, ~15:00-16:00 UTC, against the
running production system and its real Postgres/Alpaca data — not from memory, not
from reading code alone.** Every claim below is marked VERIFIED (checked live this
session, with the exact evidence), CODE-CORRECT (read carefully, matches every
sibling pattern, but never yet exercised by a real trade), or OPEN (a real, unresolved
question — do not silently resolve it either way).

Read this before touching anything short-related. The single biggest risk here is
re-deriving a wrong mental model and "fixing" something that already works, or
loosening a threshold that is doing its job on a thin sample.

---

## 0. The two things "short" means in this codebase — do not conflate them

1. **A bearish THESIS.** `strategy_fit` picks `direction: "long" | "short"` from
   pure technicals — this is a belief about which way price goes, nothing about
   which side of a contract you hold.
2. **Being short AT THE BROKER.** Actually selling shares you don't own (equity),
   or writing/selling an option (calls or puts) instead of buying one. This is
   `side == "SELL"` on the entry, and it is what `forbid_short_phase_0` /
   `shortable_check` / `short_requires_stop` gate.

**These are independent.** A bearish thesis on an equity IS implemented as
`side=SELL` (a real short sale) — #1 and #2 coincide there. A bearish thesis on an
OPTION is implemented by **buying a put** (`side=BUY`, `direction="short"`) — #1 and
#2 diverge completely. Phase A (`RiskCaps.options_disabled`'s own docstring: "long
calls/puts only, no spreads/assignment") never sells an option, so for every options
row in this system, `side` is always `BUY` regardless of what `direction` says.

This distinction is exactly what caused today's two live incidents in this exact
area (both already fixed, see `fable5findings.md`'s 2026-09-01 entries):
- `positions_service._from_decision` flipped a position's P&L sign off `direction`
  instead of `side` — a long put's real loss rendered as a profit.
- The Positions UI badged a long put "SHORT" (the thesis) while Alpaca correctly
  called the same position "Long" (the broker side) — a genuine, confusing collision
  between two different, correct facts about the same row.

**If you take one thing from this document: everywhere you are about to write
`if direction == "short"`, ask "do I mean the thesis, or do I mean short-at-the-
broker?" They are almost never the same question for an option.**

---

## 1. VERIFIED WORKING: bearish options theses (long puts)

Four real, live fills on 2026-09-01, all correct, all matching Alpaca exactly:

```
META260918P00585000  BUY  1 @ $20.55   (direction=short, side=BUY, buy_to_open)
AMD260918P00457500    BUY  1 @ $17.80   (direction=short, side=BUY, buy_to_open)
CDNS260918P00320000   BUY  2 @ $11.20   (direction=short, side=BUY, buy_to_open)
CME261016P00270000    BUY  5 @  $4.60   (direction=short, side=BUY, buy_to_open)
```

Each one went: `strategy_fit` resolves `direction=short` → options council
(`options_bull`/`options_bear`) debates and agrees → `ToolGuard` clears the full
risk stack → `open_option_trade` places the order → fills. This is the intended,
complete mechanism for a bearish options bet in this system, and it is working.

**If the ask is "make bearish option trades happen," this is already done.** Do not
rebuild it. If the ask is specifically "sell/write options instead of buying puts,"
that is §4 below — a different, unbuilt, higher-risk capability.

---

## 2. VERIFIED BLOCKED: equity shorts (real short sales) — and exactly why

**Zero equity short-sale orders (`side=SELL` on shares) have ever reached the
broker.** Not vetoed at the broker, not vetoed by a short-specific risk rule —
they never get past the Drafter's specialist-score floor to be DRAFTED at all.

Every pure-equity `direction=short` decision observed today, with its technical
score against the 40-point floor (`RiskCaps.min_specialist_avg_score`,
`prompts/drafter.py`'s injected floor — see `fable5findings.md`'s
`f1e095c6b`/`c6ece604` entries for that fix):

```
symbol  score   floor   result
PG      38.0     40      HOLD — "falls below the 40-point hard floor"
MCD     28.0     40      HOLD — "falls below the 40-point hard floor, mandating a HOLD"
ADBE    38.0     40      HOLD — "falls below the 40-point hard floor"
```

Small-sample comparison against the same day's LONG candidates:

```
direction  n   avg score   min   max
short      3     34.7      28     38    <- every one below the floor
long       4     58.5      38     72    <- only 1 of 4 below the floor
```

**n=3 is not enough to conclude a structural bias.** Two explanations are both
live possibilities and this document does not resolve which:

  (a) **The tape was genuinely bullish today** (`RISK ON` / `BULL` badge visible
      in the UI most of the session) — a bullish tape produces weak bearish
      setups and strong bullish ones BY CONSTRUCTION, and a floor doing exactly
      that is correct, not broken.
  (b) **The Haiku technical analyst is behaviorally asymmetric** despite an
      explicitly, deliberately symmetric prompt (`prompts/technical_analyst.py`
      literally says *"Score UP, symmetrically with the flags below... or the
      equivalent downtrend for a short"* and *"the same way a contradicting
      signal would be scored down"*). The rubric is not the problem if this is
      real; the model's calibration against that rubric would be.

**Do not resolve this by lowering the floor.** The user explicitly declined that
this session ("leave floors where they are... today's tape is genuinely weak"). The
correct next step is gathering more samples across more sessions/days and comparing
long vs. short score distributions with real statistical power — see §5.

---

## 3. VERIFIED NOT the blocker (checked live, ruled out)

Three things that looked like plausible suspects and were checked directly against
the live account rather than assumed:

- **`shortable_check`.** This rule vetoes on `shortable is None` OR
  `easy_to_borrow is None` — "unknown flags veto" by design
  (`risk/rules/shortable.py`). The upstream feature block that populates these
  (`AlpacaAssetInfoProvider` in `engine/features/provider.py`) is marked
  *OPTIONAL* on the feature-dict type, which is exactly the shape of bug that has
  bitten this repo before (an optional block silently never populating, see
  CLAUDE.md §4.6's dashboard example). **Tested live against the real account,
  2026-09-01, for every symbol with a short-direction fit today plus BAC/JPM:**

  ```
  BAC/JPM/PG/MCD/GE/C/AXP/ADI/DDOG/DAL/AAL
  → every single one: tradable=True shortable=True easy_to_borrow=True
  ```

  This gate is not blocking anything right now. Verify it again periodically —
  it is a live broker call and could regress — but it is not today's answer.

- **`forbid_short_phase_0`.** `ALLOW_SHORTS=1` is set on Railway; this rule
  self-gates out and is not in the veto path. Confirmed via
  `RiskCaps.from_env().shorts_enabled == True`.

- **`short_requires_stop`'s sizer input.** `packages/engine/engine/sizing/atr.py`'s
  `_sign(side)` returns -1 for a short, and `stop_price = last_price - sign *
  distance` correctly places the stop ABOVE entry with the target below for a
  short. Read carefully, matches the equity-long case's structure exactly,
  untouched by anything changed this session. CODE-CORRECT, not yet exercised.

---

## 4. NOT BUILT, BY DELIBERATE DESIGN: selling/writing options

If "make option shorts work" means **selling a call or a put** (collecting
premium, i.e. `SELL_TO_OPEN` an option) rather than buying a put for a bearish
thesis — **that does not exist in this codebase and should not be built without a
deliberate, separate decision.** This is not an oversight; it is Phase A's stated
boundary:

> `RiskCaps.options_disabled`: "Phase A: long calls/puts only, no spreads/
> assignment." `options_min_trading_level`: "level 1 = assignment-bearing
> structures (covered call / cash-secured put, Phase C); level 2 = long call/put
> (Phase A, what this gates); level 3 = spreads/straddles (Phase B)."

Why this is a materially bigger decision than "flip a flag":

- **A naked short call has unbounded loss** — worse than an equity short, because
  there is no shares-outstanding ceiling and the leverage is already baked into
  the contract.
- **Assignment risk.** Selling an option means the counterparty can exercise
  against you at any time (American-style, which these are) — the system has
  zero assignment-handling code anywhere. `pdt_ledger`, `position_manager.py`'s
  close paths, the ratchet — none of it models "this position can vanish and be
  replaced by 100x shares overnight."
- **Different account trading level required.** The account is currently level 3
  (spreads-capable) but nothing in `options/tools/trade.py` or `guard.py` builds
  a `SELL_TO_OPEN` order — the tool literally only ever emits `buy_to_open`
  (grep `optionAction` in `trade.py` — it's a constant, not a branch).
- **A short PUT (cash-secured) is the least risky version** of this and would be
  the natural first step if this is genuinely wanted — bounded loss (strike minus
  premium), collects premium instead of paying it. Still needs: a trading-level
  check, `naked_short_forbidden`-equivalent guard (CLAUDE.md already forbids
  relaxing `naked_short_forbidden` — "unbounded loss, no assignment handling"),
  margin/collateral sizing distinct from the long-option sizer, and an
  assignment-aware close path.

**If this is genuinely wanted, it is a multi-day workstream on the scale of the
original options build (`IMPL_OPTIONS_AGENTS.md`), not a bug fix.** Do not build
it as a quiet side effect of "fixing shorts." Confirm explicitly with the user
first — they have been asked to weigh in on every risk-widening change this
session and should be asked here too.

---

## 5. DECIDED (2026-09-01, same session, after this doc first shipped): leave it

Asked the user directly, given exactly the choice laid out above: leave the
floor alone, loosen it asymmetrically for shorts, or wait for more data. Answer:
**leave it — bearish exposure via long puts (§1) is enough; do not lower the
floor for equity shorts.**

This closes the "should we loosen anything" question. It does NOT close the
"is there a real model-calibration asymmetry" question in §2/§5.4 below — that
is still worth investigating on its own, with more data, because the answer
would inform prompt work elsewhere too. But do not act on it by touching any
threshold without asking again; the user has now said no once already.

## 5a. What the next model should actually do, in order

1. **Do not touch `min_specialist_avg_score` or `min_council_confidence`.**
   Declined explicitly this session — see §5's DECIDED note above. If you
   think it needs revisiting, ask — don't decide it.

2. **Instrument the long-vs-short score gap properly.** Add a query (or a small
   script, mirroring `docs/HACKATHON.md` §5's "dump the funnel" pattern) that
   pulls `technical_score` grouped by `selected_direction` over a real multi-day
   window once one exists — n=3 today proves nothing. If a real, sustained gap
   shows up (e.g., short avg consistently >15pts below long avg on comparable
   setup quality), that is evidence of real model asymmetry worth its own
   investigation — a prompt-engineering or model-choice question, not a risk-cap
   one.

3. **Get one equity short to actually fire, on paper, and watch what happens
   next.** Nothing has exercised the live order-submission path
   (`drafter.py`'s `side="SELL" if direction=="short"` → `executor.py`'s bracket
   construction → `broker.place_order`) or the close path for a short beyond
   what `position_manager.py`'s own comment already documents was found and
   fixed (see `apps/api/app/services/orders/position_manager.py:1074`'s "The
   observable failure was worse than silent" comment — a short could never be
   closed at all before that fix). That fix is in place; it has still never been
   proven against a REAL short position, only reasoned about. The fastest way to
   get real coverage: manually construct a qualifying short-direction setup
   (either wait for the tape to produce one, or build a test harness that drives
   `daily_cron` against a symbol with synthetic bearish features scoring above
   the floor) and watch it go end to end. Do this before touching any other code
   here — it is the single highest-value verification available.

4. **If the long-vs-short gap turns out to be real model behavior**, the fix is
   in the PROMPT or the MODEL, not the risk engine. Candidates, cheapest first:
   - Add 2-3 concrete worked examples to `prompts/technical_analyst.py` scoring
     a strong SHORT setup in the 65-84 band, mirroring the long-side anchors
     already there — the prompt currently states the symmetry principle but
     gives zero worked bearish examples at the top of the scale.
   - A/B the same feature dict through the model with `direction=long` vs.
     `direction=short` synthetically flipped (mirror the numbers) and compare
     scores — this isolates model behavior from real market asymmetry
     completely, and can be done in an afternoon without waiting on live tape.

5. **Only after 2-4 above, if explicitly asked:** scope selling options
   (§4) as its own plan doc, following this repo's `IMPL_*.md` convention —
   verified facts, revert-check matrix, where the implementer will go wrong —
   the same standard this document was held to.

---

## 6. Revert-check matrix (for anything you change here)

| If you change | Break it like this to prove your test catches it |
|---|---|
| Anything using `direction` for a SIGN computation on an option | Use `direction=="short"` instead of `side=="SELL"` and confirm a long-put P&L test fails with the flipped sign — this is the exact bug from §0, already fixed once in `positions_service.py`; do not reintroduce the pattern elsewhere (`drafter.py`'s uses of `direction` are for DECIDING order side at submission time and are correctly scoped — do not "fix" those). |
| `shortable_check` | Force `proposal.shortable = None` and confirm a short is vetoed with `shortable_check`, not silently passed. |
| The scoring-symmetry investigation (§5.4) | If you add worked short examples to the prompt, confirm via a mocked LLM response test that a strong bearish setup can score into the 65-84 band, not just that the prompt text changed. |
| Anything in `options/tools/trade.py` toward selling options | Do not touch `optionAction`/`buy_to_open` without first landing `naked_short_forbidden`'s enforcement and an assignment-aware close path — a test that submits a `SELL_TO_OPEN` and expects it BLOCKED absent those must exist and pass before this ships. |

---

## 7. Where you will go wrong

- **Concluding "shorts are broken" and started debugging the risk engine.**
  They are not broken; nothing has been PROPOSED to break yet (equity), and the
  options path already works. The floor is doing exactly what it's specified to
  do — the open question is only whether the specialist scoring itself is fair
  between directions, which needs more data, not more code.
- **Lowering `min_specialist_avg_score` to "get a short to fire."** That is the
  quality floor the user explicitly kept in place this session weighing it
  against today's -3.67% incident. If you lower it, you are not fixing a bug —
  you are making a risk decision that isn't yours to make silently.
- **Building option-selling because "option shorts" sounds like it should mean
  that.** Read §0 and §1 first. The bearish-options mechanism (long puts)
  already exists and already fires. Confirm explicitly before building anything
  that sells options — see §4 for exactly why that is a much bigger decision.
- **Trusting the n=3 sample in §2 as proof of anything.** It is evidence worth
  investigating, not a conclusion. Say so if you cite it.
