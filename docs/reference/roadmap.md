Where I think we're ultimately going

Your end product isn't really a "stock screener."

It's an:

Autonomous Daily Trading System

Something like:

                         MARKET
                           │
             ┌─────────────┴─────────────┐
             │                           │
        Market Data                  Alternative Data
        Price / Volume               News / SEC / Earnings
             │                           │
             └─────────────┬─────────────┘
                           │
                    MARKET STATE
                           │
             ┌─────────────┴─────────────┐
             │                           │
       Fundamental Engine          Opportunity Engine
       "Is it a good business?"     "Is something happening?"
             │                           │
             └─────────────┬─────────────┘
                           │
                    STRATEGY ENGINES
                           │
       ┌───────────┬───────┼────────┬──────────┐
       │           │       │        │          │
   Momentum     Breakout  Mean     Earnings   Reversal
                          Rev.
       │           │       │        │          │
       └───────────┴───────┼────────┴──────────┘
                           │
                     AGENT ENSEMBLE
                           │
                ┌──────────┴──────────┐
                │                     │
          Risk Management        Portfolio
             Agent               Manager
                │                     │
                └──────────┬──────────┘
                           │
                    TRADE DECISION
                           │
                    EXECUTION ENGINE
                           │
                       BROKER
                           │
                    Alpaca / etc.
                           │
                     POSITIONS
                           │
                           ▼
                      MONITORING
                           │
                           ▼
                     LEARNING LOOP

And eventually it should operate without you manually saying:

"Find me some stocks."

Instead, every trading day it should wake up, understand the market, generate opportunities, debate them, manage risk, execute, and monitor the positions.

But I would NOT jump there yet

The biggest mistake we could make is:

"Let's build 12 AI agents."

That's how you end up with an impressive-looking system that loses money.

We need to build it bottom-up.

The agents come relatively late.

The roadmap I'd use
PHASE 7 — Fundamental / Foreign Issuer Infrastructure

Status: FROZEN ✅

You've already done this.

You now have:

IFRS
US-GAAP
foreign issuers
currency separation
FX conversion
cadence-aware periods
filing-instance fallback
freshness
liquidity
provider market caps
financial institution detection
historical reproducibility

And critically:

The financial numbers are now trustworthy enough to build on.

Don't touch this casually.

PHASE 8 — Market Opportunity Engine
Objective:

Fix the exact problem you just identified:

"The recommendations are fundamentally good but dead."

This phase should answer:

Where is the action today?

8A — Market behavior audit

Before changing anything:

Measure the universe for:

ATR%
realized volatility
1D return
3D return
5D return
10D return
20D return
momentum acceleration
relative volume
volume expansion
gap %
distance from 20D high
distance from 52W high
breakout status
range compression
volatility expansion

Then segment the universe:

DEAD
LOW ACTIVITY
NORMAL
ACTIVE
HIGH VOLATILITY
EXPLOSIVE

This is where I would start right now.

8B — OpportunityScore

Build a completely separate score.

Potential architecture:

OpportunityScore
│
├── Momentum
├── Momentum Acceleration
├── Relative Volume
├── Volatility
├── Volatility Expansion
├── Breakout
├── Price Structure
└── Catalyst

But we don't choose the weights yet.

We first see what the data says.

8C — Setup classification

Instead of merely ranking stocks, classify setups:

BREAKOUT
Near resistance
+
volume expansion
+
positive momentum
+
volatility expansion
MOMENTUM
Strong multi-day trend
+
persistent relative strength
+
healthy volume
REVERSAL
Oversold
+
volume spike
+
reversal confirmation
EVENT
earnings/news/catalyst
+
unusual volume
+
price reaction
VOLATILITY
ATR expansion
+
range expansion
+
unusual volume

Now your UI becomes much more useful.

Instead of:

"Top 20 stocks"

you get:

🔥 Breakouts
🚀 Momentum
⚡ High Volatility
📰 Catalyst Plays
↩️ Reversals

That's much closer to a trader's product.

PHASE 9 — Strategy Engine

Now we create actual independent strategies.

This is where your earlier idea of multiple strategies becomes important.

I would start with perhaps:

Strategy 1 — Momentum

Your existing 12-1 momentum research becomes the long-term baseline.

But we'll eventually develop a short-term momentum strategy.

Strategy 2 — Breakout

Detect:

consolidation
resistance
volume expansion
breakout
confirmation
Strategy 3 — Momentum Continuation

Something like:

strong trend
+
pullback
+
volume contraction
+
reclaim
Strategy 4 — Mean Reversion

Find:

extreme deviation
+
oversold/overextended
+
reversal confirmation
Strategy 5 — Earnings

Specialized around:

earnings surprise
guidance
gap
volume
post-earnings continuation/reversal
Strategy 6 — Catalyst

News/SEC/event-driven.

Strategy 7 — Volatility Expansion

Find stocks transitioning from:

quiet → active

This one could be especially valuable for what you're describing.

PHASE 10 — Backtesting Infrastructure

This is one of the most important phases.

Before letting agents trade real money, every strategy needs to survive historical testing.

We need institutional-quality protections against:

Look-ahead bias

Never allow future information into a historical decision.

Survivorship bias

Don't only test today's winners.

Data leakage

News/earnings/fundamentals must use the information available at that exact timestamp.

Slippage
expected price
≠
execution price
Fees

Include them.

Liquidity

A strategy shouldn't look amazing because it theoretically bought something nobody could actually trade.

Market impact

Eventually.

PHASE 11 — Strategy Tournament

Now we let strategies compete.

Each strategy gets a standardized report:

Strategy
──────────────
CAGR
Sharpe
Sortino
Max Drawdown
Win Rate
Profit Factor
Avg Win
Avg Loss
Turnover
Exposure
Volatility
Capacity

But we shouldn't simply pick the highest return.

A strategy making:

+80% with −65% drawdown

may be far worse than:

+35% with −12% drawdown.

PHASE 12 — Multi-Agent Decision Engine

Now we bring in your agents.

Your original AlphaTrader concept becomes useful here.

I'd evolve it into something like:

                    TRADE CANDIDATE
                           │
          ┌────────────────┼────────────────┐
          │                │                │
      Technical         Fundamental       Catalyst
        Agent              Agent             Agent
          │                │                │
          └────────────────┼────────────────┘
                           │
                    Strategy Agents
                           │
          ┌────────────────┼────────────────┐
          │                │                │
       Momentum         Breakout        Mean Reversion
          │                │                │
          └────────────────┼────────────────┘
                           │
                    Debate / Consensus
                           │
                    Risk Management
                           │
                  Portfolio Management
                           │
                     EXECUTE / REJECT

And importantly:

Agents should NOT all have equal authority.

You were already moving toward this with your 12-agent architecture.

I'd preserve the idea that:

Risk Management + Portfolio Management have veto authority.

PHASE 13 — Portfolio Construction

This is where we stop thinking:

"Is this stock good?"

and start thinking:

"Should the portfolio own this stock?"

Questions:

Position size?
Correlation?
Sector exposure?
Market regime?
Existing positions?
Portfolio volatility?
Maximum drawdown?
Kelly fraction?
Stop distance?
Expected return?
Expected risk?

Your existing volatility-targeting/Kelly/VIX scaling ideas belong here.

PHASE 14 — Execution Engine

Separate decision from execution.

This is extremely important.

Decision:
BUY 500 shares


↓


Execution Engine:


Can we actually execute this?


↓


Order type
Price
Spread
Liquidity
Slippage
Market state
Partial fill
Retry
Cancel

Eventually:

limit orders
market orders
bracket orders
partial fills
retry logic
order reconciliation
broker failures
idempotency
PHASE 15 — Live Paper Trading

Absolutely no immediate jump to real money.

Run the entire autonomous system in paper mode.

For example:

30–60 trading days

Record:

What did the system want to buy?
What did it actually buy?
Why?
What was the expected setup?
What happened?
What would have happened without execution constraints?

This becomes enormously valuable.

PHASE 16 — Shadow Mode

Now the system is connected to the real broker but cannot trade.

It generates:

BUY 100 XYZ

but doesn't submit it.

We compare:

System decision
vs
actual market outcome

This catches operational bugs that backtesting can't.

PHASE 17 — Controlled Live Capital

Then:

$100
↓
$500
↓
$1,000
↓
$5,000
↓
...

with hard risk limits.

Not because we expect the system to magically scale.

Because operational reliability needs to be proven with real fills.

PHASE 18 — Autonomous Learning

Only after everything above.

Then your original "Learning Agent" becomes meaningful.

It can analyze:

Prediction
      ↓
Trade
      ↓
Outcome
      ↓
Prediction error
      ↓
Strategy attribution
      ↓
Parameter / model evaluation

But it should not be allowed to arbitrarily rewrite the trading system.

That's dangerous.

Instead:

Learning Agent
      ↓
Proposes change
      ↓
Backtest
      ↓
Walk-forward test
      ↓
Paper validation
      ↓
Human approval
      ↓
Production

Eventually you can automate even that approval process with strict promotion gates.

PHASE 19 — Production Trading Platform

Then we're talking about the actual product you described.

Something like:

                    ┌─────────────────────┐
                    │    DATA PLATFORM    │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   MARKET REGIME     │
                    │      ENGINE         │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
        Fundamental       Opportunity       Catalyst
          Engine             Engine            Engine
              │                │                │
              └────────────────┼────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │ STRATEGY ENSEMBLE   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  AGENT CONSENSUS     │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   RISK ENGINE       │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ PORTFOLIO MANAGER   │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ EXECUTION ENGINE    │
                    └──────────┬──────────┘
                               │
                           BROKER
                               │
                    ┌──────────▼──────────┐
                    │ MONITORING / AUDIT  │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ LEARNING / RESEARCH │
                    └─────────────────────┘

That is the industry-level architecture I would aim for.