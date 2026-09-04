# 5-Minute Video Script, The Refusal Ledger

**Format:** screen recording, you narrating over the live app.
**Rule:** every number you say is on screen. Never say a number you cannot point at.

---

## 0:00 to 0:25 · Hook (Dashboard)

> "Most trading agents show you what they bought. I want to show you something
> different. What mine **refused**, and whether refusing was right.
>
> This is a live Alpaca paper account, running unattended. Equity's about
> ninety-nine thousand two hundred, down roughly three quarters of a percent.
> I'm not going to pretend that's a result. It's two sessions, that's noise,
> and I'll come back to it honestly at the end.
>
> The interesting number is this one."

**Do:** point at Equity, then the **Risk saved** tile.

---

## 0:25 to 1:25 · The finding (Insights, Veto ledger)

> "Every time my risk engine blocks a trade, that decision vanishes. You never
> find out whether the rule saved you money or cost you money. Risk rules become
> folklore.
>
> So I built the counterfactual. Every refused trade gets marked to market on
> the same horizon it would have run, using **real Alpaca option quotes**, not
> synthetic prices. It never touches the broker. It's a paper twin of the trade
> I didn't take."

**Do:** click **Insights, Veto ledger.** Let the rules table fill the screen.

> "A hundred and eighteen refusals. A hundred and seven now carry real marks.
>
> My council confidence floor, the gate that says the agents aren't sure enough,
> blocked trades that would have lost close to **ten thousand dollars**. That
> rule is earning its keep.
>
> But my portfolio premium cap blocked trades that would have **made six
> thousand**. And that one's a problem, because it isn't refusing bad trades.
> It refuses when the book is full, regardless of how good the setup is. It's a
> capacity constraint doing a selection job.
>
> I could not have learned that from P&L. I learned it because I measured it."

**Do:** pause here. That's the thesis landing.

---

## 1:25 to 2:40 · The autonomous loop and how tool calls actually work

> "So how does it decide, with nobody watching? The rule is: **agents propose,
> deterministic code disposes.**"

**Do:** open **Picks**, click a symbol with a completed run. Show the stages.

> "A deterministic scanner sweeps every two minutes on ten named triggers.
> Donchian breaks, ATR expansion, z-score stretch. No model involved, costs
> nothing.
>
> Anything that fires goes to strategy fit, which scores five strategies in both
> directions, long and short, in pure Python. Still free. Most symbols die right
> here, and that matters, because screening is unlimited but **thinking is
> rationed**.
>
> Only what survives reaches two LLM agents, a bull and a bear. They argue the
> same feature dict independently and a deterministic resolver combines them.
> If they disagree or abstain, it holds and we've spent two calls, not a trade."

**Do:** show the bull/bear panel, then the tool transcript or funnel.

> "Now the important part. When they agree, the bull agent gets **tools**.
>
> Eight of them. Six are read only: fetch an option snapshot, pull underlying
> bars, read the current position, re-read its own entry thesis. Two can
> actually move money: `open_option_trade` and `adjust_option_position`.
>
> But here's the thing. **The agent cannot place an order.** It emits a tool
> call, and every single call is intercepted by a guard before it executes.
> That guard re-runs the entire deterministic risk stack from scratch:
> contract selection, liquidity, chain depth, delta band, premium caps, position
> sizing, trading level, drawdown halt. First veto wins.
>
> And when the guard refuses, it doesn't throw. It hands the model back a
> **named reason**, so the agent can adjust once and try again. The whole loop
> is capped at three rounds so it can't spiral.
>
> That's why I can run a cheaper model here. A weaker agent gives you worse
> **selection**. It cannot give you weaker **risk control**, because risk control
> was never the model's job."

---

## 2:40 to 3:15 · Alpaca CLI and cost discipline

> "Alpaca's own CLI is wired into the scheduler. Before any sweep runs, the
> agent shells out to `alpaca clock` to ask the broker directly whether the
> market is actually open, which catches early closes and unscheduled halts that
> a hardcoded calendar would miss.
>
> It's a subprocess with an argv list, never a shell string, killed on timeout,
> and it returns None on any failure. It's the **first link in a three step
> fallback chain**: CLI, then the REST API, then a local calendar. Each link
> reports its own source, so I can always tell you which one answered."

**Do:** briefly show Settings or the health panel if the source is visible.

> "And the whole thing is budgeted. Deterministic screening is unlimited, but
> paid model calls are capped at twenty symbols a day, four an hour, and three
> dollars. That's not a nice to have. I burned a ten dollar balance in one
> afternoon before those caps existed, and the ledger is what showed me where
> it went."

---

## 3:15 to 4:05 · It really trades (Positions)

**Do:** open **Positions.**

> "This is a live book. Long calls and long puts. A bearish thesis here is a
> long put, so the loss is bounded by construction. I deliberately don't write
> options. That's unbounded risk with no assignment handling, and it's out of
> scope on purpose.
>
> This one's an **equity short**, HD, six shares at three eighteen, bracketed
> with a protective buy stop above it. That fired for the first time today."

**Do:** point at NVDA 225C.

> "And this is the winner, NVDA calls, up about forty seven percent, being let
> run underneath a stop that ratchets up behind it."

**Do:** switch to **Closed.**

> "Two closes today, both stops firing at exactly their configured level. AAPL
> minus five thirty six, XLE minus six ten. One of those was a **broker side
> resting stop**. It lives at Alpaca, so it protects the position overnight even
> if my server is down."

---

## 4:05 to 4:35 · What measuring caught

> "The instrumentation didn't just produce a slide, it found real bugs.
>
> A stop that never fired, because the option's mark sat frozen for two hours
> and sixteen minutes, then gapped twenty six points in a single print. The stop
> was correct. The contract was too thin to have a price. Now a chain that can
> only produce one liquid contract gets refused before a single model call.
>
> A long put's real loss displaying as a profit, because the P&L sign was keyed
> to the bullish or bearish thesis instead of which side of the contract we
> actually hold.
>
> And a three percent drawdown halt that blocks new entries but closes nothing,
> so it never actually bounded the book's worst session. That's now an invariant
> enforced by a test."

---

## 4:35 to 5:00 · Close

> "So, am I claiming this agent makes money? No. Two sessions is noise, and I'd
> rather say that than dress it up.
>
> What I'm claiming is rarer. This agent can tell you, in dollars, **which of
> its own risk rules to delete.** It caught one of mine costing six thousand.
>
> A hundred and eighteen refusals, a hundred and seven priced against real
> quotes, every rule named and auditable, and the whole thing runs on about nine
> dollars of model spend.
>
> That's measurable, it's measured, and it survives contact with the data."

---

## Delivery notes

- **Two sections win this:** the ledger at 0:25, and the tool call / guard
  explanation at 1:25. If you overrun, cut from section 5, not those.
- **Say numbers loosely out loud** ("close to ten thousand"), let the screen
  carry the exact figure.
- **Lead with the honest P&L at 0:25 and again at 4:35.** Judges will check
  Alpaca. Naming it yourself reads as credible, not weak.
- The line that does the most work is *"the agent cannot place an order."*
  Slow down on it.
- If a tile reads "so far", say it: "these are mid-flight marks, not settled."
  Volunteering the caveat beats hoping nobody asks.
