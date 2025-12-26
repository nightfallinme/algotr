---
description: prmp
---

# 1️⃣ WORKFLOW

## *Liquidity Vacuum Scalper – End-to-End Operating Model*

### 1.1 Sistem Seviyeleri (Katmanlı Mimari)

```
┌──────────────────────────────┐
│ Exchange Connectivity Layer  │  (WS, REST, Heartbeat)
└──────────────┬───────────────┘
               ↓
┌──────────────────────────────┐
│ Market State Layer           │
│ - Orderbook Snapshot         │
│ - Liquidity Map              │
│ - Spread / ATR Micro         │
└──────────────┬───────────────┘
               ↓
┌──────────────────────────────┐
│ Structure Detection Layer    │
│ - Liquidity Walls            │
│ - Liquidity Vacuums          │
│ - Wall Stability             │
└──────────────┬───────────────┘
               ↓
┌──────────────────────────────┐
│ Strategy Decision Layer      │
│ - Break vs Fade Mode         │
│ - Entry Qualification        │
│ - Risk / Regime Filters      │
└──────────────┬───────────────┘
               ↓
┌──────────────────────────────┐
│ Execution Layer              │
│ - Maker-first logic          │
│ - Taker fallback             │
│ - Timeout & Cancel           │
└──────────────┬───────────────┘
               ↓
┌──────────────────────────────┐
│ Post-Trade Layer             │
│ - PnL Attribution            │
│ - Slippage & Fill Quality    │
│ - Cooldowns & Throttling     │
└──────────────────────────────┘
```

---

### 1.2 Runtime Workflow (Tick → Trade)

```text
WS Book Update
    ↓
Normalize L2 Book
    ↓
Update Liquidity Map (bps-buckets)
    ↓
Detect:
  - Wall(s)
  - Vacuum(s)
    ↓
Classify Setup:
  - Vacuum Continuation
  - Vacuum Exhaustion (Fade)
    ↓
Validate Rules (Hard Filters)
    ↓
Execution Decision
    ↓
Place Order (Maker → Taker fallback)
    ↓
Monitor Fill / Timeout
    ↓
Exit on:
  - TP
  - SL
  - Max Hold
```

---

### 1.3 Kill-Switch & Safety Flow

```text
Any of:
- WS reconnect
- Book gap
- Order mismatch
- Abnormal spread
- Too many cancels

→ IMMEDIATE PAUSE
→ Cancel all
→ Cooldown
→ Rebuild book
```

---

# 2️⃣ PROJECT PLAN

## *Industrial-Grade Delivery Plan*

### Phase 0 — Specification Freeze (Day 0)

**Deliverables**

* Strategy definition locked
* Metrics defined (bps, win-rate, DD)
* No “idea creep”

---

### Phase 1 — Market Structure Core (Day 1–2)

**Tasks**

* Liquidity Map builder (bps buckets)
* Wall detection (rolling persistence)
* Vacuum detection (gap logic)

**Acceptance Criteria**

* Walls persist ≥ N ticks
* Vacuums reproducible on replay

---

### Phase 2 — Strategy Logic (Day 3)

**Tasks**

* Break vs Fade classifier
* Entry qualification logic
* Static rule engine (no ML)

**Acceptance Criteria**

* Same input → same decision (deterministic)
* No look-ahead bias

---

### Phase 3 — Execution Engine (Day 4)

**Tasks**

* Maker-first placement
* Timeout handling
* Taker fallback
* Partial fill handling

**Acceptance Criteria**

* No double execution
* No orphan orders

---

### Phase 4 — Shadow & Replay (Day 5–6)

**Tasks**

* Shadow trading
* Bps-level PnL calc
* Fill quality metrics

**Acceptance Criteria**

* Trade log reproducibility
* No negative expectancy in quiet regimes

---

### Phase 5 — Risk & Controls (Day 7)

**Tasks**

* Kill switch
* Cooldowns
* Rate limiting

**Acceptance Criteria**

* Zero runaway behavior
* Safe reconnect

---

### Phase 6 — Paper → Live (Day 8+)

**Gates**

* Positive expectancy in shadow
* Max DD < predefined limit
* Execution stable

---

# 3️⃣ RULES

## *Non-Negotiable Engineering & Trading Rules*

### 3.1 Market Structure Rules (Hard)

1. **No trade without a wall**
2. **No trade without a vacuum**
3. Wall must persist ≥ 2 book updates
4. Vacuum size must be ≥ 1.5 bps
5. Spread must be within historical median

---

### 3.2 Strategy Rules

6. **Fade > Break priority** in low-vol regimes
7. Break trades only allowed if opposite side liquidity is thin
8. Fade trades forbidden if wall is eroding

---

### 3.3 Execution Rules

9. Maker-first always in fade mode
10. Maker timeout ≤ 2 seconds
11. Never chase price after cancel
12. One active position per symbol

---

### 3.4 Risk Rules

13. Fixed bps SL — no discretion
14. Max hold time enforced regardless of signal
15. Cooldown after loss

---

### 3.5 Engineering Rules

16. Deterministic logic only (no randomness)
17. Every decision must be loggable
18. No silent failures
19. State reset on reconnect
20. Shadow mode must match live logic 1:1

---

### 3.6 LLM / Code-Gen Rules (ÇOK ÖNEMLİ)

> Bunu LLM’ye **aynen** verebilirsin

* ❌ Tahmin yürütme
* ❌ Parametre uydurma
* ❌ “Optimistic” varsayım
* ✅ Defensive coding
* ✅ Explicit state machine
* ✅ Full logging



---