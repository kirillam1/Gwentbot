import base64
import html
import logging
import os
import random
import re
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

from card_render import LABEL_RE, render_card, strip_markup
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
# На Amvera постоянное хранилище — /data: база там переживает пересборки.
DB_PATH = os.getenv("DB_PATH") or os.path.join("/data" if os.path.isdir("/data") else BASE_DIR, "lavk.db")

CHANNEL_ID = "@Master_Lavk"
SUPPORT_CONTACT = "@S159YKRF4"
WELCOME_IMAGE = os.path.join(BASE_DIR, "1794190872103.jpg")

FAL_GENERATE_ENDPOINT = "https://queue.fal.run/fal-ai/flux-pulid"
FAL_EDIT_ENDPOINT = "https://queue.fal.run/fal-ai/flux-pro/kontext"
FAL_DEADLINE = 240        # секунд на всю генерацию, потом отмена и возврат звёзд
GEMINI_TIMEOUT_MS = 30_000

GENERATION_PRICE = 100
EDIT_PRICE = 50
REFERRAL_BONUS = 60
# (сколько платит, сколько звёзд падает в мешочек) — чем больше набор, тем выгоднее
TOPUP_PACKS = ((100, 100), (190, 200), (270, 300), (700, 1000))
TOPUP_MIN, TOPUP_MAX = 1, 10000
CARDS_PER_PAGE = 6
ART_DIR = os.path.join(os.path.dirname(DB_PATH) or BASE_DIR, "arts")   # чистые арты — исходники для правок

if not BOT_TOKEN:
    raise SystemExit("Не задана переменная окружения BOT_TOKEN")
if not ADMIN_IDS:
    logger.warning("ADMIN_ID не задан — админка недоступна")

apihelper.READ_TIMEOUT = 90
apihelper.CONNECT_TIMEOUT = 90

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", use_class_middlewares=True)
# Без таймаута запрос к Gemini может висеть бесконечно и подвесить генерацию карты.
gemini_client = (genai.Client(api_key=GEMINI_API_KEY, http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
                 if GEMINI_API_KEY else None)
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
    STATE_PROMO, STATE_ADMIN_PROMO,
) = range(22)

@dataclass(frozen=True)
class Faction:
    button: str
    name: str
    about: str               # описание в меню выбора
    attire: str              # внешний вид персонажа для арта
    backgrounds: tuple       # фоны в духе фракции, выбираются случайно
    palette: str
    mechanics: str           # ключевые слова способностей этой фракции (для Gemini)
    name_hint: str
    fallbacks: tuple         # запасные способности, если Gemini не ответил


FACTIONS = {
    "Nilfgaard": Faction(
        "🛡 Нильфгаард", "Нильфгаард",
        "Империя Белого Пламени: железная дисциплина, шпионы и придворные интриги. Чёрное с золотом.",
        "wearing ornate black enamelled Nilfgaardian plate armor with gold trim and a golden sun emblem on the chest, "
        "a black cloak, stern disciplined aristocratic look",
        (
            "a vast Nilfgaardian war camp at dusk, black and gold pavilions, campfires, rows of pikemen and imperial sun banners",
            "the marble throne hall of the imperial palace in the City of Golden Towers, golden sun mosaics, tall columns and braziers",
            "a burning Northern village behind advancing Nilfgaardian ranks, black banners with a golden sun, smoke and embers in the sky",
        ),
        "Color palette: black, gold, dark steel, crimson accents.",
        "Размещение, Приказ, Шпионаж, Блокировка, Раскрытие, Ассимиляция, Захват, Порядок, Уязвимость, Яд",
        "имперское имя на нильфгаардский лад (например «Кагыр Маур Дыффин», «Мэнно Коэгоорн», «Риенс ван Эмрейс»)",
        (
            "Размещение: наложите *Блокировку* на вражеский отряд и нанесите ему 3 ед. урона.",
            "Приказ: раскройте случайную карту в руке противника.",
            "Размещение: *шпионаж*. Возьмите карту из вашей колоды.",
        ),
    ),
    "Monsters": Faction(
        "👾 Чудовища", "Чудовища",
        "Твари из болот, пещер и туманов: голод, когти, древние проклятия и Дикая Охота.",
        "a terrifying monstrous being with pale veined skin, feral eyes and bestial features, wearing grim armor of bone, "
        "chitin and rotten leather adorned with skulls and claws",
        (
            "a foggy swamp at midnight, twisted dead trees like claws, glowing eyes hidden in green mist, bubbling black water",
            "a dark cave lair littered with bones and gnawed armor, dripping stalactites, a faint sickly green glow",
            "a frozen battlefield under a blood-red moon, spectral riders of the Wild Hunt in the storm clouds, crows over the dead",
        ),
        "Color palette: sickly greens, bone white, blood red, abyssal black.",
        "Размещение, Приказ, Завещание, Поглощение, Рост, Доминирование, Кровотечение, Яд, Уязвимость",
        "жуткое имя чудовища или прозвище (например «Кельтуллис», «Старая Сводница», «Имлерих»)",
        (
            "Завещание: создайте копию этого отряда в случайном ряду.",
            "Размещение: *поглотите* союзный отряд и усильтесь на его силу.",
            "Рост: в конце хода усильтесь на 1 ед., если в ряду есть отряд сильнее.",
        ),
    ),
    "Skellige": Faction(
        "⚓ Скеллиге", "Скеллиге",
        "Островные кланы: берсерки, драккары, штормы и честь, добытая в бою.",
        "a rugged Skellige island warrior with braided hair and war paint, wearing bear-fur mantles, chainmail, "
        "leather and clan armor carved with Celtic knots and runes, an axe or a round shield",
        (
            "a stormy northern coast, huge waves crashing against black cliffs, a dragon-headed longship in the surf, heavy rain",
            "a snowy mountain village of carved wooden longhouses, a roaring bonfire, clan banners and smoke from the chimneys",
            "the deck of a longship in a raging sea storm, torn sail, lightning splitting the dark sky",
        ),
        "Color palette: icy blues, earthy browns, steel grey, sea-foam white.",
        "Размещение, Приказ, Берсерк, Адреналин, Жажда крови, Ветеран, Сброс, Кровотечение, Броня",
        "суровое островное имя с прозвищем (например «Хьялмар ан Крайт», «Свеннбьорн Секира», «Бирна Бран»)",
        (
            "Берсерк: когда сила этого отряда падает до 4 ед. или ниже, превратите его в Медведя.",
            "Размещение: сбросьте карту из руки и возьмите новую.",
            "Жажда крови 2: нанесите 3 ед. урона вражескому отряду.",
        ),
    ),
    "Northern": Faction(
        "⚜️ Королевства Севера", "Королевства Севера",
        "Короли и рыцари Темерии, Редании и Каэдвена: сталь, знамёна и осадные машины.",
        "a noble Northern knight in polished steel plate armor with a blue and silver heraldic tabard, "
        "a long sword and a kite shield with a royal coat of arms",
        (
            "the war-torn courtyard of a besieged stone castle, trebuchets, arrows in the sky, blue and silver royal banners",
            "a royal throne hall lit by candles and stained-glass windows, heraldic tapestries and banners of the Northern kingdoms",
            "a misty battlefield at dawn, ranks of knights and crossbowmen, siege towers and fluttering lion banners",
        ),
        "Color palette: royal blue, bright steel, red accents, warm golden sunlight.",
        "Размещение, Приказ, Заряд, Экипаж, Воодушевление, Щит, Броня, Усиление, Ходов до восстановления",
        "благородное имя рыцаря или короля (например «Король Демавенд», «Вернон Роше», «Сэр Ян Натали»)",
        (
            "Приказ: усильте союзный отряд на 2 ед. Заряд: 3.",
            "Размещение: наложите *Щит* на союзный отряд.",
            "Воодушевление: в начале хода добавьте себе 2 ед. брони.",
        ),
    ),
    "Syndicate": Faction(
        "💰 Синдикат", "Синдикат",
        "Преступный мир Новиграда: монеты, долги, наёмные ножи и награды за головы.",
        "a cunning Novigrad criminal in a lavish but frayed velvet coat, gold rings and chains, a hidden dagger, "
        "a shady conspiratorial smirk",
        (
            "a narrow Novigrad alley at night, wet cobblestones reflecting orange lanterns, rain, shadowy figures in the doorways",
            "a smoky gambling den lit by candles, tables piled with gold coins, dice and cards, thugs watching from the shadows",
            "the Novigrad harbour docks at sunset, merchant ships, crates of contraband and the city walls behind",
        ),
        "Color palette: rich purple, gold, murky greens, deep shadows.",
        "Размещение, Приказ, Выгода, Подношение, Награда за голову, Накопление, Безумие, монеты",
        "имя или кличка бандита (например «Франческо Красавчик», «Сиги Ройвен», «Карась Беззубый»)",
        (
            "Размещение: получите 4 монеты.",
            "Приказ: потратьте 3 монеты и нанесите 4 ед. урона вражескому отряду.",
            "Подношение 2: назначьте *награду за голову* вражеского отряда.",
        ),
    ),
    "Scoiatael": Faction(
        "🐿 Скоя'таэли", "Скоя'таэли",
        "Эльфы, краснолюды и дриады-партизаны: лесные засады, луки и древняя магия.",
        "an elven guerilla fighter with sharp features and pointed ears, wearing forest camouflage, green and brown "
        "leather armor decorated with leaves and feathers, a longbow or twin blades",
        (
            "an ancient magical forest, colossal mossy oaks, crumbling white marble elven ruins, shafts of golden light",
            "a forest road in autumn, an ambush among red and orange trees, arrows flying, falling leaves",
            "moonlit elven ruins with waterfalls and glowing flowers in the valley of Dol Blathanna",
        ),
        "Color palette: forest greens, autumn browns, golden light, earthy tones.",
        "Размещение, Приказ, Гармония, Засада, Симбиоз, Перемещение, Усиление, Яд",
        "эльфийское имя (например «Иорвет», «Тораль Иаллисен», «Эльдаин аэп Каэлл»)",
        (
            "Гармония: усильте этот отряд на 1 ед.",
            "Засада: когда противник сыграет отряд, переверните эту карту и нанесите ему 4 ед. урона.",
            "Размещение: переместите вражеский отряд в другой ряд и нанесите ему 2 ед. урона.",
        ),
    ),
    "Neutral": Faction(
        "🐺 Нейтральная", "Нейтральная",
        "Ведьмаки, чародеи, наёмники и бродяги — без знамени и без хозяина.",
        "a hardened witcher-like wanderer with scars, wearing worn dark leather armor with steel studs, a silver medallion "
        "and the hilts of two swords over the shoulder",
        (
            "a dim medieval tavern, heavy oak tables, candles, ale barrels and blurry patrons in the background",
            "a misty crossroads at dusk with an old signpost, a gallows and a dark forest behind",
            "the snowy ruins of an ancient witcher keep high in the mountains, broken walls and a cold wind",
        ),
        "Color palette: dark leather browns, silver, muted greys, warm orange firelight.",
        "Размещение, Приказ, Обречённость, Рвение, Кровотечение, Щит, Усиление",
        "имя странника с прозвищем (например «Геральт из Ривии», «Ламберт», «Золтан Хивай»)",
        (
            "Размещение: нанесите 4 ед. урона вражескому отряду.",
            "Приказ: усильте союзный отряд на 3 ед.",
            "Размещение: наложите *Кровотечение* на 3 хода на вражеский отряд.",
        ),
    ),
}
FACTION_BY_NAME = {f.name: f for f in FACTIONS.values()}

ART_POSES = (
    "standing in a three-quarter view, one hand resting on the pommel of a sheathed sword",
    "raising a weapon in a battle-ready stance",
    "arms crossed over the chest, looking down at the viewer with confidence",
    "glancing over the shoulder with a wary look",
    "holding a flickering torch that lights the face from below",
    "gripping a weapon with both hands, leaning slightly forward",
    "with a cloak billowing in the wind, looking into the distance",
    "pointing forward as if commanding troops into battle",
)

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
    "🎨 <i>Лавк смешивает краски из слёз утопцев...</i>",
    "🖌 <i>Лавк точит кисть о камень Каэр Морхена...</i>",
    "🕯 <i>Духи холста шепчут твоё имя, Лавк записывает...</i>",
    "🪶 <i>Лавк набрасывает углём твой суровый профиль...</i>",
    "🔥 <i>Лавк греет краски над костром, чтобы ложились ровнее...</i>",
    "🌫 <i>Лавк выписывает туман за твоей спиной...</i>",
    "⚔️ <i>Лавк рисует блики на клинке — это самое сложное!</i>",
    "🧪 <i>Лавк добавил в краску щепотку ведьмачьего эликсира...</i>",
    "🍺 <i>Лавк отхлебнул махакамского спирта для вдохновения...</i>",
    "🐦 <i>Ворон за окном смотрит, как Лавк кладёт тени...</i>",
    "🌕 <i>Лавк ждёт, пока луна осветит холст как надо...</i>",
    "🖼 <i>Ещё пара мазков — и Лавк возьмётся за рамку...</i>",
    "📜 <i>Лавк сверяется со старыми гравюрами, чтобы всё было по канону...</i>",
    "🗡 <i>Лавк спорит с духами, какой меч тебе пойдёт больше...</i>",
)
WAIT_INTRO = "\n\n<b>Обычно это 1–2 минуты</b> — Лавк напишет, как будет готово."
WAIT_TICK = (10, 15)   # раз в сколько секунд менять фразу

SPIRIT_PHRASES = (
    "🔮 <i>Духи склонились над свитком...</i>",
    "🕯 <i>Свечи мигнули — духи шепчут...</i>",
    "🌫 <i>Из тумана доносится шёпот духов...</i>",
    "🃏 <i>Духи тасуют колоду судьбы...</i>",
    "🦉 <i>Сова ухнула — духи совещаются...</i>",
    "📜 <i>Духи листают древние хроники...</i>",
    "🌙 <i>Луна скрылась за тучей — духи думают...</i>",
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


def has_trial(user_id: int) -> bool:
    user = db.get_user(user_id)
    return bool(user and not user["free_used"])


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


def card_markup(card: dict, with_deck: bool = False):
    markup = types.InlineKeyboardMarkup()
    if card["data"].get("trial"):
        # Пробную картищу не правим — иначе правка за полцены снимала бы водяные знаки.
        markup.add(types.InlineKeyboardButton(f"🎨 Чистая картища — {GENERATION_PRICE}⭐", callback_data="menu:topup"))
    else:
        markup.add(types.InlineKeyboardButton(f"✏️ Правка — {EDIT_PRICE}⭐", callback_data=f"edit:{card['id']}"))
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


def ability_labels(text: str) -> list[str]:
    """Метки способности («Размещение», «Жажда крови 2» → «жажда крови») в нижнем регистре."""
    labels = []
    for match in LABEL_RE.finditer(strip_markup(text)):
        label = re.sub(r"\(.*?\)|\d+", "", match.group(2)).strip().lower()
        if label:
            labels.append(label.replace("ё", "е"))
    return labels


def mechanics_of(faction: Faction) -> set[str]:
    return {m.strip().lower().replace("ё", "е") for m in faction.mechanics.split(",")}


# Метки, которые есть только у какой-то одной фракции: чужим их генерировать нельзя.
_ALL_MECHANICS = [mechanics_of(f) for f in FACTIONS.values()]
COMMON_LABELS = set.intersection(*_ALL_MECHANICS)
FACTION_ONLY_LABELS = set.union(*_ALL_MECHANICS) - COMMON_LABELS


def ability_fits(text: str, faction: Faction) -> bool:
    own = mechanics_of(faction)
    labels = ability_labels(text)
    return bool(labels) and all(label in own or label not in FACTION_ONLY_LABELS for label in labels)


def generate_value(step: Step, faction_name: str) -> str:
    if step.random_range:
        return str(random.randint(*step.random_range))
    faction = FACTION_BY_NAME.get(faction_name, FACTIONS["Neutral"])
    prompt = step.ai_prompt.format(faction=faction.name, mechanics=faction.mechanics, name_hint=faction.name_hint)
    for _ in range(2):
        value = ask_gemini(prompt, keep_markup=step.keep_markup)
        if value is None:
            break
        if step.validate(value):
            logger.info("Gemini вернул неподходящее значение для %s: %r", step.key, value)
            continue
        if step.key == "ability" and not ability_fits(value, faction):
            logger.info("Способность не в духе фракции %s: %r", faction.name, value)
            continue
        return value
    return random.choice(faction.fallbacks if step.key == "ability" else step.fallbacks)


STEPS = {
    STATE_NAME: Step(
        "name", "📜 Имя",
        "📜 <b>Как тебя звать-величать?</b>\n\n"
        "Имя пишется крупно в шапке карты. Можно настоящее, можно с прозвищем или титулом — как у героев Гвинта.\n"
        "<i>Например: «Кирилл Беспощадный», «Старый Волк», «Ярл Бьорн Седобородый», «Мастер Лавк».</i>\n"
        "До 35 символов.",
        text_validator(35), STATE_CATEGORY,
        ai_prompt=("Придумай имя для персонажа карты Гвинта, фракция {faction}: {name_hint}. "
                   "Не используй существующих персонажей. 1-3 слова. Только имя."),
        fallbacks=("Грозный Воин", "Старый Волк"),
    ),
    STATE_CATEGORY: Step(
        "category", "🧬 Категория",
        "🧬 <b>Ты вообще кто такой? (Категория)</b>\n\n"
        "Мелкая строка под именем: раса и ремесло через запятую, 1–3 слова.\n"
        "<i>Например: «Человек, Солдат», «Эльф, Лучник», «Краснолюд, Кузнец», «Ведьмак», «Вампир», "
        "«Человек, Чародей».</i>",
        text_validator(30, max_words=3), STATE_POWER,
        ai_prompt="Придумай категорию для карты Гвинта, фракция {faction}, в формате «Раса, Ремесло», например «Человек, Солдат». 1-3 слова. Только категория.",
        fallbacks=("Человек, Воин", "Человек, Наёмник"),
    ),
    STATE_POWER: Step(
        "power", "⚔️ Сила",
        "⚔️ <b>Насколько больно бьёшь? (Сила)</b>\n\n"
        "Большое число в ромбе слева сверху. У простых бойцов обычно 2–6, у героев — 7–12, "
        "легенды бывают и сильнее.\n<i>Например: 5. Можно от 1 до 99.</i>",
        int_validator(1, 99), STATE_ARMOR, random_range=(2, 12),
    ),
    STATE_ARMOR: Step(
        "armor", "🛡 Броня",
        "🛡 <b>Щит есть? (Броня)</b>\n\n"
        "Число в щите справа сверху — броня принимает урон на себя. 0 — щита на карте не будет.\n"
        "<i>Обычно 0–5. Например: 2. Можно от 0 до 99.</i>",
        int_validator(0, 99), STATE_PROVISION, random_range=(0, 4),
    ),
    STATE_PROVISION: Step(
        "provision", "🍖 Наём",
        "🍖 <b>Сколько еды жрёшь в походе? (Стоимость найма)</b>\n\n"
        "Число в правом нижнем углу карты — во сколько провизии обходится боец.\n"
        "<i>Бронзовые карты обычно 4–7, золотые — 8–15. Например: 9. Можно от 4 до 99.</i>",
        int_validator(4, 99), STATE_FRAME, random_range=(4, 14),
    ),
    STATE_ABILITY: Step(
        "ability", "✨ Способность",
        "✨ <b>Что умеешь на столе? (Способность)</b>\n\n"
        "Пиши как в Гвинте: сначала метка с двоеточием, потом что делает карта.\n"
        "<i>Например:\n"
        "• «Размещение: нанесите 4 ед. урона вражескому отряду.»\n"
        "• «Приказ: усильте союзный отряд на 3 ед.»\n"
        "• «Завещание: создайте копию этого отряда.» — у Чудовищ\n"
        "• «Берсерк: превратитесь в Медведя.» — у Скеллиге</i>\n\n"
        "Метки и термины вроде «яд» или «щит» подсветятся сами, любое слово можно выделить *звёздочками*. "
        "До 300 символов.",
        text_validator(300), STATE_QUOTE,
        ai_prompt=(
            "Сгенерируй новую способность карты для игры Гвинт за фракцию {faction} в официальном стиле "
            "русской локализации игры. Используй ТОЛЬКО ключевые слова этой фракции: {mechanics}. "
            "Ключевые слова других фракций использовать нельзя. Начни с ключевого слова и двоеточия, "
            "например «Размещение:». Названия статусов, категорий и карт выдели звёздочками, например *Яд*. "
            "Максимум 25 слов. Без кавычек и пояснений."
        ),
        fallbacks=("Размещение: нанесите 4 ед. урона вражескому отряду.",),
        keep_markup=True,
    ),
    STATE_QUOTE: Step(
        "quote", "💬 Цитата",
        "💬 <b>Умная мысль для самого низа (Цитата)</b>\n\n"
        "Фраза курсивом внизу карты — что сказал бы твой персонаж или что говорят о нём другие.\n"
        "<i>Например: «Сталь — для людей, серебро — для чудовищ.», «Долг платежом красен, особенно с процентами.», "
        "«Море не прощает трусов.»</i>\nДо 150 символов.",
        text_validator(150), STATE_CONFIRM,
        ai_prompt="Сгенерируй мрачную цитату персонажа для карты Гвинт, фракция {faction}, в духе мира Ведьмака. Максимум 20 слов. Без кавычек.",
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
        bot.send_message(
            chat_id,
            prefix + "🖼 <b>Какой редкости будет твоя карта?</b>\n\n"
            "🟡 <b>Золотая</b> — легенды и герои: уникальный персонаж, золотая рамка.\n"
            "🟤 <b>Бронзовая</b> — простые бойцы, отряды и наёмники: бронзовая рамка.",
            reply_markup=frame_markup(),
        )
    elif state == STATE_CONFIRM:
        if prefix:
            bot.send_message(chat_id, prefix.strip())
        ask_for_confirmation(chat_id)
    else:
        step = STEPS[state]
        bot.send_message(chat_id, prefix + step.prompt, reply_markup=gen_ai_markup(step.key))


def price_line(chat_id: int, price: int, trial_ok: bool = False) -> str:
    if is_free(chat_id):
        return "♾ <b>Для тебя бесплатно</b> — у тебя безлимит."
    if trial_ok and balance_of(chat_id) < price and has_trial(chat_id):
        return ("🎁 <b>Первая картища — в подарок!</b> Но на ней будут крупные водяные знаки Лавка.\n"
                f"Чистая картища — {price}⭐, пополнить мешочек можно в меню.")
    return f"💰 Стоимость: <b>{price}⭐</b> (в мешочке: {balance_of(chat_id)}⭐)"


def ask_for_confirmation(chat_id: int):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ ДА, РИСУЙ!", callback_data="confirm:yes"),
        types.InlineKeyboardButton("❌ ОТМЕНА", callback_data="confirm:no"),
    )
    data = user_data[chat_id]
    text = (
        "🎉 <b>Лавк собрал твои бумажки! ПРОВЕРЯЙ:</b>\n\n"
        + card_summary(data)
        + f"\n\n{price_line(chat_id, GENERATION_PRICE, trial_ok=True)}\n\n<b>Всё верно, человече? Запускаем нейромагию?</b>"
    )
    # Подпись к фото — до 1024 символов; если анкета длиннее, фото и текст идут отдельно.
    # Фото, присланное файлом, можно переслать только как документ.
    send = bot.send_document if data.get("photo_doc") else bot.send_photo
    if len(text) <= 1024:
        send(chat_id, data["photo_id"], caption=text, reply_markup=markup)
    else:
        send(chat_id, data["photo_id"], caption="📸 Твоё фото для картищи")
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
        f"🃏 Картищ в колоде: <b>{db.count_cards(chat_id)}</b>"
    )
    if has_trial(chat_id) and not is_free(chat_id):
        text += "\n\n🎁 <b>Первая картища — в подарок!</b> Жми «Создать карту»."
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
    markup.add(types.InlineKeyboardButton("🎟 Ввести промокод", callback_data="promo"))
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


def pack_label(pay: int, credit: int) -> str:
    label = f"{pay}⭐ → {plural_cards(credit // GENERATION_PRICE)}"
    discount = round(100 - pay * 100 / credit)
    return f"{label} · −{discount}%" if discount > 0 else label


def show_topup(chat_id: int):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(*[
        types.InlineKeyboardButton(pack_label(pay, credit), callback_data=f"topup:{pay}")
        for pay, credit in TOPUP_PACKS
    ])
    markup.row(
        types.InlineKeyboardButton("✍️ Своя сумма", callback_data="topup:custom"),
        types.InlineKeyboardButton("🎟 Промокод", callback_data="promo"),
    )
    packs = "\n".join(
        f"• <b>{pay}⭐</b> → {credit}⭐ в мешочек ({plural_cards(credit // GENERATION_PRICE)})"
        for pay, credit in TOPUP_PACKS
    )
    bot.send_message(
        chat_id,
        "💰 <b>Пополнить мешочек</b>\n\n"
        f"Сейчас в мешочке: <b>{balance_text(chat_id)}</b>\n"
        f"🎨 Картища — {GENERATION_PRICE}⭐ · ✏️ Правка — {EDIT_PRICE}⭐\n\n"
        f"Чем больше набор, тем щедрее Лавк:\n{packs}\n\n"
        "Или впиши свою сумму — звёзды зачислятся один к одному.\n"
        "<i>Оплачивая, ты соглашаешься с условиями — /terms</i>",
        reply_markup=markup,
    )


def send_topup_invoice(chat_id: int, pay: int, credit: int):
    bonus = f" (из них {credit - pay}⭐ — подарок Лавка)" if credit > pay else ""
    enough = f" Хватит на {plural_cards(credit // GENERATION_PRICE)}." if credit >= GENERATION_PRICE else ""
    try:
        bot.send_invoice(
            chat_id,
            title=f"Мешочек звёзд: {credit}⭐",
            description=f"{credit}⭐ на баланс Лавка{bonus}.{enough}",
            invoice_payload=f"topup:{chat_id}:{pay}:{credit}",
            provider_token="",
            currency="XTR",
            prices=[types.LabeledPrice(label=f"{credit}⭐ в мешочек", amount=pay)],
        )
    except apihelper.ApiTelegramException as e:
        logger.error("Не удалось выставить счёт на %s⭐: %s", pay, e)
        bot.send_message(chat_id, "⚠️ Telegram не принял такой счёт. Попробуй другую сумму.")


def ask_promo(chat_id: int):
    user_states[chat_id] = STATE_PROMO
    bot.send_message(chat_id, "🎟 <b>Впиши промокод</b> — Лавк проверит, настоящий ли он.",
                     reply_markup=get_cancel_menu())


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
        "3. Первая картища бесплатна, но выдаётся с водяными знаками; пробную картищу нельзя править.\n"
        "4. Картищи — фан-арт по мотивам GWENT, бот не связан с CD PROJEKT RED.\n"
        "5. Не присылай чужие фото без согласия их владельца.\n"
        f"6. Вопросы по оплате и возвраты — /paysupport или {SUPPORT_CONTACT}.",
    )


@bot.message_handler(func=lambda msg: msg.text == BTN_CANCEL)
def cancel_input(message):
    chat_id = message.chat.id
    reset_user(chat_id)
    text = ("⏳ Картища уже рисуется — духов не остановить. Лавк пришлёт её, как закончит!"
            if chat_id in busy_users else "Эх, Лавк убирает холст. Если захочешь новую картищу — жми кнопку!")
    bot.send_message(chat_id, text, reply_markup=get_main_menu(chat_id))


@bot.message_handler(commands=["promo"])
def cmd_promo(message):
    reset_user(message.chat.id)
    ask_promo(message.chat.id)


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
    if not can_afford(chat_id, GENERATION_PRICE) and not has_trial(chat_id):
        send_not_enough(chat_id, GENERATION_PRICE)
        return
    user_data[chat_id] = {}
    user_states[chat_id] = STATE_PHOTO
    bot.send_message(
        chat_id,
        "📸 <b>Сначала давай лицо!</b>\n\nЧеловече давать хорошую картинку. Без теней, без темных стекол "
        "на глазах и чтобы ничего не закрывало! Духам нужно видеть твои черты, чтобы красиво перенести "
        "на холст. Плохое лицо — плохая картища!\n\n"
        "<i>Лучше всего — селфи или портрет анфас при дневном свете, лицо крупно, один человек в кадре.</i>",
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
    if value.isdigit() and int(value) in dict(TOPUP_PACKS):
        send_topup_invoice(chat_id, int(value), dict(TOPUP_PACKS)[int(value)])


@bot.callback_query_handler(func=lambda call: call.data == "promo")
def cb_promo(call):
    safe_call(bot.answer_callback_query, call.id)
    ask_promo(call.message.chat.id)


# ==========================================
# 10. ОПЛАТА ЗВЁЗДАМИ
# ==========================================
def parse_topup_payload(payload: str) -> Optional[tuple[int, int, int]]:
    """(user_id, сколько заплатил, сколько зачислить). Старые счета — без бонуса."""
    parts = payload.split(":")
    if parts[0] != "topup" or len(parts) not in (3, 4) or not all(p.isdigit() for p in parts[1:]):
        return None
    user_id, pay = int(parts[1]), int(parts[2])
    credit = int(parts[3]) if len(parts) == 4 else pay
    # Бонус допустим только для наборов из TOPUP_PACKS, иначе сумма зачисляется один к одному.
    if credit != pay and dict(TOPUP_PACKS).get(pay) != credit:
        return None
    return user_id, pay, credit


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
    parsed = parse_topup_payload(payment.invoice_payload)
    credit = parsed[2] if parsed and parsed[1] == payment.total_amount else payment.total_amount
    if db.add_balance(chat_id, payment.total_amount, "topup", payment.telegram_payment_charge_id):
        if credit > payment.total_amount:
            db.add_balance(chat_id, credit - payment.total_amount, "topup_bonus")
        logger.info("Пополнение %s на %s⭐ (+%s⭐ в мешочек)", chat_id, payment.total_amount, credit)
    bot.send_message(
        chat_id,
        f"💰 <b>Звёзды в мешочке!</b> +{credit}⭐\n"
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
        user_data[chat_id]["photo_doc"] = True
    else:
        bot.send_message(chat_id, "⚠️ Лавк просил картинку! Пришли фото, человече.")
        return

    user_states[chat_id] = STATE_FACTION
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(*[types.InlineKeyboardButton(f.button, callback_data=f"faction:{key}") for key, f in FACTIONS.items()])
    about = "\n\n".join(f"<b>{f.button}</b> — {f.about}" for f in FACTIONS.values())
    bot.send_message(
        chat_id,
        "🏴 <b>За кого воевать будешь?</b>\n\n"
        "От фракции зависят рамка, цвета шапки, наряд и фон на арте, а духи будут придумывать "
        f"способности в её стиле.\n\n{about}\n\n<b>Выбирай знамя, человече!</b>",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("faction:"))
def cb_faction(call):
    chat_id = call.message.chat.id
    faction_key = call.data.split(":", 1)[1]
    if user_states.get(chat_id) != STATE_FACTION or faction_key not in FACTIONS:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    faction = FACTIONS[faction_key]
    user_data[chat_id]["faction"] = faction.name
    user_data[chat_id]["faction_key"] = faction_key
    safe_call(bot.answer_callback_query, call.id)
    safe_call(bot.edit_message_text, f"✅ Фракция: <b>{esc(faction.button)}</b>\n<i>{esc(faction.about)}</i>",
              chat_id, call.message.message_id)
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
    safe_call(bot.edit_message_text, random.choice(SPIRIT_PHRASES), chat_id, call.message.message_id)
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
        safe_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=None)
        bot.send_message(chat_id, "Эх, порвали холст! Начинай заново, когда будешь готов.",
                         reply_markup=get_main_menu(chat_id))
        return

    if chat_id in busy_users:
        bot.answer_callback_query(call.id, "⏳ Лавк ещё рисует прошлую картищу!", show_alert=True)
        return
    paid = charge(chat_id, GENERATION_PRICE, "generation")
    mode = {True: "paid", False: "free"}.get(paid)
    if mode is None and db.claim_trial(chat_id):
        mode = "trial"
    if mode is None:
        safe_call(bot.answer_callback_query, call.id)
        # Состояние не сбрасываем: после пополнения можно снова нажать «ДА, РИСУЙ!».
        send_not_enough(chat_id, GENERATION_PRICE)
        return

    safe_call(bot.answer_callback_query, call.id)
    data = user_data[chat_id]
    reset_user(chat_id)
    busy_users.add(chat_id)
    safe_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=None)
    # Новое сообщение возвращает главное меню вместо клавиатуры «Отмена» на всё время генерации.
    bot.send_message(chat_id, "✅ <b>Данные подтверждены!</b> Лавк пошёл за красками...",
                     reply_markup=get_main_menu(chat_id))
    threading.Thread(target=generate_card, args=(chat_id, data, mode), daemon=True).start()


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
        bot.send_photo(chat_id, card["file_id"], caption=caption, reply_markup=card_markup(card, with_deck=True))
        return

    # action == "edit"
    if card["data"].get("trial"):
        bot.answer_callback_query(call.id, "Пробную картищу править нельзя — закажи чистую!", show_alert=True)
        return
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
    safe_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=None)
    bot.send_message(chat_id, f"✅ <b>Правка принята:</b> {esc(instruction)}", reply_markup=get_main_menu(chat_id))
    threading.Thread(target=edit_card, args=(chat_id, card, instruction, "paid" if paid else "free"),
                     daemon=True).start()


# ==========================================
# 13. ГЕНЕРАЦИЯ И ПРАВКА АРТА (Fal.ai)
# ==========================================
class FalError(Exception):
    pass


def to_data_uri(image_bytes: bytes, mime: str) -> str:
    # Fal принимает base64 data URI в полях-файлах, отдельная загрузка в storage не нужна.
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def request_fal_image(endpoint: str, payload: dict) -> bytes:
    """Генерация через очередь Fal: запрос не висит на одном соединении, есть общий дедлайн."""
    if not FAL_KEY:
        raise FalError("не задан FAL_API_KEY")
    headers = {"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"}
    response = requests.post(endpoint, json=payload, headers=headers, timeout=60)
    if not response.ok:
        logger.error("Fal.ai %s: %s", response.status_code, response.text[:1000])
        raise FalError(f"Fal.ai вернул код {response.status_code}")
    job = response.json()
    deadline = time.monotonic() + FAL_DEADLINE
    while True:
        if time.monotonic() > deadline:
            logger.error("Fal.ai: истёк дедлайн для %s", job.get("request_id"))
            try:
                requests.put(job["cancel_url"], headers=headers, timeout=15)
            except Exception:
                logger.exception("Fal.ai: не удалось отменить запрос")
            raise FalError("художник слишком долго не отвечал")
        time.sleep(2)
        try:
            status = requests.get(job["status_url"], headers=headers, timeout=30).json().get("status")
        except (requests.RequestException, ValueError):
            logger.warning("Fal.ai: не удалось получить статус, повторю")
            continue
        if status == "COMPLETED":
            break
    result = requests.get(job["response_url"], headers=headers, timeout=60)
    try:
        result.raise_for_status()
        image_url = result.json()["images"][0]["url"]
    except (requests.HTTPError, ValueError, KeyError, IndexError, TypeError):
        logger.error("Неожиданный ответ Fal.ai: %s %s", result.status_code, result.text[:1000])
        raise FalError("Fal.ai вернул ответ без картинки")
    art = requests.get(image_url, timeout=90)
    art.raise_for_status()
    return art.content


def download_telegram_file(file_id: str) -> bytes:
    return bot.download_file(bot.get_file(file_id).file_path)


def refund(chat_id: int, price: int, mode: str) -> str:
    if mode == "paid":
        db.add_balance(chat_id, price, "refund")
        return f"\n💰 {price}⭐ вернулись в мешочек."
    if mode == "trial":
        db.release_trial(chat_id)
        return "\n🎁 Пробная картища не сгорела — попробуй ещё раз."
    return ""


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
    "Official GWENT: The Witcher Card Game card illustration, hand-painted digital painting by CD Projekt Red "
    "concept artists, visible painterly brush strokes, rich textures, stylized dark fantasy, Slavic folklore "
    "atmosphere, dramatic rim lighting, cinematic composition. "
)
ART_SCENE = (
    "Waist-up portrait, the character in the lower two thirds, face clearly visible and lit, room above the head. "
    "A fully painted detailed environment behind the character fills the whole image, atmospheric depth, "
    "no plain or empty backdrop."
)
ART_NEGATIVE = (
    "photorealistic, photograph, photo, hyperrealistic, 3d render, cgi, smooth plastic skin, modern clothing, "
    "plain background, studio backdrop, white background, empty background, gradient background, "
    "text, letters, watermark, card frame, border, ui"
)


def build_art_prompt(data: dict) -> str:
    faction = FACTIONS.get(data.get("faction_key")) or FACTION_BY_NAME.get(data.get("faction"), FACTIONS["Neutral"])
    role = ask_gemini(
        "Translate this fantasy character class into English, 1-4 words, reply with the translation only: "
        + data.get("category", ""),
        temperature=0.2,
    )
    who = f"The character is a {role}, " if role and len(role) <= 60 else "The character is "
    # Фон — раньше остального: модель читает начало промпта внимательнее.
    return (
        f"{ART_STYLE}Background: {random.choice(faction.backgrounds)}. "
        f"{who}{faction.attire}, {random.choice(ART_POSES)}. {faction.palette} {ART_SCENE}"
    )


def status_ticker(chat_id: int, message_id: int, stop: threading.Event):
    """Раз в 10–15 секунд меняет фразу ожидания, чтобы было видно, что Лавк не завис."""
    last = None
    while not stop.wait(random.uniform(*WAIT_TICK)):
        phrase = random.choice([p for p in LAVK_WAIT_PHRASES if p != last])
        last = phrase
        safe_call(bot.edit_message_text, phrase + WAIT_INTRO, chat_id, message_id)


def run_art_job(chat_id: int, price: int, mode: str, job: Callable[[int], None]):
    """Общая обвязка: статус, возврат звёзд при ошибке, снятие флага занятости.

    mode: paid — списаны звёзды, free — безлимит/админ, trial — пробная картища.
    """
    status_msg = bot.send_message(chat_id, random.choice(LAVK_WAIT_PHRASES) + WAIT_INTRO)
    stop = threading.Event()
    threading.Thread(target=status_ticker, args=(chat_id, status_msg.message_id, stop), daemon=True).start()
    try:
        job(status_msg.message_id)
    except FalError as e:
        stop.set()
        note = refund(chat_id, price, mode)
        safe_call(bot.edit_message_text, f"⚠️ Духи не смогли нарисовать: {esc(e)}{note}", chat_id, status_msg.message_id)
        bot.send_message(chat_id, "Попробуй ещё раз чуть позже.", reply_markup=get_main_menu(chat_id))
    except Exception:
        stop.set()
        # Текст исключения пользователю не показываем: в URL Telegram-файлов содержится токен бота.
        logger.exception("Ошибка генерации для %s", chat_id)
        note = refund(chat_id, price, mode)
        safe_call(bot.edit_message_text, f"⚠️ Системная ошибка связи. Попробуй ещё раз.{note}", chat_id, status_msg.message_id)
        bot.send_message(chat_id, "Лавк ждёт тебя в меню.", reply_markup=get_main_menu(chat_id))
    finally:
        stop.set()
        busy_users.discard(chat_id)


def art_path(card_id: int) -> str:
    return os.path.join(ART_DIR, f"{card_id}.jpg")


def save_art(card_id: int, art: bytes):
    try:
        os.makedirs(ART_DIR, exist_ok=True)
        with open(art_path(card_id), "wb") as f:
            f.write(art)
    except OSError:
        logger.exception("Не удалось сохранить арт картищи %s", card_id)


def load_art(card: dict) -> bytes:
    """Чистый арт для правки: с диска, иначе из Telegram (у старых карт — отдельное фото арта)."""
    path = art_path(card["id"])
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    return download_telegram_file(card.get("art_file_id") or card["file_id"])


def deliver_card(chat_id: int, art: bytes, card_data: dict, title: str, status_id: int,
                 parent_id: Optional[int] = None) -> int:
    """Собирает карту поверх арта, отправляет полную карту + карту в рамке и сохраняет в колоду."""
    trial = bool(card_data.get("trial"))
    full, card_only = render_card(art, card_data, trial=trial)
    caption = f"{title}\n«{esc(strip_markup(card_data.get('name', '')))}» · {esc(card_data.get('faction', ''))}"
    sent = bot.send_media_group(chat_id, [
        types.InputMediaPhoto(full, caption=caption, parse_mode="HTML"),
        types.InputMediaPhoto(card_only),
    ])
    safe_call(bot.delete_message, chat_id, status_id)
    card_id = db.add_card(chat_id, card_data, sent[0].photo[-1].file_id, parent_id=parent_id)
    save_art(card_id, art)
    if trial:
        note = ("🎁 <b>Это пробная картища</b> — поэтому на ней водяные знаки Лавка.\n"
                f"Чистая картища без надписей — {GENERATION_PRICE}⭐. Пополни мешочек и закажи настоящую!")
    else:
        note = ("🃏 Картища легла в твою колоду — ищи её в «Моих картищах».\n"
                "<i>Вторая картинка — сама карта в рамке, удобно для аватарки.</i>")
    # Главное меню — обычной клавиатурой, правка (или пополнение) — кнопкой под отдельным сообщением.
    bot.send_message(chat_id, note, reply_markup=get_main_menu(chat_id))
    action = "Пополнить мешочек?" if trial else "Хочешь что-то поправить на арте?"
    bot.send_message(chat_id, f"✏️ {action}", reply_markup=card_markup({"id": card_id, "data": card_data}))
    return card_id


def generate_card(chat_id: int, data: dict, mode: str):
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
            "id_weight": 0.85,
            # Позже подмешиваем лицо — больше свободы рисованному стилю; 0 давал почти фото.
            "start_step": 2,
            # По умолчанию промпт обрезается до 128 токенов, и описание фона терялось.
            "max_sequence_length": "512",
        })
        card_data = {k: v for k, v in data.items()
                     if k not in ("photo_id", "photo_mime", "photo_doc", "faction_key")}
        if mode == "trial":
            card_data["trial"] = True
        title = "🎁 <b>ПРОБНАЯ КАРТИЩА ГОТОВА!</b>" if mode == "trial" else "🔥 <b>ТВОЯ ИСТИННАЯ КАРТИЩА ГОТОВА!</b>"
        deliver_card(chat_id, art, card_data, title, status_id)
        if mode == "paid":
            award_referrer(chat_id)

    run_art_job(chat_id, GENERATION_PRICE, mode, job)


def edit_card(chat_id: int, card: dict, instruction: str, mode: str):
    def job(status_id: int):
        image = load_art(card)
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
        card_data.pop("trial", None)
        deliver_card(chat_id, art, card_data, "✏️ <b>ПРАВКА ГОТОВА!</b>", status_id, parent_id=card["id"])

    run_art_job(chat_id, EDIT_PRICE, mode, job)


# ==========================================
# 14. АДМИНКА
# ==========================================
def admin_panel_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm:stats"),
        types.InlineKeyboardButton("📢 Рассылка", callback_data="adm:bc"),
    )
    markup.add(types.InlineKeyboardButton("🔎 Найти бойца", callback_data="adm:find"),
               types.InlineKeyboardButton("🎟 Промокоды", callback_data="adm:promo"))
    return markup


PROMO_RE = re.compile(r"^[A-ZА-ЯЁ0-9_-]{3,32}$")


def show_promos(chat_id: int):
    promos = db.list_promos()
    lines = [
        f"• <code>{esc(p['code'])}</code> — {p['amount']}⭐, использован {p['uses']}"
        + (f"/{p['max_uses']}" if p["max_uses"] else " (без лимита)")
        for p in promos
    ]
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("➕ Создать промокод", callback_data="adm:promonew"))
    markup.add(*[types.InlineKeyboardButton(f"🗑 {p['code']}", callback_data=f"adm:promodel:{p['code']}") for p in promos])
    bot.send_message(
        chat_id,
        "🎟 <b>Промокоды</b>\n\n" + ("\n".join(lines) if lines else "Пока ни одного.")
        + "\n\n<i>Каждый боец может активировать промокод только один раз.</i>",
        reply_markup=markup,
    )


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
    elif action == "promo":
        show_promos(chat_id)
    elif action == "promonew":
        user_states[chat_id] = STATE_ADMIN_PROMO
        bot.send_message(
            chat_id,
            "➕ Пришли промокод одной строкой: <code>КОД СУММА [ЛИМИТ]</code>\n\n"
            "<i>Например: <code>LAVK100 100 50</code> — 100⭐, первые 50 активаций.\n"
            "<code>ПОДАРОК 50</code> — 50⭐ без лимита активаций.</i>\n"
            "Код — 3–32 символа: буквы, цифры, «-» и «_».",
            reply_markup=get_cancel_menu(),
        )
    elif action == "promodel" and len(parts) == 3:
        db.deactivate_promo(parts[2])
        bot.send_message(chat_id, f"🗑 Промокод <code>{esc(parts[2])}</code> отключён.")
        show_promos(chat_id)
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
@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_ADMIN_PROMO and is_admin(msg.from_user.id))
def admin_promo(message):
    chat_id = message.chat.id
    parts = (message.text or "").split()
    if (len(parts) not in (2, 3) or not PROMO_RE.match(parts[0].upper())
            or not all(p.isdigit() for p in parts[1:]) or not 1 <= int(parts[1]) <= 100_000):
        bot.send_message(chat_id, "⚠️ Формат: <code>КОД СУММА [ЛИМИТ]</code>, например <code>LAVK100 100 50</code>.")
        return
    code, amount = parts[0].upper(), int(parts[1])
    max_uses = int(parts[2]) if len(parts) == 3 else 0
    if not db.create_promo(code, amount, max_uses):
        bot.send_message(chat_id, "⚠️ Такой промокод уже был. Придумай другой.")
        return
    reset_user(chat_id)
    limit = f"{max_uses} активаций" if max_uses else "без лимита"
    bot.send_message(chat_id, f"✅ Промокод <code>{esc(code)}</code> создан: {amount}⭐, {limit}.",
                     reply_markup=get_main_menu(chat_id))
    show_promos(chat_id)


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_PROMO)
def process_promo(message):
    chat_id = message.chat.id
    code = (message.text or "").strip().upper()
    status, amount = db.redeem_promo(code, chat_id) if PROMO_RE.match(code) else ("not_found", 0)
    if status == "not_found":
        bot.send_message(chat_id, "🤨 Лавк такого промокода не знает. Проверь буквы или жми «Отмена».")
        return
    reset_user(chat_id)
    text = {
        "ok": f"🎉 <b>Промокод сработал!</b> +{amount}⭐\nВ мешочке теперь: <b>{balance_text(chat_id)}</b>",
        "used": "😏 Этот промокод ты уже активировал, человече. Второй раз не прокатит!",
        "exhausted": "😢 Опоздал — этот промокод уже разобрали.",
    }[status]
    bot.send_message(chat_id, text, reply_markup=get_main_menu(chat_id))


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_TOPUP_AMOUNT)
def process_topup_amount(message):
    chat_id = message.chat.id
    text = message.text.strip()
    if not text.isdigit() or not TOPUP_MIN <= int(text) <= TOPUP_MAX:
        bot.send_message(chat_id, f"⚠️ Впиши число от {TOPUP_MIN} до {TOPUP_MAX}.")
        return
    reset_user(chat_id)
    bot.send_message(chat_id, "🧾 Лавк выписал счёт:", reply_markup=get_main_menu(chat_id))
    send_topup_invoice(chat_id, int(text), int(text))


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
        bot.send_message(chat_id, random.choice(SPIRIT_PHRASES) + "\n<b>Подожди чуть-чуть!</b>")
    elif state in (STATE_FACTION, STATE_FRAME, STATE_CONFIRM, STATE_EDIT_CONFIRM, STATE_ADMIN_BROADCAST_CONFIRM):
        bot.send_message(chat_id, "👆 Выбери вариант кнопкой выше, человече.")
    elif state == STATE_PHOTO:
        bot.send_message(chat_id, "⚠️ Лавк просил картинку! Пришли фото, человече.")
    elif state in STEPS or state in (STATE_EDIT_PROMPT, STATE_TOPUP_AMOUNT, STATE_PROMO, STATE_ADMIN_PROMO):
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
