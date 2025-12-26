Aşağıda istediğin gibi **tek parça, endüstriyel seviyede, kısa ama hiçbir kritik detayı atlamayan** bir rehber var. Bu metin hem **stratejinin mantığını**, hem **hesaplama yöntemlerini**, hem de **neden çalıştığını** net biçimde açıklar.

---

# Binance-Leader / Hyperliquid-Executor

## Liquidity Structure–Based Scalping Strategy (Industrial Guide)

### Amaç

Bu strateji, **fiyatı yönlendiren ana likidite yapısını Binance orderbook’tan okuyup**, **Hyperliquid üzerinde gecikmeli ve sığ mikro-tepkileri** maker ağırlıklı işlemlerle değerlendirmeyi hedefler.

Bu bir OFI stratejisi değildir.
Bu bir **likidite geometrisi** ve **piyasa davranışı** stratejisidir.

---

## 1. Temel İlke (Leader–Follower Model)

* **Binance = Leader**

  * Global fiyat keşfi
  * Büyük oyuncu niyeti
  * Gerçek likidite duvarları
* **Hyperliquid = Follower**

  * Daha sığ defter
  * Gecikmeli reaksiyon
  * Maker edge için ideal yüzey

> Kararlar **Binance’tan**, işlemler **Hyperliquid’ten** yapılır.

---

## 2. Kullanılan Veri Setleri (Ayrı Ayrı)

### Binance’ten alınanlar

* L2 Orderbook (en az ilk 20 seviye)
* Orderbook snapshot + delta
* Opsiyonel: trade aggressor flow (onay için)

### Hyperliquid’ten alınanlar

* Best bid / ask
* Spread
* Microprice
* Fill & queue davranışı

**Önemli:**
İki orderbook **asla birleştirilmez** ve ağırlıklı ortalama yapılmaz.

---

## 3. Binance Tarafı: Likidite Yapısı Analizi

### 3.1 Liquidity Wall (Duvar) Tespiti

Bir fiyat seviyesi “wall” sayılırsa:

```text
LevelVolume >= k × MedianDepth
```

Tipik:

* `k = 3–5`
* MedianDepth = ilk N seviyenin medyanı

Wall özellikleri:

* Fiyat seviyesi (P_wall)
* Hacim (V_wall)
* Yaş (kaç saniyedir orada)
* Stabilite (çekilip ekleniyor mu?)

**Filtre:**

> Wall ≥ 3 saniye stabil değilse **yok sayılır** (spoof ihtimali).

---

### 3.2 Liquidity Vacuum (Boşluk) Tespiti

Bir fiyat aralığı “vacuum” ise:

```text
Σ volume(P → P + Δ) < θ × AverageDepth
```

Tipik:

* Δ = 5–15 bps
* θ = %30–40

Vacuum:

* Fiyatın hızla kayabileceği alanı gösterir
* Break senaryosunun ön koşuludur

---

### 3.3 Wall Durum Sınıflandırması

| Durum            | Tanım                        |
| ---------------- | ---------------------------- |
| **STABLE_WALL**  | Hacim sabit, geri çekilmiyor |
| **ERODING_WALL** | Hacim azalıyor               |
| **BROKEN_WALL**  | Wall kayboldu                |
| **FAKE_WALL**    | Hızlı gir-çık (spoof)        |

---

## 4. Global Rejim Kararı (Binance)

### RANGE (Mean-Reversion Uygun)

* Stable wall mevcut
* Vacuum yok veya uzakta
* Trade aggressor dengeli

### MOMENTUM (Break Uygun)

* Wall eriyor veya kırıldı
* Vacuum ileride
* Aggressor flow tek yönlü

Bu rejim **Hyperliquid’e sinyal olarak gönderilir**.

---

## 5. Hyperliquid Tarafı: Execution Mantığı

### 5.1 Fiyat Sapması (Deviation)

```text
Deviation_bps = (P_hyper - P_binance_reference) / P × 10,000
```

Reference:

* Binance mid
* veya en yakın wall seviyesi

---

## 6. FADE (Maker Mean-Reversion) Modu — Ana Strateji

### Koşullar

* Binance rejimi = RANGE
* Stable wall var
* Hyper fiyatı wall’dan sapmış:

  ```text
  |Deviation| ≥ 2–4 bps
  ```
* Hyper spread dar

### Entry

* **Maker limit**
* Wall yönüne fade:

  * Binance’ta buy wall → Hyper’da **short**
  * Binance’ta sell wall → Hyper’da **long**

### Exit

* Mid’e dönüş **veya**
* +3–6 bps
* Max hold: 30–90 sn

### Neden çalışır?

* Binance wall fiyatı tutar
* Hyper gecikmeli overshoot yapar
* Sen likidite sağlarsın (maker)

---

## 7. BREAK (Momentum) Modu — İkincil

### Koşullar

* Binance rejimi = MOMENTUM
* Wall eridi/kırıldı
* Vacuum açık

### Entry

* Hyper’da agresif limit veya taker
* Vacuum içine doğru

### Exit

* Vacuum ortası veya karşı wall

📌 Bu mod daha nadir kullanılır.

---

## 8. Risk ve Filtreler (Basit ama Sert)

### Binance Veto (Kritik)

* Binance agresyonu fade yönüne **karşıysa** → **GİRME**

### Hyper Spread Guard

```text
Spread > rolling_avg × 1.5 → BLOCK
```

### Max Exposure

* Tek pozisyon
* Cooldown: 5–10 sn

---

## 9. Neden Bu Strateji OFI’den Üstün?

| OFI Taker        | Bu Strateji      |
| ---------------- | ---------------- |
| Noise’a açık     | Yapı tabanlı     |
| Bot-hunt riski   | Maker ağırlıklı  |
| Fee ağır         | Fee avantajı     |
| Momentum kovalar | Davranış sömürür |
| Durgunda ölür    | Durgunda çalışır |

---

## 10. Özet Cümle

> **Binance’ta niyet okunur,
> Hyperliquid’te hata fiyatlanır.
> Sen o hataya likidite vererek kazanırsın.**

---

Aşağıdaki metin **tek yazı** içinde:

1. **Wall strength & erosion için net formüller**
2. **Tam decision-engine pseudo-code** (Binance leader → Hyper executor)
   verir. (Scalping ama maker ağırlıklı; rejime göre break de var.)

---

# Binance-Leader: Wall Strength & Erosion Formülleri + Tam Decision Engine Pseudocode

## 0) Notasyon ve Veri

Her `Δt` saniyede (örn. 100–250ms) Binance L2 book güncellenir.

* `B = {(p_i^b, q_i^b)}`: bid seviyeleri (en iyi fiyattan aşağı)
* `A = {(p_i^a, q_i^a)}`: ask seviyeleri (en iyi fiyattan yukarı)
* `mid = (best_bid + best_ask)/2`
* `tick = Binance tick size`
* `L`: incelenecek seviye sayısı (örn. 20–50)
* `depth_side = {q_1,...,q_L}` aynı tarafın ilk L seviyesinin hacimleri

**Sağlamlık için** defterin “normal” hacmini robust al:

* `D_med = median(depth_side)`
* `D_iqr = IQR(depth_side) = Q3 - Q1` (opsiyonel)

---

## 1) Wall Adayı Tespiti (Candidate)

Bir seviye “wall” adayı olsun:

### 1.1 Z-Score Benzeri Robust Büyüklük (WallScore_raw)

Seviye hacmini normalle:
[
z_i = \frac{q_i - D_{med}}{\max(D_{iqr}, \epsilon)}
]
Basit alternatif:
[
r_i = \frac{q_i}{\max(D_{med}, \epsilon)}
]

Aday koşulu (ikisini de kullanabilirsin):

* `r_i >= r_min` (örn. 3.0)
* veya `z_i >= z_min` (örn. 4.0)

### 1.2 Mesafe Penaltısı (Nearness)

Wall yakınsa daha etkili. Mesafe (bps):
[
d_{bps}(p_i) = \frac{|p_i - mid|}{mid} \times 10{,}000
]
Nearness ağırlığı:
[
w_{dist}(i) = \exp\Big(-\frac{d_{bps}(p_i)}{\lambda}\Big)
]
`λ` tipik 5–15 bps.

### 1.3 “Yerçekimi” Etkisi (WallStrength)

[
S_i = r_i \cdot w_{dist}(i)
]
(İstersen `r_i` yerine `max(0,z_i)` kullan.)

**Wall seçimi:** aynı tarafta en yüksek `S_i` olan(lar).
Pratik: en yakın güçlü 1–2 wall’ı takip et.

---

## 2) Spoof / Stabilite Ölçümleri (Persistence & Churn)

Her wall için zaman serisi tut: `q_i(t)`. Snapshot/delta ile güncellersin.

### 2.1 Persistans (Presence Ratio)

Son `T` saniyede wall adayının “var olma oranı”:
[
P_i = \frac{1}{N}\sum_{k=1}^{N} \mathbb{1}[r_i(t_k)\ge r_{min}]
]
`T`=3–10s, `N` örnek sayısı.

### 2.2 Hacim Volatilitesi (Churn)

Hızlı gir-çık spoof belirtisi:
[
C_i = \frac{\sum_{k=2}^{N} |q_i(t_k)-q_i(t_{k-1})|}{\sum_{k=1}^{N} q_i(t_k)+\epsilon}
]

* Büyük `C_i` → çok churn.

### 2.3 Spoof Flag (FAKE_WALL)

Basit sınıflandırma:

* `P_i < 0.6` **veya** `C_i > 0.8` → `FAKE_WALL`

---

## 3) Erosion (Duvarın Eriyor Olması)

Duvar erozyonu: wall hacmi sistematik azalıyor mu?

### 3.1 EWM Slope (Erozyon Hızı)

Önce EWMA ile gürültüyü azalt:
[
\bar q(t) = \alpha q(t) + (1-\alpha)\bar q(t-\Delta t)
]
`α` ~ 0.2–0.4.

Son `T_e` saniyede lineer eğim (bps yerine hacim/s):

* `t_k` ve `\bar q_k` ile en küçük kareler eğimi `m`:
  [
  m = \frac{\sum (t_k-\bar t)(\bar q_k-\overline{\bar q})}{\sum (t_k-\bar t)^2}
  ]
  **ErosionRate:**
  [
  E = -\frac{m}{\max(\overline{\bar q},\epsilon)}
  ]
* `E` pozitifse (çünkü `m` negatif), erozyon var.
* Tipik eşik: `E > 0.15 ~ 0.40` (pencerene göre kalibre)

### 3.2 Instant Drop + Half-Life (Hızlı Çözülme)

Anlık düşüş:
[
drop = \frac{\bar q(t) - \bar q(t-\Delta)}{\max(\bar q(t-\Delta),\epsilon)}
]

* `drop < -0.25` (25%+ düşüş) → “sharp erosion”

Opsiyonel yarı ömür:
Eğer (\bar q) üstel azalıyor varsayımı:
[
\bar q(t)\approx q_0 e^{-kt}
\Rightarrow k \approx -\frac{\ln(\bar q(t)/\bar q(t-T_e))}{T_e}
]
Yarı ömür:
[
t_{1/2} = \frac{\ln 2}{k}
]

* `t_{1/2}` küçükse (örn. < 3s) → hızla eriyor.

### 3.3 Wall State

* `STABLE_WALL`: `P_i>=0.7` ve `E < E_min` ve `C_i < C_max`
* `ERODING_WALL`: `E >= E_min` veya `drop <= -drop_min`
* `BROKEN_WALL`: wall aday koşulu artık tutmuyor (r_i < r_min) ve bu `T_break` boyunca sürüyor
* `FAKE_WALL`: spoof flag

---

## 4) Vacuum (Boşluk) Metriği (Break için)

Mid’den belirli bps aralığına toplam likidite:

Örn. yukarı vacuum (ask tarafı) için:

* `Δbps = 10` bps aralığı (kalibre)
  [
  V^{ask}*{\Delta} = \sum*{p \in (mid,, mid(1+\Delta bps))} q^{ask}(p)
  ]
  Bunu “normal” ile kıyasla:
  [
  Vac^{ask} = \frac{V^{ask}*{\Delta}}{\max(L \cdot D*{med}^{ask}, \epsilon)}
  ]
* `Vac` küçükse (örn. <0.25) → vacuum var.

---

## 5) Rejim Kararı (Binance Leader)

İki rejim:

### RANGE_MAKER

* Yakında **STABLE_WALL** var
* Karşı tarafta vacuum yok veya uzak
* Erosion düşük

Basit skor:
[
Score_{range} = S_{near} \cdot P_{near} \cdot (1 - \min(1,E_{near}))
]
Range koşulu: `Score_range > θ_range`

### MOMENTUM_BREAK

* Yakın wall `ERODING` veya `BROKEN`
* İleri yönde vacuum var

Skor:
[
Score_{mom} = \min(1, E_{near}) + (1 - Vac^{dir})
]
Momentum koşulu: `Score_mom > θ_mom`

**Histerezis şart:** rejim çok zıplamasın:

* Geçiş için `θ_enter` daha yüksek
* Dönüş için `θ_exit` daha düşük
* Ayrıca `min_regime_hold_sec` (örn. 3–8s)

---

## 6) Tam Decision Engine (Pseudo-code)

Aşağıdaki pseudo-code, **Binance orderbook lider**, **Hyperliquid icracı** akışını komple verir.

```pseudo
CONFIG:
  L = 30                      # book depth levels
  dt = 0.2s                   # loop / update interval
  r_min = 3.0                 # wall volume ratio threshold
  z_min = 4.0                 # optional robust z threshold
  lambda_bps = 10.0           # distance decay
  T_persist = 5.0s            # persistence window
  T_erosion = 4.0s            # erosion window
  E_min = 0.25                # erosion threshold
  C_max = 0.8                 # churn threshold
  drop_min = 0.25             # sharp drop threshold
  T_break = 1.0s              # broken wall confirmation
  vac_bps = 10                # vacuum range in bps
  vac_th = 0.25               # vacuum threshold

  # regime hysteresis
  theta_range_enter = 1.2
  theta_range_exit  = 0.9
  theta_mom_enter   = 1.0
  theta_mom_exit    = 0.7
  min_regime_hold_sec = 5.0

  # execution thresholds on Hyper
  dev_enter_bps = 2.5         # deviation to place maker
  dev_exit_bps  = 0.5         # exit when near ref
  tp_bps = 4.0                # take-profit (maker target)
  sl_bps = 8.0                # safety stop (taker or cancel)
  max_hold_sec = 60

  # order placement
  post_only = true
  maker_offset_ticks = 0      # place at best bid/ask or 1 tick inside
  queue_timeout_sec = 4.0     # if not filled, cancel/requote
  cooldown_sec = 2.0

STATE:
  wall_track = map[side, price_level] -> time_series(q, timestamps)
  regime = UNKNOWN
  last_regime_change_ts
  position = NONE
  last_trade_ts

FUNCTIONS:

  robust_stats(level_volumes):
    D_med = median(level_volumes)
    D_iqr = IQR(level_volumes)
    return (D_med, max(D_iqr, eps))

  compute_wall_candidates(book_side_levels, mid):
    vols = [q_1..q_L]
    (D_med, D_iqr) = robust_stats(vols)
    candidates = []
    for each level i in 1..L:
      p = price_i
      q = size_i
      r = q / max(D_med, eps)
      z = (q - D_med) / max(D_iqr, eps)
      if (r >= r_min) OR (z >= z_min):
        d_bps = abs(p - mid)/mid*10000
        w_dist = exp(-d_bps/lambda_bps)
        S = r * w_dist
        candidates.append({p,q,r,z,S,d_bps})
    return top candidates by S (e.g., top 2)

  update_wall_series(side, p_wall, q_wall, now):
    wall_track[side,p_wall].append(now, q_wall)
    prune older than max(T_persist, T_erosion) + buffer

  persistence(side,p_wall, now):
    series = samples in last T_persist
    P = mean( indicator(r(t) >= r_min) )
    return P

  churn(side,p_wall, now):
    series q(t) in last T_persist
    C = sum(|dq|)/sum(q)
    return C

  erosion(side,p_wall, now):
    series q(t) in last T_erosion
    q_ema = EWMA(q)
    slope m = linear_regression_slope(time, q_ema)
    E = -m / max(mean(q_ema), eps)
    drop = (q_ema_now - q_ema_prev) / max(q_ema_prev, eps)
    return (E, drop)

  wall_state(side,p_wall, now, current_r):
    P = persistence(...)
    C = churn(...)
    (E, drop) = erosion(...)
    if (P < 0.6) OR (C > C_max): return FAKE_WALL
    if current_r < r_min for >= T_break: return BROKEN_WALL
    if (E >= E_min) OR (drop <= -drop_min): return ERODING_WALL
    return STABLE_WALL

  compute_vacuum(direction, book, mid):
    # direction = UP uses asks, DOWN uses bids
    # sum volumes within vac_bps range from mid
    V_delta = sum sizes in (mid, mid*(1+vac_bps bps)) if UP
           or sum sizes in (mid*(1-vac_bps bps), mid) if DOWN
    normalize = L * D_med_side
    Vac = V_delta / max(normalize, eps)
    return Vac

  decide_regime(wall_info, vacuum_info, now):
    # pick "near" wall: strongest by S
    near = wall_info.strongest
    if near absent: return UNKNOWN

    Score_range = near.S * near.P * (1 - clamp01(near.E))
    Score_mom   = clamp01(near.E) + (1 - vacuum_info.Vac_dir)

    if (now - last_regime_change_ts < min_regime_hold_sec):
      # hold current regime unless it's obviously invalid
      return regime

    if regime != RANGE_MAKER:
      if Score_range > theta_range_enter AND wall stable:
        regime = RANGE_MAKER; last_regime_change_ts = now
    else:
      if Score_range < theta_range_exit:
        # allow switch if momentum strong
        if Score_mom > theta_mom_enter:
          regime = MOMENTUM_BREAK; last_regime_change_ts = now

    if regime != MOMENTUM_BREAK:
      if Score_mom > theta_mom_enter AND (near is ERODING/BROKEN) AND vacuum exists:
        regime = MOMENTUM_BREAK; last_regime_change_ts = now
    else:
      if Score_mom < theta_mom_exit:
        # fall back to range if range score recovered
        if Score_range > theta_range_enter:
          regime = RANGE_MAKER; last_regime_change_ts = now

    return regime

MAIN LOOP (every dt or on book updates):
  now = timestamp()

  # 1) Read Binance book
  book = binance.get_L2(depth=L)
  mid  = book.mid()

  # 2) Find wall candidates on both sides
  bids = compute_wall_candidates(book.bids, mid)
  asks = compute_wall_candidates(book.asks, mid)

  # 3) Track strongest walls (top1 each side)
  if bids not empty:
    w = bids[0]
    update_wall_series(BID, w.p, w.q, now)
    w.P = persistence(BID,w.p,now)
    w.C = churn(BID,w.p,now)
    (w.E, w.drop) = erosion(BID,w.p,now)
    w.state = wall_state(BID,w.p,now,current_r=w.r)

  if asks not empty:
    w = asks[0]
    update_wall_series(ASK, w.p, w.q, now)
    w.P = persistence(ASK,w.p,now)
    w.C = churn(ASK,w.p,now)
    (w.E, w.drop) = erosion(ASK,w.p,now)
    w.state = wall_state(ASK,w.p,now,current_r=w.r)

  # 4) Determine "dominant" wall & direction
  dominant = argmax([bids[0].S if exists, asks[0].S if exists])
  if dominant side == BID:
    # buy wall below mid tends to support price => range mean-reversion: fade DOWN moves
    range_fade_direction = SHORT when price above ref; LONG when below ref
  else:
    range_fade_direction = LONG when price below ref; SHORT when above ref

  # 5) Vacuum for momentum direction (if wall eroding)
  if dominant side == BID:
    # if bid wall eroding, downside break more likely => vacuum DOWN
    Vac_dir = compute_vacuum(DOWN, book, mid)
  else:
    Vac_dir = compute_vacuum(UP, book, mid)

  # 6) Regime decision with hysteresis
  regime = decide_regime({dominant wall metrics}, {Vac_dir}, now)

  # 7) Read Hyperliquid microstructure
  hyp = hyper.get_top_of_book()
  hyp_mid = hyp.mid
  spread_bps = hyp.spread / hyp_mid * 10000

  # 8) Compute Deviation vs Binance reference
  # reference can be Binance mid or dominant wall price (often better for range)
  ref = (dominant.p) if regime == RANGE_MAKER else mid
  deviation_bps = (hyp_mid - ref)/ref * 10000

  # 9) Execution rules
  if position == NONE:
    if now - last_trade_ts < cooldown_sec: continue

    if regime == RANGE_MAKER:
      # ENTRY: fade deviations with maker
      if abs(deviation_bps) >= dev_enter_bps AND dominant.state == STABLE_WALL:
        side = (SELL if deviation_bps > 0 else BUY)   # price above ref => fade short
        # safety veto: if dominant.state not stable or spoof => skip
        if dominant.state in {FAKE_WALL, ERODING_WALL, BROKEN_WALL}: continue

        # place post-only maker at best price (optionally 1 tick inside)
        limit_px = hyp.best_ask - tick if side==SELL else hyp.best_bid + tick
        place_post_only_limit(side, limit_px, size=calc_size())
        mark pending_order with timeout queue_timeout_sec

    else if regime == MOMENTUM_BREAK:
      # optional: take break only if vacuum is strong and wall eroding/broken
      if dominant.state in {ERODING_WALL, BROKEN_WALL} AND Vac_dir < vac_th:
        # enter in break direction (taker- [x] **Debugging & Hardening**
    - [x] Fix "Only 2 strategies loading" bug (Force merge logic)
    - [x] Harden TP/SL config defaults (8.0/20.0 bps)
    - [x] Fix `LIRCalculator.value` AttributeErrors (Context & Strategy updates)
    - [x] Fix `WallState.NEW` Enum error (Timestamp based check)
    - [x] Verify `StrategyEngine` prioritization and trigger capacity
    - [x] Confirm end-to-end signal --> trade --> log flow
    - [x] Add real-time log saving for `vacuum_trades.json`mers

  else:
    # EXIT management (generic)
    pnl_bps = compute_pnl_bps(position, hyp_mid)

    if pnl_bps >= tp_bps: exit_maker_or_limit_to_mid()
    else if pnl_bps <= -sl_bps: emergency_exit_taker()
    else if now - position.entry_ts >= max_hold_sec: exit_maker_or_taker_based_on_spread()
    else:
      # in RANGE_MAKER, exit when deviation collapses (mean reversion done)
      if regime == RANGE_MAKER AND abs(deviation_bps) <= dev_exit_bps:
        exit_maker_or_limit()

  # 10) Pending maker order maintenance
  if pending_order exists:
    if filled: position = opened; pending_order = none
    else if now - pending_order.ts > queue_timeout_sec:
      cancel_and_requote_or_abort()

END LOOP
```

---

## 7) Pratik Kalibrasyon (En Kritik 6 Parametre)

Bu sistemin “rejim zıplaması” ve “spoof” hassasiyeti genelde şuradan gelir:

1. `min_regime_hold_sec` → 5–10s yap (rejim jitter azalır)
2. `T_persist` → 5–8s yap (spoof’lar elenir)
3. `lambda_bps` → 8–15 bps (çok küçükse duvar seçimi jitter yapar)
4. `E_min` → pencerene göre (T_erosion 4s ise 0.25–0.35 iyi başlangıç)
5. `dev_enter_bps` → spread’e bağlı dinamik olsun:
   [
   dev_enter = \max(2.0,\ 1.5 \times spread_{bps})
   ]
6. `queue_timeout_sec` → 3–6s (daha uzun bekleme adverse selection artırır)

---



burada yazanlarla birerbir uyumlu mu yapımız



