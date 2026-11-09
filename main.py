import base64
import html
import logging
import os
import random
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import requests
import telebot
from google import genai
from google.genai import types as genai_types
from telebot import apihelper, types

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lavk_bot")

# ==========================================
# 1. КОНФИГУРАЦИЯ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
FAL_KEY = os.getenv("FAL_API_KEY")
CHANNEL_ID = "@Master_Lavk"
FAL_ENDPOINT = "https://fal.run/fal-ai/flux-pulid"
WELCOME_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "1794190872103.jpg")

if not BOT_TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")

apihelper.READ_TIMEOUT = 90
apihelper.CONNECT_TIMEOUT = 90

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

user_states: dict[int, int] = {}
user_data: dict[int, dict] = {}

(
    STATE_NONE, STATE_PHOTO, STATE_FACTION, STATE_NAME,
    STATE_CATEGORY, STATE_POWER, STATE_ARMOR, STATE_PROVISION,
    STATE_FRAME, STATE_ABILITY, STATE_QUOTE, STATE_CONFIRM, STATE_GENERATING,
) = range(13)

# key: (кнопка, название фракции, промпт для арта)
FACTIONS = {
    "Nilfgaard": (
        "🛡 Нильфгаард", "Нильфгаард",
        "A highly detailed dark fantasy portrait of a character, wearing heavy, ornate black and gold Nilfgaardian plate armor with a prominent sun emblem. The character exudes a stern, disciplined, and aristocratic aura. Detailed facial features, cinematic lighting, sharp focus on the armor's metallic textures. Background: A sprawling, muddy Nilfgaardian military camp at dusk, featuring imposing black and gold pavilions, flickering campfires, and silhouettes of soldiers with tall pikes in the distance. Color palette: black, gold, dark grey, crimson accents. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution, trending on ArtStation."
    ),
    "Monsters": (
        "👾 Чудовища", "Чудовища",
        "A highly detailed dark fantasy portrait of a terrifying character, mutated with monstrous features, pale veiny skin, wearing grim bone and chitin armor adorned with skulls. The character looks feral and predatory. Eerie, unnatural lighting casting long shadows. Background: A dense, terrifying, foggy swamp at midnight. Twisted, dead ancient trees reaching like claws, glowing yellow eyes hidden in the thick green mist, bubbling toxic water. Color palette: sickly greens, bone white, blood red, abyssal black. Masterpiece, digital oil painting, official Gwent card art style, macabre, 8k resolution."
    ),
    "Skellige": (
        "⚓ Скеллиге", "Скеллиге",
        "A highly detailed dark fantasy portrait of a rugged viking warrior character, wearing heavy bear-fur mantles, leather padding, and Skellige clan armor with carved celtic knots and runic symbols. Weathered, sea-salt stained textures. Harsh, dramatic, cold lighting. Background: A stormy nordic oceanic coast. Massive crashing waves against jagged steep cliffs, dark storm clouds, heavy rain, and the prow of a wooden viking longship with a dragon head in the turbulent waters. Color palette: cold icy blues, earthy browns, steel grey, sea foam white. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    ),
    "Northern": (
        "⚜️ Королевства Севера", "Королевства Севера",
        "A highly detailed dark fantasy portrait of a noble medieval character, wearing gleaming, polished steel plate armor draped with blue and silver heraldic tabards. Heroic and stoic pose. Bright, warm, cinematic sunlight highlighting the metallic reflections. Background: A bustling, war-torn courtyard of a besieged massive stone medieval castle. Thick stone walls, wooden trebuchets, flying arrows in the sky, and fluttering blue and silver royal banners. Color palette: royal blue, bright steel, red accents, warm golden sunlight. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    ),
    "Syndicate": (
        "💰 Синдикат", "Синдикат",
        "A highly detailed dark fantasy portrait of a cunning and wealthy character, wearing lavish but slightly dirty and frayed medieval merchant clothes, heavy velvet coats, and multiple gold rings on fingers. Shady, conspiratorial look. Warm, glowing lantern light mixed with deep, harsh shadows. Background: A claustrophobic, dirty Novigrad city alleyway at night. Wet cobblestones reflecting the orange glow of wall lanterns, rain puddles, scattered golden coins, and a shadowy figure in the background. Color palette: rich purple, gold, dark murky greens, deep shadows. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    ),
    "Scoiatael": (
        "🐿 Скоя'таэли", "Скоя'таэли",
        "A highly detailed dark fantasy portrait of an elven guerilla character, wearing intricate forest camouflage, green and brown leather armor decorated with leaves and animal fangs. Agile, alert posture. Soft, dappled forest lighting filtering through leaves. Background: A deep, ancient, magical elven forest. Colossal ancient oak trees covered in moss, crumbling white marble elven ruins, blooming glowing flora, and shafts of golden ethereal sunlight piercing the dense canopy. Color palette: lush forest greens, autumn browns, golden light, earthy tones. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    ),
    "Neutral": (
        "🐺 Нейтральная", "Нейтральная",
        "A highly detailed dark fantasy portrait of a hardened, weary character like a witcher or mercenary, wearing well-worn, dark leather studded armor, a silver medallion, and the hilts of twin swords strapped to the back. Gritty, realistic textures. Warm, flickering firelight illuminating the face. Background: A cozy but dimly lit medieval tavern interior. Heavy wooden oak tables, flickering wax candles on skulls, massive ale barrels, and blurry silhouettes of tavern patrons drinking in the background. Color palette: dark leather browns, silver, muted greys, warm orange firelight. Masterpiece, digital oil painting, official Gwent card art style, 8k resolution."
    ),
}

FRAMES = {"gold": "Золотая", "bronze": "Бронзовая"}

MENU_CREATE = "🃏 Создать карту"
MENU_CANCEL = "❌ Отмена"
MENU_STUBS = ["📜 Мои картищи", "👤 Мой профиль", "🔗 Позвать друзей", "🆘 Поддержка"]


# ==========================================
# 2. ВСПОМОГАТЕЛЬНОЕ
# ==========================================
def esc(value) -> str:
    return html.escape(str(value))


def safe_call(func, *args, **kwargs):
    """Для некритичных вызовов (удаление/редактирование старых сообщений, ответ на callback)."""
    try:
        return func(*args, **kwargs)
    except apihelper.ApiTelegramException as e:
        logger.debug("Telegram API call skipped: %s", e)
        return None


def check_sub(user_id: int) -> bool:
    # Работает, только если бот — администратор канала CHANNEL_ID.
    try:
        status = bot.get_chat_member(CHANNEL_ID, user_id).status
        return status in ("member", "administrator", "creator")
    except apihelper.ApiTelegramException as e:
        logger.warning("Не удалось проверить подписку %s: %s", user_id, e)
        return False


def send_sub_request(chat_id: int):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📢 Пойти в пещеру (Подписаться)", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}"))
    markup.add(types.InlineKeyboardButton("🔄 Я подписался!", callback_data="check_sub_btn"))
    bot.send_message(
        chat_id,
        "🛑 <b>СТОЯТЬ, ЧЕЛОВЕЧЕ!</b>\nЛавк не пускает чужаков. Сначала зайди в мою пещеру "
        "(подпишись на канал), а потом будем делать картищи!",
        reply_markup=markup,
    )


def get_main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton(MENU_CREATE))
    markup.add(types.KeyboardButton(MENU_STUBS[0]), types.KeyboardButton(MENU_STUBS[1]))
    markup.add(types.KeyboardButton(MENU_STUBS[2]), types.KeyboardButton(MENU_STUBS[3]))
    return markup


def get_cancel_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton(MENU_CANCEL))
    return markup


def gen_ai_markup(step_key: str):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🎲 Пусть духи придумают", callback_data=f"ai_{step_key}"))
    return markup


def reset_user(chat_id: int):
    user_states[chat_id] = STATE_NONE
    user_data.pop(chat_id, None)


# ==========================================
# 3. ГЕНЕРАЦИЯ ПОЛЕЙ (Gemini / случайные числа)
# ==========================================
def ask_gemini(prompt: str) -> Optional[str]:
    if gemini_client is None:
        return None
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(temperature=1.2),
        )
        text = (response.text or "").strip().strip('"«»').replace("*", "")
        return text or None
    except Exception:
        logger.exception("Ошибка Gemini")
        return None


def text_validator(max_len: int, max_words: Optional[int] = None):
    def validate(value: str) -> Optional[str]:
        if not value:
            return "⚠️ Пусто! Напиши что-нибудь, человече."
        if len(value) > max_len:
            return f"⚠️ Слишком длинно ({len(value)}/{max_len})!"
        if max_words and len(value.split()) > max_words:
            return f"⚠️ Не больше {max_words} слов!"
        return None
    return validate


def int_validator(lo: int, hi: int):
    def validate(value: str) -> Optional[str]:
        if not value.isdigit() or not lo <= int(value) <= hi:
            return f"⚠️ Только цифры от {lo} до {hi}!"
        return None
    return validate


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    prompt: str
    validate: Callable[[str], Optional[str]]
    next_state: int
    ai_prompt: str = ""
    fallbacks: tuple = ()
    random_range: Optional[tuple] = None


def generate_value(step: Step, faction: str) -> str:
    if step.random_range:
        return str(random.randint(*step.random_range))
    value = ask_gemini(step.ai_prompt.format(faction=faction))
    if value is None or step.validate(value):
        if value is not None:
            logger.info("Gemini вернул неподходящее значение для %s: %r", step.key, value)
        value = random.choice(step.fallbacks)
    return value


STEPS = {
    STATE_NAME: Step(
        "name", "📜 Имя", "📜 <b>Как тебя звать-величать?</b>",
        text_validator(35), STATE_CATEGORY,
        ai_prompt="Сгенерируй случайное, уникальное имя для персонажа Гвинта (фракция {faction}). 1-3 слова. Только имя.",
        fallbacks=("Грозный Воин", "Старый Волк"),
    ),
    STATE_CATEGORY: Step(
        "category", "🧬 Категория", "🧬 <b>Ты вообще кто такой? (Категория)</b>",
        text_validator(30, max_words=3), STATE_POWER,
        ai_prompt="Сгенерируй случайные теги/категорию для карты Гвинта, фракция {faction}. 1-2 слова. Только теги.",
        fallbacks=("Человек, Воин", "Чудовище"),
    ),
    STATE_POWER: Step(
        "power", "⚔️ Сила", "⚔️ <b>Насколько больно бьешь? (Сила 1-99)</b>",
        int_validator(1, 99), STATE_ARMOR, random_range=(2, 12),
    ),
    STATE_ARMOR: Step(
        "armor", "🛡 Броня", "🛡 <b>Щит есть? (Броня 0-99)</b>",
        int_validator(0, 99), STATE_PROVISION, random_range=(0, 4),
    ),
    STATE_PROVISION: Step(
        "provision", "🍖 Наём", "🍖 <b>Сколько еды жрешь в походе? (Стоимость найма 4-99)</b>",
        int_validator(4, 99), STATE_FRAME, random_range=(4, 14),
    ),
    STATE_ABILITY: Step(
        "ability", "✨ Способность", "✨ <b>Что делать умеешь на столе? (Способность)</b>",
        text_validator(150), STATE_QUOTE,
        ai_prompt="Сгенерируй новую способность карты для игры Гвинт (фракция {faction}). Максимум 25 слов. Без кавычек.",
        fallbacks=("Размещение: нанесите 2 единицы урона.",),
    ),
    STATE_QUOTE: Step(
        "quote", "💬 Цитата", "💬 <b>Умная мысль для самого низа (Цитата)</b>",
        text_validator(150), STATE_CONFIRM,
        ai_prompt="Сгенерируй случайную мрачную цитату для карты Гвинт (фракция {faction}). Максимум 20 слов. Без кавычек.",
        fallbacks=("Смерть — лишь начало.",),
    ),
}
STEP_BY_KEY = {step.key: state for state, step in STEPS.items()}


# ==========================================
# 4. ПЕРЕХОДЫ МЕЖДУ ШАГАМИ
# ==========================================
def go_to(chat_id: int, state: int, prefix: str = ""):
    user_states[chat_id] = state
    if state == STATE_FRAME:
        markup = types.InlineKeyboardMarkup()
        markup.add(*[
            types.InlineKeyboardButton(("🟡 " if key == "gold" else "🟤 ") + name, callback_data=f"frame_{key}")
            for key, name in FRAMES.items()
        ])
        bot.send_message(chat_id, prefix + "🖼 <b>Какой редкости будет твоя карта?</b>", reply_markup=markup)
    elif state == STATE_CONFIRM:
        if prefix:
            bot.send_message(chat_id, prefix.strip())
        ask_for_confirmation(chat_id)
    else:
        step = STEPS[state]
        bot.send_message(chat_id, prefix + step.prompt, reply_markup=gen_ai_markup(step.key))


def card_summary(data: dict) -> str:
    return (
        f"🏴 Фракция: {esc(data['faction'])}\n"
        f"📜 Имя: <b>{esc(data['name'])}</b>\n"
        f"🧬 Категория: {esc(data['category'])}\n"
        f"⚔️ Сила: {esc(data['power'])} | 🛡 Броня: {esc(data['armor'])} | 🍖 Наём: {esc(data['provision'])}\n"
        f"🖼 Редкость: {esc(data['frame'])}\n"
        f"✨ Способность: {esc(data['ability'])}\n"
        f"💬 Цитата: <i>{esc(data['quote'])}</i>"
    )


def ask_for_confirmation(chat_id: int):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ ДА, РИСУЙ!", callback_data="confirm_yes"),
        types.InlineKeyboardButton("❌ ОТМЕНА", callback_data="confirm_no"),
    )
    text = (
        "🎉 <b>Лавк собрал твои бумажки! ПРОВЕРЯЙ:</b>\n\n"
        + card_summary(user_data[chat_id])
        + "\n\n<b>Всё верно, человече? Запускаем нейромагию?</b>"
    )
    bot.send_message(chat_id, text, reply_markup=markup)


# ==========================================
# 5. ОБРАБОТЧИКИ
# ==========================================
def show_welcome(chat_id: int):
    reset_user(chat_id)
    welcome_text = (
        "👋 <b>Ууу, здорово, человече!</b> Я — Лавк, твой верный проводник в мире легендарного Гвинта.\n\n"
        "Здесь пахнет краской, старым пергаментом и магией! 🎨✨\n\n"
        "Готов увековечить себя на великой картище? Выбирай дело по душе, и погнали творить искусство!"
    )
    if os.path.exists(WELCOME_IMAGE):
        with open(WELCOME_IMAGE, "rb") as photo:
            bot.send_photo(chat_id, photo, caption=welcome_text, reply_markup=get_main_menu())
    else:
        bot.send_message(chat_id, welcome_text, reply_markup=get_main_menu())


@bot.message_handler(commands=["start"])
def send_welcome(message):
    if not check_sub(message.from_user.id):
        send_sub_request(message.chat.id)
        return
    show_welcome(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "check_sub_btn")
def verify_sub_callback(call):
    if not check_sub(call.from_user.id):
        bot.answer_callback_query(call.id, "Ууу, Лавк не видит подписки! Ты обманываешь!", show_alert=True)
        return
    safe_call(bot.answer_callback_query, call.id, "Лавк рад! Заходи!")
    safe_call(bot.delete_message, call.message.chat.id, call.message.message_id)
    show_welcome(call.message.chat.id)


@bot.message_handler(func=lambda msg: msg.text == MENU_CANCEL)
def cancel_creation(message):
    if user_states.get(message.chat.id) == STATE_GENERATING:
        bot.send_message(message.chat.id, "⏳ Лавк уже рисует, подожди чуть-чуть!")
        return
    reset_user(message.chat.id)
    bot.send_message(message.chat.id, "Эх, Лавк убирает холст. Если захочешь новую картищу — жми кнопку!",
                     reply_markup=get_main_menu())


@bot.message_handler(func=lambda msg: msg.text in MENU_STUBS)
def menu_stubs(message):
    bot.send_message(message.chat.id, "🛠 <i>Эта кнопка пока в разработке. Скоро Лавк её доделает!</i>")


@bot.message_handler(func=lambda msg: msg.text == MENU_CREATE)
def start_creation(message):
    chat_id = message.chat.id
    if user_states.get(chat_id) == STATE_GENERATING:
        bot.send_message(chat_id, "⏳ Лавк уже рисует твою прошлую картищу, подожди!")
        return
    if not check_sub(message.from_user.id):
        send_sub_request(chat_id)
        return
    user_data[chat_id] = {}
    user_states[chat_id] = STATE_PHOTO
    bot.send_message(
        chat_id,
        "📸 <b>Сначала давай лицо!</b>\n\nЧеловече давать хорошую картинку. Без теней, без темных стекол "
        "на глазах и чтобы ничего не закрывало! Духам нужно видеть твои черты, чтобы красиво перенести "
        "на холст. Плохое лицо — плохая картища!",
        reply_markup=get_cancel_menu(),
    )


@bot.message_handler(content_types=["photo", "document", "text"],
                     func=lambda msg: user_states.get(msg.chat.id) == STATE_PHOTO)
def process_photo(message):
    chat_id = message.chat.id
    if message.photo:
        user_data[chat_id]["photo_id"] = message.photo[-1].file_id
        user_data[chat_id]["photo_mime"] = "image/jpeg"
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        user_data[chat_id]["photo_id"] = message.document.file_id
        user_data[chat_id]["photo_mime"] = message.document.mime_type
    else:
        bot.send_message(chat_id, "⚠️ Лавк просил картинку! Пришли фото, человече.")
        return

    user_states[chat_id] = STATE_FACTION
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[types.InlineKeyboardButton(val[0], callback_data=f"faction_{key}") for key, val in FACTIONS.items()])
    bot.send_message(chat_id, "🏴 <b>За кого воевать будешь?</b>\n\nВыбирай знамя, человече!", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("faction_"))
def process_faction(call):
    chat_id = call.message.chat.id
    faction_key = call.data.split("_", 1)[1]
    if user_states.get(chat_id) != STATE_FACTION or faction_key not in FACTIONS:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    label, name, prompt = FACTIONS[faction_key]
    user_data[chat_id]["faction"] = name
    user_data[chat_id]["faction_prompt"] = prompt
    safe_call(bot.answer_callback_query, call.id)
    safe_call(bot.edit_message_text, f"✅ Фракция: <b>{esc(label)}</b>", chat_id, call.message.message_id)
    go_to(chat_id, STATE_NAME)


@bot.callback_query_handler(func=lambda call: call.data.startswith("frame_"))
def process_frame(call):
    chat_id = call.message.chat.id
    frame_key = call.data.split("_", 1)[1]
    if user_states.get(chat_id) != STATE_FRAME or frame_key not in FRAMES:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    user_data[chat_id]["frame"] = FRAMES[frame_key]
    safe_call(bot.answer_callback_query, call.id)
    safe_call(bot.edit_message_text, f"✅ Редкость: <b>{esc(FRAMES[frame_key])}</b>", chat_id, call.message.message_id)
    go_to(chat_id, STATE_ABILITY)


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) in STEPS)
def process_step_text(message):
    chat_id = message.chat.id
    state = user_states[chat_id]
    step = STEPS[state]
    value = message.text.strip()
    error = step.validate(value)
    if error:
        bot.send_message(chat_id, error)
        return
    user_data[chat_id][step.key] = value
    go_to(chat_id, step.next_state)


def run_ai_step(chat_id: int, state: int, message_id: int):
    step = STEPS[state]
    faction = user_data.get(chat_id, {}).get("faction", "Нейтральная")
    try:
        value = generate_value(step, faction)
        # Пользователь мог отменить создание, пока духи думали.
        if user_states.get(chat_id) != state:
            return
        user_data[chat_id][step.key] = value
        safe_call(bot.delete_message, chat_id, message_id)
        go_to(chat_id, step.next_state, prefix=f"{step.label}: <b>{esc(value)}</b>\n\n")
    except Exception:
        logger.exception("Ошибка при генерации шага %s", step.key)


@bot.callback_query_handler(func=lambda call: call.data.startswith("ai_"))
def process_ai_button(call):
    chat_id = call.message.chat.id
    state = STEP_BY_KEY.get(call.data.split("_", 1)[1])
    if state is None or user_states.get(chat_id) != state:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    safe_call(bot.answer_callback_query, call.id, "Духи думают...")
    # Убираем кнопку сразу, чтобы повторное нажатие не запустило вторую генерацию.
    safe_call(bot.edit_message_text, "<i>(Духи шепчут...)</i>", chat_id, call.message.message_id)
    threading.Thread(target=run_ai_step, args=(chat_id, state, call.message.message_id), daemon=True).start()


@bot.callback_query_handler(func=lambda call: call.data in ("confirm_yes", "confirm_no"))
def process_confirmation(call):
    chat_id = call.message.chat.id
    if user_states.get(chat_id) != STATE_CONFIRM:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    safe_call(bot.answer_callback_query, call.id)

    if call.data == "confirm_no":
        reset_user(chat_id)
        safe_call(bot.edit_message_text, "Эх, порвали холст!", chat_id, call.message.message_id)
        bot.send_message(chat_id, "Начинай заново, когда будешь готов!", reply_markup=get_main_menu())
        return

    user_states[chat_id] = STATE_GENERATING
    safe_call(bot.edit_message_text, "✅ Данные подтверждены! Лавк пошел за красками...", chat_id, call.message.message_id)
    threading.Thread(target=generate_card, args=(chat_id, dict(user_data[chat_id])), daemon=True).start()


# ==========================================
# 6. ГЕНЕРАЦИЯ АРТА (Fal.ai)
# ==========================================
class FalError(Exception):
    pass


def request_fal_image(prompt: str, image_bytes: bytes, mime: str) -> str:
    # Fal принимает base64 data URI в полях-файлах, отдельная загрузка в storage не нужна.
    image_uri = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    payload = {
        "prompt": prompt,
        "reference_image_url": image_uri,
        "image_size": "portrait_4_3",
        "num_inference_steps": 28,
        "guidance_scale": 4.0,
        "true_cfg": 1.0,
        "id_weight": 0.85,
    }
    headers = {"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"}
    response = requests.post(FAL_ENDPOINT, json=payload, headers=headers, timeout=180)
    if not response.ok:
        logger.error("Fal.ai %s: %s", response.status_code, response.text[:1000])
        raise FalError(f"Fal.ai вернул код {response.status_code}")
    try:
        return response.json()["images"][0]["url"]
    except (ValueError, KeyError, IndexError, TypeError):
        logger.error("Неожиданный ответ Fal.ai: %s", response.text[:1000])
        raise FalError("Fal.ai вернул ответ без картинки")


def generate_card(chat_id: int, data: dict):
    status_msg = bot.send_message(
        chat_id, "🎨 <i>Лавк творит истинную магию (вживляем твое лицо в арт)... Это займет около 30-40 секунд!</i>"
    )
    try:
        if not FAL_KEY:
            raise FalError("не задан FAL_API_KEY")
        file_info = bot.get_file(data["photo_id"])
        image_bytes = bot.download_file(file_info.file_path)
        image_url = request_fal_image(data["faction_prompt"], image_bytes, data["photo_mime"])
        art = requests.get(image_url, timeout=90)
        art.raise_for_status()

        safe_call(bot.delete_message, chat_id, status_msg.message_id)
        bot.send_photo(
            chat_id,
            art.content,
            caption="🔥 <b>ТВОЯ ИСТИННАЯ КАРТИЩА ГОТОВА!</b>\n\n" + card_summary(data),
            reply_markup=get_main_menu(),
        )
    except FalError as e:
        safe_call(bot.edit_message_text, f"⚠️ Духи не смогли нарисовать: {esc(e)}", chat_id, status_msg.message_id)
        bot.send_message(chat_id, "Попробуй ещё раз чуть позже.", reply_markup=get_main_menu())
    except Exception:
        # Текст исключения пользователю не показываем: в URL Telegram-файлов содержится токен бота.
        logger.exception("Ошибка генерации карты для %s", chat_id)
        safe_call(bot.edit_message_text, "⚠️ Системная ошибка связи. Попробуй ещё раз.", chat_id, status_msg.message_id)
        bot.send_message(chat_id, "Лавк ждёт тебя в меню.", reply_markup=get_main_menu())
    finally:
        reset_user(chat_id)


@bot.message_handler(content_types=["text", "photo", "document", "sticker", "voice", "video"])
def fallback_handler(message):
    state = user_states.get(message.chat.id, STATE_NONE)
    if state == STATE_GENERATING:
        bot.send_message(message.chat.id, "⏳ Лавк рисует, подожди чуть-чуть!")
    elif state in (STATE_FACTION, STATE_FRAME, STATE_CONFIRM):
        bot.send_message(message.chat.id, "👆 Выбери вариант кнопкой выше, человече.")
    elif state in STEPS:
        bot.send_message(message.chat.id, "⚠️ Лавк ждёт текст!")
    else:
        bot.send_message(message.chat.id, "Жми кнопки в меню, человече!", reply_markup=get_main_menu())


if __name__ == "__main__":
    logger.info("Bot is listening...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)
