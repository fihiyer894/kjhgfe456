import os
import json
import logging
from flask import Flask, request
import requests
from html import escape

# ======= Логування =======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ======= Конфігурація =======
TOKEN    = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# ВИПРАВЛЕННЯ: перевірка обов'язкових змінних середовища при старті
if not TOKEN:
    raise ValueError("Змінна середовища API_TOKEN не встановлена!")
if not ADMIN_ID:
    raise ValueError("Змінна середовища ADMIN_ID не встановлена або дорівнює 0!")

app = Flask(__name__)

# ======= Реквізити (єдине місце — більше дублювання) =======
PAYMENT_DETAILS = (
    "Оплата здійснюється на офіційний рахунок ФОП 👩🏻‍💻\n\n"
    "Отримувач:\n"
    "ФОП Романюк Анжела Василівна\n"
    "UA033220010000026006340057875\n"
    "ЄДРПОУ 3316913762\n"
    "Призначення платежу: Оплата за консультаційні інформаційні послуги\n\n"
    "❤️ ОБОВ'ЯЗКОВО: після оплати надішліть, будь ласка, чек або скрін "
    "на @CreatorBotInst або в розділ «Повідомлення»"
)

# ======= State для керування чатами =======
# active_chats[user_id] = 'pending' | 'active'
active_chats   = {}
# admin_target — ID користувача, з яким зараз спілкується адмін
admin_target   = None

# ======= State для консультацій і етапів звітів =======
consult_request = {}  # user_id -> {"stage": "choose_duration"/"await_contact", "duration": "20"|"40"}
reports_request = {}  # user_id -> {"stage": "...", "type": "submit"/"taxcheck"}
support_request = {}  # user_id -> {"stage": "group_selected", "group": "1"|"2"|"3"}
decret_request  = {}  # user_id -> {"stage": "await_contact"}

# ======= Reply та Inline розмітки =======
def main_menu_markup():
    return {
        "keyboard": [
            [{"text": "Меню"}],
            [{"text": "Поставити питання"}, {"text": "Реквізити для оплати"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

def user_finish_markup():
    return {
        "keyboard": [[{"text": "Завершити чат"}]],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

def admin_reply_markup(user_id):
    return {
        "inline_keyboard": [
            [{"text": "Відповісти", "callback_data": f"reply_{user_id}"}],
            [{"text": "Завершити чат", "callback_data": f"close_{user_id}"}],
        ]
    }

def welcome_services_inline():
    return {
        "inline_keyboard": [
            [{"text": "• консультації",              "callback_data": "consult"}],
            [{"text": "• супровід ФОП",              "callback_data": "support"}],
            [{"text": "• реєстрація / закриття",     "callback_data": "regclose"}],
            [{"text": "• звітність і податки",       "callback_data": "reports"}],
            [{"text": "• реєстрація/закриття ПРРО",  "callback_data": "prro"}],
            [{"text": "• декрет ФОП",                "callback_data": "decret"}]
        ]
    }

def return_to_menu_markup():
    return {
        "keyboard": [[{"text": "Повернутися в меню"}]],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

# ======= Консультація =======
def consult_duration_inline():
    # ВИПРАВЛЕННЯ: текст кнопок і callback_data тепер збігаються (20/40, а не 30/45)
    return {
        "inline_keyboard": [
            [{"text": "20 хв — 400 грн", "callback_data": "consult_20"}],
            [{"text": "40 хв — 800 грн", "callback_data": "consult_40"}],
            [{"text": "Повернутися в меню", "callback_data": "consult_back"}]
        ]
    }

# ======= Супровід ФОП =======
def support_groups_inline():
    return {
        "inline_keyboard": [
            [{"text": "Група ФОП 1", "callback_data": "support_1"}],
            [{"text": "Група ФОП 2", "callback_data": "support_2"}],
            [{"text": "Група ФОП 3", "callback_data": "support_3"}],
            [{"text": "Повернутися в меню", "callback_data": "support_back"}]
        ]
    }

def support_next_inline():
    return {
        "inline_keyboard": [
            [{"text": "Реквізити для оплати", "callback_data": "support_pay"}],
            [{"text": "Поставити питання",    "callback_data": "support_admin"}],
            [{"text": "Повернутися в меню",   "callback_data": "support_back"}]
        ]
    }

# ======= Реєстрація / закриття ФОП =======
def regclose_inline():
    return {
        "inline_keyboard": [
            [{"text": "Реєстрація ФОП",     "callback_data": "fop_register"}],
            [{"text": "Закриття ФОП",       "callback_data": "fop_close"}],
            [{"text": "Повернутися в меню", "callback_data": "regclose_back"}]
        ]
    }

def fop_register_inline():
    return {
        "inline_keyboard": [
            [{"text": "Реєструємо",  "callback_data": "fop_register_pay"}],
            [{"text": "Повернутися", "callback_data": "regclose"}]
        ]
    }

def fop_close_inline():
    return {
        "inline_keyboard": [
            [{"text": "Закриваємо", "callback_data": "fop_close_pay"}],
            [{"text": "Повернутися", "callback_data": "regclose"}]
        ]
    }

# ======= Звітність і податки =======
def reports_inline():
    return {
        "inline_keyboard": [
            [{"text": "Подача звіту",                    "callback_data": "report_submit"}],
            [{"text": "Оплата податку / перевірка ФОП", "callback_data": "report_tax_check"}],
            [{"text": "Повернутися в меню",              "callback_data": "reports_back"}],
        ]
    }

def report_submit_service_inline():
    return {
        "inline_keyboard": [
            [{"text": "Хочу цю послугу", "callback_data": "report_submit_contacts"}],
            [{"text": "Повернутися",     "callback_data": "reports"}],
        ]
    }

def report_tax_check_inline():
    return {
        "inline_keyboard": [
            [{"text": "Перевіряємо", "callback_data": "tax_check_contacts"}],
            [{"text": "Повернутися", "callback_data": "reports"}]
        ]
    }

def tax_check_pay_inline():
    return {
        "inline_keyboard": [
            [{"text": "Оплата / реквізити", "callback_data": "tax_check_pay"}],
            [{"text": "Повернутися",        "callback_data": "reports"}]
        ]
    }

# ======= ПРРО =======
def prro_inline():
    return {
        "inline_keyboard": [
            [{"text": "Реєстрація ПРРО",    "callback_data": "prro_register"}],
            [{"text": "Закриття ПРРО",      "callback_data": "prro_close"}],
            [{"text": "Повернутися в меню", "callback_data": "prro_back"}]
        ]
    }

def prro_register_step_inline():
    return {
        "inline_keyboard": [
            [{"text": "Реєструємо",  "callback_data": "prro_register_pay"}],
            [{"text": "Повернутися", "callback_data": "prro"}],
        ]
    }

def prro_register_pay_inline():
    return {
        "inline_keyboard": [
            [{"text": "Оплата / реквізити", "callback_data": "prro_pay"}],
            [{"text": "Повернутися",        "callback_data": "prro"}],
        ]
    }

def prro_close_step_inline():
    return {
        "inline_keyboard": [
            [{"text": "Закриваємо",  "callback_data": "prro_close_apply"}],
            [{"text": "Повернутися", "callback_data": "prro"}],
        ]
    }

def prro_close_pay_inline():
    return {
        "inline_keyboard": [
            [{"text": "Оплата / реквізити", "callback_data": "prro_close_pay"}],
            [{"text": "Повернутися",        "callback_data": "prro"}],
        ]
    }

# ======= Декрет ФОП =======
def decret_inline():
    return {
        "inline_keyboard": [
            [{"text": "Хочу оформити",      "callback_data": "decret_apply"}],
            [{"text": "Повернутися в меню", "callback_data": "decret_back"}]
        ]
    }

def decret_pay_inline():
    return {
        "inline_keyboard": [
            [{"text": "Оплатити / реквізити", "callback_data": "decret_pay"}],
            [{"text": "Повернутися",          "callback_data": "decret"}]
        ]
    }

# ======= ТЕКСТИ =======
WELCOME_SERVICES_TEXT = (
    "🌿 Вітаю вас у бухгалтерському боті!\n\n"
    "Я — ваш бухгалтер та помічник у питаннях ФОП.\n"
    "Тут ви знайдете зрозумілу допомогу та професійні послуги для вашого бізнесу.\n"
    "Оберіть послугу, яка вам потрібна 👇"
)

CONSULT_INTRO_TEXT = (
    "Консультація — це зручно, швидко і по суті 💬\n"
    "Ви можете обрати формат:\n\n"
    "▫️ 20 хв — 400 грн\n"
    "▫️ 40 хв — 800 грн\n"
    "Консультація проходить онлайн (Telegram / Instagram).\n\n"
    "Оберіть, будь ласка, тривалість 👇"
)

CONSULT_CONTACTS_TEXT = (
    "Чудово! 💼\n"
    "Щоб зафіксувати час консультації — {duration} хв ({price} грн), "
    "будь ласка, залиште ваші контакти:\n"
    "• Ім'я та прізвище\n"
    "• Нік в Instagram або Telegram"
)

# ВИПРАВЛЕННЯ: ціна тепер відповідає тривалості
CONSULT_PRICES = {"20": "400", "40": "800"}

SUPPORT_INFO_TEXT = (
    "💼 Супровід ФОП — це коли про ваш облік піклуються за вас 🌸\n\n"
    "Ви не думаєте про податки, звітність чи перевірки — усе під контролем.\n"
    "Я беру ваш ФОП на повне бухгалтерське обслуговування 💪\n\n"
    "🔍 У супровід входить:\n"
    "• перевірка правильності вашої діяльності\n"
    "• нагадування про терміни сплати податку\n"
    "• повідомлення про нові зміни та закони\n"
    "• ведення Книги обліку доходів\n"
    "• консультаційна підтримка\n\n"
    "Звітність оплачується додатково ❗\n\n"
    "🕓 Термін — 1 місяць (з можливістю продовження)\n\n"
    "Щоб я краще розуміла ваш запит 👇\n"
    "Оберіть, будь ласка, вашу групу ФОП:"
)

SUPPORT_GROUP_1_TEXT = (
    "💼 Інформація по Групі ФОП 1 🌸\n\n"
    "Ви сплачуєте єдиний податок, військовий збір та ЄСВ щомісяця, звітність — 1 раз на рік.\n\n"
    "💰 Вартість супроводу — 700 грн / місяць\n"
    "Додаткові послуги оплачуються окремо.\n"
    "Узгоджуємо деталі індивідуально!\n\n"
    "Бажаєте отримати реквізити для оплати, щоб розпочати співпрацю? 👇"
)

SUPPORT_GROUP_2_TEXT = (
    "💼 Інформація по Групі ФОП 2 🌸\n\n"
    "Ви сплачуєте єдиний податок, військовий збір та ЄСВ щомісяця, звітність — 1 раз на рік.\n\n"
    "💰 Вартість супроводу — 1000 грн / місяць\n"
    "Додаткові послуги оплачуються окремо.\n"
    "Узгоджуємо деталі індивідуально!\n\n"
    "Бажаєте отримати реквізити для оплати, щоб розпочати співпрацю? 👇"
)

# ВИПРАВЛЕННЯ: перевірте реальну ціну для групи 3 — зараз стоїть 1200 грн як приклад
SUPPORT_GROUP_3_TEXT = (
    "💼 Інформація по Групі ФОП 3 🌸\n\n"
    "Ви сплачуєте єдиний податок, військовий збір та ЄСВ щомісяця, звітність — 1 раз на рік.\n\n"
    "💰 Вартість супроводу — 1200 грн / місяць\n"
    "Додаткові послуги оплачуються окремо.\n"
    "Узгоджуємо деталі індивідуально!\n\n"
    "Бажаєте отримати реквізити для оплати, щоб розпочати співпрацю? 👇"
)

REGCLOSE_INTRO_TEXT = "Оберіть, що саме вам потрібно 👇"

FOP_REGISTER_TEXT = (
    "Я допоможу швидко та безпечно зареєструвати ФОП «під ключ».\n\n"
    "Що входить у послугу:\n"
    "- Консультація щодо вибору КВЕДів та системи оподаткування;\n"
    "- Підготовка документів для реєстрації;\n"
    "- Подання заяви до державного реєстратора (онлайн або офлайн);\n"
    "- Отримання виписки з ЄДР;\n"
    "- Реєстрація в податковій та/або як платника єдиного податку (за потреби);\n"
    "- Консультація для подальшої роботи\n\n"
    "Термін виконання: 1–2 робочі дні.\n"
    "Результат: офіційно зареєстрований ФОП, готовий до роботи.\n\n"
    "Вартість — 2500 грн."
)

# ВИПРАВЛЕННЯ: використовуємо PAYMENT_DETAILS скрізь — більше дублювання реквізитів
FOP_REGISTER_PAY_TEXT = PAYMENT_DETAILS
FOP_CLOSE_PAY_TEXT    = PAYMENT_DETAILS
TAX_CHECK_PAY_TEXT    = PAYMENT_DETAILS
PRRO_REGISTER_PAY_TEXT = PAYMENT_DETAILS
PRRO_CLOSE_PAY_TEXT   = PAYMENT_DETAILS
DECRET_PAY_TEXT       = PAYMENT_DETAILS

FOP_CLOSE_TEXT = (
    "Я допоможу офіційно припинити підприємницьку діяльність швидко, без черг і зайвих клопотів.\n"
    "Підготую всі документи, подам заяву до держреєстратора, закрию ФОП у податковій та здам необхідну звітність.\n\n"
    "Що входить у послугу:\n"
    "- Консультація щодо процедури закриття ФОП;\n"
    "- Підготовка та подання заяви до державного реєстратора;\n"
    "- Здача фінальної звітності до податкової;\n"
    "- Отримання підтвердження про припинення діяльності;\n\n"
    "Термін: від 3 до 7 робочих днів.\n"
    "Результат: ФОП офіційно закрито, без податкових боргів і з чистою історією.\n\n"
    "Вартість — 2000 грн."
)

REPORTS_INTRO_TEXT = (
    "Оберіть, що саме потрібно зараз 👇\n\n"
    "📊 Подання звітності\n"
    "Я підготую і здам усі декларації замість вас — без помилок, штрафів і зайвого клопоту.\n\n"
    "💰 Сплата податків / Перевірка ФОП\n"
    "Допоможу перевірити актуальні суми податків, строки сплати та підкажу, як оплатити правильно.\n\n"
    "Ви просто обираєте, а я все організовую 🌿"
)

REPORT_SUBMIT_TEXT = (
    "Я беру на себе повний процес підготовки та подання звітності для фізичних осіб-підприємців.\n"
    "Підготую декларації, перевірю правильність даних, подам їх до податкової та проконтролюю результат.\n\n"
    "Що входить у послугу:\n"
    "- Підготовка та подання податкової декларації;\n"
    "- Звітність по ЄСВ та єдиному податку;\n"
    "- Контроль строків подачі;\n"
    "- Повідомлення про успішну здачу звіту.\n\n"
    "Результат: звітність здана вчасно, правильно й без штрафів."
)

REPORT_SUBMIT_CONTACTS_TEXT = (
    "Чудово! 🙌\n"
    "Рада, що ви обрали послугу «Подання звітності» 💼\n\n"
    "Щоб я могла підготувати все правильно, мені потрібно кілька деталей:\n"
    "1️⃣ Твій ПІБ (як у ФОП) та Податковий номер (ІПН)\n"
    "2️⃣ Електронний ключ та пароль\n"
    "3️⃣ Період, за який потрібно здати звітність (наприклад: 3 квартал 2025)"
)

REPORT_TAX_CHECK_TEXT = (
    "Я допомагаю перевірити актуальні податкові зобов'язання, стан розрахунків та суми до сплати.\n"
    "Підкажу, які податки і внески потрібно сплатити, а також як це зробити швидко і безпечно.\n\n"
    "У послугу входить:\n"
    "- Перевірка стану ФОП у податковій системі;\n"
    "- Визначення наявних боргів і штрафів;\n"
    "- Консультація щодо сум і строків сплати;\n"
    "- Підтримка у проведенні оплати (реквізити, способи оплати).\n\n"
    "Зі мною ви будете впевнені, що податкові питання під контролем."
)

REPORT_TAX_CHECK_CONTACTS_TEXT = (
    "Готово! 😊\n"
    "Щоб я могла швидко перевірити стан вашого ФОП, надішліть, будь ласка:\n"
    "1. Податковий номер (ІПН)\n"
    "2. ПІБ, як у реєстрації ФОП\n"
    "3. Електронний ключ та пароль\n\n"
    "Після цього я перевірю інформацію і повідомлю про наявність податкових зобов'язань та боргів.\n\n"
    "Вартість — 800 грн."
)

PRRO_INTRO_TEXT = (
    "Виберіть одну з послуг, яка вам потрібна:\n\n"
    "1️⃣ Реєстрація ПРРО\n"
    "Допоможу швидко та без помилок зареєструвати ваш програмний РРО відповідно до вимог законодавства.\n\n"
    "2️⃣ Закриття ПРРО\n"
    "Професійно допоможу закрити ПРРО, якщо він більше не потрібен."
)

PRRO_REGISTER_TEXT = (
    "Надаю комплексну допомогу у реєстрації програмного реєстратора розрахункових операцій (ПРРО).\n\n"
    "Що входить у послугу:\n"
    "- Консультація щодо вибору ПРРО;\n"
    "- Підготовка необхідних документів;\n"
    "- Реєстрація ПРРО в ДПС;\n"
    "- Навчання та консультації щодо використання ПРРО;\n"
    "- Отримання підтвердження від податкової.\n\n"
    "Ваші переваги:\n"
    "⚪ Повна підтримка на кожному етапі\n"
    "⚪ Оперативність та мінімум паперової тяганини\n"
    "⚪ Упевненість у правильності та законності процесу\n"
    "⚪ Збереження часу і ресурсів вашого бізнесу"
)

PRRO_REGISTER_CONTACTS_TEXT = (
    "Дякую, що обрали реєстрацію ПРРО! 💪\n"
    "Щоб розпочати, будь ласка, надішліть мені:\n"
    "1. Назву вашого бізнесу або ПІБ підприємця\n"
    "2. Податковий номер (ІПН)\n"
    "3. Електронний ключ та пароль\n"
    "4. Який ПРРО бажаєте зареєструвати? (якщо не знаєте — я допоможу з вибором)\n\n"
    "Нижче скидаю реквізити для оплати.\n"
    "Вартість — 2000 грн.\n\n"
    "Як тільки отримаю ці дані, розпочну підготовку документів і оформлення заявки."
)

PRRO_CLOSE_INTRO_TEXT = (
    "Допомагаю професійно та швидко закрити ваш програмний реєстратор розрахункових операцій (ПРРО) "
    "відповідно до законодавства.\n\n"
    "Що входить у послугу:\n"
    "- Консультація щодо процесу закриття\n"
    "- Підготовка необхідних документів\n"
    "- Подання заяви на закриття ПРРО\n"
    "- Контроль статусу заявки\n\n"
    "Ваші переваги:\n"
    "⚪ Мінімум клопоту — ми зробимо всю роботу за вас\n"
    "⚪ Оперативне і правильне оформлення документів\n"
    "⚪ Упевненість у дотриманні всіх вимог законодавства\n"
    "⚪ Підтримка та консультації на кожному етапі"
)

PRRO_CLOSE_CONTACT_TEXT = (
    "Дякую за звернення щодо закриття ПРРО.\n"
    "Для початку надайте, будь ласка:\n\n"
    "- Повну назву вашого бізнесу або ПІБ підприємця\n"
    "- Ідентифікаційний код платника податків (ІПН)\n"
    "- Електронний ключ та пароль\n\n"
    "Вартість послуги — 1800 грн."
)

DECRET_SERVICE_TEXT = (
    "Допомагаю правильно оформити та отримати декретні виплати відповідно до законодавства України.\n\n"
    "Що входить у послугу:\n"
    "⚪ Консультація щодо прав на декретні виплати (маму, батька чи опікуна)\n"
    "⚪ Підготовка та оформлення необхідних документів\n"
    "⚪ Подача заяв та документів до відповідних державних органів\n"
    "⚪ Контроль статусу розгляду заявки та виплат\n"
    "⚪ Підтримка та консультації протягом усього процесу\n\n"
    "Ваші переваги:\n"
    "▪ Економія часу — ми зробимо всю бюрократичну роботу за вас\n"
    "▪ Впевненість у правильності оформлення\n"
    "▪ Професійна підтримка і відповіді на всі питання\n"
    "▪ Максимальна швидкість отримання виплат"
)

DECRET_CONTACTS_TEXT = (
    "Дякую за звернення!\n"
    "Для початку оформлення декретних виплат, будь ласка, надайте:\n\n"
    "▪ Повні ПІБ заявника\n"
    "▪ Дату початку декретної відпустки або очікувану дату пологів\n"
    "▪ Контактний телефон\n\n"
    "Після отримання цих даних підготуємо необхідні документи та розпочнемо процедуру.\n\n"
    "Вартість послуги — 3000 грн."
)

# ======= Хелпери для відправки повідомлень і медіа =======
def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    url  = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        resp = requests.post(url, data=data, timeout=8)
        result = resp.json()
        if not result.get("ok"):
            # ВИПРАВЛЕННЯ: логуємо помилки Telegram замість silent pass
            log.error("send_message failed: %s | chat=%s", result.get("description"), chat_id)
    except Exception as e:
        log.error("send_message exception: %s | chat=%s", e, chat_id)

def send_media(chat_id, msg):
    for key, api in [
        ("photo",    "sendPhoto"),
        ("document", "sendDocument"),
        ("video",    "sendVideo"),
        ("audio",    "sendAudio"),
        ("voice",    "sendVoice")
    ]:
        if key in msg:
            file_id = msg[key][-1]["file_id"] if key == "photo" else msg[key]["file_id"]
            payload = {"chat_id": chat_id, key: file_id}
            if "caption" in msg:
                payload["caption"] = msg.get("caption")
            try:
                requests.post(f"https://api.telegram.org/bot{TOKEN}/{api}", data=payload, timeout=8)
            except Exception as e:
                log.error("send_media exception: %s | chat=%s", e, chat_id)
            return True
    return False

# ======= Скидання стану користувача =======
def clear_user_state(user_id):
    consult_request.pop(user_id, None)
    reports_request.pop(user_id, None)
    support_request.pop(user_id, None)
    decret_request.pop(user_id, None)
    active_chats.pop(user_id, None)

# ======= Головний обробник подій Telegram =======
@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    global admin_target
    update = request.get_json(force=True)

    # ─── Інлайн-кнопки (callback_query) ───
    if "callback_query" in update:
        cb      = update["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        data    = cb.get("data", "")
        from_id = cb["from"]["id"]

        # ── Супровід ФОП ──
        if data == "support":
            send_message(chat_id, SUPPORT_INFO_TEXT, reply_markup=support_groups_inline())
            return "ok", 200

        if data in ("support_1", "support_2", "support_3"):
            group = data.split("_")[1]
            support_request[from_id] = {"stage": "group_selected", "group": group}
            text_map = {"1": SUPPORT_GROUP_1_TEXT, "2": SUPPORT_GROUP_2_TEXT, "3": SUPPORT_GROUP_3_TEXT}
            send_message(chat_id, text_map[group], reply_markup=support_next_inline())
            return "ok", 200

        if data == "support_pay":
            # ВИПРАВЛЕННЯ: використовуємо PAYMENT_DETAILS замість хардкоду
            send_message(chat_id, PAYMENT_DETAILS, parse_mode=None)
            return "ok", 200

        if data == "support_admin":
            if chat_id not in active_chats:
                active_chats[chat_id] = "pending"
                send_message(chat_id, "Очікуйте відповіді адміністратора...", reply_markup=user_finish_markup())
                notif = f"<b>Нове повідомлення по супроводу ФОП!</b>\nID: <pre>{chat_id}</pre>"
                send_message(ADMIN_ID, notif, parse_mode="HTML", reply_markup=admin_reply_markup(chat_id))
            else:
                send_message(chat_id, "Очікуйте відповіді адміністратора...", reply_markup=user_finish_markup())
            return "ok", 200

        if data == "support_back":
            support_request.pop(from_id, None)
            send_message(chat_id, "👋 Ласкаво просимо! Оберіть дію:", reply_markup=main_menu_markup())
            return "ok", 200

        # ── Консультація ──
        if data == "consult":
            consult_request[from_id] = {"stage": "choose_duration"}
            send_message(chat_id, CONSULT_INTRO_TEXT, reply_markup=consult_duration_inline())
            return "ok", 200

        # ВИПРАВЛЕННЯ: обробляємо consult_20 / consult_40 (а не _30/_45/_60)
        if data in ("consult_20", "consult_40"):
            duration = data.split("_")[1]
            price    = CONSULT_PRICES.get(duration, "?")
            consult_request[from_id] = {"stage": "await_contact", "duration": duration}
            msg = CONSULT_CONTACTS_TEXT.format(duration=duration, price=price)
            send_message(chat_id, msg, reply_markup=return_to_menu_markup())
            return "ok", 200

        if data == "consult_back":
            consult_request.pop(from_id, None)
            active_chats.pop(from_id, None)
            send_message(chat_id, "👋 Ласкаво просимо! Оберіть дію:", reply_markup=main_menu_markup())
            return "ok", 200

        # ── Реєстрація / Закриття ФОП ──
        if data == "regclose":
            send_message(chat_id, REGCLOSE_INTRO_TEXT, reply_markup=regclose_inline())
            return "ok", 200

        if data == "fop_register":
            send_message(chat_id, FOP_REGISTER_TEXT, reply_markup=fop_register_inline())
            return "ok", 200

        if data == "fop_register_pay":
            send_message(chat_id, FOP_REGISTER_PAY_TEXT, reply_markup=regclose_inline())
            return "ok", 200

        if data == "fop_close":
            send_message(chat_id, FOP_CLOSE_TEXT, reply_markup=fop_close_inline())
            return "ok", 200

        if data == "fop_close_pay":
            send_message(chat_id, FOP_CLOSE_PAY_TEXT, reply_markup=regclose_inline())
            return "ok", 200

        if data == "regclose_back":
            send_message(chat_id, "👋 Ласкаво просимо! Оберіть дію:", reply_markup=main_menu_markup())
            return "ok", 200

        # ── Звітність і податки ──
        if data == "reports":
            send_message(chat_id, REPORTS_INTRO_TEXT, reply_markup=reports_inline())
            return "ok", 200

        if data == "report_submit":
            send_message(chat_id, REPORT_SUBMIT_TEXT, reply_markup=report_submit_service_inline())
            return "ok", 200

        if data == "report_submit_contacts":
            reports_request[from_id] = {"stage": "await_contact", "type": "submit"}
            send_message(chat_id, REPORT_SUBMIT_CONTACTS_TEXT, reply_markup=return_to_menu_markup())
            return "ok", 200

        if data == "report_tax_check":
            send_message(chat_id, REPORT_TAX_CHECK_TEXT, reply_markup=report_tax_check_inline())
            return "ok", 200

        if data == "tax_check_contacts":
            reports_request[from_id] = {"stage": "await_contact", "type": "taxcheck"}
            send_message(chat_id, REPORT_TAX_CHECK_CONTACTS_TEXT, reply_markup=tax_check_pay_inline())
            return "ok", 200

        if data == "tax_check_pay":
            send_message(chat_id, TAX_CHECK_PAY_TEXT, reply_markup=return_to_menu_markup())
            return "ok", 200

        if data == "reports_back":
            send_message(chat_id, "👋 Ласкаво просимо! Оберіть дію:", reply_markup=main_menu_markup())
            return "ok", 200

        # ── ПРРО ──
        if data == "prro":
            send_message(chat_id, PRRO_INTRO_TEXT, reply_markup=prro_inline())
            return "ok", 200

        if data == "prro_register":
            send_message(chat_id, PRRO_REGISTER_TEXT, reply_markup=prro_register_step_inline())
            return "ok", 200

        if data == "prro_register_pay":
            send_message(chat_id, PRRO_REGISTER_CONTACTS_TEXT, reply_markup=prro_register_pay_inline())
            return "ok", 200

        if data == "prro_pay":
            send_message(chat_id, PRRO_REGISTER_PAY_TEXT, reply_markup=return_to_menu_markup())
            return "ok", 200

        if data == "prro_close":
            send_message(chat_id, PRRO_CLOSE_INTRO_TEXT, reply_markup=prro_close_step_inline())
            return "ok", 200

        if data == "prro_close_apply":
            send_message(chat_id, PRRO_CLOSE_CONTACT_TEXT, reply_markup=prro_close_pay_inline())
            return "ok", 200

        if data == "prro_close_pay":
            send_message(chat_id, PRRO_CLOSE_PAY_TEXT, reply_markup=return_to_menu_markup())
            return "ok", 200

        if data == "prro_back":
            send_message(chat_id, "👋 Ласкаво просимо! Оберіть дію:", reply_markup=main_menu_markup())
            return "ok", 200

        # ── Декрет ФОП ──
        if data == "decret":
            send_message(chat_id, DECRET_SERVICE_TEXT, reply_markup=decret_inline())
            return "ok", 200

        if data == "decret_apply":
            decret_request[from_id] = {"stage": "await_contact"}
            send_message(chat_id, DECRET_CONTACTS_TEXT, reply_markup=decret_pay_inline())
            return "ok", 200

        if data == "decret_pay":
            send_message(chat_id, DECRET_PAY_TEXT, reply_markup=return_to_menu_markup())
            return "ok", 200

        if data == "decret_back":
            send_message(chat_id, "👋 Ласкаво просимо! Оберіть дію:", reply_markup=main_menu_markup())
            return "ok", 200

        # ── Адмін: відповісти / завершити ──
        if data.startswith("reply_") and int(from_id) == ADMIN_ID:
            # ВИПРАВЛЕННЯ: зберігаємо конкретного отримувача, а не targets[0]
            user_id      = int(data.split("_")[1])
            admin_target = user_id
            active_chats[user_id] = "active"
            send_message(ADMIN_ID, f"Надішліть повідомлення або медіа для користувача {user_id}.")
            return "ok", 200

        if data.startswith("close_") and int(from_id) == ADMIN_ID:
            user_id = int(data.split("_")[1])
            if admin_target == user_id:
                admin_target = None
            active_chats.pop(user_id, None)
            # ВИПРАВЛЕННЯ: прибрано зайві пробіли в повідомленнях
            send_message(user_id, "⛔️ Чат завершено адміністратором. Ви повернулись у головне меню.", reply_markup=main_menu_markup())
            send_message(ADMIN_ID, "Чат завершено.")
            return "ok", 200

    # ─── Текстові повідомлення ───
    msg       = update.get("message")
    if not msg:
        return "ok", 200

    cid       = msg.get("chat", {}).get("id")
    text      = msg.get("text", "") or ""
    user_data = msg.get("from", {})
    user_id   = user_data.get("id")
    user_name = (user_data.get("first_name", "") + " " + user_data.get("last_name", "")).strip() or "Користувач"
    has_media = any(k in msg for k in ("photo", "document", "video", "audio", "voice"))

    # ── /start або Повернутися в меню ──
    if text.startswith("/start") or text == "Повернутися в меню":
        clear_user_state(user_id)
        if admin_target == user_id:
            admin_target = None
        send_message(cid, "👋 Ласкаво просимо! Оберіть дію:", reply_markup=main_menu_markup())
        return "ok", 200

    if text == "Меню":
        send_message(cid, WELCOME_SERVICES_TEXT, reply_markup=welcome_services_inline(), parse_mode="HTML")
        return "ok", 200

    # ВИПРАВЛЕННЯ: реквізити тепер використовують PAYMENT_DETAILS
    if text == "Реквізити для оплати" and cid not in active_chats:
        send_message(cid, PAYMENT_DETAILS, reply_markup=main_menu_markup())
        return "ok", 200

    # ── Поставити питання ──
    if text == "Поставити питання" and cid not in active_chats:
        active_chats[cid] = "pending"
        # ВИПРАВЛЕННЯ: прибрано зайві пробіли в повідомленні
        send_message(cid, "Очікуйте відповіді адміністратора...", reply_markup=user_finish_markup())
        notif = f"<b>Нове повідомлення від користувача!</b>\nВід: {escape(user_name)}\nID: <pre>{cid}</pre>"
        send_message(ADMIN_ID, notif, parse_mode="HTML", reply_markup=admin_reply_markup(cid))
        return "ok", 200

    # ── Завершення чату користувачем ──
    if text == "Завершити чат" and cid in active_chats:
        if admin_target == cid:
            admin_target = None
        active_chats.pop(cid, None)
        send_message(cid, "⛔️ Чат завершено. Ви повернулись у головне меню.", reply_markup=main_menu_markup())
        send_message(ADMIN_ID, f"Користувач {cid} завершив чат.")
        return "ok", 200

    # ── Переписка користувача → адмін ──
    if cid in active_chats and active_chats[cid] == "active":
        if has_media:
            send_media(ADMIN_ID, msg)
            send_message(ADMIN_ID, f"[медіа від {cid}]", reply_markup=admin_reply_markup(cid))
        elif text and text != "Завершити чат":
            send_message(ADMIN_ID, f"Користувач {cid}:\n<pre>{escape(text)}</pre>", parse_mode="HTML", reply_markup=admin_reply_markup(cid))
        return "ok", 200

    # ── Адмін → поточний співрозмовник ──
    if cid == ADMIN_ID:
        # ВИПРАВЛЕННЯ: використовуємо admin_target, а не targets[0]
        target = admin_target
        if not target:
            return "ok", 200
        if has_media:
            send_media(target, msg)
            send_message(target, "💬 Відповідь адміністратора (медіа).", reply_markup=user_finish_markup())
        elif text.lower().startswith("завершити"):
            active_chats.pop(target, None)
            admin_target = None
            send_message(target, "⛔️ Чат завершено адміністратором. Ви повернулись у головне меню.", reply_markup=main_menu_markup())
            send_message(ADMIN_ID, "Чат завершено.")
        elif text:
            send_message(target, f"💬 Відповідь адміністратора:\n<pre>{escape(text)}</pre>", parse_mode="HTML", reply_markup=user_finish_markup())
        return "ok", 200

    # ── Якщо в чаті pending — лише "Завершити чат" ──
    if cid in active_chats:
        send_message(cid, "У активному чаті доступні тільки переписка і кнопка «Завершити чат».", reply_markup=user_finish_markup())
        return "ok", 200

    # ── Збір контактів: консультація ──
    if user_id in consult_request and consult_request[user_id].get("stage") == "await_contact":
        duration = consult_request[user_id].get("duration")
        price    = CONSULT_PRICES.get(duration, "?")
        note = (
            f"<b>Заявка на консультацію</b>\n"
            f"Тривалість: {duration} хв — {price} грн\n"
            f"Від: {escape(user_name)}\n"
            f"ID: <pre>{user_id}</pre>\n"
        )
        if has_media:
            send_message(ADMIN_ID, note, parse_mode="HTML", reply_markup=admin_reply_markup(user_id))
            send_media(ADMIN_ID, msg)
        elif text:
            note += f"Контакти: <pre>{escape(text.strip())}</pre>"
            send_message(ADMIN_ID, note, parse_mode="HTML", reply_markup=admin_reply_markup(user_id))
        send_message(user_id, "Дякуємо! Ваші дані отримано, з вами зв'яжеться адміністратор.", reply_markup=main_menu_markup())
        consult_request.pop(user_id, None)
        return "ok", 200

    # ── Збір контактів: звітність/податки ──
    if user_id in reports_request and reports_request[user_id].get("stage") == "await_contact":
        req_type = reports_request[user_id].get("type")
        if req_type == "submit":
            note = (
                f"<b>Заявка на подання звітності</b>\n"
                f"Від: {escape(user_name)}\n"
                f"ID: <pre>{user_id}</pre>\n"
            )
            if text:
                note += f"Контакти: <pre>{escape(text.strip())}</pre>"
            send_message(ADMIN_ID, note, parse_mode="HTML", reply_markup=admin_reply_markup(user_id))
            send_message(user_id, "Дякуємо! Ваші дані отримано, звітність буде підготовлена найближчим часом.", reply_markup=main_menu_markup())
            reports_request.pop(user_id, None)
            return "ok", 200
        elif req_type == "taxcheck":
            note = (
                f"<b>Запит на перевірку ФОП/податків</b>\n"
                f"Від: {escape(user_name)}\n"
                f"ID: <pre>{user_id}</pre>\n"
            )
            if text:
                note += f"Контакти: <pre>{escape(text.strip())}</pre>"
            send_message(ADMIN_ID, note, parse_mode="HTML", reply_markup=admin_reply_markup(user_id))
            send_message(user_id, "Дякуємо! Перевірка буде виконана і вся інформація надана у відповіді.", reply_markup=main_menu_markup())
            reports_request.pop(user_id, None)
            return "ok", 200

    # ── Збір контактів: декрет ──
    if user_id in decret_request and decret_request[user_id].get("stage") == "await_contact":
        note = (
            f"<b>Заявка на оформлення декретних</b>\n"
            f"Від: {escape(user_name)}\n"
            f"ID: <pre>{user_id}</pre>\n"
        )
        if has_media:
            send_message(ADMIN_ID, note, parse_mode="HTML", reply_markup=admin_reply_markup(user_id))
            send_media(ADMIN_ID, msg)
        elif text:
            note += f"Контакти: <pre>{escape(text.strip())}</pre>"
            send_message(ADMIN_ID, note, parse_mode="HTML", reply_markup=admin_reply_markup(user_id))
        send_message(user_id, "Дякуємо! Ваші дані отримано, розпочнемо підготовку документів. Якщо потрібно щось ще — зв'яжемось з вами.", reply_markup=main_menu_markup())
        decret_request.pop(user_id, None)
        return "ok", 200

    # ── Fallback ──
    send_message(cid, "Будь ласка, оберіть дію з меню 👇", reply_markup=main_menu_markup())
    return "ok", 200

# ======= Ping ендпоінт =======
@app.route("/", methods=["GET"])
def index():
    return "OK", 200

if __name__ == "__main__":
    app.run("0.0.0.0", port=int(os.getenv("PORT", "5000")))
