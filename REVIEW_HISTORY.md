# Code Review History

История всех ревью кода, найденных ошибок и применённых исправлений.
Обновляется после каждой сессии проверки кода.

---

## Session 1 — 2026-03-20 — Первоначальное написание бота (Binance)

**Описание:** Первая версия бота — тейкерская стратегия арбитража между Binance и Polymarket.
**Статус:** Стратегия устарела (убрана задержка 500ms + введены тейкерские комиссии). Код полностью переписан в Session 2.
**Формальное ревью:** не проводилось.

---

## Session 2 — 2026-03-20 — Полный рерайт под Polymarket CLOB мейкер-стратегию

**Описание:** Переписан с нуля под новую мету 2026 — мейкер на Polymarket CLOB, 5-минутные BTC-рынки.
**Коммит с исправлениями:** `479f2a0`
**Итог:** 7 ошибок (5 критических, 2 средних)

### Найденные ошибки

| # | Severity | Файл | Строки | Описание |
|---|---|---|---|---|
| 1 | CRITICAL | `ws_orderbook.py` | 90–92 | `Config.SYMBOL` и `Config.WS_BASE` не существуют в Config → `AttributeError` при старте |
| 2 | CRITICAL | `main.py` | 41 | `Config._get()` вызывается как classmethod, но `_get()` — функция уровня модуля → `AttributeError` |
| 3 | CRITICAL | `polymarket_client.py` | 205–206 | EIP-712 подпись через `encode_defunct` + приватный метод `Web3().eth.account._structured_data_encoder` — неверный API для typed data, все ордера отклонялись бы CLOB |
| 4 | CRITICAL | `market_maker.py` | 46 | `SIDE_BUY`/`SIDE_SELL` импортировались из `market_calculator`, где они не определены → `ImportError` при старте |
| 5 | CRITICAL | `market_calculator.py` | 143–145 | `endDateIso` — ISO-строка (`"2026-03-20T15:00:00Z"`), `int()` поднимает `ValueError`, который поглощался блоком `except` → все рынки тихо не парсились → `self._markets` всегда пуст → бот никогда не торговал |
| 6 | MEDIUM | `market_maker.py` | 196 | `if edge >= Config.MIN_EDGE_BPS or True:` — `or True` делает edge-проверку мёртвым кодом, ордера выставлялись без проверки минимального преимущества |
| 7 | MEDIUM | `polymarket_client.py` | 112–114 | Таймаут сессии aiohttp = 90ms (бюджет cancel/replace) применялся ко всем запросам, включая `check_approvals`, `get_fee_rate` → регулярные тайм-ауты при старте |

### Коренные причины

- **Рефактор конфига без обновления зависимостей:** `Config.SYMBOL`/`Config.WS_BASE` остались от старой версии, новые имена (`BTC_SYMBOL`, `BINANCE_WS_URL`) не были проверены в использующих файлах.
- **Неверный API eth_account:** `encode_defunct` — для `personal_sign`, не EIP-712. Правильная функция — `encode_typed_data` (eth_account ≥ 0.6) / `encode_structured_data` (eth_account < 0.6).
- **Импорт из неверного модуля:** `SIDE_BUY`/`SIDE_SELL` определены в `polymarket_client.py`, но были указаны в импорте из `market_calculator`.
- **Неправильное предположение о типе данных API:** Gamma API возвращает даты как ISO 8601 строки, не unix timestamps.
- **`or True` — отладочный артефакт:** Судя по всему, оставлен случайно при написании.

### Применённые исправления

```
ws_orderbook.py:90-92   Config.SYMBOL → Config.BTC_SYMBOL
                         Config.WS_BASE → Config.BINANCE_WS_URL

main.py:41              Config._get("LOG_LEVEL","INFO") → os.getenv("LOG_LEVEL","INFO")
                         добавлен: import os

polymarket_client.py    Удалены: import hashlib, import json,
                                 from eth_account.messages import encode_defunct,
                                 from eth_account.structured_data.hashing import hash_domain, hash_message,
                                 from web3 import Web3
                         Добавлен: from eth_account.messages import encode_typed_data
                         :205-206  Web3()._structured_data_encoder + encode_defunct
                                   → encode_typed_data(full_message=structured_data)
                         :112-114  ClientTimeout(total=90ms/1000) → ClientTimeout(total=5.0)

market_maker.py:46      from market_calculator import ..., SIDE_BUY, SIDE_SELL
                         → from polymarket_client import ..., SIDE_BUY, SIDE_SELL
                :196    if edge >= Config.MIN_EDGE_BPS or True:
                         → if edge >= Config.MIN_EDGE_BPS:

market_calculator.py    добавлен: from datetime import datetime
                :143-148 end_ts = int(item.get("endDateIso", ...))
                          → end_str = item.get(...)
                            dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                            end_ts = int(dt.timestamp())
```

---

---

## Session 3 — 2026-03-20 — Ревью логики и работоспособности бота

**Описание:** Глубокое ревью алгоритмической логики — не синтаксис, а корректность работы end-to-end.
**Оценка работоспособности: 25 / 100**
**Статус:** Ошибки найдены, исправления не применялись (только отчёт).

### Найденные ошибки

| # | Severity | Файл | Строки | Описание |
|---|---|---|---|---|
| 1 | CRITICAL | `ws_orderbook.py` | 91–92 | Binance combined streams URL неверный: `/ws/s1/s2` вместо `/stream?streams=s1/s2`. `_handle()` ищет ключ `"stream"` в сообщении — он есть только у combined format. Одиночный `/ws/` поток его не имеет → book никогда не обновляется → `mid_price = None` всегда |
| 2 | CRITICAL | `market_calculator.py` | 113 | Тег `"btc-5m"` не существует в Gamma API → 0 рынков → `self._markets` пуст → `current_market()` всегда `None` → бот крутит пустой цикл без единого ордера |
| 3 | CRITICAL | `market_maker.py` | 195–196 | Знак `edge_bps` перепутан для BUY: формула `(quoted − fair) × 10000` даёт +700bps при покупке за 0.92 при fair=0.85, бот трактует это как преимущество. Реально это переплата. Для BUY формула должна быть `(fair − quoted) × 10000` |
| 4 | HIGH | config.py + market_maker.py | 55 | `P_UP_THRESHOLD = 0.80 < TARGET_PRICE_YES = 0.92` → отрицательный EV: EV = 0.80×$0.08 − 0.20×$0.92 = **−$0.12 за акцию**. Для безубыточности при покупке по 0.92 нужен порог > 0.92 |
| 5 | HIGH | `polymarket_client.py` | 326–333 | `cancel_replace`: `cancel_ok` не проверяется. Если cancel вернул False и place успешен → два открытых ордера на рынке → двойное заполнение / неконтролируемый риск |
| 6 | HIGH | Общий поток | — | Нет обработки случая 0 рынков от Gamma API: бот уходит в бесконечный пустой цикл без error-лога и без выхода |
| 7 | MEDIUM | `market_calculator.py` | 147–149 | `window_start` из Gamma API может не совпасть с `current_window_start()` если `endDateIso` не выровнен по 5-минутной сетке. `current_market()` вернёт None даже при наличии нужного рынка |
| 8 | MEDIUM | `market_maker.py` | 226 | Только `SIDE_BUY` — односторонние котировки (только биды). Maker-рибейты на Polymarket предполагают двусторонние котировки. Профит от рибейтов под вопросом при one-sided стратегии |
| 9 | MEDIUM | `market_calculator.py` | 130 | Memory leak: `self._markets` dict никогда не очищается. После 1 дня работы ~288 объектов в памяти, после 7 дней ~2000+, растёт бесконечно |
| 10 | LOW | `market_maker.py` | 43, 272 | Неиспользуемый импорт `Tuple`; на первой итерации `_market_refresh_loop` сразу вызывает fetch без задержки (дублирует вызов из `run()`) |

### Что работает корректно

| Компонент | Статус |
|---|---|
| EIP-712 подпись через `encode_typed_data` | ✓ |
| `feeRateBps` в подписанном struct | ✓ |
| `get_fee_rate` с 5с кешем | ✓ |
| `cancel_replace` через `asyncio.gather` | ✓ (кроме проверки cancel_ok) |
| Математика BUY: makerAmount=USDC, takerAmount=shares | ✓ |
| Window timing (`seconds_to_close`, `is_entry_window`) | ✓ |
| ISO-дата парсинг endDateIso | ✓ |
| Логистическая функция p_up_signal | ✓ математически |
| Config loading, reconnect-логика WS | ✓ |

### Коренные причины

- **Binance URL:** Незнание разницы между single-stream endpoint (`/ws/<stream>`) и combined-stream endpoint (`/stream?streams=s1/s2`). Это разные пути, не просто разные параметры.
- **Gamma API tag:** Несуществующий тег. Правильный поиск по Gamma — через `slug`, `clob_token_ids`, или `condition_id`, не произвольный `tag`.
- **Edge sign:** Концептуальная ошибка: формула `(quoted − fair)` описывает преимущество продавца (хорошо, что продаёшь выше fair), но для покупателя нужно `(fair − quoted)` (хорошо, что покупаешь ниже fair).
- **P_UP_THRESHOLD vs TARGET_PRICE:** Не проверена математика EV. Для покупки по цене X с нулевыми комиссиями нужен сигнал > X для положительного EV.
- **Cancel check:** Асимметричная обработка ошибок в `asyncio.gather` — только вторая задача (place) проверяется на ошибку.

### Необходимые исправления

```
ws_orderbook.py:92          URL: /ws/{s1}/{s2}
                              → /stream?streams={s1}/{s2}

market_calculator.py:112-116  params "tag":"btc-5m"
                              → использовать slug-поиск или clob_token_ids

market_maker.py:195-196     edge = self._calc.edge_bps(fair_yes, target, fair_yes)
                              → edge = (fair_yes - target) * 10_000
                            if edge >= Config.MIN_EDGE_BPS
                              → вместе с поднятием P_UP_THRESHOLD > TARGET_PRICE_YES

config.py:55-56             P_UP_THRESHOLD   = 0.80  → 0.94
                            P_DOWN_THRESHOLD = 0.20  → 0.06

polymarket_client.py:326-333  добавить проверку cancel_ok + лог

Общий поток               добавить выход/ретраи при 0 рынков от Gamma

market_calculator.py:130  добавить очистку устаревших рынков из self._markets
```

<!-- Новые сессии ревью добавляются выше этой строки -->
