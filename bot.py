"""
Telegram-бот для бухгалтерських послуг ФОП
Повністю переписаний з покращеннями:
  - Персистентний стан (JSON)
  - Rate limiting / антиспам
  - Статистика для адміна (/stats, /chats, /broadcast)
  - Thread-safe операції
  - Тайм-аут очікування + авто-нагадування
  - Централізована маршрутизація callback
  - Уніфікований error handler
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from html import escape
from pathlib import Path
from typing import Any

import requests
from flask import Flask, request

# ════════════════════════════════════════════════════════════════
#  ЛОГУВАННЯ
# ════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fop_bot")

# ════════════════════════════════════════════════════════════════
#  КОНФІГУРАЦІЯ
# ════════════════════════════════════════════════════════════════
TOKEN    = os.getenv("API_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
STATE_FILE = Path(os.getenv("STATE_FILE", "bot_state.json"))

if not TOKEN:
    raise ValueError("Змінна середовища API_TOKEN не встановлена!")
if not ADMIN_ID:
    raise ValueError("Змінна середовища ADMIN_ID не встановлена або дорівнює 0!")

# Антиспам: не більше MAX_MSG повідомлень за COOLDOWN секунд
SPAM_MAX_MSG  = int(os.getenv("SPAM_MAX_MSG", "5"))
SPAM_COOLDOWN = int(os.getenv("SPAM_COOLDOWN", "10"))

# Через скільки хвилин нагадати адміну про нового pending-запиту
PENDING_REMIND_MIN = int(os.getenv("PENDING_REMIND_MIN", "15"))

TG_API = f"https://api.telegram.org/bot{TOKEN}"

app = Flask(__name__)

# ════════════════════════════════════════════════════════════════
#  РЕКВІЗИТИ (єдине місце)
# ════════════════════════════════════════════════════════════════
PAYMENT_DETAILS = (
    "💳 Оплата здійснюється на офіційний рахунок ФОП\n\n"
    "<b>[ТУТ БУДУТЬ ВАШІ РЕКВІЗИТИ]</b>\n\n"
    "❤️ <b>ОБОВ'ЯЗКОВО:</b> після оплати надішліть, будь ласка, "
    "чек або скрін на @CreatorBotInst або в розділ «Повідомлення»"
)

# ════════════════════════════════════════════════════════════════
#  ЦІНИ (єдине місце)
# ════════════════════════════════════════════════════════════════
PRICES = {
    "consult_20":  ("20 хв",  "400 грн"),
    "consult_40":  ("40 хв",  "800 грн"),
    "support_1":   ("Група 1", "700 грн/міс"),
    "support_2":   ("Група 2", "1 000 грн/міс"),
    "support_3":   ("Група 3", "1 200 грн/міс"),
    "fop_register": ("Реєстрація ФОП",  "2 500 грн"),
    "fop_close":    ("Закриття ФОП",    "2 000 грн"),
    "report_submit": ("Подача звіту",   "за домовленістю"),
    "tax_check":    ("Перевірка ФОП",   "800 грн"),
    "prro_register": ("Реєстрація ПРРО","2 000 грн"),
    "prro_close":   ("Закриття ПРРО",   "1 800 грн"),
    "decret":       ("Декрет ФОП",      "3 000 грн"),
}

# ════════════════════════════════════════════════════════════════
#  THREAD-SAFE СТАН
# ════════════════════════════════════════════════════════════════
_lock = threading.Lock()

def _default_state() -> dict:
    return {
        "active_chats":    {},   # uid -> "pending" | "active"
        "admin_target":    None,
        "consult_request": {},   # uid -> {stage, duration}
        "reports_request": {},   # uid -> {stage, type}
        "support_request": {},   # uid -> {stage, group}
        "decret_request":  {},   # uid -> {stage}
        "spam_tracker":    {},   # uid -> [timestamps]
        "pending_since":   {},   # uid -> ISO timestamp
        "stats": {
            "total_users": 0,
            "seen_users":  [],
            "messages_in": 0,
            "chats_closed": 0,
            "service_clicks": {},
        },
    }

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge missing keys
            default = _default_state()
            for k, v in default.items():
                data.setdefault(k, v)
            # JSON keys are always str — convert uid keys to int
            for field in ("active_chats", "consult_request", "reports_request",
                          "support_request", "decret_request", "spam_tracker", "pending_since"):
                data[field] = {int(k): v for k, v in data.get(field, {}).items()}
            data["stats"]["seen_users"] = [int(u) for u in data["stats"].get("seen_users", [])]
            if data.get("admin_target") is not None:
                data["admin_target"] = int(data["admin_target"])
            return data
        except Exception as e:
            log.error("Не вдалося завантажити стан: %s — починаємо чисто", e)
    return _default_state()

def _save_state(state: dict) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(STATE_FILE)
    except Exception as e:
        log.error("Не вдалося зберегти стан: %s", e)

STATE = _load_state()

def with_state(fn):
    """Декоратор: захоплює лок, передає стан, зберігає після виконання."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _lock:
            result = fn(STATE, *args, **kwargs)
            _save_state(STATE)
        return result
    return wrapper

# ════════════════════════════════════════════════════════════════
#  АНТИСПАМ
# ════════════════════════════════════════════════════════════════
def is_spam(state: dict, uid: int) -> bool:
    now = time.time()
    tracker = state["spam_tracker"]
    times = [t for t in tracker.get(uid, []) if now - t < SPAM_COOLDOWN]
    times.append(now)
    tracker[uid] = times
    return len(times) > SPAM_MAX_MSG

# ════════════════════════════════════════════════════════════════
#  TELEGRAM API ХЕЛПЕРИ
# ════════════════════════════════════════════════════════════════
def _tg(method: str, **kwargs) -> dict | None:
    """Базовий виклик Telegram Bot API."""
    try:
        resp = requests.post(f"{TG_API}/{method}", json=kwargs, timeout=10)
        data = resp.json()
        if not data.get("ok"):
            log.warning("TG/%s failed: %s", method, data.get("description"))
        return data
    except Exception as e:
        log.error("TG/%s exception: %s", method, e)
        return None

def send(chat_id: int, text: str,
         markup=None, parse_mode: str = "HTML",
         disable_preview: bool = True) -> dict | None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if markup:
        payload["reply_markup"] = markup
    return _tg("sendMessage", **payload)

def answer_cb(callback_id: str, text: str = "", alert: bool = False) -> None:
    _tg("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)

def send_media(chat_id: int, msg: dict) -> bool:
    map_ = {
        "photo":    ("sendPhoto",    lambda m: m["photo"][-1]["file_id"]),
        "document": ("sendDocument", lambda m: m["document"]["file_id"]),
        "video":    ("sendVideo",    lambda m: m["video"]["file_id"]),
        "audio":    ("sendAudio",    lambda m: m["audio"]["file_id"]),
        "voice":    ("sendVoice",    lambda m: m["voice"]["file_id"]),
        "sticker":  ("sendSticker",  lambda m: m["sticker"]["file_id"]),
    }
    for key, (method, getter) in map_.items():
        if key in msg:
            payload: dict[str, Any] = {"chat_id": chat_id, key: getter(msg)}
            if "caption" in msg:
                payload["caption"] = msg["caption"]
            _tg(method, **payload)
            return True
    return False

def notify_admin(state: dict, text: str, markup=None) -> None:
    send(ADMIN_ID, text, markup=markup)

def admin_chat_markup(user_id: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✍️ Відповісти", "callback_data": f"reply_{user_id}"}],
            [{"text": "⛔ Завершити чат", "callback_data": f"close_{user_id}"}],
        ]
    }

# ════════════════════════════════════════════════════════════════
#  СКИДАННЯ СТАНУ КОРИСТУВАЧА
# ════════════════════════════════════════════════════════════════
def clear_user(state: dict, uid: int) -> None:
    for field in ("consult_request", "reports_request", "support_request",
                  "decret_request", "active_chats", "pending_since"):
        state[field].pop(uid, None)
    if state["admin_target"] == uid:
        state["admin_target"] = None

# ════════════════════════════════════════════════════════════════
#  РОЗМІТКИ МЕНЮ
# ════════════════════════════════════════════════════════════════
MAIN_REPLY = {
    "keyboard": [
        [{"text": "📋 Меню"}],
        [{"text": "💬 Поставити питання"}, {"text": "💳 Реквізити"}]
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False,
}

FINISH_CHAT_REPLY = {
    "keyboard": [[{"text": "⛔ Завершити чат"}]],
    "resize_keyboard": True,
    "one_time_keyboard": False,
}

BACK_TO_MENU_REPLY = {
    "keyboard": [[{"text": "↩️ Повернутися в меню"}]],
    "resize_keyboard": True,
    "one_time_keyboard": False,
}

def inline(*rows: list[dict]) -> dict:
    """Shortcut для inline_keyboard."""
    return {"inline_keyboard": list(rows)}

SERVICES_INLINE = inline(
    [{"text": "📞 Консультації",              "callback_data": "consult"}],
    [{"text": "🛡 Супровід ФОП",              "callback_data": "support"}],
    [{"text": "📝 Реєстрація / закриття ФОП", "callback_data": "regclose"}],
    [{"text": "📊 Звітність і податки",       "callback_data": "reports"}],
    [{"text": "🖥 Реєстрація / закриття ПРРО","callback_data": "prro"}],
    [{"text": "👶 Декрет ФОП",               "callback_data": "decret"}],
)

MENU_BTN = [{"text": "↩️ Головне меню", "callback_data": "main_menu"}]

# ════════════════════════════════════════════════════════════════
#  ТЕКСТИ
# ════════════════════════════════════════════════════════════════
T = {
"welcome": (
    "🌿 <b>Вітаю у бухгалтерському боті!</b>\n\n"
    "Я — ваш особистий бухгалтер та помічник з питань ФОП.\n"
    "Тут ви знайдете зрозумілу допомогу та професійні послуги.\n\n"
    "Оберіть потрібну послугу 👇"
),
"consult_intro": (
    "📞 <b>Консультація</b> — швидко, зручно і по суті\n\n"
    "▫️ 20 хв — 400 грн\n"
    "▫️ 40 хв — 800 грн\n\n"
    "Консультація проходить онлайн (Telegram / Instagram).\n"
    "Оберіть тривалість 👇"
),
"consult_contact": (
    "Чудово! 💼\n\n"
    "Щоб зафіксувати час консультації — <b>{duration} ({price})</b>, "
    "залиште, будь ласка:\n"
    "• Ім'я та прізвище\n"
    "• Нік в Instagram або Telegram\n"
    "• Зручний час для зв'язку"
),
"support_intro": (
    "🛡 <b>Супровід ФОП</b> — коли про ваш облік піклуються за вас\n\n"
    "✅ Перевірка правильності діяльності\n"
    "✅ Нагадування про терміни сплати податків\n"
    "✅ Новини і зміни у законодавстві\n"
    "✅ Ведення Книги обліку доходів\n"
    "✅ Консультаційна підтримка\n\n"
    "❗ Звітність оплачується додатково\n"
    "🕓 Термін — 1 місяць (з можливістю продовження)\n\n"
    "Оберіть вашу групу ФОП 👇"
),
"support_group": (
    "💼 <b>Супровід ФОП — Група {group}</b>\n\n"
    "Щомісячна сплата: єдиний податок, військовий збір, ЄСВ.\n"
    "Звітність — 1 раз на рік.\n\n"
    "💰 Вартість — <b>{price}</b>\n"
    "Додаткові послуги оплачуються окремо.\n"
    "Деталі узгоджуємо індивідуально!\n\n"
    "Що далі? 👇"
),
"regclose_intro": "Оберіть потрібну послугу 👇",
"fop_register": (
    "📝 <b>Реєстрація ФОП «під ключ»</b>\n\n"
    "Що входить:\n"
    "• Консультація щодо КВЕДів та системи оподаткування\n"
    "• Підготовка та подання документів\n"
    "• Отримання виписки з ЄДР\n"
    "• Реєстрація в податковій / як платника єдиного податку\n"
    "• Консультація для подальшої роботи\n\n"
    "⏱ Термін: 1–2 робочі дні\n"
    "💰 Вартість — <b>2 500 грн</b>"
),
"fop_close": (
    "📝 <b>Закриття ФОП</b>\n\n"
    "Що входить:\n"
    "• Консультація щодо процедури закриття\n"
    "• Підготовка та подання заяви до держреєстратора\n"
    "• Здача фінальної звітності до податкової\n"
    "• Отримання підтвердження про припинення діяльності\n\n"
    "⏱ Термін: 3–7 робочих днів\n"
    "💰 Вартість — <b>2 000 грн</b>"
),
"reports_intro": (
    "📊 <b>Звітність і податки</b>\n\n"
    "📂 <b>Подача звіту</b>\n"
    "Підготую і здам усі декларації без помилок і штрафів.\n\n"
    "💰 <b>Перевірка ФОП / сплата податків</b>\n"
    "Перевірю борги, суми та строки — підкажу, як сплатити правильно.\n\n"
    "Оберіть 👇"
),
"report_submit": (
    "📂 <b>Подача звітності</b>\n\n"
    "Що входить:\n"
    "• Підготовка та подача податкової декларації\n"
    "• Звітність по ЄСВ та єдиному податку\n"
    "• Контроль строків\n"
    "• Повідомлення про успішну здачу\n\n"
    "Результат: звітність здана вчасно і без штрафів ✅"
),
"report_submit_contact": (
    "🙌 <b>Чудово!</b>\n\n"
    "Щоб підготувати все правильно, надішліть:\n"
    "1️⃣ ПІБ та ІПН (як у ФОП)\n"
    "2️⃣ Електронний ключ та пароль\n"
    "3️⃣ Період звітності (напр.: 3 квартал 2025)"
),
"tax_check": (
    "💰 <b>Перевірка ФОП та сплата податків</b>\n\n"
    "Що входить:\n"
    "• Перевірка стану ФОП у податковій\n"
    "• Визначення боргів і штрафів\n"
    "• Консультація щодо строків сплати\n"
    "• Підтримка при проведенні оплати\n\n"
    "💰 Вартість — <b>800 грн</b>"
),
"tax_check_contact": (
    "😊 <b>Готово!</b>\n\n"
    "Надішліть, будь ласка:\n"
    "1. ІПН\n"
    "2. ПІБ, як у реєстрації ФОП\n"
    "3. Електронний ключ та пароль\n\n"
    "Перевірю і повідомлю про наявні зобов'язання."
),
"prro_intro": (
    "🖥 <b>ПРРО — програмний реєстратор розрахункових операцій</b>\n\n"
    "1️⃣ <b>Реєстрація ПРРО</b> — швидко і без помилок\n"
    "2️⃣ <b>Закриття ПРРО</b> — якщо він більше не потрібен\n\n"
    "Оберіть 👇"
),
"prro_register": (
    "🖥 <b>Реєстрація ПРРО</b>\n\n"
    "Що входить:\n"
    "• Консультація щодо вибору ПРРО\n"
    "• Підготовка документів і реєстрація в ДПС\n"
    "• Навчання та консультація щодо використання\n"
    "• Отримання підтвердження від податкової\n\n"
    "💰 Вартість — <b>2 000 грн</b>"
),
"prro_register_contact": (
    "💪 <b>Дякую за вибір!</b>\n\n"
    "Надішліть, будь ласка:\n"
    "1. Назву бізнесу або ПІБ підприємця\n"
    "2. ІПН\n"
    "3. Електронний ключ та пароль\n"
    "4. Яке ПРРО хочете (якщо не знаєте — допоможу обрати)\n\n"
    "Нижче — реквізити для оплати."
),
"prro_close": (
    "🖥 <b>Закриття ПРРО</b>\n\n"
    "Що входить:\n"
    "• Консультація щодо процесу закриття\n"
    "• Підготовка документів і подача заяви\n"
    "• Контроль статусу заявки\n\n"
    "💰 Вартість — <b>1 800 грн</b>"
),
"prro_close_contact": (
    "📋 Для закриття ПРРО надайте:\n\n"
    "• ПІБ або назву бізнесу\n"
    "• ІПН\n"
    "• Електронний ключ та пароль\n\n"
    "Нижче — реквізити для оплати."
),
"decret": (
    "👶 <b>Декрет ФОП</b>\n\n"
    "Що входить:\n"
    "• Консультація щодо прав на виплати\n"
    "• Підготовка та оформлення документів\n"
    "• Подача заяв до держорганів\n"
    "• Контроль статусу та підтримка\n\n"
    "💰 Вартість — <b>3 000 грн</b>"
),
"decret_contact": (
    "📋 Для оформлення декретних надайте:\n\n"
    "• Повні ПІБ заявника\n"
    "• Дату початку декрету або очікувану дату пологів\n"
    "• Контактний телефон\n\n"
    "Підготуємо документи і розпочнемо процедуру."
),
"ask_question": "Очікуйте відповіді адміністратора... ⏳",
"chat_closed_user": "⛔ Чат завершено. Повертаємось у головне меню.",
"chat_closed_admin_by_admin": "⛔ Чат завершено адміністратором. Повертаємось у головне меню.",
"data_received": "✅ Дякуємо! Ваші дані отримано. Адміністратор зв'яжеться з вами найближчим часом.",
"spam_warn": "⚠️ Будь ласка, не надсилайте повідомлення так часто.",
}

# ════════════════════════════════════════════════════════════════
#  ОБРОБНИКИ CALLBACK (централізована таблиця маршрутів)
# ════════════════════════════════════════════════════════════════
def _back_to_menu(state, chat_id, from_id, cb_id):
    clear_user(state, from_id)
    send(chat_id, T["welcome"], markup=SERVICES_INLINE)
    answer_cb(cb_id)

def _send_payment(state, chat_id, from_id, cb_id):
    send(chat_id, PAYMENT_DETAILS, markup=inline(MENU_BTN))
    answer_cb(cb_id)

# ─── Консультація ────────────────────────────────────────────
def _cb_consult(state, chat_id, from_id, cb_id):
    state["consult_request"][from_id] = {"stage": "choose_duration"}
    send(chat_id, T["consult_intro"], markup=inline(
        [{"text": "20 хв — 400 грн", "callback_data": "consult_20"}],
        [{"text": "40 хв — 800 грн", "callback_data": "consult_40"}],
        MENU_BTN,
    ))
    answer_cb(cb_id)

def _cb_consult_duration(state, chat_id, from_id, cb_id, key: str):
    dur, price = PRICES[key]
    state["consult_request"][from_id] = {"stage": "await_contact", "duration": dur, "price": price}
    send(chat_id, T["consult_contact"].format(duration=dur, price=price),
         markup=BACK_TO_MENU_REPLY)
    answer_cb(cb_id, f"Обрано {dur}")

# ─── Супровід ────────────────────────────────────────────────
def _cb_support(state, chat_id, from_id, cb_id):
    send(chat_id, T["support_intro"], markup=inline(
        [{"text": "Група ФОП 1", "callback_data": "support_1"}],
        [{"text": "Група ФОП 2", "callback_data": "support_2"}],
        [{"text": "Група ФОП 3", "callback_data": "support_3"}],
        MENU_BTN,
    ))
    answer_cb(cb_id)

def _cb_support_group(state, chat_id, from_id, cb_id, group: str):
    key = f"support_{group}"
    _, price = PRICES[key]
    state["support_request"][from_id] = {"stage": "group_selected", "group": group}
    send(chat_id, T["support_group"].format(group=group, price=price), markup=inline(
        [{"text": "💳 Реквізити для оплати", "callback_data": "pay"}],
        [{"text": "💬 Поставити питання",    "callback_data": "ask_admin"}],
        MENU_BTN,
    ))
    answer_cb(cb_id)

# ─── Реєстрація / закриття ──────────────────────────────────
def _cb_regclose(state, chat_id, from_id, cb_id):
    send(chat_id, T["regclose_intro"], markup=inline(
        [{"text": "📝 Реєстрація ФОП", "callback_data": "fop_register"}],
        [{"text": "📝 Закриття ФОП",   "callback_data": "fop_close"}],
        MENU_BTN,
    ))
    answer_cb(cb_id)

def _cb_fop_register(state, chat_id, from_id, cb_id):
    send(chat_id, T["fop_register"], markup=inline(
        [{"text": "✅ Реєструємо",       "callback_data": "fop_register_pay"}],
        [{"text": "↩️ Назад",           "callback_data": "regclose"}],
    ))
    answer_cb(cb_id)

def _cb_fop_register_pay(state, chat_id, from_id, cb_id):
    send(chat_id, PAYMENT_DETAILS, markup=inline(
        [{"text": "↩️ Назад", "callback_data": "regclose"}],
    ))
    answer_cb(cb_id)

def _cb_fop_close(state, chat_id, from_id, cb_id):
    send(chat_id, T["fop_close"], markup=inline(
        [{"text": "✅ Закриваємо",  "callback_data": "fop_close_pay"}],
        [{"text": "↩️ Назад",      "callback_data": "regclose"}],
    ))
    answer_cb(cb_id)

def _cb_fop_close_pay(state, chat_id, from_id, cb_id):
    send(chat_id, PAYMENT_DETAILS, markup=inline(
        [{"text": "↩️ Назад", "callback_data": "regclose"}],
    ))
    answer_cb(cb_id)

# ─── Звітність ───────────────────────────────────────────────
def _cb_reports(state, chat_id, from_id, cb_id):
    send(chat_id, T["reports_intro"], markup=inline(
        [{"text": "📂 Подача звіту",                   "callback_data": "report_submit"}],
        [{"text": "💰 Сплата податку / перевірка ФОП", "callback_data": "report_tax_check"}],
        MENU_BTN,
    ))
    answer_cb(cb_id)

def _cb_report_submit(state, chat_id, from_id, cb_id):
    send(chat_id, T["report_submit"], markup=inline(
        [{"text": "✅ Хочу цю послугу", "callback_data": "report_submit_contacts"}],
        [{"text": "↩️ Назад",          "callback_data": "reports"}],
    ))
    answer_cb(cb_id)

def _cb_report_submit_contacts(state, chat_id, from_id, cb_id):
    state["reports_request"][from_id] = {"stage": "await_contact", "type": "submit"}
    send(chat_id, T["report_submit_contact"], markup=BACK_TO_MENU_REPLY)
    answer_cb(cb_id)

def _cb_report_tax_check(state, chat_id, from_id, cb_id):
    send(chat_id, T["tax_check"], markup=inline(
        [{"text": "✅ Перевіряємо", "callback_data": "tax_check_contacts"}],
        [{"text": "↩️ Назад",      "callback_data": "reports"}],
    ))
    answer_cb(cb_id)

def _cb_tax_check_contacts(state, chat_id, from_id, cb_id):
    state["reports_request"][from_id] = {"stage": "await_contact", "type": "taxcheck"}
    send(chat_id, T["tax_check_contact"], markup=inline(
        [{"text": "💳 Реквізити для оплати", "callback_data": "tax_check_pay"}],
        [{"text": "↩️ Назад",               "callback_data": "reports"}],
    ))
    answer_cb(cb_id)

def _cb_tax_check_pay(state, chat_id, from_id, cb_id):
    send(chat_id, PAYMENT_DETAILS, markup=BACK_TO_MENU_REPLY)
    answer_cb(cb_id)

# ─── ПРРО ────────────────────────────────────────────────────
def _cb_prro(state, chat_id, from_id, cb_id):
    send(chat_id, T["prro_intro"], markup=inline(
        [{"text": "🖥 Реєстрація ПРРО", "callback_data": "prro_register"}],
        [{"text": "🖥 Закриття ПРРО",   "callback_data": "prro_close"}],
        MENU_BTN,
    ))
    answer_cb(cb_id)

def _cb_prro_register(state, chat_id, from_id, cb_id):
    send(chat_id, T["prro_register"], markup=inline(
        [{"text": "✅ Реєструємо",  "callback_data": "prro_register_apply"}],
        [{"text": "↩️ Назад",      "callback_data": "prro"}],
    ))
    answer_cb(cb_id)

def _cb_prro_register_apply(state, chat_id, from_id, cb_id):
    send(chat_id, T["prro_register_contact"], markup=inline(
        [{"text": "💳 Реквізити для оплати", "callback_data": "prro_pay"}],
        [{"text": "↩️ Назад",               "callback_data": "prro"}],
    ))
    answer_cb(cb_id)

def _cb_prro_pay(state, chat_id, from_id, cb_id):
    send(chat_id, PAYMENT_DETAILS, markup=BACK_TO_MENU_REPLY)
    answer_cb(cb_id)

def _cb_prro_close(state, chat_id, from_id, cb_id):
    send(chat_id, T["prro_close"], markup=inline(
        [{"text": "✅ Закриваємо", "callback_data": "prro_close_apply"}],
        [{"text": "↩️ Назад",     "callback_data": "prro"}],
    ))
    answer_cb(cb_id)

def _cb_prro_close_apply(state, chat_id, from_id, cb_id):
    send(chat_id, T["prro_close_contact"], markup=inline(
        [{"text": "💳 Реквізити для оплати", "callback_data": "prro_close_pay"}],
        [{"text": "↩️ Назад",               "callback_data": "prro"}],
    ))
    answer_cb(cb_id)

def _cb_prro_close_pay(state, chat_id, from_id, cb_id):
    send(chat_id, PAYMENT_DETAILS, markup=BACK_TO_MENU_REPLY)
    answer_cb(cb_id)

# ─── Декрет ──────────────────────────────────────────────────
def _cb_decret(state, chat_id, from_id, cb_id):
    send(chat_id, T["decret"], markup=inline(
        [{"text": "✅ Хочу оформити",   "callback_data": "decret_apply"}],
        MENU_BTN,
    ))
    answer_cb(cb_id)

def _cb_decret_apply(state, chat_id, from_id, cb_id):
    state["decret_request"][from_id] = {"stage": "await_contact"}
    send(chat_id, T["decret_contact"], markup=inline(
        [{"text": "💳 Реквізити для оплати", "callback_data": "decret_pay"}],
        MENU_BTN,
    ))
    answer_cb(cb_id)

def _cb_decret_pay(state, chat_id, from_id, cb_id):
    send(chat_id, PAYMENT_DETAILS, markup=BACK_TO_MENU_REPLY)
    answer_cb(cb_id)

# ─── Запит до адміна ─────────────────────────────────────────
def _cb_ask_admin(state, chat_id, from_id, cb_id, user_name: str = "Користувач"):
    if from_id not in state["active_chats"]:
        state["active_chats"][from_id] = "pending"
        state["pending_since"][from_id] = datetime.utcnow().isoformat()
        send(chat_id, T["ask_question"], markup=FINISH_CHAT_REPLY)
        notif = (
            f"💬 <b>Нове питання від користувача</b>\n"
            f"👤 {escape(user_name)}\n"
            f"🆔 <code>{from_id}</code>"
        )
        notify_admin(state, notif, markup=admin_chat_markup(from_id))
    else:
        send(chat_id, T["ask_question"], markup=FINISH_CHAT_REPLY)
    answer_cb(cb_id)

# ════════════════════════════════════════════════════════════════
#  АДМІН: СТАТИСТИКА / BROADCAST
# ════════════════════════════════════════════════════════════════
def handle_admin_commands(state: dict, text: str, cid: int) -> bool:
    """Обробляє команди адміна. Повертає True якщо команда оброблена."""
    if not text.startswith("/"):
        return False

    cmd_parts = text.strip().split(None, 1)
    cmd = cmd_parts[0].lower()
    arg = cmd_parts[1] if len(cmd_parts) > 1 else ""

    if cmd == "/stats":
        st = state["stats"]
        total = len(st.get("seen_users", []))
        msg_in = st.get("messages_in", 0)
        closed = st.get("chats_closed", 0)
        active = len([v for v in state["active_chats"].values() if v == "active"])
        pending = len([v for v in state["active_chats"].values() if v == "pending"])
        top_services = sorted(st.get("service_clicks", {}).items(), key=lambda x: -x[1])[:5]
        top_txt = "\n".join(f"  • {k}: {v}" for k, v in top_services) or "  —"
        send(cid,
            f"📊 <b>Статистика бота</b>\n\n"
            f"👥 Всього користувачів: <b>{total}</b>\n"
            f"📨 Повідомлень отримано: <b>{msg_in}</b>\n"
            f"💬 Активних чатів: <b>{active}</b>\n"
            f"⏳ В очікуванні: <b>{pending}</b>\n"
            f"✅ Чатів закрито: <b>{closed}</b>\n\n"
            f"🔥 Топ послуг:\n{top_txt}"
        )
        return True

    if cmd == "/chats":
        active_list = [
            f"  • <code>{uid}</code> — {status}"
            for uid, status in state["active_chats"].items()
        ]
        if active_list:
            send(cid, "💬 <b>Активні чати:</b>\n\n" + "\n".join(active_list))
        else:
            send(cid, "💬 Немає активних чатів.")
        return True

    if cmd == "/target":
        target = state.get("admin_target")
        send(cid, f"🎯 Поточний співрозмовник: <code>{target}</code>" if target
             else "🎯 Зараз немає активного співрозмовника.")
        return True

    if cmd == "/broadcast" and arg:
        users = state["stats"].get("seen_users", [])
        sent_ok = 0
        for uid in users:
            result = send(uid, f"📢 <b>Повідомлення від адміністратора:</b>\n\n{arg}")
            if result and result.get("ok"):
                sent_ok += 1
            time.sleep(0.05)   # Telegram rate limit
        send(cid, f"✅ Розсилку завершено. Надіслано: {sent_ok}/{len(users)}")
        return True

    if cmd == "/help":
        send(cid,
            "🛠 <b>Команди адміна:</b>\n\n"
            "/stats — статистика\n"
            "/chats — активні чати\n"
            "/target — поточний співрозмовник\n"
            "/broadcast [текст] — розсилка всім\n"
            "/help — ця довідка\n\n"
            "Також: напишіть «завершити» щоб закрити поточний чат."
        )
        return True

    return False

# ════════════════════════════════════════════════════════════════
#  WEBHOOK ENDPOINT
# ════════════════════════════════════════════════════════════════
@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True)
    if not update:
        return "bad request", 400

    with _lock:
        _process_update(STATE, update)
        _save_state(STATE)

    return "ok", 200

def _process_update(state: dict, update: dict) -> None:
    # ─── Callback query ───
    if "callback_query" in update:
        _handle_callback(state, update["callback_query"])
        return

    # ─── Звичайне повідомлення ───
    msg = update.get("message")
    if not msg:
        return
    _handle_message(state, msg)

# ════════════════════════════════════════════════════════════════
#  ОБРОБНИК CALLBACK
# ════════════════════════════════════════════════════════════════
def _handle_callback(state: dict, cb: dict) -> None:
    chat_id = cb["message"]["chat"]["id"]
    from_id = cb["from"]["id"]
    data    = cb.get("data", "")
    cb_id   = cb["id"]
    user_name = (cb["from"].get("first_name","")+" "+cb["from"].get("last_name","")).strip()

    # Трекаємо сервіси
    svc = state["stats"].setdefault("service_clicks", {})
    svc[data] = svc.get(data, 0) + 1

    # ── Таблиця маршрутів ──
    routes = {
        "main_menu":             lambda: _back_to_menu(state, chat_id, from_id, cb_id),
        "pay":                   lambda: _send_payment(state, chat_id, from_id, cb_id),
        "consult":               lambda: _cb_consult(state, chat_id, from_id, cb_id),
        "consult_20":            lambda: _cb_consult_duration(state, chat_id, from_id, cb_id, "consult_20"),
        "consult_40":            lambda: _cb_consult_duration(state, chat_id, from_id, cb_id, "consult_40"),
        "support":               lambda: _cb_support(state, chat_id, from_id, cb_id),
        "support_1":             lambda: _cb_support_group(state, chat_id, from_id, cb_id, "1"),
        "support_2":             lambda: _cb_support_group(state, chat_id, from_id, cb_id, "2"),
        "support_3":             lambda: _cb_support_group(state, chat_id, from_id, cb_id, "3"),
        "regclose":              lambda: _cb_regclose(state, chat_id, from_id, cb_id),
        "fop_register":          lambda: _cb_fop_register(state, chat_id, from_id, cb_id),
        "fop_register_pay":      lambda: _cb_fop_register_pay(state, chat_id, from_id, cb_id),
        "fop_close":             lambda: _cb_fop_close(state, chat_id, from_id, cb_id),
        "fop_close_pay":         lambda: _cb_fop_close_pay(state, chat_id, from_id, cb_id),
        "reports":               lambda: _cb_reports(state, chat_id, from_id, cb_id),
        "report_submit":         lambda: _cb_report_submit(state, chat_id, from_id, cb_id),
        "report_submit_contacts":lambda: _cb_report_submit_contacts(state, chat_id, from_id, cb_id),
        "report_tax_check":      lambda: _cb_report_tax_check(state, chat_id, from_id, cb_id),
        "tax_check_contacts":    lambda: _cb_tax_check_contacts(state, chat_id, from_id, cb_id),
        "tax_check_pay":         lambda: _cb_tax_check_pay(state, chat_id, from_id, cb_id),
        "prro":                  lambda: _cb_prro(state, chat_id, from_id, cb_id),
        "prro_register":         lambda: _cb_prro_register(state, chat_id, from_id, cb_id),
        "prro_register_apply":   lambda: _cb_prro_register_apply(state, chat_id, from_id, cb_id),
        "prro_pay":              lambda: _cb_prro_pay(state, chat_id, from_id, cb_id),
        "prro_close":            lambda: _cb_prro_close(state, chat_id, from_id, cb_id),
        "prro_close_apply":      lambda: _cb_prro_close_apply(state, chat_id, from_id, cb_id),
        "prro_close_pay":        lambda: _cb_prro_close_pay(state, chat_id, from_id, cb_id),
        "decret":                lambda: _cb_decret(state, chat_id, from_id, cb_id),
        "decret_apply":          lambda: _cb_decret_apply(state, chat_id, from_id, cb_id),
        "decret_pay":            lambda: _cb_decret_pay(state, chat_id, from_id, cb_id),
        "ask_admin":             lambda: _cb_ask_admin(state, chat_id, from_id, cb_id, user_name),
    }

    # ── Адмінські кнопки ──
    if from_id == ADMIN_ID:
        if data.startswith("reply_"):
            uid = int(data.split("_", 1)[1])
            state["admin_target"] = uid
            state["active_chats"][uid] = "active"
            state["pending_since"].pop(uid, None)
            send(ADMIN_ID, f"✍️ Надсилайте повідомлення або медіа для <code>{uid}</code>.\n"
                           f"Щоб закрити — напишіть «завершити».")
            answer_cb(cb_id, "Режим відповіді активовано")
            return
        if data.startswith("close_"):
            uid = int(data.split("_", 1)[1])
            _close_chat(state, uid, by_admin=True)
            answer_cb(cb_id, "Чат закрито")
            return

    handler = routes.get(data)
    if handler:
        handler()
    else:
        answer_cb(cb_id, "Невідома дія")

# ════════════════════════════════════════════════════════════════
#  ОБРОБНИК ПОВІДОМЛЕНЬ
# ════════════════════════════════════════════════════════════════
def _handle_message(state: dict, msg: dict) -> None:
    cid       = msg["chat"]["id"]
    text      = msg.get("text", "") or ""
    user_data = msg.get("from", {})
    uid       = user_data.get("id")
    user_name = (user_data.get("first_name","")+" "+user_data.get("last_name","")).strip() or "Користувач"
    has_media = any(k in msg for k in ("photo","document","video","audio","voice","sticker"))

    # ── Трекаємо нових користувачів ──
    st = state["stats"]
    st["messages_in"] = st.get("messages_in", 0) + 1
    if uid not in st.get("seen_users", []):
        st.setdefault("seen_users", []).append(uid)

    # ── Антиспам ──
    if cid != ADMIN_ID and is_spam(state, uid):
        send(cid, T["spam_warn"])
        return

    # ── /start або повернення в меню ──
    if text in ("/start", "↩️ Повернутися в меню", "Повернутися в меню"):
        clear_user(state, uid)
        send(cid, T["welcome"], markup=SERVICES_INLINE)
        return

    # ── Меню ──
    if text in ("📋 Меню", "Меню"):
        send(cid, T["welcome"], markup=SERVICES_INLINE)
        return

    # ── Реквізити (без активного чату) ──
    if text in ("💳 Реквізити", "Реквізити для оплати") and cid not in state["active_chats"]:
        send(cid, PAYMENT_DETAILS, markup=MAIN_REPLY)
        return

    # ── Поставити питання ──
    if text in ("💬 Поставити питання", "Поставити питання") and cid not in state["active_chats"]:
        state["active_chats"][cid] = "pending"
        state["pending_since"][cid] = datetime.utcnow().isoformat()
        send(cid, T["ask_question"], markup=FINISH_CHAT_REPLY)
        notif = (
            f"💬 <b>Нове питання від користувача</b>\n"
            f"👤 {escape(user_name)}\n"
            f"🆔 <code>{cid}</code>"
        )
        notify_admin(state, notif, markup=admin_chat_markup(cid))
        return

    # ── Завершення чату користувачем ──
    if text in ("⛔ Завершити чат", "Завершити чат") and cid in state["active_chats"]:
        _close_chat(state, cid, by_admin=False)
        return

    # ── Адмін ──
    if cid == ADMIN_ID:
        _handle_admin_message(state, msg, text, has_media)
        return

    # ── Активний чат: пересилання адміну ──
    if cid in state["active_chats"] and state["active_chats"][cid] == "active":
        _forward_to_admin(state, msg, cid, uid, user_name, text, has_media)
        return

    # ── Pending: тільки "Завершити чат" ──
    if cid in state["active_chats"]:
        send(cid, "⏳ Очікуйте відповіді. Щоб скасувати — натисніть «⛔ Завершити чат».",
             markup=FINISH_CHAT_REPLY)
        return

    # ── Збір контактів ──
    if _handle_contact_collection(state, msg, cid, uid, user_name, text, has_media):
        return

    # ── Fallback ──
    send(cid, "Будь ласка, оберіть дію з меню 👇", markup=MAIN_REPLY)

def _handle_admin_message(state: dict, msg: dict, text: str, has_media: bool) -> None:
    cid = ADMIN_ID

    # Команди адміна
    if handle_admin_commands(state, text, cid):
        return

    target = state.get("admin_target")
    if not target:
        send(cid,
             "🤖 Немає активного співрозмовника.\n"
             "Натисніть <b>Відповісти</b> у повідомленні від користувача.\n\n"
             "Команди: /stats /chats /target /broadcast /help")
        return

    if text.lower().startswith("завершити"):
        _close_chat(state, target, by_admin=True)
        return

    if has_media:
        send_media(target, msg)
        send(target, "💬 <i>Відповідь адміністратора (медіа)</i>", markup=FINISH_CHAT_REPLY)
    elif text:
        send(target, f"💬 <b>Відповідь адміністратора:</b>\n\n{escape(text)}",
             markup=FINISH_CHAT_REPLY)

def _forward_to_admin(state: dict, msg: dict, cid: int, uid: int,
                      user_name: str, text: str, has_media: bool) -> None:
    markup = admin_chat_markup(cid)
    if has_media:
        send_media(ADMIN_ID, msg)
        send(ADMIN_ID, f"📎 <b>Медіа від</b> {escape(user_name)} <code>{cid}</code>", markup=markup)
    elif text:
        send(ADMIN_ID,
             f"💬 <b>{escape(user_name)}</b> <code>{cid}</code>:\n\n{escape(text)}",
             markup=markup)

def _close_chat(state: dict, uid: int, by_admin: bool) -> None:
    if state.get("admin_target") == uid:
        state["admin_target"] = None
    state["active_chats"].pop(uid, None)
    state["pending_since"].pop(uid, None)
    state["stats"]["chats_closed"] = state["stats"].get("chats_closed", 0) + 1

    user_msg = T["chat_closed_admin_by_admin"] if by_admin else T["chat_closed_user"]
    send(uid, user_msg, markup=MAIN_REPLY)

    who = "адміністратором" if by_admin else "користувачем"
    send(ADMIN_ID, f"✅ Чат з <code>{uid}</code> завершено {who}.")

def _handle_contact_collection(state: dict, msg: dict, cid: int, uid: int,
                                user_name: str, text: str, has_media: bool) -> bool:
    """Збирає дані від користувача після вибору послуги. Повертає True якщо оброблено."""

    def _notify_admin_contact(service: str, details: str, extra: str = "") -> None:
        note = (
            f"📋 <b>Нова заявка: {service}</b>\n"
            f"👤 {escape(user_name)}\n"
            f"🆔 <code>{uid}</code>\n"
        )
        if details:
            note += f"\n{details}"
        if extra:
            note += f"\n{extra}"
        notify_admin(state, note, markup=admin_chat_markup(uid))
        if has_media:
            send_media(ADMIN_ID, msg)
        send(cid, T["data_received"], markup=MAIN_REPLY)

    # ── Консультація ──
    cr = state["consult_request"].get(uid, {})
    if cr.get("stage") == "await_contact":
        dur   = cr.get("duration", "?")
        price = cr.get("price", "?")
        _notify_admin_contact(
            f"Консультація {dur} ({price})",
            f"Контакти: <pre>{escape(text.strip())}</pre>" if text else ""
        )
        state["consult_request"].pop(uid, None)
        return True

    # ── Звітність: подача ──
    rr = state["reports_request"].get(uid, {})
    if rr.get("stage") == "await_contact" and rr.get("type") == "submit":
        _notify_admin_contact(
            "Подача звітності",
            f"Дані: <pre>{escape(text.strip())}</pre>" if text else ""
        )
        state["reports_request"].pop(uid, None)
        return True

    # ── Звітність: перевірка ──
    if rr.get("stage") == "await_contact" and rr.get("type") == "taxcheck":
        _notify_admin_contact(
            "Перевірка ФОП / податків",
            f"Дані: <pre>{escape(text.strip())}</pre>" if text else ""
        )
        state["reports_request"].pop(uid, None)
        return True

    # ── Декрет ──
    dr = state["decret_request"].get(uid, {})
    if dr.get("stage") == "await_contact":
        _notify_admin_contact(
            "Декрет ФОП",
            f"Дані: <pre>{escape(text.strip())}</pre>" if text else ""
        )
        state["decret_request"].pop(uid, None)
        return True

    return False

# ════════════════════════════════════════════════════════════════
#  ФОНОВИЙ ПОТОК: нагадування адміну про довге очікування
# ════════════════════════════════════════════════════════════════
def _reminder_worker() -> None:
    while True:
        time.sleep(60)
        try:
            with _lock:
                now = datetime.utcnow()
                for uid, since_str in list(STATE.get("pending_since", {}).items()):
                    since = datetime.fromisoformat(since_str)
                    if (now - since) >= timedelta(minutes=PENDING_REMIND_MIN):
                        if STATE["active_chats"].get(uid) == "pending":
                            send(ADMIN_ID,
                                 f"⏰ <b>Нагадування!</b>\n"
                                 f"Користувач <code>{uid}</code> очікує відповіді вже "
                                 f"{PENDING_REMIND_MIN}+ хв.",
                                 markup=admin_chat_markup(uid))
                        STATE["pending_since"].pop(uid, None)
                _save_state(STATE)
        except Exception as e:
            log.error("reminder_worker error: %s", e)

threading.Thread(target=_reminder_worker, daemon=True, name="reminder").start()

# ════════════════════════════════════════════════════════════════
#  HEALTHCHECK ENDPOINTS
# ════════════════════════════════════════════════════════════════
@app.route("/", methods=["GET"])
def index():
    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    with _lock:
        active  = len(STATE.get("active_chats", {}))
        users   = len(STATE["stats"].get("seen_users", []))
    return {"status": "ok", "active_chats": active, "total_users": users}, 200

# ════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    log.info("Запуск бота на порту %d", port)
    app.run("0.0.0.0", port=port)
