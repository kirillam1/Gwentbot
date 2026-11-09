import base64
import html
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Optional
from urllib.parse import quote

import requests
import telebot
from google import genai
from google.genai import types as genai_types
from telebot import apihelper, types
from telebot.handler_backends import BaseMiddleware, CancelUpdate

from card_render import render_card, strip_markup
from db import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lavk_bot")

# ==========================================
# 1. КОНФИГУРАЦИЯ
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
FAL_KEY = os.getenv("FAL_API_KEY")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_ID", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "lavk.db"))

CHANNEL_ID = "@Master_Lavk"
SUPPORT_CONTACT = "@S159YKRF4"
WELCOME_IMAGE = os.path.join(BASE_DIR, "1794190872103.jpg")

FAL_GENERATE_ENDPOINT = "https://fal.run/fal-ai/flux-pulid"
FAL_EDIT_ENDPOINT = "https://fal.run/fal-ai/flux-pro/kontext"

GENERATION_PRICE = 120
EDIT_PRICE = 60
REFERRAL_BONUS = 60
TOPUP_PRESETS = (120, 240, 600, 1200)
TOPUP_MIN, TOPUP_MAX = 1, 10000
CARDS_PER_PAGE = 6

if not BOT_TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")
if not ADMIN_IDS:
    logger.warning("ADMIN_ID не задан — админка недоступна")

apihelper.READ_TIMEOUT = 90
apihelper.CONNECT_TIMEOUT = 90

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", use_class_middlewares=True)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
db = Database(DB_PATH)

# Состояние диалога живёт в памяти: после перезапуска незавершённый ввод просто начинается заново.
user_states: dict[int, int] = {}
user_data: dict[int, dict] = {}
busy_users: set[int] = set()

(
    STATE_NONE, STATE_PHOTO, STATE_FACTION, STATE_NAME,
    STATE_CATEGORY, STATE_POWER, STATE_ARMOR, STATE_PROVISION,
    STATE_FRAME, STATE_ABILITY, STATE_QUOTE, STATE_CONFIRM, STATE_SPIRITS,
    STATE_EDIT_PROMPT, STATE_EDIT_CONFIRM, STATE_TOPUP_AMOUNT,
    STATE_ADMIN_FIND, STATE_ADMIN_BALANCE, STATE_ADMIN_BROADCAST, STATE_ADMIN_BROADCAST_CONFIRM,
) = range(20)

# key: (кнопка, название фракции, промпт для арта)
FACTIONS = {
    "Nilfgaard": (
        "🛡 Нильфгаард", "Нильфгаард",
        "A highly detailed dark fantasy portrait of a character, wearing heavy, ornate black and gold Nilfgaardian plate armor with a prominent sun emblem. The character exudes a stern, disciplined, and aristocratic aura. Detailed facial features, cinematic lighting, sharp focus on the armor's metallic textures. Background: A sprawling, muddy Nilfgaardian military camp at dusk, featuring imposing black and gold pavilions, flickering campfires, and silhouettes of soldiers with tall pikes in the distance. Color palette: black, gold, dark grey, crimson accents."
    ),
    "Monsters": (
        "👾 Чудовища", "Чудовища",
        "A highly detailed dark fantasy portrait of a terrifying character, mutated with monstrous features, pale veiny skin, wearing grim bone and chitin armor adorned with skulls. The character looks feral and predatory. Eerie, unnatural lighting casting long shadows. Background: A dense, terrifying, foggy swamp at midnight. Twisted, dead ancient trees reaching like claws, glowing yellow eyes hidden in the thick green mist, bubbling toxic water. Color palette: sickly greens, bone white, blood red, abyssal black."
    ),
    "Skellige": (
        "⚓ Скеллиге", "Скеллиге",
        "A highly detailed dark fantasy portrait of a rugged viking warrior character, wearing heavy bear-fur mantles, leather padding, and Skellige clan armor with carved celtic knots and runic symbols. Weathered, sea-salt stained textures. Harsh, dramatic, cold lighting. Background: A stormy nordic oceanic coast. Massive crashing waves against jagged steep cliffs, dark storm clouds, heavy rain, and the prow of a wooden viking longship with a dragon head in the turbulent waters. Color palette: cold icy blues, earthy browns, steel grey, sea foam white."
    ),
    "Northern": (
        "⚜️ Королевства Севера", "Королевства Севера",
        "A highly detailed dark fantasy portrait of a noble medieval character, wearing gleaming, polished steel plate armor draped with blue and silver heraldic tabards. Heroic and stoic pose. Bright, warm, cinematic sunlight highlighting the metallic reflections. Background: A bustling, war-torn courtyard of a besieged massive stone medieval castle. Thick stone walls, wooden trebuchets, flying arrows in the sky, and fluttering blue and silver royal banners. Color palette: royal blue, bright steel, red accents, warm golden sunlight."
    ),
    "Syndicate": (
        "💰 Синдикат", "Синдикат",
        "A highly detailed dark fantasy portrait of a cunning and wealthy character, wearing lavish but slightly dirty and frayed medieval merchant clothes, heavy velvet coats, and multiple gold rings on fingers. Shady, conspiratorial look. Warm, glowing lantern light mixed with deep, harsh shadows. Background: A claustrophobic, dirty Novigrad city alleyway at night. Wet cobblestones reflecting the orange glow of wall lanterns, rain puddles, scattered golden coins, and a shadowy figure in the background. Color palette: rich purple, gold, dark murky greens, deep shadows."
    ),
    "Scoiatael": (
        "🐿 Скоя'таэли", "Скоя'таэли",
        "A highly detailed dark fantasy portrait of an elven guerilla character, wearing intricate forest camouflage, green and brown leather armor decorated with leaves and animal fangs. Agile, alert posture. Soft, dappled forest lighting filtering through leaves. Background: A deep, ancient, magical elven forest. Colossal ancient oak trees covered in moss, crumbling white marble elven ruins, blooming glowing flora, and shafts of golden ethereal sunlight piercing the dense canopy. Color palette: lush forest greens, autumn browns, golden light, earthy tones."
    ),
    "Neutral": (
        "🐺 Нейтральная", "Нейтральная",
        "A highly detailed dark fantasy portrait of a hardened, weary character like a witcher or mercenary, wearing well-worn, dark leather studded armor, a silver medallion, and the hilts of twin swords strapped to the back. Gritty, realistic textures. Warm, flickering firelight illuminating the face. Background: A cozy but dimly lit medieval tavern interior. Heavy wooden oak tables, flickering wax candles on skulls, massive ale barrels, and blurry silhouettes of tavern patrons drinking in the background. Color palette: dark leather browns, silver, muted greys, warm orange firelight."
    ),
}

FRAMES = {"gold": "Золотая", "bronze": "Бронзовая"}

# Ранги Гвинта по числу картищ в колоде.
RANKS = (
    (0, "🪵 Новобранец · ранг 30"),
    (1, "🥉 Ранг 25"),
    (3, "🥉 Ранг 20"),
    (5, "🥈 Ранг 15"),
    (10, "🥈 Ранг 10"),
    (20, "🥇 Ранг 5"),
    (35, "🥇 Ранг 1"),
    (50, "💎 Про-ранг"),
)

LAVK_WAIT_PHRASES = (
    "🎨 <i>Лавк смешивает краски из слёз утопцев... Это займёт около 30-40 секунд!</i>",
    "🎨 <i>Лавк зовёт духов холста, они уже шепчут твоё имя... Подожди 30-40 секунд!</i>",
    "🎨 <i>Лавк точит кисть о камень Каэр Морхена... Ещё 30-40 секунд, человече!</i>",
)

BTN_CREATE = "🃏 Создать карту"
BTN_DECK = "📜 Мои картищи"
BTN_PROFILE = "👤 Мой профиль"
BTN_TOPUP = "💰 Пополнить"
BTN_INVITE = "🔗 Позвать друзей"
BTN_SUPPORT = "🆘 Поддержка"
BTN_ADMIN = "👑 Админка"
BTN_CANCEL = "❌ Отмена"


# ==========================================
# 2. ВСПОМОГАТЕЛЬНОЕ
# ==========================================
def esc(value) -> str:
    return html.escape(str(value))


def safe_call(func, *args, **kwargs):
    """Для некритичных вызовов: удаление/редактирование старых сообщений, ответы на callback, уведомления."""
    try:
        return func(*args, **kwargs)
    except apihelper.ApiTelegramException as e:
        logger.debug("Telegram API call skipped: %s", e)
        return None


@lru_cache(maxsize=1)
def bot_username() -> str:
    return bot.get_me().username


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_free(user_id: int) -> bool:
    user = db.get_user(user_id)
    return is_admin(user_id) or bool(user and user["unlimited"])


def balance_of(user_id: int) -> int:
    user = db.get_user(user_id)
    return user["balance"] if user else 0


def balance_text(user_id: int) -> str:
    return "♾ Безлимит" if is_free(user_id) else f"{balance_of(user_id)}⭐"


def can_afford(user_id: int, price: int) -> bool:
    return is_free(user_id) or balance_of(user_id) >= price


def charge(user_id: int, price: int, kind: str) -> Optional[bool]:
    """None — денег не хватило, True — списано, False — бесплатно (безлимит/админ)."""
    if is_free(user_id):
        return False
    return True if db.spend(user_id, price, kind) else None


def plural_cards(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} картища"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} картищи"
    return f"{n} картищ"


def rank_info(cards: int) -> tuple[str, Optional[int]]:
    current, next_at = RANKS[0][1], None
    for threshold, title in RANKS:
        if cards >= threshold:
            current = title
        else:
            next_at = threshold
            break
    return current, next_at


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
    markup.add(types.InlineKeyboardButton("🔄 Я подписался!", callback_data="check_sub"))
    bot.send_message(
        chat_id,
        "🛑 <b>СТОЯТЬ, ЧЕЛОВЕЧЕ!</b>\nЛавк не пускает чужаков. Сначала зайди в мою пещеру "
        "(подпишись на канал), а потом будем делать картищи!",
        reply_markup=markup,
    )


def get_main_menu(user_id: int):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton(BTN_CREATE))
    markup.add(types.KeyboardButton(BTN_DECK), types.KeyboardButton(BTN_PROFILE))
    markup.add(types.KeyboardButton(BTN_TOPUP), types.KeyboardButton(BTN_INVITE))
    markup.add(types.KeyboardButton(BTN_SUPPORT))
    if is_admin(user_id):
        markup.add(types.KeyboardButton(BTN_ADMIN))
    return markup


def get_cancel_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton(BTN_CANCEL))
    return markup


def topup_button_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💰 Пополнить мешочек", callback_data="menu:topup"))
    return markup


def send_not_enough(chat_id: int, price: int):
    bot.send_message(
        chat_id,
        f"💸 <b>Ууу, мешочек пустоват!</b>\nНужно {price}⭐, а у тебя {balance_of(chat_id)}⭐.\n"
        "Подкинь звёзд — и Лавк сразу возьмётся за кисть!",
        reply_markup=topup_button_markup(),
    )


def reset_user(chat_id: int):
    user_states[chat_id] = STATE_NONE
    user_data.pop(chat_id, None)


def card_summary(data: dict) -> str:
    text = (
        f"🏴 Фракция: {esc(data.get('faction', '—'))}\n"
        f"📜 Имя: <b>{esc(data.get('name', '—'))}</b>\n"
        f"🧬 Категория: {esc(data.get('category', '—'))}\n"
        f"⚔️ Сила: {esc(data.get('power', '—'))} | 🛡 Броня: {esc(data.get('armor', '—'))} | "
        f"🍖 Наём: {esc(data.get('provision', '—'))}\n"
        f"🖼 Редкость: {esc(data.get('frame', '—'))}\n"
        f"✨ Способность: {esc(strip_markup(data.get('ability', '—')))}\n"
        f"💬 Цитата: <i>{esc(data.get('quote', '—'))}</i>"
    )
    if data.get("edit"):
        text += f"\n✏️ Правка: {esc(data['edit'])}"
    return text


def card_markup(card_id: int, with_deck: bool = False):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(f"✏️ Правка — {EDIT_PRICE}⭐", callback_data=f"edit:{card_id}"))
    if with_deck:
        markup.add(types.InlineKeyboardButton("📜 К колоде", callback_data="deck:0"))
    return markup


# ==========================================
# 3. ДОСТУП: только личка, блокировка, учёт пользователей
# ==========================================
class AccessMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()
        self.update_types = ["message", "callback_query"]

    def pre_process(self, update, data):
        user = update.from_user
        chat = update.chat if isinstance(update, types.Message) else getattr(update.message, "chat", None)
        if user is None or chat is None or chat.type != "private":
            return CancelUpdate()
        data["is_new"] = db.upsert_user(user.id, user.username, user.first_name)
        record = db.get_user(user.id)
        if not record["blocked"] or is_admin(user.id):
            return None
        # Оплата уже прошла — звёзды нужно зачислить даже заблокированному.
        if isinstance(update, types.Message) and update.content_type == "successful_payment":
            return None
        if isinstance(update, types.CallbackQuery):
            safe_call(bot.answer_callback_query, update.id, "🚫 Лавк закрыл перед тобой двери пещеры.", show_alert=True)
        else:
            safe_call(bot.send_message, chat.id, f"🚫 Лавк закрыл перед тобой двери пещеры. Вопросы — {SUPPORT_CONTACT}")
        return CancelUpdate()

    def post_process(self, update, data, exception):
        pass


bot.setup_middleware(AccessMiddleware())


# ==========================================
# 4. ГЕНЕРАЦИЯ ПОЛЕЙ (Gemini / случайные числа)
# ==========================================
def ask_gemini(prompt: str, keep_markup: bool = False, temperature: float = 1.2) -> Optional[str]:
    if gemini_client is None:
        return None
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(temperature=temperature),
        )
        text = (response.text or "").strip().strip('"«»')
        if keep_markup:
            text = text.replace("**", "*")
        else:
            text = text.replace("*", "")
        return text or None
    except Exception:
        logger.exception("Ошибка Gemini")
        return None


def text_validator(max_len: int, max_words: Optional[int] = None, min_len: int = 1):
    def validate(value: str) -> Optional[str]:
        value = strip_markup(value)
        if len(value) < min_len:
            return "⚠️ Маловато! Напиши чуть подробнее, человече."
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
    keep_markup: bool = False


def generate_value(step: Step, faction: str) -> str:
    if step.random_range:
        return str(random.randint(*step.random_range))
    value = ask_gemini(step.ai_prompt.format(faction=faction), keep_markup=step.keep_markup)
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
        "ability", "✨ Способность",
        "✨ <b>Что делать умеешь на столе? (Способность)</b>\n\n"
        "<i>Пиши как в Гвинте: «Размещение: нанесите 4 урона». Метка с двоеточием и термины вроде "
        "«яд» или «щит» выделятся сами, а любое слово можно выделить *звёздочками*.</i>",
        text_validator(150), STATE_QUOTE,
        ai_prompt=(
            "Сгенерируй новую способность карты для игры Гвинт (фракция {faction}) в официальном стиле игры. "
            "Начни с ключевого слова и двоеточия, например «Размещение:», «Приказ:», «Выносливость:» или "
            "«Месть:». Названия статусов, категорий и карт выдели звёздочками, например *Яд* или *Ведьма*. "
            "Максимум 25 слов. Без кавычек и пояснений."
        ),
        fallbacks=(
            "Размещение: нанесите 4 единицы урона вражескому отряду.",
            "Приказ: усильте союзный отряд на 3 единицы.",
            "Размещение: наложите *Яд* на вражеский отряд.",
        ),
        keep_markup=True,
    ),
    STATE_QUOTE: Step(
        "quote", "💬 Цитата", "💬 <b>Умная мысль для самого низа (Цитата)</b>",
        text_validator(150), STATE_CONFIRM,
        ai_prompt="Сгенерируй случайную мрачную цитату для карты Гвинт (фракция {faction}). Максимум 20 слов. Без кавычек.",
        fallbacks=("Смерть — лишь начало.",),
    ),
}
STEP_BY_KEY = {step.key: state for state, step in STEPS.items()}
CARD_FLOW = (STATE_NAME, STATE_CATEGORY, STATE_POWER, STATE_ARMOR, STATE_PROVISION, STATE_FRAME, STATE_ABILITY, STATE_QUOTE)


def gen_ai_markup(step_key: str):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🎲 Пусть духи придумают", callback_data=f"ai:{step_key}"))
    markup.add(types.InlineKeyboardButton("🌀 Духи, решите всё за меня", callback_data=f"aiall:{step_key}"))
    return markup


def frame_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("🟡 Золотая", callback_data="frame:gold"),
        types.InlineKeyboardButton("🟤 Бронзовая", callback_data="frame:bronze"),
    )
    markup.add(types.InlineKeyboardButton("🌀 Духи, решите всё за меня", callback_data="aiall:frame"))
    return markup


# ==========================================
# 5. ПЕРЕХОДЫ МЕЖДУ ШАГАМИ СОЗДАНИЯ
# ==========================================
def go_to(chat_id: int, state: int, prefix: str = ""):
    user_states[chat_id] = state
    if state == STATE_FRAME:
        bot.send_message(chat_id, prefix + "🖼 <b>Какой редкости будет твоя карта?</b>", reply_markup=frame_markup())
    elif state == STATE_CONFIRM:
        if prefix:
            bot.send_message(chat_id, prefix.strip())
        ask_for_confirmation(chat_id)
    else:
        step = STEPS[state]
        bot.send_message(chat_id, prefix + step.prompt, reply_markup=gen_ai_markup(step.key))


def price_line(chat_id: int, price: int) -> str:
    if is_free(chat_id):
        return "♾ <b>Для тебя бесплатно</b> — у тебя безлимит."
    return f"💰 Стоимость: <b>{price}⭐</b> (в мешочке: {balance_of(chat_id)}⭐)"


def ask_for_confirmation(chat_id: int):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ ДА, РИСУЙ!", callback_data="confirm:yes"),
        types.InlineKeyboardButton("❌ ОТМЕНА", callback_data="confirm:no"),
    )
    text = (
        "🎉 <b>Лавк собрал твои бумажки! ПРОВЕРЯЙ:</b>\n\n"
        + card_summary(user_data[chat_id])
        + f"\n\n{price_line(chat_id, GENERATION_PRICE)}\n\n<b>Всё верно, человече? Запускаем нейромагию?</b>"
    )
    bot.send_message(chat_id, text, reply_markup=markup)


# ==========================================
# 6. ГЛАВНЫЙ ЭКРАН, ПРОФИЛЬ, ДРУЗЬЯ, ПОПОЛНЕНИЕ
# ==========================================
def show_welcome(chat_id: int):
    reset_user(chat_id)
    text = (
        "👋 <b>Ууу, здорово, человече!</b> Я — Лавк, твой верный проводник в мире легендарного Гвинта.\n\n"
        "Здесь пахнет краской, старым пергаментом и магией! 🎨✨ "
        "Пришли мне своё лицо — и я превращу тебя в настоящую картищу.\n\n"
        f"💰 Мешочек: <b>{balance_text(chat_id)}</b>\n"
        f"🤝 Приведено друзей: <b>{db.count_referrals(chat_id)}</b>\n"
        f"🃏 Картищ в колоде: <b>{db.count_cards(chat_id)}</b>\n\n"
        f"🎨 Картища — {GENERATION_PRICE}⭐ · ✏️ Правка — {EDIT_PRICE}⭐"
    )
    if os.path.exists(WELCOME_IMAGE):
        with open(WELCOME_IMAGE, "rb") as photo:
            bot.send_photo(chat_id, photo, caption=text, reply_markup=get_main_menu(chat_id))
    else:
        bot.send_message(chat_id, text, reply_markup=get_main_menu(chat_id))


def show_profile(chat_id: int):
    cards = db.count_cards(chat_id)
    rank, next_at = rank_info(cards)
    rank_line = f"🏅 Ранг: <b>{rank}</b>"
    if next_at is not None:
        rank_line += f"\n    до следующего — ещё {plural_cards(next_at - cards)}"
    text = (
        "👤 <b>Твой свиток, человече</b>\n\n"
        f"🆔 ID: <code>{chat_id}</code>\n"
        f"{rank_line}\n"
        f"💰 Мешочек: <b>{balance_text(chat_id)}</b>\n"
        f"🃏 Картищ в колоде: <b>{cards}</b>\n"
        f"🤝 Приведено друзей: <b>{db.count_referrals(chat_id)}</b>\n"
        f"🪙 Заработано на друзьях: <b>{db.referral_earnings(chat_id)}⭐</b>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💰 Пополнить", callback_data="menu:topup"),
        types.InlineKeyboardButton("🔗 Позвать друзей", callback_data="menu:invite"),
    )
    bot.send_message(chat_id, text, reply_markup=markup)


def show_invite(chat_id: int):
    link = f"https://t.me/{bot_username()}?start=ref_{chat_id}"
    text = (
        "🔗 <b>Зови друзей в пещеру Лавка!</b>\n\n"
        f"За каждую картищу, которую купит приведённый тобой друг, Лавк насыплет тебе <b>{REFERRAL_BONUS}⭐</b>. "
        "Без ограничений — сколько картищ, столько и звёзд!\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"🤝 Приведено друзей: <b>{db.count_referrals(chat_id)}</b>\n"
        f"🪙 Заработано: <b>{db.referral_earnings(chat_id)}⭐</b>"
    )
    share_text = "Лавк рисует настоящие карты Гвинта с твоим лицом! Заходи 🃏"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={quote(link)}&text={quote(share_text)}"
    ))
    bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)


def show_topup(chat_id: int):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[
        types.InlineKeyboardButton(f"{amount}⭐ · {plural_cards(amount // GENERATION_PRICE)}", callback_data=f"topup:{amount}")
        for amount in TOPUP_PRESETS
    ])
    markup.add(types.InlineKeyboardButton("✍️ Своя сумма", callback_data="topup:custom"))
    bot.send_message(
        chat_id,
        "💰 <b>Пополнить мешочек</b>\n\n"
        f"Сейчас в мешочке: <b>{balance_text(chat_id)}</b>\n"
        f"🎨 Картища — {GENERATION_PRICE}⭐ · ✏️ Правка — {EDIT_PRICE}⭐\n\n"
        "Выбери, сколько звёзд насыпать, или впиши свою сумму.\n"
        "<i>Оплачивая, ты соглашаешься с условиями — /terms</i>",
        reply_markup=markup,
    )


def send_topup_invoice(chat_id: int, amount: int):
    try:
        bot.send_invoice(
            chat_id,
            title=f"Мешочек звёзд: {amount}⭐",
            description=f"{amount}⭐ на баланс Лавка. Хватит на {plural_cards(amount // GENERATION_PRICE)}.",
            invoice_payload=f"topup:{chat_id}:{amount}",
            provider_token="",
            currency="XTR",
            prices=[types.LabeledPrice(label=f"{amount}⭐", amount=amount)],
        )
    except apihelper.ApiTelegramException as e:
        logger.error("Не удалось выставить счёт на %s⭐: %s", amount, e)
        bot.send_message(chat_id, "⚠️ Telegram не принял такой счёт. Попробуй другую сумму.")


def show_support(chat_id: int):
    bot.send_message(
        chat_id,
        "🆘 <b>Поддержка</b>\n\n"
        f"Что-то сломалось, картища вышла кривой или звёзды потерялись? Пиши смотрителю пещеры: {SUPPORT_CONTACT}\n\n"
        "Чтобы помочь быстрее, сразу укажи свой ID: "
        f"<code>{chat_id}</code>",
    )


# ==========================================
# 7. МОИ КАРТИЩИ
# ==========================================
def deck_markup(chat_id: int, page: int):
    total = db.count_cards(chat_id)
    pages = max(1, (total + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE)
    page = min(max(page, 0), pages - 1)
    cards = db.list_cards(chat_id, page * CARDS_PER_PAGE, CARDS_PER_PAGE)
    markup = types.InlineKeyboardMarkup()
    for card in cards:
        data = card["data"]
        mark = "✏️" if card["parent_id"] else "🃏"
        markup.add(types.InlineKeyboardButton(
            f"{mark} {data.get('name', 'Без имени')} · {data.get('faction', '')}", callback_data=f"card:{card['id']}"
        ))
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(types.InlineKeyboardButton("◀️", callback_data=f"deck:{page - 1}"))
        nav.append(types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(types.InlineKeyboardButton("▶️", callback_data=f"deck:{page + 1}"))
        markup.row(*nav)
    return total, markup


def show_deck(chat_id: int, page: int = 0, message_id: Optional[int] = None):
    total, markup = deck_markup(chat_id, page)
    if not total:
        bot.send_message(chat_id, "📜 <b>Твоя колода пуста, человече!</b>\n\n"
                                  f"Жми «{BTN_CREATE}» — и Лавк нарисует первую картищу.")
        return
    text = f"📜 <b>Твоя колода</b> — {plural_cards(total)}\n\nВыбирай картищу, чтобы посмотреть или заказать правку:"
    if message_id:
        if safe_call(bot.edit_message_text, text, chat_id, message_id, reply_markup=markup) is not None:
            return
    bot.send_message(chat_id, text, reply_markup=markup)


# ==========================================
# 8. КОМАНДЫ И КНОПКИ МЕНЮ
# ==========================================
@bot.message_handler(commands=["start"])
def cmd_start(message, data):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)
    if data.get("is_new") and len(args) > 1 and args[1].startswith("ref_") and args[1][4:].isdigit():
        referrer_id = int(args[1][4:])
        if db.set_referrer(chat_id, referrer_id):
            name = esc(message.from_user.first_name or "Безымянный")
            safe_call(bot.send_message, referrer_id,
                      f"🤝 По твоей ссылке в пещеру пришёл новый боец — <b>{name}</b>!\n"
                      f"Как только он купит картищу, Лавк насыплет тебе {REFERRAL_BONUS}⭐.")
    if not check_sub(message.from_user.id):
        send_sub_request(chat_id)
        return
    show_welcome(chat_id)


@bot.message_handler(commands=["menu"])
def cmd_menu(message):
    show_welcome(message.chat.id)


@bot.message_handler(commands=["paysupport"])
def cmd_paysupport(message):
    bot.send_message(
        message.chat.id,
        "💳 <b>Вопросы по оплате</b>\n\n"
        f"Если звёзды списались, а мешочек не пополнился, или нужен возврат — напиши {SUPPORT_CONTACT} "
        f"и укажи свой ID <code>{message.chat.id}</code>. Лавк разберётся!",
    )


@bot.message_handler(commands=["terms"])
def cmd_terms(message):
    bot.send_message(
        message.chat.id,
        "📜 <b>Условия пещеры Лавка</b>\n\n"
        "1. Звёзды, внесённые в мешочек, тратятся на создание картищ "
        f"({GENERATION_PRICE}⭐) и правки ({EDIT_PRICE}⭐).\n"
        "2. Если генерация не удалась по вине бота, звёзды автоматически возвращаются в мешочек.\n"
        "3. Картищи — фан-арт по мотивам GWENT, бот не связан с CD PROJEKT RED.\n"
        "4. Не присылай чужие фото без согласия их владельца.\n"
        f"5. Вопросы по оплате и возвраты — /paysupport или {SUPPORT_CONTACT}.",
    )


@bot.message_handler(func=lambda msg: msg.text == BTN_CANCEL)
def cancel_input(message):
    reset_user(message.chat.id)
    bot.send_message(message.chat.id, "Эх, Лавк убирает холст. Если захочешь новую картищу — жми кнопку!",
                     reply_markup=get_main_menu(message.chat.id))


@bot.message_handler(func=lambda msg: msg.text == BTN_PROFILE)
def menu_profile(message):
    reset_user(message.chat.id)
    show_profile(message.chat.id)


@bot.message_handler(func=lambda msg: msg.text == BTN_INVITE)
def menu_invite(message):
    reset_user(message.chat.id)
    show_invite(message.chat.id)


@bot.message_handler(func=lambda msg: msg.text == BTN_TOPUP)
def menu_topup(message):
    reset_user(message.chat.id)
    show_topup(message.chat.id)


@bot.message_handler(func=lambda msg: msg.text == BTN_SUPPORT)
def menu_support(message):
    show_support(message.chat.id)


@bot.message_handler(func=lambda msg: msg.text == BTN_DECK)
def menu_deck(message):
    reset_user(message.chat.id)
    show_deck(message.chat.id)


@bot.message_handler(func=lambda msg: msg.text == BTN_CREATE)
def start_creation(message):
    chat_id = message.chat.id
    if chat_id in busy_users:
        bot.send_message(chat_id, "⏳ Лавк ещё рисует твою прошлую картищу, подожди!")
        return
    if not check_sub(message.from_user.id):
        send_sub_request(chat_id)
        return
    if not can_afford(chat_id, GENERATION_PRICE):
        send_not_enough(chat_id, GENERATION_PRICE)
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


# ==========================================
# 9. ОБЩИЕ CALLBACK-КНОПКИ
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data == "noop")
def cb_noop(call):
    safe_call(bot.answer_callback_query, call.id)


@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def cb_check_sub(call):
    if not check_sub(call.from_user.id):
        bot.answer_callback_query(call.id, "Ууу, Лавк не видит подписки! Ты обманываешь!", show_alert=True)
        return
    safe_call(bot.answer_callback_query, call.id, "Лавк рад! Заходи!")
    safe_call(bot.delete_message, call.message.chat.id, call.message.message_id)
    show_welcome(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data in ("menu:topup", "menu:invite"))
def cb_menu(call):
    safe_call(bot.answer_callback_query, call.id)
    reset_user(call.message.chat.id)
    if call.data == "menu:topup":
        show_topup(call.message.chat.id)
    else:
        show_invite(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("topup:"))
def cb_topup(call):
    chat_id = call.message.chat.id
    value = call.data.split(":", 1)[1]
    safe_call(bot.answer_callback_query, call.id)
    if value == "custom":
        user_states[chat_id] = STATE_TOPUP_AMOUNT
        bot.send_message(chat_id, f"✍️ Сколько звёзд насыпать? Впиши число от {TOPUP_MIN} до {TOPUP_MAX}.",
                         reply_markup=get_cancel_menu())
        return
    if value.isdigit() and int(value) in TOPUP_PRESETS:
        send_topup_invoice(chat_id, int(value))


# ==========================================
# 10. ОПЛАТА ЗВЁЗДАМИ
# ==========================================
def parse_topup_payload(payload: str) -> Optional[tuple[int, int]]:
    parts = payload.split(":")
    if len(parts) == 3 and parts[0] == "topup" and parts[1].isdigit() and parts[2].isdigit():
        return int(parts[1]), int(parts[2])
    return None


@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout(query):
    parsed = parse_topup_payload(query.invoice_payload)
    user = db.get_user(query.from_user.id)
    if (
        parsed is None or parsed[0] != query.from_user.id or parsed[1] != query.total_amount
        or query.currency != "XTR"
    ):
        bot.answer_pre_checkout_query(query.id, ok=False, error_message="Этот счёт устарел. Создай новый в меню пополнения.")
        return
    if user and user["blocked"]:
        bot.answer_pre_checkout_query(query.id, ok=False, error_message="Доступ к боту ограничен.")
        return
    bot.answer_pre_checkout_query(query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def successful_payment(message):
    payment = message.successful_payment
    chat_id = message.chat.id
    if payment.currency != "XTR":
        logger.error("Неожиданная валюта платежа: %s", payment.currency)
        return
    if db.add_balance(chat_id, payment.total_amount, "topup", payment.telegram_payment_charge_id):
        logger.info("Пополнение %s на %s⭐", chat_id, payment.total_amount)
    bot.send_message(
        chat_id,
        f"💰 <b>Звёзды в мешочке!</b> +{payment.total_amount}⭐\n"
        f"Теперь у тебя: <b>{balance_text(chat_id)}</b>. Лавк уже точит кисть!",
        reply_markup=get_main_menu(chat_id),
    )


# ==========================================
# 11. СОЗДАНИЕ КАРТЫ: callback-и
# ==========================================
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
    markup.add(*[types.InlineKeyboardButton(val[0], callback_data=f"faction:{key}") for key, val in FACTIONS.items()])
    bot.send_message(chat_id, "🏴 <b>За кого воевать будешь?</b>\n\nВыбирай знамя, человече!", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("faction:"))
def cb_faction(call):
    chat_id = call.message.chat.id
    faction_key = call.data.split(":", 1)[1]
    if user_states.get(chat_id) != STATE_FACTION or faction_key not in FACTIONS:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    label, name, prompt = FACTIONS[faction_key]
    user_data[chat_id]["faction"] = name
    user_data[chat_id]["faction_prompt"] = prompt
    safe_call(bot.answer_callback_query, call.id)
    safe_call(bot.edit_message_text, f"✅ Фракция: <b>{esc(label)}</b>", chat_id, call.message.message_id)
    go_to(chat_id, STATE_NAME)


@bot.callback_query_handler(func=lambda call: call.data.startswith("frame:"))
def cb_frame(call):
    chat_id = call.message.chat.id
    frame_key = call.data.split(":", 1)[1]
    if user_states.get(chat_id) != STATE_FRAME or frame_key not in FRAMES:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    user_data[chat_id]["frame"] = FRAMES[frame_key]
    safe_call(bot.answer_callback_query, call.id)
    safe_call(bot.edit_message_text, f"✅ Редкость: <b>{esc(FRAMES[frame_key])}</b>", chat_id, call.message.message_id)
    go_to(chat_id, STATE_ABILITY)


def run_spirits(chat_id: int, start_state: int, fill_all: bool, message_id: int):
    """Заполняет один шаг или все оставшиеся. Пока духи думают, состояние — STATE_SPIRITS."""
    data = user_data.get(chat_id)
    if data is None:
        return
    faction = data.get("faction", "Нейтральная")
    states = CARD_FLOW[CARD_FLOW.index(start_state):] if fill_all else (start_state,)
    generated = {}
    try:
        for state in states:
            if state == STATE_FRAME:
                generated["frame"] = random.choice(list(FRAMES.values()))
            else:
                generated[STEPS[state].key] = generate_value(STEPS[state], faction)
    except Exception:
        logger.exception("Ошибка духов на шаге %s", start_state)
        if user_states.get(chat_id) == STATE_SPIRITS:
            go_to(chat_id, start_state, prefix="⚠️ Духи замолчали. Попробуй ещё раз или напиши сам.\n\n")
        return
    # Пользователь мог отменить создание, пока духи думали.
    if user_states.get(chat_id) != STATE_SPIRITS or user_data.get(chat_id) is not data:
        return
    data.update(generated)
    safe_call(bot.delete_message, chat_id, message_id)
    if fill_all:
        go_to(chat_id, STATE_CONFIRM, prefix="🌀 <b>Духи всё решили за тебя!</b>")
    elif start_state == STATE_FRAME:
        go_to(chat_id, STATE_ABILITY)
    else:
        step = STEPS[start_state]
        value = generated[step.key]
        go_to(chat_id, step.next_state, prefix=f"{step.label}: <b>{esc(value)}</b>\n\n")


@bot.callback_query_handler(func=lambda call: call.data.startswith(("ai:", "aiall:")))
def cb_spirits(call):
    chat_id = call.message.chat.id
    kind, key = call.data.split(":", 1)
    state = STATE_FRAME if key == "frame" else STEP_BY_KEY.get(key)
    if state is None or user_states.get(chat_id) != state:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    user_states[chat_id] = STATE_SPIRITS
    safe_call(bot.answer_callback_query, call.id, "Духи думают...")
    # Убираем кнопки сразу, чтобы повторное нажатие не запустило вторую генерацию.
    safe_call(bot.edit_message_text, "<i>(Духи шепчут...)</i>", chat_id, call.message.message_id)
    threading.Thread(
        target=run_spirits, args=(chat_id, state, kind == "aiall", call.message.message_id), daemon=True
    ).start()


@bot.callback_query_handler(func=lambda call: call.data in ("confirm:yes", "confirm:no"))
def cb_confirm(call):
    chat_id = call.message.chat.id
    if user_states.get(chat_id) != STATE_CONFIRM:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return

    if call.data == "confirm:no":
        safe_call(bot.answer_callback_query, call.id)
        reset_user(chat_id)
        safe_call(bot.edit_message_text, "Эх, порвали холст!", chat_id, call.message.message_id)
        bot.send_message(chat_id, "Начинай заново, когда будешь готов!", reply_markup=get_main_menu(chat_id))
        return

    if chat_id in busy_users:
        bot.answer_callback_query(call.id, "⏳ Лавк ещё рисует прошлую картищу!", show_alert=True)
        return
    paid = charge(chat_id, GENERATION_PRICE, "generation")
    if paid is None:
        safe_call(bot.answer_callback_query, call.id)
        # Состояние не сбрасываем: после пополнения можно снова нажать «ДА, РИСУЙ!».
        send_not_enough(chat_id, GENERATION_PRICE)
        return

    safe_call(bot.answer_callback_query, call.id)
    data = user_data[chat_id]
    reset_user(chat_id)
    busy_users.add(chat_id)
    safe_call(bot.edit_message_text, "✅ Данные подтверждены! Лавк пошел за красками...", chat_id, call.message.message_id)
    threading.Thread(target=generate_card, args=(chat_id, data, paid), daemon=True).start()


# ==========================================
# 12. ПРАВКИ
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data.startswith(("deck:", "card:", "edit:")))
def cb_deck(call):
    chat_id = call.message.chat.id
    action, value = call.data.split(":", 1)
    if not value.isdigit():
        safe_call(bot.answer_callback_query, call.id)
        return
    if action == "deck":
        safe_call(bot.answer_callback_query, call.id)
        # Из карточки с фото текст не отредактировать — тогда show_deck пришлёт новое сообщение.
        # Старые (>48 ч) сообщения приходят как InaccessibleMessage без content_type.
        is_text = getattr(call.message, "content_type", None) == "text"
        show_deck(chat_id, int(value), call.message.message_id if is_text else None)
        return

    card = db.get_card(int(value), chat_id)
    if card is None:
        safe_call(bot.answer_callback_query, call.id, "Такой картищи нет в твоей колоде.")
        return

    if action == "card":
        safe_call(bot.answer_callback_query, call.id)
        caption = (f"🃏 <b>{esc(strip_markup(card['data'].get('name', '')))}</b> · {esc(card['data'].get('faction', ''))}"
                   f"\n📅 {card['created_at'][:10]}")
        if card["data"].get("edit"):
            caption += f"\n✏️ Правка: {esc(card['data']['edit'])}"
        bot.send_photo(chat_id, card["file_id"], caption=caption, reply_markup=card_markup(card["id"], with_deck=True))
        return

    # action == "edit"
    if chat_id in busy_users:
        bot.answer_callback_query(call.id, "⏳ Лавк ещё рисует прошлую картищу!", show_alert=True)
        return
    safe_call(bot.answer_callback_query, call.id)
    if not can_afford(chat_id, EDIT_PRICE):
        send_not_enough(chat_id, EDIT_PRICE)
        return
    user_data[chat_id] = {"edit_card_id": card["id"]}
    user_states[chat_id] = STATE_EDIT_PROMPT
    bot.send_message(
        chat_id,
        f"✏️ <b>Правка картищи «{esc(card['data'].get('name', ''))}»</b>\n\n"
        "Расскажи Лавку, что поправить. Например: <i>«добавь шрам на щеку»</i>, "
        "<i>«сделай фон ночным»</i>, <i>«дай в руки огненный меч»</i>.\n"
        "Лицо и стиль картищи духи сохранят.",
        reply_markup=get_cancel_menu(),
    )


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_EDIT_PROMPT)
def process_edit_prompt(message):
    chat_id = message.chat.id
    text = message.text.strip()
    error = text_validator(300, min_len=3)(text)
    if error:
        bot.send_message(chat_id, error)
        return
    user_data[chat_id]["edit_prompt"] = text
    user_states[chat_id] = STATE_EDIT_CONFIRM
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Правим!", callback_data="editok"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="editno"),
    )
    bot.send_message(
        chat_id,
        f"✏️ <b>Правка:</b> {esc(text)}\n\n{price_line(chat_id, EDIT_PRICE)}\n\nЗапускаем?",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data in ("editok", "editno"))
def cb_edit_confirm(call):
    chat_id = call.message.chat.id
    if user_states.get(chat_id) != STATE_EDIT_CONFIRM:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    if call.data == "editno":
        safe_call(bot.answer_callback_query, call.id)
        reset_user(chat_id)
        safe_call(bot.edit_message_text, "Эх, отложили кисть.", chat_id, call.message.message_id)
        bot.send_message(chat_id, "Правку можно заказать в любой момент из «Моих картищ».",
                         reply_markup=get_main_menu(chat_id))
        return
    if chat_id in busy_users:
        bot.answer_callback_query(call.id, "⏳ Лавк ещё рисует прошлую картищу!", show_alert=True)
        return
    card = db.get_card(user_data[chat_id]["edit_card_id"], chat_id)
    if card is None:
        safe_call(bot.answer_callback_query, call.id, "Картища пропала из колоды.")
        reset_user(chat_id)
        return
    paid = charge(chat_id, EDIT_PRICE, "edit")
    if paid is None:
        safe_call(bot.answer_callback_query, call.id)
        send_not_enough(chat_id, EDIT_PRICE)
        return
    safe_call(bot.answer_callback_query, call.id)
    instruction = user_data[chat_id]["edit_prompt"]
    reset_user(chat_id)
    busy_users.add(chat_id)
    safe_call(bot.edit_message_text, f"✅ Правка принята: {esc(instruction)}", chat_id, call.message.message_id)
    threading.Thread(target=edit_card, args=(chat_id, card, instruction, paid), daemon=True).start()


# ==========================================
# 13. ГЕНЕРАЦИЯ И ПРАВКА АРТА (Fal.ai)
# ==========================================
class FalError(Exception):
    pass


def to_data_uri(image_bytes: bytes, mime: str) -> str:
    # Fal принимает base64 data URI в полях-файлах, отдельная загрузка в storage не нужна.
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def request_fal_image(endpoint: str, payload: dict) -> bytes:
    if not FAL_KEY:
        raise FalError("не задан FAL_API_KEY")
    headers = {"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"}
    response = requests.post(endpoint, json=payload, headers=headers, timeout=180)
    if not response.ok:
        logger.error("Fal.ai %s: %s", response.status_code, response.text[:1000])
        raise FalError(f"Fal.ai вернул код {response.status_code}")
    try:
        image_url = response.json()["images"][0]["url"]
    except (ValueError, KeyError, IndexError, TypeError):
        logger.error("Неожиданный ответ Fal.ai: %s", response.text[:1000])
        raise FalError("Fal.ai вернул ответ без картинки")
    art = requests.get(image_url, timeout=90)
    art.raise_for_status()
    return art.content


def download_telegram_file(file_id: str) -> bytes:
    return bot.download_file(bot.get_file(file_id).file_path)


def refund(chat_id: int, price: int, paid: bool) -> str:
    if not paid:
        return ""
    db.add_balance(chat_id, price, "refund")
    return f"\n💰 {price}⭐ вернулись в мешочек."


def award_referrer(user_id: int):
    user = db.get_user(user_id)
    referrer_id = user["referrer_id"] if user else None
    if not referrer_id:
        return
    db.add_balance(referrer_id, REFERRAL_BONUS, "referral")
    safe_call(bot.send_message, referrer_id,
              f"🪙 Твой друг купил картищу — Лавк насыпал тебе <b>{REFERRAL_BONUS}⭐</b>!\n"
              f"В мешочке теперь: <b>{balance_text(referrer_id)}</b>")


ART_STYLE = (
    "Official GWENT: The Witcher Card Game card art. Hand-painted digital illustration in the style of "
    "CD Projekt Red Gwent artists, painterly brushwork with visible brush strokes, textured oil-paint look, "
    "slightly stylized grim dark fantasy, dramatic chiaroscuro lighting, muted earthy color grading. "
    "Waist-up portrait of the character, centered, facing the viewer, vertical composition with headroom. "
)
ART_NEGATIVE = (
    "photorealistic, photograph, photo, hyperrealistic, 3d render, cgi, smooth plastic skin, "
    "text, letters, watermark, card frame, border, ui"
)


def build_art_prompt(data: dict) -> str:
    role = ask_gemini(
        "Translate this fantasy character class into English, 1-4 words, reply with the translation only: "
        + data.get("category", ""),
        temperature=0.2,
    )
    who = f"The character is a {role}. " if role and len(role) <= 60 else ""
    return ART_STYLE + who + data["faction_prompt"]


def run_art_job(chat_id: int, price: int, paid: bool, job: Callable[[int], None]):
    """Общая обвязка: статус, возврат звёзд при ошибке, снятие флага занятости."""
    status_msg = bot.send_message(chat_id, random.choice(LAVK_WAIT_PHRASES), reply_markup=get_main_menu(chat_id))
    try:
        job(status_msg.message_id)
    except FalError as e:
        note = refund(chat_id, price, paid)
        safe_call(bot.edit_message_text, f"⚠️ Духи не смогли нарисовать: {esc(e)}{note}", chat_id, status_msg.message_id)
        bot.send_message(chat_id, "Попробуй ещё раз чуть позже.", reply_markup=get_main_menu(chat_id))
    except Exception:
        # Текст исключения пользователю не показываем: в URL Telegram-файлов содержится токен бота.
        logger.exception("Ошибка генерации для %s", chat_id)
        note = refund(chat_id, price, paid)
        safe_call(bot.edit_message_text, f"⚠️ Системная ошибка связи. Попробуй ещё раз.{note}", chat_id, status_msg.message_id)
        bot.send_message(chat_id, "Лавк ждёт тебя в меню.", reply_markup=get_main_menu(chat_id))
    finally:
        busy_users.discard(chat_id)


def deliver_card(chat_id: int, art: bytes, card_data: dict, title: str, parent_id: Optional[int] = None) -> int:
    """Собирает карту поверх арта, отправляет карту + чистый арт и сохраняет в колоду."""
    card_image = render_card(art, card_data)
    caption = f"{title}\n«{esc(strip_markup(card_data.get('name', '')))}» · {esc(card_data.get('faction', ''))}"
    sent = bot.send_media_group(chat_id, [
        types.InputMediaPhoto(card_image, caption=caption, parse_mode="HTML"),
        types.InputMediaPhoto(art),
    ])
    card_id = db.add_card(chat_id, card_data, sent[0].photo[-1].file_id,
                          art_file_id=sent[1].photo[-1].file_id, parent_id=parent_id)
    bot.send_message(
        chat_id,
        "🃏 Картища легла в твою колоду — ищи её в «Моих картищах».\n"
        "<i>Второй картинкой — чистый арт без рамки, пригодится для аватарки.</i>",
        reply_markup=card_markup(card_id),
    )
    return card_id


def generate_card(chat_id: int, data: dict, paid: bool):
    def job(status_id: int):
        image = download_telegram_file(data["photo_id"])
        art = request_fal_image(FAL_GENERATE_ENDPOINT, {
            "prompt": build_art_prompt(data),
            "negative_prompt": ART_NEGATIVE,
            "reference_image_url": to_data_uri(image, data["photo_mime"]),
            "image_size": "portrait_4_3",
            "num_inference_steps": 28,
            "guidance_scale": 4.0,
            "true_cfg": 1.0,
            "id_weight": 0.8,
        })
        card_data = {k: v for k, v in data.items() if k not in ("photo_id", "photo_mime", "faction_prompt")}
        safe_call(bot.delete_message, chat_id, status_id)
        deliver_card(chat_id, art, card_data, "🔥 <b>ТВОЯ ИСТИННАЯ КАРТИЩА ГОТОВА!</b>")
        if paid:
            award_referrer(chat_id)

    run_art_job(chat_id, GENERATION_PRICE, paid, job)


def edit_card(chat_id: int, card: dict, instruction: str, paid: bool):
    def job(status_id: int):
        # У карт, созданных до появления рамки, file_id — это сам арт.
        image = download_telegram_file(card.get("art_file_id") or card["file_id"])
        english = ask_gemini(
            "Translate this image edit request into one short English instruction for an image editing model. "
            "Reply with the instruction only.\n\n" + instruction,
            temperature=0.2,
        ) or instruction
        art = request_fal_image(FAL_EDIT_ENDPOINT, {
            "prompt": f"{english}. Keep the person's face, identity and the hand-painted Gwent card art style unchanged.",
            "image_url": to_data_uri(image, "image/jpeg"),
            "aspect_ratio": "3:4",
            "guidance_scale": 3.5,
            "output_format": "jpeg",
        })
        card_data = dict(card["data"], edit=instruction)
        safe_call(bot.delete_message, chat_id, status_id)
        deliver_card(chat_id, art, card_data, "✏️ <b>ПРАВКА ГОТОВА!</b>", parent_id=card["id"])

    run_art_job(chat_id, EDIT_PRICE, paid, job)


# ==========================================
# 14. АДМИНКА
# ==========================================
def admin_panel_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm:stats"),
        types.InlineKeyboardButton("📢 Рассылка", callback_data="adm:bc"),
    )
    markup.add(types.InlineKeyboardButton("🔎 Найти бойца", callback_data="adm:find"))
    return markup


def show_admin_panel(chat_id: int):
    bot.send_message(chat_id, "👑 <b>Трон смотрителя пещеры</b>\n\nЧто делаем, господин?", reply_markup=admin_panel_markup())


def admin_user_view(user_id: int):
    user = db.get_user(user_id)
    if user is None:
        return None, None
    username = f"@{esc(user['username'])}" if user["username"] else "—"
    text = (
        f"👤 <b>{esc(user['first_name'] or 'Без имени')}</b> {username}\n"
        f"🆔 <code>{user['user_id']}</code>\n"
        f"💰 Баланс: <b>{user['balance']}⭐</b>\n"
        f"♾ Безлимит: <b>{'да' if user['unlimited'] else 'нет'}</b>\n"
        f"🚫 Заблокирован: <b>{'да' if user['blocked'] else 'нет'}</b>\n"
        f"🃏 Картищ: <b>{db.count_cards(user_id)}</b>\n"
        f"🤝 Рефералов: <b>{db.count_referrals(user_id)}</b> (заработал {db.referral_earnings(user_id)}⭐)\n"
        f"🔗 Пригласил: <code>{user['referrer_id'] or '—'}</code>\n"
        f"📅 С нами с: {user['created_at'][:10]}"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Разблокировать" if user["blocked"] else "🚫 Заблокировать",
                                   callback_data=f"adm:block:{user_id}"),
        types.InlineKeyboardButton("♾ Забрать безлимит" if user["unlimited"] else "♾ Дать безлимит",
                                   callback_data=f"adm:unl:{user_id}"),
    )
    markup.add(types.InlineKeyboardButton("💰 Начислить / списать", callback_data=f"adm:bal:{user_id}"))
    return text, markup


def send_admin_user(chat_id: int, user_id: int, message_id: Optional[int] = None):
    text, markup = admin_user_view(user_id)
    if text is None:
        bot.send_message(chat_id, "🤷 Такого бойца Лавк не знает. Он должен хотя бы раз написать боту.")
        return
    if message_id and safe_call(bot.edit_message_text, text, chat_id, message_id, reply_markup=markup) is not None:
        return
    bot.send_message(chat_id, text, reply_markup=markup)


@bot.message_handler(commands=["admin"])
@bot.message_handler(func=lambda msg: msg.text == BTN_ADMIN)
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        return
    reset_user(message.chat.id)
    show_admin_panel(message.chat.id)


def run_broadcast(admin_chat_id: int, from_chat_id: int, message_id: int):
    delivered = failed = 0
    for user_id in db.active_user_ids():
        while True:
            try:
                bot.copy_message(user_id, from_chat_id, message_id)
                delivered += 1
                break
            except apihelper.ApiTelegramException as e:
                if e.error_code == 429:
                    time.sleep(int(e.result_json.get("parameters", {}).get("retry_after", 5)))
                    continue
                failed += 1
                break
            except Exception:
                logger.exception("Ошибка рассылки для %s", user_id)
                failed += 1
                break
        time.sleep(0.05)  # ~20 сообщений/с — ниже лимита Telegram
    bot.send_message(admin_chat_id, f"📢 <b>Рассылка завершена</b>\n✅ Доставлено: {delivered}\n❌ Не доставлено: {failed}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm:"))
def cb_admin(call):
    chat_id = call.message.chat.id
    if not is_admin(call.from_user.id):
        safe_call(bot.answer_callback_query, call.id)
        return
    parts = call.data.split(":")
    action = parts[1]
    safe_call(bot.answer_callback_query, call.id)

    if action == "stats":
        s = db.stats()
        bot.send_message(
            chat_id,
            "📊 <b>Статистика пещеры</b>\n\n"
            f"👥 Бойцов: <b>{s['users']}</b> (сегодня +{s['users_today']})\n"
            f"🚫 Заблокировано: {s['blocked']} · ♾ Безлимит: {s['unlimited']}\n"
            f"🃏 Картищ: <b>{s['cards']}</b> (сегодня +{s['cards_today']})\n"
            f"⭐ Пополнено звёзд: <b>{s['stars']}</b> (сегодня +{s['stars_today']})\n"
            f"⏳ Рисуется сейчас: {len(busy_users)}",
        )
    elif action == "find":
        user_states[chat_id] = STATE_ADMIN_FIND
        bot.send_message(chat_id, "🔎 Пришли ID или @username бойца.", reply_markup=get_cancel_menu())
    elif action == "bc":
        user_states[chat_id] = STATE_ADMIN_BROADCAST
        bot.send_message(chat_id, "📢 Пришли сообщение для рассылки — текст, фото, видео, что угодно. "
                                  "Лавк покажет превью перед отправкой.", reply_markup=get_cancel_menu())
    elif action in ("bcgo", "bcno"):
        if user_states.get(chat_id) != STATE_ADMIN_BROADCAST_CONFIRM:
            return
        source_id = user_data[chat_id]["broadcast_message_id"]
        reset_user(chat_id)
        safe_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=None)
        if action == "bcno":
            bot.send_message(chat_id, "Рассылка отменена.", reply_markup=get_main_menu(chat_id))
            return
        bot.send_message(chat_id, "📢 Рассылка пошла! Лавк сообщит, когда закончит.", reply_markup=get_main_menu(chat_id))
        threading.Thread(target=run_broadcast, args=(chat_id, chat_id, source_id), daemon=True).start()
    elif action in ("block", "unl", "bal") and len(parts) == 3 and parts[2].lstrip("-").isdigit():
        target_id = int(parts[2])
        user = db.get_user(target_id)
        if user is None:
            return
        if action == "block":
            if is_admin(target_id):
                bot.send_message(chat_id, "Админа заблокировать нельзя.")
                return
            blocked = not user["blocked"]
            db.set_flag(target_id, "blocked", blocked)
            safe_call(bot.send_message, target_id,
                      "🚫 Лавк закрыл перед тобой двери пещеры." if blocked else "✅ Лавк снова пускает тебя в пещеру!")
            send_admin_user(chat_id, target_id, call.message.message_id)
        elif action == "unl":
            unlimited = not user["unlimited"]
            db.set_flag(target_id, "unlimited", unlimited)
            safe_call(bot.send_message, target_id,
                      "♾ <b>Лавк даровал тебе безлимит!</b> Рисуй сколько душе угодно." if unlimited
                      else "♾ Безлимит закончился. Картищи снова за звёзды.")
            send_admin_user(chat_id, target_id, call.message.message_id)
        else:
            user_states[chat_id] = STATE_ADMIN_BALANCE
            user_data[chat_id] = {"target_id": target_id}
            bot.send_message(chat_id, f"💰 Сколько звёзд начислить бойцу <code>{target_id}</code>?\n"
                                      "Отрицательное число — списать.", reply_markup=get_cancel_menu())


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_ADMIN_FIND and is_admin(msg.from_user.id))
def admin_find(message):
    user = db.find_user(message.text)
    if user is None:
        bot.send_message(message.chat.id, "🤷 Не нашёл. Пришли другой ID или @username, или жми «Отмена».")
        return
    reset_user(message.chat.id)
    bot.send_message(message.chat.id, "Нашёл!", reply_markup=get_main_menu(message.chat.id))
    send_admin_user(message.chat.id, user["user_id"])


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_ADMIN_BALANCE and is_admin(msg.from_user.id))
def admin_balance(message):
    chat_id = message.chat.id
    text = message.text.strip()
    if not text.lstrip("-").isdigit() or int(text) == 0 or abs(int(text)) > 1_000_000:
        bot.send_message(chat_id, "⚠️ Нужно целое число, не ноль. Например 120 или -60.")
        return
    amount = int(text)
    target_id = user_data[chat_id]["target_id"]
    reset_user(chat_id)
    db.add_balance(target_id, amount, "admin")
    if amount > 0:
        safe_call(bot.send_message, target_id,
                  f"🎁 <b>Лавк насыпал тебе {amount}⭐!</b>\nВ мешочке теперь: <b>{balance_text(target_id)}</b>")
    bot.send_message(chat_id, f"✅ Готово: {amount:+}⭐", reply_markup=get_main_menu(chat_id))
    send_admin_user(chat_id, target_id)


@bot.message_handler(
    content_types=["text", "photo", "video", "animation", "document", "audio", "voice", "sticker", "video_note"],
    func=lambda msg: user_states.get(msg.chat.id) == STATE_ADMIN_BROADCAST and is_admin(msg.from_user.id),
)
def admin_broadcast_message(message):
    chat_id = message.chat.id
    user_data[chat_id] = {"broadcast_message_id": message.message_id}
    user_states[chat_id] = STATE_ADMIN_BROADCAST_CONFIRM
    bot.send_message(chat_id, "👇 Так увидят сообщение бойцы:")
    bot.copy_message(chat_id, chat_id, message.message_id)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Разослать всем", callback_data="adm:bcgo"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="adm:bcno"),
    )
    bot.send_message(chat_id, f"Отправить {len(db.active_user_ids())} бойцам?", reply_markup=markup)


# ==========================================
# 15. ТЕКСТОВЫЙ ВВОД ПО СОСТОЯНИЯМ И ЗАПАСНОЙ ОБРАБОТЧИК
# ==========================================
@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_TOPUP_AMOUNT)
def process_topup_amount(message):
    chat_id = message.chat.id
    text = message.text.strip()
    if not text.isdigit() or not TOPUP_MIN <= int(text) <= TOPUP_MAX:
        bot.send_message(chat_id, f"⚠️ Впиши число от {TOPUP_MIN} до {TOPUP_MAX}.")
        return
    reset_user(chat_id)
    bot.send_message(chat_id, "🧾 Лавк выписал счёт:", reply_markup=get_main_menu(chat_id))
    send_topup_invoice(chat_id, int(text))


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) in STEPS)
def process_step_text(message):
    chat_id = message.chat.id
    step = STEPS[user_states[chat_id]]
    value = message.text.strip()
    error = step.validate(value)
    if error:
        bot.send_message(chat_id, error)
        return
    user_data[chat_id][step.key] = value
    go_to(chat_id, step.next_state)


@bot.message_handler(content_types=["text", "photo", "document", "sticker", "voice", "video", "animation"])
def fallback_handler(message):
    chat_id = message.chat.id
    state = user_states.get(chat_id, STATE_NONE)
    if state == STATE_SPIRITS:
        bot.send_message(chat_id, "🌀 Духи ещё шепчут, подожди чуть-чуть!")
    elif state in (STATE_FACTION, STATE_FRAME, STATE_CONFIRM, STATE_EDIT_CONFIRM, STATE_ADMIN_BROADCAST_CONFIRM):
        bot.send_message(chat_id, "👆 Выбери вариант кнопкой выше, человече.")
    elif state == STATE_PHOTO:
        bot.send_message(chat_id, "⚠️ Лавк просил картинку! Пришли фото, человече.")
    elif state in STEPS or state in (STATE_EDIT_PROMPT, STATE_TOPUP_AMOUNT):
        bot.send_message(chat_id, "⚠️ Лавк ждёт текст!")
    elif chat_id in busy_users:
        bot.send_message(chat_id, "⏳ Лавк рисует, подожди чуть-чуть!")
    else:
        bot.send_message(chat_id, "Ууу, Лавк не понял. Жми кнопки в меню, человече!",
                         reply_markup=get_main_menu(chat_id))


if __name__ == "__main__":
    logger.info("Lavk is listening as @%s...", bot_username())
    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=30,
        skip_pending=True,
        allowed_updates=["message", "callback_query", "pre_checkout_query"],
    )
