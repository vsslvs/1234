# План реализации 10 улучшений Maker-бота

## Улучшение 1: Order Book Imbalance (OBI)

### Цель
Использовать 20 уровней ордербука Binance для предсказания направления BTC.

### Файлы
- `ws_orderbook.py` — добавить вычисление OBI
- `market_calculator.py` — интегрировать OBI в p_up_signal()
- `bot_state.py` — добавить obi в dashboard state
- `config.py` — добавить OBI_WEIGHT

### Шаги

**Шаг 1.1: ws_orderbook.py — добавить свойство order_book_imbalance**

В класс `OrderBook` добавить метод:

```python
@property
def imbalance(self) -> float:
    """
    Order book imbalance from top 5 levels: (bid_vol - ask_vol) / (bid_vol + ask_vol).
    Returns [-1, +1]. Positive = more buyers, negative = more sellers.
    """
    bid_vol = sum(qty for _, qty in self.bids[:5]) if len(self.bids) >= 5 else 0
    ask_vol = sum(qty for _, qty in self.asks[:5]) if len(self.asks) >= 5 else 0
    total = bid_vol + ask_vol
    if total < 1e-10:
        return 0.0
    return (bid_vol - ask_vol) / total
```

Также добавить скользящее среднее OBI для сглаживания шума.
В класс `OrderBookWS.__init__()` добавить:

```python
self._obi_history: Deque[float] = deque(maxlen=50)  # ~5s при 100ms updates
```

В `_update_book()` добавить:

```python
obi = self.book.imbalance
self._obi_history.append(obi)
```

Добавить свойство:

```python
@property
def smoothed_obi(self) -> float:
    """Exponentially-weighted OBI over last ~5 seconds."""
    if not self._obi_history:
        return 0.0
    values = list(self._obi_history)
    n = len(values)
    alpha = 2.0 / (n + 1)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema
```

**Шаг 1.2: config.py — добавить параметр**

```python
OBI_WEIGHT: float = _get("OBI_WEIGHT", "0.08", float)
```

**Шаг 1.3: market_calculator.py — интегрировать в p_up_signal()**

После расчёта p через Φ(z) и trend bias (строка ~334), добавить OBI blend:

```python
# --- Order Book Imbalance adjustment ---
obi_w = Config.OBI_WEIGHT
if obi_w > 0:
    obi = self._ob_ws.smoothed_obi  # [-1, +1]
    obi_shift = obi_w * obi  # max ±8% shift
    p = max(0.01, min(0.99, p + obi_shift))
```

**Шаг 1.4: bot_state.py — добавить поле obi**

```python
obi: float = 0.0
```

**Шаг 1.5: market_maker.py — обновить dashboard**

В `_update_dashboard()`:
```python
ds.obi = self._ob_ws.smoothed_obi
```

---

## Улучшение 2: Mean Reversion (Ornstein-Uhlenbeck)

### Цель
Учесть что BTC на 5-минутных масштабах возвращается к среднему после сильных движений.

### Файлы
- `ws_orderbook.py` — добавить трекинг mean reversion
- `market_calculator.py` — изменить формулу p_up_signal()
- `config.py` — добавить MEAN_REVERSION_KAPPA

### Шаги

**Шаг 2.1: config.py — добавить параметр**

```python
# Mean reversion strength for the signal model.
# 0.0 = pure random walk, 0.3 = moderate mean reversion.
MEAN_REVERSION_KAPPA: float = _get("MEAN_REVERSION_KAPPA", "0.20", float)
```

**Шаг 2.2: ws_orderbook.py — добавить средний return**

В класс `OrderBookWS` добавить свойство:

```python
@property
def mean_return_5m(self) -> float:
    """Average return of recent closed 5m candles."""
    if len(self._closed_returns) < 3:
        return 0.0
    returns = list(self._closed_returns)
    return sum(returns) / len(returns)
```

**Шаг 2.3: market_calculator.py — модифицировать z-score**

В `p_up_signal()`, после вычисления `ret` (строка ~306), добавить mean reversion adjustment:

```python
# --- Mean reversion adjustment ---
kappa = Config.MEAN_REVERSION_KAPPA
if kappa > 0:
    mean_ret = self._ob_ws.mean_return_5m
    # Pull ret toward mean: ret_adj = ret - κ × (ret - mean_ret)
    # Simplified: dampens extreme returns
    ret = ret * (1.0 - kappa) + mean_ret * kappa
```

Это заменяет чистый ret на dampened ret перед вычислением z = ret / σ_remaining.

---

## Улучшение 3: Volume Signal

### Цель
Использовать объём торгов Binance для оценки качества ценового движения.

### Файлы
- `ws_orderbook.py` — трекать volume и volume ratio
- `market_calculator.py` — volume-weighted confidence
- `config.py` — добавить VOLUME_CONFIDENCE_WEIGHT

### Шаги

**Шаг 3.1: ws_orderbook.py — добавить трекинг объёма**

В `__init__()` добавить:

```python
self._closed_volumes: Deque[float] = deque(maxlen=48)
```

В `_update_candle()`, при is_closed, добавить:

```python
self._closed_volumes.append(float(k.get("v", 0)))
```

Добавить свойство:

```python
@property
def volume_ratio(self) -> float:
    """
    Current candle volume / median of recent closed candle volumes.
    > 1.0 = above-average volume (stronger signal).
    < 1.0 = below-average volume (weaker signal).
    """
    if len(self._closed_volumes) < 3 or self.candle.volume <= 0:
        return 1.0
    sorted_vols = sorted(self._closed_volumes)
    median_vol = sorted_vols[len(sorted_vols) // 2]
    if median_vol <= 0:
        return 1.0
    return self.candle.volume / median_vol
```

**Шаг 3.2: config.py — добавить параметр**

```python
# How much volume ratio affects signal confidence.
# 0.0 = disabled, 0.15 = moderate.
VOLUME_CONFIDENCE_WEIGHT: float = _get("VOLUME_CONFIDENCE_WEIGHT", "0.10", float)
```

**Шаг 3.3: market_calculator.py — volume-weighted sigma**

В `p_up_signal()`, после вычисления sigma_remaining:

```python
# --- Volume-adjusted confidence ---
# High volume → signal is stronger (reduce σ → higher confidence)
# Low volume → signal is weaker (increase σ → lower confidence)
vol_w = Config.VOLUME_CONFIDENCE_WEIGHT
if vol_w > 0:
    vol_ratio = self._ob_ws.volume_ratio
    # vol_ratio > 1 → shrink σ (more confident)
    # vol_ratio < 1 → expand σ (less confident)
    vol_factor = 1.0 - vol_w * (vol_ratio - 1.0)
    vol_factor = max(0.7, min(1.3, vol_factor))  # clamp to ±30%
    sigma_remaining *= vol_factor
```

---

## Улучшение 4: Adaptive Trend Weight

### Цель
Динамически менять вес часового тренда в зависимости от его силы.

### Файлы
- `market_calculator.py` — изменить blend логику
- `config.py` — добавить MIN/MAX trend weight

### Шаги

**Шаг 4.1: config.py — добавить параметры**

```python
# Adaptive trend: weight scales from MIN to MAX based on trend strength.
TREND_WEIGHT_MIN: float = _get("TREND_WEIGHT_MIN", "0.03", float)
TREND_WEIGHT_MAX: float = _get("TREND_WEIGHT_MAX", "0.40", float)
```

Оставить `TREND_BIAS_WEIGHT` как fallback, но по умолчанию заменить его адаптивной логикой.

**Шаг 4.2: market_calculator.py — адаптивный weight**

Заменить строки ~327-334 в `p_up_signal()`:

```python
# --- Adaptive multi-timeframe trend bias ---
w_min = Config.TREND_WEIGHT_MIN
w_max = Config.TREND_WEIGHT_MAX
if w_max > 0:
    bias = self._ob_ws.hourly_trend_bias  # [-1, +1]
    # Stronger trend → higher weight
    trend_strength = abs(bias)  # 0..1
    w = w_min + (w_max - w_min) * trend_strength
    trend_target = 0.5 + 0.5 * bias
    p = p * (1.0 - w) + trend_target * w
```

Если `TREND_WEIGHT_MAX = 0`, тренд полностью отключён. Если оба = 0.15, поведение идентично текущему.

---

## Улучшение 5: Fix Sigma Calculation

### Цель
Исправить два бага в расчёте realized_sigma_5m.

### Файлы
- `ws_orderbook.py` — исправить realized_sigma_5m

### Шаги

**Шаг 5.1: Sample variance вместо population variance**

Заменить в `realized_sigma_5m`:

```python
@property
def realized_sigma_5m(self) -> float:
    if len(self._closed_returns) < self._MIN_CANDLES_FOR_SIGMA:
        return Config.SIGMA_5M
    returns = list(self._closed_returns)
    n = len(returns)

    # Exponential weighting: recent candles matter more
    # Half-life = 12 candles (1 hour)
    half_life = 12.0
    weights = [2.0 ** ((i - n + 1) / half_life) for i in range(n)]
    total_w = sum(weights)

    # Weighted mean
    mean = sum(w * r for w, r in zip(weights, returns)) / total_w

    # Weighted sample variance (Bessel-corrected)
    var = sum(w * (r - mean) ** 2 for w, r in zip(weights, returns)) / (total_w - weights[-1])

    return max(0.0005, min(0.006, math.sqrt(var)))
```

Изменения:
1. Деление на `(total_w - w_max)` вместо `len` — корректная Bessel correction для взвешенной дисперсии
2. Экспоненциальные веса с half-life = 1 час
3. Свежие свечи влияют сильнее

---

## Улучшение 6: Adaptive Stop-Loss

### Цель
Stop-loss threshold зависит от entry price, волатильности и оставшегося времени.

### Файлы
- `market_maker.py` — изменить stop-loss логику
- `config.py` — добавить STOP_LOSS_BASE, STOP_LOSS_VOL_SCALE

### Шаги

**Шаг 6.1: config.py — новые параметры**

```python
# Adaptive stop-loss: threshold = base + vol_scale × (σ / 0.002)
# Then multiplied by time_factor = max(0.3, stc / 120)
STOP_LOSS_BASE: float = _get("STOP_LOSS_BASE", "0.06", float)
STOP_LOSS_VOL_SCALE: float = _get("STOP_LOSS_VOL_SCALE", "0.04", float)
# Minimum seconds to close before stop-loss can trigger.
# Near close, let the market resolve naturally.
STOP_LOSS_MIN_STC: float = _get("STOP_LOSS_MIN_STC", "15", float)
```

**Шаг 6.2: market_maker.py — адаптивная логика**

Заменить stop-loss блок (строки ~369-377):

```python
if Config.STOP_LOSS_ENABLED:
    stc = market.seconds_to_close

    # Don't stop-loss near close — let it resolve
    if stc > Config.STOP_LOSS_MIN_STC:
        sigma = self._ob_ws.realized_sigma_5m

        # Adaptive threshold: wider when vol is high, tighter when low
        base = Config.STOP_LOSS_BASE
        vol_adj = Config.STOP_LOSS_VOL_SCALE * (sigma / 0.002)
        time_factor = max(0.3, min(1.0, stc / 120.0))
        threshold = (base + vol_adj) * time_factor

        for side in (state.yes, state.no):
            if not side.was_ever_filled or side.stopped_out:
                continue
            current_fair = p_up if side.side_label == "YES" else 1.0 - p_up
            reversal = side.last_entry_price - current_fair
            if reversal > threshold:
                await self._stop_loss_exit(state, side, current_fair)
                return
```

Поведение:
- `stc=200s, σ=0.002`: threshold = (0.06 + 0.04) × 1.0 = 10%
- `stc=200s, σ=0.004`: threshold = (0.06 + 0.08) × 1.0 = 14% (высокая vol → шире)
- `stc=60s, σ=0.002`:  threshold = (0.06 + 0.04) × 0.5 = 5% (скоро закрытие → теснее)
- `stc<15s`:           stop-loss отключён → ждём resolution

---

## Улучшение 7: Volatility Regime Detection

### Цель
Определять режим волатильности и адаптировать sizing/spread.

### Файлы
- `ws_orderbook.py` — добавить vol regime
- `market_maker.py` — адаптировать sizing и spread
- `config.py` — параметры режимов
- `bot_state.py` — добавить vol_regime в dashboard

### Шаги

**Шаг 7.1: ws_orderbook.py — добавить vol percentile**

```python
@property
def vol_percentile(self) -> float:
    """
    Where current realized sigma sits in the distribution of recent sigmas.
    Returns 0.0-1.0. Values >0.9 indicate unusually high volatility.
    """
    if len(self._closed_returns) < self._MIN_CANDLES_FOR_SIGMA:
        return 0.5  # unknown → assume median

    current = self.realized_sigma_5m
    # Compare to rolling window of per-candle absolute returns
    abs_returns = sorted(abs(r) for r in self._closed_returns)
    rank = sum(1 for r in abs_returns if r <= current)
    return rank / len(abs_returns)
```

Добавить enum-like свойство:

```python
@property
def vol_regime(self) -> str:
    """Volatility regime: 'calm', 'normal', or 'storm'."""
    pct = self.vol_percentile
    if pct > 0.90:
        return "storm"
    elif pct < 0.30:
        return "calm"
    return "normal"
```

**Шаг 7.2: config.py — параметры режимов**

```python
# Volatility regime adjustments.
# In "storm" mode: reduce size, widen spread.
VOL_REGIME_STORM_SIZE_MULT: float = _get("VOL_REGIME_STORM_SIZE_MULT", "0.5", float)
VOL_REGIME_STORM_SPREAD_MULT: float = _get("VOL_REGIME_STORM_SPREAD_MULT", "1.5", float)
# In "calm" mode: can tighten spread slightly.
VOL_REGIME_CALM_SPREAD_MULT: float = _get("VOL_REGIME_CALM_SPREAD_MULT", "0.85", float)
```

**Шаг 7.3: market_maker.py — применить режим**

В `_quote_both_sides()`, после вычисления base_spread:

```python
# --- Volatility regime adjustment ---
vol_regime = self._ob_ws.vol_regime
if vol_regime == "storm":
    base_spread *= Config.VOL_REGIME_STORM_SPREAD_MULT
elif vol_regime == "calm":
    base_spread *= Config.VOL_REGIME_CALM_SPREAD_MULT
```

После вычисления Kelly sizes:

```python
if vol_regime == "storm":
    yes_size *= Config.VOL_REGIME_STORM_SIZE_MULT
    no_size *= Config.VOL_REGIME_STORM_SIZE_MULT
```

**Шаг 7.4: bot_state.py + dashboard**

Добавить `vol_regime: str = "normal"` в BotState.
Обновить `_update_dashboard()`:
```python
ds.vol_regime = self._ob_ws.vol_regime
```

---

## Улучшение 8: Smart Hedge Timeout

### Цель
Таймаут хеджа масштабируется с оставшимся временем и учитывает P&L позиции.

### Файлы
- `market_maker.py` — изменить hedge timeout логику
- `config.py` — добавить HEDGE_TIMEOUT_FRAC, HEDGE_ONLY_IF_LOSING

### Шаги

**Шаг 8.1: config.py — параметры**

```python
# Hedge timeout as fraction of remaining window time.
# timeout = max(5, min(HEDGE_TIMEOUT_SEC, stc × HEDGE_TIMEOUT_FRAC))
HEDGE_TIMEOUT_FRAC: float = _get("HEDGE_TIMEOUT_FRAC", "0.15", float)
# Only aggressively hedge if the filled position is losing.
HEDGE_ONLY_IF_LOSING: bool = _get("HEDGE_ONLY_IF_LOSING", "true",
                                   lambda v: v.lower() in ("true", "1", "yes"))
```

**Шаг 8.2: market_maker.py — адаптивный timeout**

Заменить hedge timeout блок (строки ~394-419):

```python
# --- Inventory skew + smart hedge timeout ---
yes_spread = base_spread
no_spread = base_spread
now_mono = time.monotonic()
stc = market.seconds_to_close

# Dynamic timeout: fraction of remaining time, capped by HEDGE_TIMEOUT_SEC
dynamic_timeout = max(5.0, min(Config.HEDGE_TIMEOUT_SEC, stc * Config.HEDGE_TIMEOUT_FRAC))

def _should_aggressive_hedge(filled_side: MarketSide) -> bool:
    """Only rush hedge if position is underwater."""
    if not Config.HEDGE_ONLY_IF_LOSING:
        return True  # always aggressive (old behavior)
    current_fair = p_up if filled_side.side_label == "YES" else 1.0 - p_up
    return filled_side.last_entry_price > current_fair + 0.02  # losing by >2¢

if state.yes.was_ever_filled and not state.no.was_ever_filled:
    elapsed_since_fill = now_mono - state.yes.first_fill_time if state.yes.first_fill_time > 0 else 0.0
    if elapsed_since_fill > dynamic_timeout and _should_aggressive_hedge(state.yes):
        no_spread *= Config.HEDGE_AGGRESSIVE_SPREAD_MULT
        if Config.SELL_EXIT_ENABLED:
            await self._try_sell_exit(state.yes, self._last_yes_bid)
    else:
        no_spread *= (1.0 - self._INVENTORY_SKEW)
elif state.no.was_ever_filled and not state.yes.was_ever_filled:
    elapsed_since_fill = now_mono - state.no.first_fill_time if state.no.first_fill_time > 0 else 0.0
    if elapsed_since_fill > dynamic_timeout and _should_aggressive_hedge(state.no):
        yes_spread *= Config.HEDGE_AGGRESSIVE_SPREAD_MULT
        if Config.SELL_EXIT_ENABLED:
            await self._try_sell_exit(state.no, self._last_no_bid)
    else:
        yes_spread *= (1.0 - self._INVENTORY_SKEW)
```

Поведение:
- stc=280s: timeout = max(5, min(25, 42)) = 25s (как раньше)
- stc=100s: timeout = max(5, min(25, 15)) = 15s (быстрее!)
- stc=30s:  timeout = max(5, min(25, 4.5)) = 5s  (очень быстро)
- Если позиция в плюсе (entry < fair): НЕ паникуем, оставляем обычный skew

---

## Улучшение 9: Candle Close Location

### Цель
Учитывать паттерн закрытия последней свечи (hammer, shooting star, doji).

### Файлы
- `ws_orderbook.py` — добавить close_location
- `market_calculator.py` — blend в p_up_signal()
- `config.py` — добавить CANDLE_PATTERN_WEIGHT

### Шаги

**Шаг 9.1: ws_orderbook.py — добавить свойство**

В класс `Candle5m`:

```python
@property
def close_location(self) -> float:
    """
    Where the candle closed within its high-low range.
    1.0 = closed at high (bullish), 0.0 = closed at low (bearish).
    0.5 = middle (neutral / doji).
    """
    hl_range = self.high - self.low
    if hl_range < 1e-10:
        return 0.5
    return (self.close - self.low) / hl_range
```

В `OrderBookWS` добавить трекинг последней закрытой свечи:

```python
# В __init__:
self._last_closed_candle: Optional[Candle5m] = None

# В _update_candle(), при is_closed:
self._last_closed_candle = self.candle
```

Добавить свойство:

```python
@property
def last_candle_close_location(self) -> float:
    """Close location of the most recently closed 5m candle."""
    if self._last_closed_candle is None:
        return 0.5
    return self._last_closed_candle.close_location
```

**Шаг 9.2: config.py — параметр**

```python
CANDLE_PATTERN_WEIGHT: float = _get("CANDLE_PATTERN_WEIGHT", "0.04", float)
```

**Шаг 9.3: market_calculator.py — blend**

После OBI adjustment (или после trend), добавить:

```python
# --- Candle close location bias ---
cp_w = Config.CANDLE_PATTERN_WEIGHT
if cp_w > 0:
    cl = self._ob_ws.last_candle_close_location  # [0, 1]
    cl_bias = (cl - 0.5) * 2.0 * cp_w  # map [0,1] → [-cp_w, +cp_w]
    p = max(0.01, min(0.99, p + cl_bias))
```

---

## Улучшение 10: Time-of-Day Bias

### Цель
Корректировать волатильность и поведение бота в зависимости от торговой сессии.

### Файлы
- `ws_orderbook.py` или `market_calculator.py` — добавить time-of-day multiplier
- `config.py` — добавить параметр включения

### Шаги

**Шаг 10.1: config.py — параметр**

```python
# Time-of-day volatility adjustment.
# Scales sigma based on known BTC volatility patterns across trading sessions.
TOD_VOL_ADJUST_ENABLED: bool = _get("TOD_VOL_ADJUST_ENABLED", "true",
                                     lambda v: v.lower() in ("true", "1", "yes"))
```

**Шаг 10.2: market_calculator.py — добавить функцию**

```python
def _tod_vol_multiplier() -> float:
    """
    Time-of-day volatility multiplier for BTC.

    Based on empirical observation of BTC 5m volatility by hour (UTC):
      - Asian session (00-04):  lower vol (~85%)
      - Europe open (07-10):    rising vol (~105%)
      - US open (13-16):        highest vol (~120%)
      - US close/evening (20-23): lower vol (~90%)
    """
    from datetime import datetime, timezone
    hour = datetime.now(timezone.utc).hour

    TOD_MAP = {
        0: 0.85, 1: 0.85, 2: 0.85, 3: 0.85,
        4: 0.90, 5: 0.95, 6: 0.95, 7: 1.05,
        8: 1.05, 9: 1.05, 10: 1.00, 11: 1.00,
        12: 1.05, 13: 1.15, 14: 1.20, 15: 1.15,
        16: 1.10, 17: 1.05, 18: 1.00, 19: 0.95,
        20: 0.90, 21: 0.90, 22: 0.90, 23: 0.85,
    }
    return TOD_MAP.get(hour, 1.0)
```

**Шаг 10.3: market_calculator.py — применить в p_up_signal()**

После получения sigma, перед вычислением sigma_remaining:

```python
if Config.TOD_VOL_ADJUST_ENABLED:
    sigma *= _tod_vol_multiplier()
```

---

## Порядок реализации (приоритет)

| Фаза | Улучшения | Обоснование |
|------|-----------|-------------|
| **Фаза 1** | #5 (Fix Sigma) + #6 (Adaptive Stop-Loss) | Исправление багов и защита капитала — минимальный риск, максимальный impact |
| **Фаза 2** | #1 (OBI) + #3 (Volume) | Самые мощные новые сигналы — используют уже доступные данные |
| **Фаза 3** | #2 (Mean Reversion) + #4 (Adaptive Trend) | Улучшение модели сигнала |
| **Фаза 4** | #7 (Vol Regime) + #8 (Smart Hedge) | Защита капитала в экстремальных условиях |
| **Фаза 5** | #9 (Candle Pattern) + #10 (ToD Bias) | Дополнительная калибровка |

## Тестирование

Каждую фазу запускать в paper mode на 4-8 часов и сравнивать:
- Win rate (rolling-50)
- Avg P&L per trade
- Max drawdown
- Stop-loss trigger frequency
- Two-sided fill rate

Метрики записывать в stats.py log_summary().
