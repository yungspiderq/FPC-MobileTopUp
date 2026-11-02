"""
Функциональные тесты плагина mobile_topup.py на моках.

Проверяют: метаданные и настройки, обе авторизации DesslyHub (legacy apikey и v2 HMAC),
привязки лотов (игра/номинал/источник), сбор обязательных полей покупателя
(Character ID и т.п.) из полей заказа и из чата (включая частичный ввод и псевдонимы),
полный цикл выдачи (DRY-RUN, legacy, v2), ошибки каталога (-200/-201), денежные
защиты, идемпотентность, опрос транзакций, восстановление после перезапуска,
каталог игр в Telegram-меню и удаление плагина.

Все запросы к DesslyHub и Telegram подменены заглушками - тест не тратит деньги и не
пишет в ваши чаты. Настройки создаются во временной папке: ваши реальные
storage/plugins/mobile_topup/ НЕ затрагиваются.

Запуск из корня FunPayCardinal:
    cp tests_plugin_mobile_topup.py /path/to/FunPayCardinal/
    cd /path/to/FunPayCardinal && pip install -r requirements.txt && python3 tests_plugin_mobile_topup.py
"""

MODULE_NAME = "mobile_topup"
TEST_TXN = "tx-1"

GAME = {"id": 123, "name": "PUBG Mobile",
        "positions": [{"id": 492, "name": "60 UC", "price": "0.9900"},
                      {"id": 493, "name": "325 UC", "price": "4.9900"}],
        "fields": {"Character ID": {"type": "string", "required": True}},
        "servers": {}}

GAME_TWO_FIELDS = {"id": 125, "name": "Two Fields Game",
                   "positions": [{"id": 500, "name": "100 gems", "price": "1.5000"}],
                   "fields": {"Player ID": {"type": "string"}, "Server": {"type": "string"}},
                   "servers": {}}


def _orders_route(method="GET", path="/orders", json=None, data=None, params=None):
    if method == "POST":
        return {"order_id": 555, "status": "pending"}
    return {"items": [{"order_id": 555, "order_status": "success", "service_type": "mobile_refill",
                       "payment_method": "balance", "commission": "0.05", "final_amount": "1.04",
                       "created_at": "2026-10-24 10:00:00", "updated_at": "2026-10-24 10:05:00",
                       "completed_at": None, "error_code": "", "payment_url": "",
                       "service_result": None}],
            "total": 1, "limit": 20, "offset": 0}


DEFAULT_ROUTES = {
    # legacy (apikey)
    "/merchants/balance": {"balance": "50.0000"},
    "/merchants/transactions": {"transactions": []},
    "/exchange_rate/steam/5": {"exchange_rate": 90.0},
    "/exchange_rate/steam": {"exchange_rates": {"1": 1.0, "5": 90.0}},
    "/service/mobile/variant/1/games": {"games": [{"name": "PUBG Mobile", "id": 123},
                                                  {"name": "Free Fire", "id": 124}]},
    "/service/mobile/variant/1/games/123": GAME,
    "/service/mobile/variant/1/games/125": GAME_TWO_FIELDS,
    "/service/mobile/variant/1/games/refill": {"transaction_id": "tx-1", "status": "pending",
                                               "error_code": 0},
    "/merchants/transaction/tx-1/status": {"status": "success"},
    # v2 (HMAC)
    "/balance": {"balance": "50.0000", "available_balance": "48.0000", "overdraft": "0.0000",
                 "reserve": "2.0000"},
    "/orders": _orders_route,
    "/orders/555": {"order_id": 555, "order_status": "success", "service_type": "mobile_refill",
                    "payment_method": "balance", "commission": "0.05", "final_amount": "1.04",
                    "created_at": "2026-10-24 10:00:00", "updated_at": "2026-10-24 10:05:00",
                    "completed_at": None, "error_code": "", "payment_url": "",
                    "service_result": {"position": 492, "fields": {"Character ID": "777"}}},
    "/catalog/mobile-refill/games": {"games": [{"name": "PUBG Mobile", "id": 123}]},
    "/catalog/mobile-refill/games/123": GAME,
    "/currency/exchange_rates/steam/5": {"exchange_rate": 90.0},
    "/currency/exchange_rates/steam": {"exchange_rates": {"1": 1.0, "5": 90.0}},
}

PENDING_ROUTES = {"/merchants/transaction/tx-1/status": {"status": "pending"}}

import hashlib
import hmac as hmac_lib
import importlib.util
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid as uuid_lib
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

_spec = importlib.util.spec_from_file_location(f"plugins.{MODULE_NAME}", f"plugins/{MODULE_NAME}.py")
plugin = importlib.util.module_from_spec(_spec)
sys.modules[f"plugins.{MODULE_NAME}"] = plugin
_spec.loader.exec_module(plugin)

import telebot.types as tt  # noqa: E402
from FunPayAPI import types  # noqa: E402
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


# ══════════════════════════════════════ МОКИ ══════════════════════════════════════
class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakeSession:
    """Заменяет requests.Session: отвечает заготовками и пишет все запросы в self.calls."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []
        self.fail_times = 0

    def request(self, method, url, headers=None, json=None, data=None, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "json": json, "data": data, "params": params, "timeout": timeout})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("connection reset by peer")
        path = url.split("/api/v1")[-1].split("?")[0]
        for key in sorted(self.routes, key=len, reverse=True):
            if path == key or path.startswith(key):
                value = self.routes[key]
                if callable(value):
                    value = value(method=method, path=path, json=json, data=data, params=params)
                if isinstance(value, tuple):
                    return FakeResponse(value[0], value[1])
                return FakeResponse(value)
        return FakeResponse({"error_code": -1}, 200)

    def posts(self, path=None):
        result = [i for i in self.calls if i["method"] == "POST"]
        if path:
            result = [i for i in result if path in i["url"]]
        return result

    def post_body(self, path):
        """Тело последнего POST на path (json-парс из data или json)."""
        calls = self.posts(path)
        if not calls:
            return None
        call = calls[-1]
        if call.get("data"):
            return json.loads(call["data"].decode("utf-8"))
        return call.get("json")


class FakeBot:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.answers = []
        self.deleted = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=len(self.messages), text=text)

    def reply_to(self, message, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=1), id=len(self.messages), text=text)

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edits.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=message_id, text=text)

    def answer_callback_query(self, callback_id, text=None, **kwargs):
        self.answers.append(text)

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeTelegram:
    def __init__(self):
        self.bot = FakeBot()
        self.authorized_users = {1: {}}
        self.notifications = []
        self.msg_handlers = []
        self.cbq_handlers = []
        self.user_states = {}
        self.commands = {}

    def send_notification(self, text, keyboard=None, notification_type="10", photo=None, pin=False):
        self.notifications.append((text, notification_type))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def add_command_to_menu(self, command, help_text):
        self.commands[command] = help_text

    def get_state(self, chat_id, user_id):
        try:
            return self.user_states[chat_id][user_id]
        except KeyError:
            return None

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states.setdefault(chat_id, {})[user_id] = {"state": state, "mid": message_id,
                                                             "data": data or {}}

    def clear_state(self, chat_id, user_id, del_msg=False):
        try:
            mid = self.user_states[chat_id][user_id].get("mid")
            del self.user_states[chat_id][user_id]
            if del_msg:
                self.bot.delete_message(chat_id, mid)
            return mid
        except KeyError:
            return None

    def check_state(self, chat_id, user_id, state):
        try:
            return self.user_states[chat_id][user_id]["state"] == state
        except KeyError:
            return False


class FakeAccount:
    def __init__(self):
        self.id = 111
        self.username = "seller"
        self.orders = {}
        self.chats = {}

    def get_order(self, order_id, **kwargs):
        return self.orders.get(str(order_id))

    def get_chat_by_name(self, name, make_request=False):
        return self.chats.get(name)


class FakeCardinal:
    def __init__(self):
        self.telegram = FakeTelegram()
        self.account = FakeAccount()
        self.blacklist = ["badguy"]
        self.run_id = 1
        self.sent = []
        self.registered_commands = []
        self.old_mode_enabled = False

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands.append((uuid, commands))

    def send_message(self, chat_id, message_text, chat_name=None, interlocutor_id=None, attempts=3,
                     watermark=True):
        self.sent.append({"chat_id": chat_id, "text": message_text, "chat_name": chat_name,
                          "watermark": watermark})
        return [SimpleNamespace(id=1, text=message_text, chat_id=chat_id)]


class SyncThread:
    """Заменяет threading.Thread: выполняет функцию сразу, синхронно (детерминированные тесты)."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs = target, args or (), kwargs or {}
        self.started = False

    def start(self):
        self.started = True
        if self.target:
            self.target(*self.args, **self.kwargs)

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


class FakeTgMessage:
    def __init__(self, text, chat_id=1, user_id=1):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.from_user = SimpleNamespace(id=user_id, username="tester", language_code="ru")
        self.id = 1
        self.content_type = "text"


class FakeChatMessage:
    """Сообщение покупателя в чате FunPay (для NewMessageEvent)."""

    def __init__(self, text, chat_id=888, author_id=999, by_bot=False):
        self.text = text
        self.chat_id = chat_id
        self.author_id = author_id
        self.by_bot = by_bot
        self.id = 1
        self.is_image = False
        self.is_audio = False

    def __str__(self):
        return self.text


def FakeCallback(data, chat_id=1, user_id=1):
    """Настоящий telebot.types.CallbackQuery - плагин проверяет тип через isinstance."""
    return tt.CallbackQuery.de_json({
        "id": f"cbq-{abs(hash(data)) % 10 ** 9}",
        "from": {"id": user_id, "is_bot": False, "first_name": "Tester", "username": "tester"},
        "chat_instance": "1",
        "data": data,
        "message": {"message_id": 5, "date": 1700000000,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": 111, "is_bot": True, "first_name": "Bot"},
                    "text": "/menu"},
    })


def make_order_full(order_id="900001", fields=None, price=1500.0, buyer="buyer1",
                    currency=types.Currency.RUB, status=types.OrderStatuses.PAID):
    """Настоящий объект FunPayAPI.types.Order с заполненными полями покупателя."""
    lot_fields = {}
    for index, (name, value) in enumerate((fields or {}).items()):
        key = f"f{index + 1}"
        lot_fields[key] = types.LotField(key, value, name)
    return types.Order(order_id, status, None, None, None, lot_fields, 1, price, currency, None,
                       999, buyer, 111, "seller", 888, None, [], "ru")


def make_order_short(order_id="900001", description="Тестовый лот", price=1500.0, buyer="buyer1",
                     status=types.OrderStatuses.PAID):
    import datetime as _dt
    return types.OrderShortcut(order_id, description, price, types.Currency.RUB, buyer, 999, 888,
                               status, _dt.datetime.now(), "Steam, Игры", None, "<html>")


def make_event(order_id="900001", description="Тестовый лот", lot_name=None, lot_id=None,
               price=1500.0, buyer="buyer1"):
    event = NewOrderEvent("tag", make_order_short(order_id, description, price, buyer))
    event.config_section_name = lot_name
    event.config_section_obj = None
    event.lot_id = lot_id
    event.lot_shortcut = None
    event.delivered = False
    return event


# ══════════════════════════════ ПЕСОЧНИЦА ══════════════════════════════
TMP = tempfile.mkdtemp(prefix=f"{MODULE_NAME}_tests_")


def sandbox(settings=None, routes=None, orders=None, sync_threads=True):
    """
    Готовит чистое окружение: временные файлы настроек, кардинал с моками,
    синхронные потоки и (опционально) маршруты DesslyHub.
    """
    plugin.PLUGIN_DIR = TMP
    plugin.SETTINGS_PATH = os.path.join(TMP, "settings.json")
    plugin.ORDERS_PATH = os.path.join(TMP, "orders.json")
    plugin.CACHE_PATH = os.path.join(TMP, "catalog.json")
    for path in (plugin.SETTINGS_PATH, plugin.ORDERS_PATH, plugin.CACHE_PATH):
        if os.path.exists(path):
            os.remove(path)
    plugin._settings = {}
    plugin._orders = {}
    plugin._last_low_balance_alert = 0.0
    plugin._thread_started = False
    for extra_global in ("_esim_pages", "_esim_variants"):
        if hasattr(plugin, extra_global):
            setattr(plugin, extra_global, [])
    if sync_threads:
        plugin.threading = SimpleNamespace(Thread=SyncThread)
    current = plugin.load_settings()
    current.update({"enabled": True, "dry_run": False, "apikey": "test-key", "secret": "test-sec",
                    "api_mode": "legacy", "poll_interval": 3})
    if settings:
        current.update(settings)
    plugin.save_settings(current)
    cardinal = FakeCardinal()
    session = FakeSession(DEFAULT_ROUTES.copy() if routes is None else routes)

    def _make_client(s=None):
        s = s or plugin.load_settings()
        return plugin.DesslyClient(s.get("apikey"), s.get("secret"), s.get("base_url"),
                                   s.get("api_mode"), 20, 0, session=session)

    plugin.make_client = _make_client
    for order_id, order in (orders or {"900001": make_order_full()}).items():
        cardinal.account.orders[order_id] = order
    return cardinal, session


def notifications(cardinal, needle=None):
    texts = [t for t, _ in cardinal.telegram.notifications]
    if needle is None:
        return texts
    return [t for t in texts if needle in t]


def sent_texts(cardinal, needle=None):
    texts = [i["text"] for i in cardinal.sent]
    if needle is None:
        return texts
    return [t for t in texts if needle in t]


# ══════════════════════════════ ОБЩИЕ ТЕСТЫ ══════════════════════════════
def test_metadata():
    check("NAME на английском", bool(plugin.NAME) and plugin.NAME.isascii())
    check("VERSION формата X.Y.Z", bool(re.fullmatch(r"\d+\.\d+\.\d+", plugin.VERSION)))
    check("CREDITS = yungspider", plugin.CREDITS == "yungspider")
    try:
        uuid_lib.UUID(plugin.UUID)
        valid_uuid = True
    except ValueError:
        valid_uuid = False
    check("UUID валиден", valid_uuid)
    check("SETTINGS_PAGE = True", plugin.SETTINGS_PAGE is True)
    doc = inspect.getdoc(plugin) or ""
    check("докстринг: Автор yungspider", "Автор: yungspider" in doc)
    check("докстринг: блок Возможности", "Возможности:" in doc)
    check("докстринг: FunPay Cardinal v0.1.17+", "v0.1.17+" in doc)
    check("DESCRIPTION на русском", any("а" <= ch <= "я" for ch in plugin.DESCRIPTION.lower()))
    check("BIND_TO_PRE_INIT есть", getattr(plugin, "BIND_TO_PRE_INIT", None))
    check("BIND_TO_POST_START есть", getattr(plugin, "BIND_TO_POST_START", None))
    check("BIND_TO_DELETE есть", getattr(plugin, "BIND_TO_DELETE", None) is not None)


def test_settings():
    plugin.PLUGIN_DIR = TMP
    plugin.SETTINGS_PATH = os.path.join(TMP, "settings.json")
    plugin.ORDERS_PATH = os.path.join(TMP, "orders.json")
    plugin.CACHE_PATH = os.path.join(TMP, "catalog.json")
    if os.path.exists(plugin.SETTINGS_PATH):
        os.remove(plugin.SETTINGS_PATH)
    plugin._settings = {}
    settings = plugin.load_settings()
    check("по умолчанию выключен", settings.get("enabled") is False)
    check("по умолчанию DRY-RUN", settings.get("dry_run") is True)
    check("файл настроек создан", os.path.exists(plugin.SETTINGS_PATH))
    raw = json.load(open(plugin.SETTINGS_PATH, encoding="utf-8"))
    check("api_mode по умолчанию auto", raw.get("api_mode") == "auto")
    # поврежденный файл -> дефолты
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write("not a json {{{")
    plugin._settings = {}
    settings = plugin.load_settings(force=True)
    check("битые настройки -> дефолты", settings.get("dry_run") is True)
    # приведение типов
    raw = dict(settings)
    raw["min_balance_usd"] = "7.5"
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    plugin._settings = {}
    settings = plugin.load_settings(force=True)
    check("строка -> float для min_balance_usd", settings.get("min_balance_usd") == 7.5)


def test_client_auth_legacy():
    c, session = sandbox(settings={"api_mode": "legacy", "secret": ""})
    client = plugin.make_client()
    balance = client.balance()
    check("legacy: баланс получен", balance.get("balance") is not None)
    call = session.calls[-1]
    check("legacy: заголовок apikey", call["headers"].get("apikey") == "test-key")
    check("legacy: путь /merchants/balance", "/merchants/balance" in call["url"])
    check("legacy: нет HMAC-заголовков", "X-Signature" not in call["headers"])


def test_client_auth_v2():
    c, session = sandbox(settings={"api_mode": "v2"})
    client = plugin.make_client()
    balance = client.balance()
    check("v2: баланс получен", balance.get("balance") is not None)
    call = session.calls[-1]
    check("v2: путь /balance", call["url"].endswith("/api/v1/balance"))
    check("v2: заголовок X-Api-Key", call["headers"].get("X-Api-Key") == "test-key")
    timestamp = call["headers"].get("X-Timestamp", "")
    expected = hmac_lib.new(b"test-sec", ("test-key" + timestamp).encode("utf-8"),
                            hashlib.sha256).hexdigest()
    check("v2: подпись HMAC для GET (пустое тело)", call["headers"].get("X-Signature") == expected)
    check("v2: timestamp числовой", timestamp.isdigit())


def test_client_auth_v2_post_signature():
    c, session = sandbox(settings={"api_mode": "v2"})
    client = plugin.make_client()
    client.create_order("steam_refill", {"username": "gamer", "amount": 5.0})
    call = session.posts("/orders")[-1]
    body = call["data"].decode("utf-8")
    timestamp = call["headers"]["X-Timestamp"]
    expected = hmac_lib.new(b"test-sec", ("test-key" + timestamp + body).encode("utf-8"),
                            hashlib.sha256).hexdigest()
    check("v2: подпись HMAC покрывает тело POST", call["headers"]["X-Signature"] == expected)
    parsed = json.loads(body)
    check("v2: тело POST компактное (separators)", body == json.dumps(parsed, ensure_ascii=False,
                                                                     separators=(",", ":")))
    check("v2: Content-Type json", call["headers"].get("Content-Type") == "application/json")


def test_resolve_mode():
    routes = DEFAULT_ROUTES.copy()
    c, session = sandbox(settings={"api_mode": "auto"}, routes=routes)
    client = plugin.make_client()
    check("auto: найден v2 (есть /balance)", client.resolve_mode() == "v2")
    routes_no_v2 = {k: v for k, v in DEFAULT_ROUTES.items() if not k.startswith("/balance")}
    c, session = sandbox(settings={"api_mode": "auto"}, routes=routes_no_v2)
    client = plugin.make_client()
    check("auto: откат на legacy", client.resolve_mode() == "legacy")
    c, session = sandbox(settings={"api_mode": "auto"}, routes={})
    client = plugin.make_client()
    check("auto: ничего не отвечает -> none", client.resolve_mode() == "none")
    try:
        client.require_mode()
        raised = False
    except plugin.DesslyError as e:
        raised = e.code == -5
    check("require_mode на none -> DesslyError(-5)", raised)
    c, session = sandbox(settings={"api_mode": "v2", "secret": ""})
    client = plugin.make_client()
    check("v2 без секрета -> none", client.resolve_mode() == "none")
    c, session = sandbox(settings={"api_mode": "legacy"})
    client = plugin.make_client()
    check("legacy игнорирует секрет", client.resolve_mode() == "legacy")


def test_client_errors_and_retries():
    c, session = sandbox(settings={"api_mode": "legacy"},
                         routes={"/merchants/balance": {"balance": "9.0"}})
    client = plugin.make_client()
    try:
        client.exchange_rate(5)   # маршрута нет -> дефолтный {"error_code": -1}
        got = None
    except plugin.DesslyError as e:
        got = e
    check("ошибка API -> DesslyError(-1)", got is not None and got.code == -1)
    session.fail_times = 2
    client_no_retry = plugin.DesslyClient("test-key", "", plugin.BASE_URL, "legacy", 20, 0,
                                          session=session)
    try:
        client_no_retry.balance()
        raised = False
    except plugin.DesslyError:
        raised = True
    check("сетевой сбой без ретраев -> ошибка", raised)
    session.fail_times = 2
    client_retry = plugin.DesslyClient("test-key", "", plugin.BASE_URL, "legacy", 20, 2,
                                       session=session)
    balance = client_retry.balance()
    check("сетевой сбой с ретраями -> успех", balance.get("balance") == 9.0)


def test_storage():
    c, _ = sandbox()
    plugin.set_record("1", status="done", lot="x")
    check("set_record/get_record", (plugin.get_record("1") or {}).get("status") == "done")
    for i in range(600):
        plugin.set_record(f"bulk-{i}", status="done")
    check("журнал подрезается", len(plugin.load_orders()) <= 510)
    plugin.save_cache("unit", [1, 2, 3])
    data, ts = plugin.load_cache("unit")
    check("кэш каталога работает", data == [1, 2, 3] and ts > 0)


def test_menu_ui():
    c, _ = sandbox()
    plugin.pre_init(c)
    registered = [cmd for _uuid, cmds in c.registered_commands for cmd, _t, _v in cmds]
    check(f"команда /{plugin.CMD} зарегистрирована", plugin.CMD in registered)
    plugin.on_cmd_command(FakeTgMessage("/" + plugin.CMD))
    check("меню открылось по команде", any(plugin.PLUG in m for m in c.telegram.bot.messages))
    unauthorized = FakeCallback(f"{plugin.CB}:menu", user_id=777)
    before = len(c.telegram.bot.messages)
    plugin.on_settings_callback(c, unauthorized)
    check("неавторизованный пользователь отбит", c.telegram.bot.answers and
          c.telegram.bot.answers[-1] == "Недостаточно прав.")
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:toggle_enabled"))
    check("toggle_enabled переключил", plugin.load_settings().get("enabled") is False)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:toggle_enabled"))
    check("toggle_enabled вернул", plugin.load_settings().get("enabled") is True)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:apimode"))
    check("apimode: legacy -> v2", plugin.load_settings().get("api_mode") == "v2")
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:apimode"))
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:apimode"))
    check("apimode: цикл v2 -> auto -> legacy", plugin.load_settings().get("api_mode") == "legacy")
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:close"))
    check("close закрывает меню", any("закрыто" in e for e in c.telegram.bot.edits))


def test_input_flow():
    c, _ = sandbox()
    plugin.pre_init(c)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:input:apikey"))
    check("ask_input выставил состояние",
          c.telegram.check_state(1, 1, plugin.INPUT_STATE))
    plugin.handle_input(FakeTgMessage("new-secret-key"))
    check("apikey сохранен", plugin.load_settings().get("apikey") == "new-secret-key")
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:input:min_balance_usd"))
    plugin.handle_input(FakeTgMessage("12.5"))
    check("min_balance_usd сохранен float", plugin.load_settings().get("min_balance_usd") == 12.5)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:input:min_balance_usd"))
    plugin.handle_input(FakeTgMessage("-3"))
    check("отрицательный порог отклонен", plugin.load_settings().get("min_balance_usd") == 12.5)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:input:apikey"))
    plugin.handle_input(FakeTgMessage("-"))
    check("ключ удален по '-'", plugin.load_settings().get("apikey") == "")


def test_delete():
    c, _ = sandbox()
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write("{}")
    plugin.on_delete(c, SimpleNamespace(id="call-1"))
    check("on_delete удалил папку плагина", not os.path.exists(plugin.PLUGIN_DIR))


def test_recover_interrupted():
    c, _ = sandbox()
    plugin.set_record("777", status="sending", lot="Лот")
    started = []

    def fake_thread(target=None, args=(), kwargs=None, daemon=None, name=None):
        def start():
            started.append(name)
            if name != f"{plugin.PLUG}Poller" and target:
                target(*(args or ()), **(kwargs or {}))
        return SimpleNamespace(start=start)

    plugin.threading = SimpleNamespace(Thread=fake_thread)
    plugin._thread_started = False
    plugin.post_start(c)
    record = plugin.get_record("777")
    check("прерванная отправка -> error", record.get("status") == "error")
    check("уведомление о прерванном заказе", bool(notifications(c, "оборвался")))
    check("поток опроса запущен", f"{plugin.PLUG}Poller" in started)


class RunIdController:
    """Кардинал, у которого run_id «меняется» после N обращений - так останавливается поллер."""

    def __init__(self, cardinal, allow=8):
        self.telegram = cardinal.telegram
        self.account = cardinal.account
        self.blacklist = cardinal.blacklist
        self.sent = cardinal.sent
        self.old_mode_enabled = False
        self._allow = allow
        self._calls = 0

    @property
    def run_id(self):
        self._calls += 1
        return 0 if self._calls <= self._allow else 1

    @run_id.setter
    def run_id(self, value):
        pass


def test_poller_timeouts():
    """Поллер: зависшая транзакция -> timeout; ожидание данных -> timeout_data."""
    routes = dict(DEFAULT_ROUTES)
    routes.update(PENDING_ROUTES)
    c, _ = sandbox(settings={"poll_max_minutes": 0, "wait_data_minutes": 0, "api_mode": "legacy"},
                   routes=routes)
    real_time = plugin.time
    plugin.time = SimpleNamespace(time=time.time, sleep=lambda s: None)
    try:
        now = int(time.time())
        plugin.set_record("t1", status="waiting_result", txn=TEST_TXN, sent_at=now - 120,
                          created=now - 120, chat_id=888, buyer="buyer1")
        plugin.set_record("t2", status="waiting_data", asked_at=now - 120, created=now - 120,
                          chat_id=888, buyer="buyer1")
        c.account.orders["t1"] = make_order_full("t1")
        plugin.poller_loop(RunIdController(c, allow=8), 0)
        check("поллер: зависшая транзакция -> timeout", plugin.get_record("t1").get("status") == "timeout")
        check("поллер: ожидание данных -> timeout_data",
              plugin.get_record("t2").get("status") == "timeout_data")
        check("поллер: уведомление о зависшей транзакции", bool(notifications(c, "не завершилась")))
    finally:
        plugin.time = real_time


def test_low_balance_notify():
    routes = dict(DEFAULT_ROUTES)
    routes["/merchants/balance"] = {"balance": "1.0000"}
    c, _ = sandbox(settings={"min_balance_usd": 5.0, "api_mode": "legacy"}, routes=routes)
    plugin.check_low_balance(c)
    check("низкий баланс -> уведомление", bool(notifications(c, "баланс DesslyHub опустился")))


# ══════════════════════════════ ТЕСТЫ ПЛАГИНА ══════════════════════════════
def add_mapping(name="Донат PUBG 60 UC", game_id=123, position_id=492, variant=None):
    settings = plugin.load_settings()
    mappings = dict(settings.get("mappings") or {})
    mapping = {"game_id": game_id, "position_id": position_id}
    if variant:
        mapping["variant"] = variant
    mappings[name] = mapping
    settings["mappings"] = mappings
    plugin.save_settings(settings)


def test_mappings():
    c, _ = sandbox()
    plugin.apply_mapping("Донат PUBG 60 UC | 123 | 492")
    plugin.apply_mapping("Донат FF | 124 | 77 | 2")
    mappings = plugin.load_settings()["mappings"]
    check("привязка игра/номинал", mappings.get("Донат PUBG 60 UC") ==
          {"game_id": 123, "position_id": 492})
    check("привязка с источником", mappings.get("Донат FF") ==
          {"game_id": 124, "position_id": 77, "variant": 2})
    try:
        plugin.apply_mapping("Лот | abc | 492")
        raised = False
    except plugin.PluginConfigError:
        raised = True
    check("нечисловой ID игры -> ошибка", raised)
    try:
        plugin.apply_mapping("Лот | 123 | 492 | 5")
        raised = False
    except plugin.PluginConfigError:
        raised = True
    check("источник != 1/2 -> ошибка", raised)
    check("render_mapping", "123" in plugin.render_mapping("Донат PUBG 60 UC",
                                                           mappings["Донат PUBG 60 UC"]))


def test_field_extraction():
    c, _ = sandbox()
    settings = plugin.load_settings()
    order = make_order_full(fields={"Character ID": "123456789"})
    values = plugin.collect_fields(order, settings, ["Character ID"], {})
    check("поле найдено по точному имени", values == {"Character ID": "123456789"})
    settings["field_aliases"] = {"Character ID": ["uid", "айди"]}
    order2 = make_order_full(fields={"UID игрока": "555"})
    values = plugin.collect_fields(order2, settings, ["Character ID"], {})
    check("поле найдено по псевдониму", values == {"Character ID": "555"})
    found = plugin.parse_field_message("Character ID: 777", ["Character ID"], settings)
    check("parse: поле: значение", found == {"Character ID": "777"})
    found = plugin.parse_field_message("777", ["Character ID"], settings)
    check("parse: одно поле одной строкой", found == {"Character ID": "777"})
    found = plugin.parse_field_message("айди: 888", ["Character ID"], settings)
    check("parse: по псевдониму", found == {"Character ID": "888"})
    found = plugin.parse_field_message("привет", ["Character ID", "Server"], settings)
    check("parse: мусор -> пусто", found == {})


def test_dry_run_flow():
    c, session = sandbox(settings={"dry_run": True})
    add_mapping()
    c.account.orders["900001"] = make_order_full(fields={"Character ID": "123456789"})
    plugin.on_new_order(c, make_event(lot_name="Донат PUBG 60 UC"))
    record = plugin.get_record("900001")
    check("dry-run: статус dry_run", record.get("status") == "dry_run")
    check("dry-run: покупок не было",
          not session.posts("/service/mobile/variant/1/games/refill") and
          not session.posts("/orders"))
    check("dry-run: уведомление продавцу", bool(notifications(c, "DRY-RUN")))


def test_real_flow_legacy():
    c, session = sandbox()
    add_mapping()
    c.account.orders["900001"] = make_order_full(fields={"Character ID": "123456789"})
    plugin.on_new_order(c, make_event(lot_name="Донат PUBG 60 UC"))
    record = plugin.get_record("900001")
    check("legacy: статус waiting_result", record.get("status") == "waiting_result")
    body = session.post_body("/service/mobile/variant/1/games/refill")
    check("legacy: тело запроса",
          body == {"position": 492, "fields": {"Character ID": "123456789"},
                   "reference": "funpay-900001"})
    plugin.poll_record(c, "900001")
    record = plugin.get_record("900001")
    check("legacy: success -> done", record.get("status") == "done")
    check("legacy: покупателю ушла игра", bool(sent_texts(c, "PUBG Mobile")))
    check("legacy: покупателю ушел номинал", bool(sent_texts(c, "60 UC")))


def test_real_flow_v2():
    c, session = sandbox(settings={"api_mode": "v2"})
    add_mapping()
    c.account.orders["900001"] = make_order_full(fields={"Character ID": "123456789"})
    plugin.on_new_order(c, make_event(lot_name="Донат PUBG 60 UC"))
    record = plugin.get_record("900001")
    check("v2: статус waiting_result", record.get("status") == "waiting_result")
    check("v2: order_id как txn", record.get("txn") == "555")
    body = session.post_body("/orders")
    check("v2: тело /orders",
          body == {"payment_method": "balance", "service_type": "mobile_refill",
                   "service_params": {"position": 492, "fields": {"Character ID": "123456789"}},
                   "reference": "funpay-900001"})
    plugin.poll_record(c, "900001")
    check("v2: success -> done", plugin.get_record("900001").get("status") == "done")
    check("v2: spent из final_amount", plugin.get_record("900001").get("spent_usd") == 1.04)


def test_position_not_found():
    c, session = sandbox()
    add_mapping(position_id=999)
    c.account.orders["900001"] = make_order_full(fields={"Character ID": "123456789"})
    plugin.on_new_order(c, make_event(lot_name="Донат PUBG 60 UC"))
    record = plugin.get_record("900001")
    check("нет номинала -> failed(-201)", record.get("status") == "failed" and
          record.get("error_code") == -201)
    check("нет номинала: покупки не было", not session.posts("/refill"))


def test_missing_fields_flow():
    c, session = sandbox()
    add_mapping()
    c.account.orders["900001"] = make_order_full(fields={})
    plugin.on_new_order(c, make_event(lot_name="Донат PUBG 60 UC"))
    record = plugin.get_record("900001")
    check("нет полей: waiting_data", record.get("status") == "waiting_data")
    check("нет полей: запрос перечисляет поля", bool(sent_texts(c, "Character ID")))
    plugin.on_new_message(c, NewMessageEvent("tag", FakeChatMessage("Character ID: 777",
                                                                    chat_id=888)))
    record = plugin.get_record("900001")
    check("после данных: waiting_result", record.get("status") == "waiting_result")
    body = session.post_body("/service/mobile/variant/1/games/refill")
    check("покупка с данными из чата",
          body and body.get("fields") == {"Character ID": "777"})


def test_two_fields_partial():
    routes = dict(DEFAULT_ROUTES)
    c, session = sandbox(routes=routes)
    add_mapping(name="Two Fields", game_id=125, position_id=500)
    c.account.orders["900001"] = make_order_full(fields={"Player ID": "111"})
    plugin.on_new_order(c, make_event(lot_name="Two Fields"))
    record = plugin.get_record("900001")
    check("два поля: waiting_data", record.get("status") == "waiting_data")
    check("два поля: первое уже собрано",
          (record.get("fields_data") or {}).get("Player ID") == "111")
    check("два поля: запрошено недостающее", bool(sent_texts(c, "Server")))
    plugin.on_new_message(c, NewMessageEvent("tag", FakeChatMessage("Server: EU", chat_id=888)))
    record = plugin.get_record("900001")
    check("два поля: после второго - waiting_result", record.get("status") == "waiting_result")
    body = session.post_body("/service/mobile/variant/1/games/refill")
    check("два поля: оба в запросе",
          body and body.get("fields") == {"Player ID": "111", "Server": "EU"})


def test_guards():
    c, session = sandbox(settings={"max_order_cost_usd": 0.5})
    add_mapping()
    c.account.orders["900001"] = make_order_full(fields={"Character ID": "123456789"})
    plugin.on_new_order(c, make_event(lot_name="Донат PUBG 60 UC"))
    check("лимит стоимости -> failed(-3)",
          plugin.get_record("900001").get("error_code") == -3)
    c, session = sandbox(settings={"min_margin_percent": 99.0})
    add_mapping()
    c.account.orders["900001"] = make_order_full(fields={"Character ID": "123456789"})
    plugin.on_new_order(c, make_event(lot_name="Донат PUBG 60 UC"))
    check("маржа ниже порога -> failed",
          plugin.get_record("900001").get("status") == "failed")


def test_idempotency():
    c, session = sandbox()
    add_mapping()
    c.account.orders["900001"] = make_order_full(fields={"Character ID": "123456789"})
    event = make_event(lot_name="Донат PUBG 60 UC")
    plugin.on_new_order(c, event)
    plugin.on_new_order(c, event)
    check("ровно одна покупка на заказ",
          len(session.posts("/service/mobile/variant/1/games/refill")) == 1)


def test_catalog_ui():
    c, _ = sandbox()
    plugin.pre_init(c)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:catalog"))
    check("каталог: запрошен поиск", c.telegram.check_state(1, 1, plugin.INPUT_STATE))
    plugin.handle_input(FakeTgMessage("PUBG"))
    check("каталог: игра найдена",
          any("PUBG Mobile" in m for m in c.telegram.bot.messages))
    plugin.on_extra_callback(c, FakeCallback(f"{plugin.CB}:positions:123"), "positions:123")
    check("позиции: номиналы показаны",
          any("492" in m and "60 UC" in m for m in c.telegram.bot.messages + c.telegram.bot.edits))
    check("позиции: обязательные поля показаны",
          any("Character ID" in m for m in c.telegram.bot.messages + c.telegram.bot.edits))


def test_aliases_ui():
    c, _ = sandbox()
    plugin.pre_init(c)
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:aliases"))
    check("псевдонимы: экран открыт",
          any("Псевдонимы полей" in m for m in c.telegram.bot.messages + c.telegram.bot.edits))
    plugin.on_settings_callback(c, FakeCallback(f"{plugin.CB}:input:field_alias"))
    plugin.handle_input(FakeTgMessage("Character ID: uid, айди"))
    aliases = plugin.load_settings().get("field_aliases") or {}
    check("псевдонимы сохранены", aliases.get("Character ID") == ["uid", "айди"])


def main():
    tests = [test_metadata, test_settings, test_client_auth_legacy, test_client_auth_v2,
             test_client_auth_v2_post_signature, test_resolve_mode, test_client_errors_and_retries,
             test_storage, test_menu_ui, test_input_flow, test_recover_interrupted,
             test_low_balance_notify, test_poller_timeouts,
             test_mappings, test_field_extraction, test_dry_run_flow, test_real_flow_legacy,
             test_real_flow_v2, test_position_not_found, test_missing_fields_flow,
             test_two_fields_partial, test_guards, test_idempotency, test_catalog_ui,
             test_aliases_ui, test_delete]
    for test in tests:
        print(f"\n──── {test.__name__} ────")
        try:
            test()
        except Exception:
            import traceback
            traceback.print_exc()
            FAIL.append(f"{test.__name__}: EXCEPTION")
    print(f"\n{'=' * 60}")
    print(f"Результат: {len(PASS)} пройдено, {len(FAIL)} провалено")
    for name in FAIL:
        print("  ✗", name)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
