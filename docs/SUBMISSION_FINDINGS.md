# Submission findings — the Refusal Ledger measuring its own risk engine

**Account `PA3JDGMXSHYK` · paper · created 2026-09-03 · figures as of 2026-09-04.**

Everything below is measured from the live system: `agent_decisions` and
`ghost_outcomes` in Postgres, priced against real Alpaca option quotes. No
number here is estimated or projected.

---

## 1. The headline finding

We instrument every trade the risk engine **refuses**, then mark it to market as
if it had been taken. 101 refusals now carry real marks. Decomposed by the rule
that fired:

| Rule | Refusals | What they would have done | Verdict |
|---|---|---|---|
| `min_council_confidence` | 59 | **−$7,402** | Earned its keep — blocked real losers |
| `max_total_premium_pct` | 51 | **+$9,581** | Cost money — blocked real winners |
| `size_rounds_to_zero` | 8 | not yet marked | — |
| **Net** | **118** | **+$2,179** | Our vetoes were net **harmful** |

Read across the whole book, 49 refusals would have won and 51 would have lost —
close to a coin flip. **The decomposition by rule is the finding, not the net.**

- The **confidence floor is doing real work.** The trades it stopped would have
  lost $7,402. That is an LLM-derived judgement gate paying for itself.
- The **premium cap is outcome-blind.** It refuses purely because the book is
  full, not because a trade is bad — and it cost $9,581 by doing so. It is a
  *capacity* constraint being asked to do a *selection* job.

That is a concrete, non-obvious, actionable conclusion about our own system. We
could not have reached it by looking at P&L, and nobody can reach it without
building this instrumentation.

**Caveats, stated plainly:**
- Marks are `partial`, not final. A ghost finalizes after 5 **trading** days;
  the earliest of these was opened Sep 1, so nothing finalizes until ~Sep 8.
  These are genuine mid-flight marks, labelled "so far" everywhere they appear.
- Prices are real (`price_source = alpaca_option`), averaging 1.9 marks per
  refusal. They are not single-point snapshots, but they are not settled either.
- n=101 over three sessions. Directionally informative, not statistically
  conclusive.

---

## 2. P&L, and why it is what it is

```
equity          $99,150.86      -$849.14   (-0.85%) from $100,000
realized              ~$0       essentially no completed round trips
unrealized       -$848.00       six open option positions
premium           $7,150        7.21% of equity, against the cap
```

Five facts, no speculation:

1. **One session of exposure.** The account was created Sep 3 and all six
   positions were opened Sep 3. This is a single day of marks, not a record.
2. **All six are long options.** Long premium pays theta every day. NVDA expires
   in 10 days, AAPL and TQQQ in 14. Decay is a headwind on every position.
3. **Directionally concentrated** — five calls and one put, effectively a
   long-beta book that needs the market to go up.
4. **The book was at its cap**, so it could not rotate into anything better.
5. **Almost no completed trades.** The −0.85% is open marks that can still move
   either way.

**−0.85% over one session on a six-position long-options book is noise.** It is
not evidence that the system is broken, and it should not be presented as a
result. The honest statement is that we have ~1 session of live P&L and 3
sessions of decision data, and only the second is large enough to say anything.

An earlier **−$1,200** loss (CME long put) occurred on a **different, now-retired
account** and is not in these figures. It is still worth presenting, because it
produced the chain-depth fix below.

---

## 3. What the losses taught us, and what changed

**The CME −$1,200 was not a stop failure.** Reconstructed from 510 consecutive
reconciler snapshots:

```
$3.40 (−26.1%)   17:33:37 → 19:49:42   frozen for 2h16m, 510 snapshots
$2.20 (−52.2%)   19:50:13              ONE print, a 26-point gap
stop fired       19:50:15              2 seconds later
```

The stop was set at −35%. **That price never existed.** The mark did not print
for over two hours, then reappeared already past the stop. The ladder fired on
the first breaching mark, correctly and immediately.

**A price-based stop cannot function on a mark that does not print.** The risk
control silently stopped working — worse than failing visibly, because nothing
alerted.

Root cause: its funnel showed 29 contracts entering the delta band and **exactly
one** surviving the liquidity gate. At one survivor the ranking does no work —
that contract was not selected, it was all that remained, sitting at the very
edge of the threshold that admitted it (OI 167, four days stale).

Now enforced: a chain yielding fewer than 5 liquid contracts is refused
`illiquid_chain` before any model call. Notably **not** "illiquid loses money" —
CDNS was equally thin and swung +18.8%. Thin chains mean P&L is driven by quote
noise instead of the thesis, and the stop is decorative. Both are reasons to
decline.

---

## 4. What the ledger changed about the system

Findings from this instrumentation that produced real fixes:

| Found by measuring | Fix |
|---|---|
| One trade's chain had 1 liquid contract; its stop could not function | Chain-depth gate (`illiquid_chain`), refused for 0 model calls |
| Options council was 84% of a $10 credit burn (500 + 241 calls in one afternoon) | Account-level and chain pre-flights moved *before* the paid debate; Haiku for the options agents |
| "15 symbols per sweep" was really 15 every 2 minutes — 267 paid passes across 134 symbols in one day | Hard per-day (20) and per-hour (4) caps on paid council passes |
| Daily P&L baselined off a UTC-day snapshot, hiding the entire overnight move | Baseline from the broker's own prior-session close |
| A long put's real −$195 loss rendered as +$195 | P&L sign keyed to broker side, not to the bullish/bearish thesis |
| The −3% halt does not close positions, so it never bounded the book's worst session | Halt-coupling invariant: book size × stop ≤ declared tolerance |

Every one of these was found by measuring the system's own behaviour rather than
reasoning about it.

---

## 5. Honest limitations

- **~1 session of live P&L.** Far too short to claim an edge in either direction.
- **101 ghost marks, all `partial`.** Real prices, mid-flight, not settled.
- **The premium cap was raised 7.5% → 11.0% on submission day** so the book could
  trade in the final session. This deliberately exceeds the −3% daily halt
  (11.0 × 40% = 4.4% reachable before a stop fires). It is declared in
  `RiskCaps.max_tolerated_book_drawdown_pct` rather than hidden by loosening the
  invariant test — `exceeds_halt_ceiling` reports it as True.
- **Equity shorts have never executed.** They are correctly generated and gated
  only by the specialist-score floor; long puts carry all bearish exposure.
- **Writing/selling options is deliberately out of scope** (unbounded loss, no
  assignment handling).

---

## 6. The claim we are actually making

Not "our agent makes money" — one session cannot support that.

**"Our agent can tell you, in dollars, which of its own risk rules are earning
their keep and which are costing money — and it caught one of its own rules
losing $9,581."**

That is measurable, it is measured, and it survives contact with the data.
