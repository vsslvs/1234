#!/usr/bin/env python3
"""
Интерактивный помощник настройки бота Polymarket BTC Market Maker.
Запуск: python3 setup.py
"""
import os
import sys
import re

# ─────────────────────────────────────────────────────────────────
# Цвета и стили для терминала
# ─────────────────────────────────────────────────────────────────
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
BG_BLUE = "\033[44m"

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def print_header():
    clear_screen()
    print()
    print(f"  {BG_BLUE}{WHITE}{BOLD}                                                        {RESET}")
    print(f"  {BG_BLUE}{WHITE}{BOLD}     ⚙  НАСТРОЙКА БОТА  —  Polymarket BTC Maker         {RESET}")
    print(f"  {BG_BLUE}{WHITE}{BOLD}                                                        {RESET}")
    print()


def print_section(title):
    width = 56
    print()
    print(f"  {CYAN}{BOLD}{'─' * width}{RESET}")
    print(f"  {CYAN}{BOLD}  {title}{RESET}")
    print(f"  {CYAN}{BOLD}{'─' * width}{RESET}")
    print()


def print_success(msg):
    print(f"  {GREEN}✔ {msg}{RESET}")


def print_warning(msg):
    print(f"  {YELLOW}⚠ {msg}{RESET}")


def print_error(msg):
    print(f"  {RED}✖ {msg}{RESET}")


def print_info(msg):
    print(f"  {DIM}{msg}{RESET}")


def ask(label, hint="", default="", secret=False, validator=None):
    """Запрос значения у пользователя с подсказкой и валидацией."""
    print(f"  {BOLD}{WHITE}{label}{RESET}")
    if hint:
        print_info(hint)

    while True:
        if default and not secret:
            prompt = f"  {MAGENTA}▸{RESET} [{default}]: "
        else:
            prompt = f"  {MAGENTA}▸{RESET} "

        try:
            value = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print_warning("Настройка отменена.")
            sys.exit(1)

        if not value and default:
            value = default

        if not value:
            print_error("Значение не может быть пустым.")
            continue

        if validator:
            err = validator(value)
            if err:
                print_error(err)
                continue

        if secret:
            masked = value[:6] + "…" + value[-4:] if len(value) > 12 else "****"
            print_success(f"Сохранено: {masked}")
        else:
            print_success(f"Сохранено: {value}")
        print()
        return value


def ask_yes_no(question, default_yes=True):
    """Да/Нет вопрос."""
    hint = "[Д/н]" if default_yes else "[д/Н]"
    print(f"  {BOLD}{WHITE}{question}{RESET} {DIM}{hint}{RESET}")
    try:
        answer = input(f"  {MAGENTA}▸{RESET} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default_yes
    if not answer:
        return default_yes
    return answer in ("д", "да", "y", "yes", "1")


# ─────────────────────────────────────────────────────────────────
# Валидаторы
# ─────────────────────────────────────────────────────────────────
def validate_private_key(val):
    val = val.strip()
    if not val.startswith("0x"):
        return "Ключ должен начинаться с '0x'"
    hex_part = val[2:]
    if len(hex_part) != 64:
        return f"Длина hex-части должна быть 64 символа (сейчас {len(hex_part)})"
    if not re.match(r"^[0-9a-fA-F]+$", hex_part):
        return "Ключ содержит недопустимые символы (допустимы 0-9, a-f, A-F)"
    return None


def validate_float(val):
    try:
        float(val)
    except ValueError:
        return "Введите числовое значение (например: 0.92)"
    return None


def validate_positive_float(val):
    err = validate_float(val)
    if err:
        return err
    if float(val) <= 0:
        return "Значение должно быть больше 0"
    return None


def validate_int(val):
    try:
        int(val)
    except ValueError:
        return "Введите целое число"
    return None


def validate_positive_int(val):
    err = validate_int(val)
    if err:
        return err
    if int(val) <= 0:
        return "Значение должно быть больше 0"
    return None


def validate_price(val):
    err = validate_float(val)
    if err:
        return err
    f = float(val)
    if f <= 0 or f >= 1:
        return "Цена должна быть от 0.01 до 0.99"
    return None


def validate_url(val):
    if not val.startswith(("http://", "https://", "wss://", "ws://")):
        return "URL должен начинаться с http://, https://, ws:// или wss://"
    return None


# ─────────────────────────────────────────────────────────────────
# Загрузка существующего .env
# ─────────────────────────────────────────────────────────────────
def load_existing_env():
    """Читает текущий .env если существует, возвращает dict."""
    values = {}
    if not os.path.exists(ENV_PATH):
        return values
    with open(ENV_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                # Убираем inline комментарии
                if " #" in val:
                    val = val[: val.index(" #")]
                values[key.strip()] = val.strip()
    return values


# ─────────────────────────────────────────────────────────────────
# Основной процесс настройки
# ─────────────────────────────────────────────────────────────────
def run_setup():
    existing = load_existing_env()

    def get_default(key, fallback=""):
        val = existing.get(key, fallback)
        if val == "0xyour_private_key_here":
            return ""
        return val

    print_header()
    print(f"  {WHITE}Этот помощник проведёт вас через настройку бота.{RESET}")
    print(f"  {WHITE}Для каждого параметра показано значение по умолчанию в [скобках].{RESET}")
    print(f"  {WHITE}Нажмите {BOLD}Enter{RESET}{WHITE}, чтобы принять значение по умолчанию.{RESET}")

    if existing:
        print()
        print_info(f"Найден существующий файл .env — значения будут предзаполнены.")

    # ── 1. Кошелёк ─────────────────────────────────────────────
    print_section("🔑  КОШЕЛЁК")

    private_key = ask(
        "Приватный ключ кошелька Polymarket",
        hint="EOA-ключ из MetaMask (начинается с 0x, 66 символов)",
        default=get_default("POLYMARKET_PRIVATE_KEY"),
        secret=True,
        validator=validate_private_key,
    )

    # ── 2. Торговые параметры ──────────────────────────────────
    print_section("📊  ТОРГОВЫЕ ПАРАМЕТРЫ")

    order_size = ask(
        "Размер ордера (USDC)",
        hint="Сколько USDC ставить на каждый ордер",
        default=get_default("ORDER_SIZE_USDC", "50"),
        validator=validate_positive_float,
    )

    max_orders = ask(
        "Макс. одновременных ордеров на рынок",
        hint="Обычно 4 (2 YES + 2 NO)",
        default=get_default("MAX_OPEN_ORDERS", "4"),
        validator=validate_positive_int,
    )

    target_yes = ask(
        "Целевая цена для стороны YES",
        hint="От 0.01 до 0.99 (92 центов = 0.92)",
        default=get_default("TARGET_PRICE_YES", "0.92"),
        validator=validate_price,
    )

    target_no = ask(
        "Целевая цена для стороны NO",
        hint="От 0.01 до 0.99 (92 центов = 0.92)",
        default=get_default("TARGET_PRICE_NO", "0.92"),
        validator=validate_price,
    )

    # ── 3. Управление рисками ─────────────────────────────────
    print_section("🛡  УПРАВЛЕНИЕ РИСКАМИ")

    max_exposure = ask(
        "Макс. общая экспозиция (USDC)",
        hint="Максимальная сумма всех открытых позиций",
        default=get_default("MAX_EXPOSURE_USDC", "500"),
        validator=validate_positive_float,
    )

    min_edge = ask(
        "Мин. преимущество (базисные пункты)",
        hint="Пропускать рынок если ожидаемое преимущество ниже (50 = 0.5%)",
        default=get_default("MIN_EDGE_BPS", "50"),
        validator=validate_positive_int,
    )

    # ── 4. Таймеры ─────────────────────────────────────────────
    print_section("⏱  ТАЙМЕРЫ")

    quote_refresh = ask(
        "Интервал обновления котировок (мс)",
        hint="Как часто пересчитывать котировки (200 мс по умолчанию)",
        default=get_default("QUOTE_REFRESH_MS", "200"),
        validator=validate_positive_int,
    )

    cancel_timeout = ask(
        "Таймаут отмены/замены ордера (мс)",
        hint="Жёсткий дедлайн на cancel/replace (90 мс)",
        default=get_default("CANCEL_REPLACE_TIMEOUT_MS", "90"),
        validator=validate_positive_int,
    )

    entry_window = ask(
        "Окно входа (секунды до закрытия рынка)",
        hint="За сколько секунд до закрытия начинать торговать",
        default=get_default("ENTRY_WINDOW_SEC", "10"),
        validator=validate_positive_int,
    )

    exit_window = ask(
        "Окно выхода (секунды до закрытия рынка)",
        hint="За сколько секунд до закрытия прекратить котировки",
        default=get_default("EXIT_WINDOW_SEC", "2"),
        validator=validate_positive_int,
    )

    # ── 5. Подключения (опционально) ──────────────────────────
    print_section("🌐  ПОДКЛЮЧЕНИЯ")

    advanced = ask_yes_no("Настроить URL-адреса подключений? (обычно не требуется)", default_yes=False)
    print()

    if advanced:
        clob_api = ask(
            "CLOB API URL",
            default=get_default("CLOB_API_URL", "https://clob.polymarket.com"),
            validator=validate_url,
        )
        clob_ws = ask(
            "CLOB WebSocket URL",
            default=get_default("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/"),
            validator=validate_url,
        )
        binance_ws = ask(
            "Binance WebSocket URL",
            default=get_default("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws"),
            validator=validate_url,
        )
    else:
        clob_api = get_default("CLOB_API_URL", "https://clob.polymarket.com")
        clob_ws = get_default("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/")
        binance_ws = get_default("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws")

    # ── 6. Логирование ─────────────────────────────────────────
    print_section("📋  ЛОГИРОВАНИЕ")

    log_level = ask(
        "Уровень логирования",
        hint="DEBUG / INFO / WARNING / ERROR (INFO по умолчанию)",
        default=get_default("LOG_LEVEL", "INFO"),
    )

    # ── Сводка и запись ────────────────────────────────────────
    print_section("📝  ИТОГОВАЯ КОНФИГУРАЦИЯ")

    masked_key = private_key[:6] + "…" + private_key[-4:]
    summary = [
        ("Приватный ключ", masked_key),
        ("Размер ордера", f"{order_size} USDC"),
        ("Макс. ордеров", max_orders),
        ("Цена YES", target_yes),
        ("Цена NO", target_no),
        ("Макс. экспозиция", f"{max_exposure} USDC"),
        ("Мин. преимущество", f"{min_edge} bps"),
        ("Обновление котировок", f"{quote_refresh} мс"),
        ("Таймаут отмены", f"{cancel_timeout} мс"),
        ("Окно входа", f"{entry_window} сек"),
        ("Окно выхода", f"{exit_window} сек"),
        ("Уровень логов", log_level),
    ]

    max_label = max(len(s[0]) for s in summary)
    for label, value in summary:
        print(f"  {WHITE}{label.ljust(max_label)}{RESET}  {GREEN}{value}{RESET}")

    print()
    save = ask_yes_no("Сохранить конфигурацию в .env?", default_yes=True)

    if not save:
        print()
        print_warning("Конфигурация НЕ сохранена.")
        sys.exit(0)

    # ── Запись .env файла ──────────────────────────────────────
    env_content = f"""\
# Приватный ключ кошелька Polymarket (EOA — MetaMask)
POLYMARKET_PRIVATE_KEY={private_key}

# Подключения к Polymarket CLOB
CLOB_API_URL={clob_api}
CLOB_WS_URL={clob_ws}

# Binance WebSocket для цены BTC
BINANCE_WS_URL={binance_ws}

# Торговые параметры
BTC_SYMBOL=BTCUSDT
ORDER_SIZE_USDC={order_size}
MAX_OPEN_ORDERS={max_orders}
TARGET_PRICE_YES={target_yes}
TARGET_PRICE_NO={target_no}

# Таймеры
QUOTE_REFRESH_MS={quote_refresh}
CANCEL_REPLACE_TIMEOUT_MS={cancel_timeout}
ENTRY_WINDOW_SEC={entry_window}
EXIT_WINDOW_SEC={exit_window}

# Управление рисками
MAX_EXPOSURE_USDC={max_exposure}
MIN_EDGE_BPS={min_edge}

# Логирование
LOG_LEVEL={log_level}
"""

    with open(ENV_PATH, "w") as f:
        f.write(env_content)

    print()
    print(f"  {GREEN}{BOLD}{'─' * 56}{RESET}")
    print(f"  {GREEN}{BOLD}  ✔  Конфигурация сохранена в .env{RESET}")
    print(f"  {GREEN}{BOLD}{'─' * 56}{RESET}")
    print()
    print(f"  {WHITE}Запуск бота:{RESET}  {CYAN}python3 main.py{RESET}")
    print()


if __name__ == "__main__":
    run_setup()
