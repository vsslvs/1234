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

<!-- Новые сессии ревью добавляются выше этой строки -->
