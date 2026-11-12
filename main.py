import base64
import html
import io
import json
import logging
import os
import random
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Callable, Optional
from urllib.parse import quote

import requests
import telebot
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
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
# Запасные модели: у каждой свой бесплатный суточный лимит. Когда основная исчерпана — идём дальше.
GEMINI_MODELS = list(dict.fromkeys(
    [GEMINI_MODEL] + [m.strip() for m in os.getenv(
        "GEMINI_FALLBACK_MODELS", "gemini-3.1-flash-lite,gemini-flash-lite-latest,gemini-2.5-flash").split(",") if m.strip()]))
FAL_KEY = os.getenv("FAL_API_KEY")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_ID", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
# На Amvera постоянное хранилище — /data: база там переживает пересборки.
DB_PATH = os.getenv("DB_PATH") or os.path.join("/data" if os.path.isdir("/data") else BASE_DIR, "lavk.db")

CHANNEL_ID = "@Master_Lavk"
SUPPORT_CONTACT = "@S159YKRF4"
WELCOME_IMAGE = os.path.join(BASE_DIR, "1794190872103.jpg")

FAL_GENERATE_ENDPOINT = "https://queue.fal.run/fal-ai/flux-pulid"
FAL_EDIT_ENDPOINT = "https://queue.fal.run/fal-ai/flux-pro/kontext"
FAL_MASK_ENDPOINT = "https://queue.fal.run/fal-ai/birefnet/v2"
FAL_FILL_ENDPOINT = "https://queue.fal.run/fal-ai/flux-pro/v1/fill"
FAL_SAFETY = "4"   # фильтр Fal для правок: 1 — строже всего, 6 — мягче; по умолчанию 2 бракует мрачное фэнтези
FAL_DEADLINE = 200        # секунд на одну попытку генерации, потом отмена (и ещё одна попытка)
GEMINI_TIMEOUT_MS = 20_000  # обычный ответ — 1–8 с; дольше — модель перегружена, лучше взять следующую
MEDIA_TIMEOUT = 300       # секунд на загрузку готовой карты в Telegram (канал бывает очень медленным)

GENERATION_PRICE = 100
EDIT_PRICE = 50
REFERRAL_BONUS = 50
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

class LogExceptions(telebot.ExceptionHandler):
    """Ошибка в одном обработчике не должна перезапускать опрос Telegram для всех."""

    def handle(self, exception):
        logger.error("Ошибка в обработчике: %r", exception, exc_info=exception)
        return True


# По умолчанию у telebot 2 потока: одна медленная операция (загрузка фото, проверка подписки)
# тормозила ответы всем остальным пользователям.
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", use_class_middlewares=True, num_threads=8,
                      exception_handler=LogExceptions())
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
    STATE_PROMO, STATE_ADMIN_PROMO, STATE_ADMIN_SCENE, STATE_ADMIN_MODELTEST,
) = range(24)

@dataclass(frozen=True)
class Faction:
    button: str
    name: str
    about: str               # описание в меню выбора
    backgrounds: tuple       # фоны в духе фракции, выбираются случайно
    palette: str
    mechanics: str           # ключевые слова способностей этой фракции (для Gemini)
    names: tuple             # примеры имён: подсказка духам и запасной вариант
    categories: tuple
    quote_theme: str
    quotes: tuple
    fallbacks: tuple         # запасные способности, если Gemini не ответил


FACTIONS = {
    "Nilfgaard": Faction(
        "🛡 Нильфгаард", "Нильфгаард",
        "Империя Белого Пламени: железная дисциплина, шпионы и придворные интриги. Чёрное с золотом.",
        (
            "a vast Nilfgaardian war camp, black and gold pavilions, campfires, rows of pikemen and imperial sun banners",
            "the marble throne hall of the imperial palace in the City of Golden Towers, golden sun mosaics, tall columns and braziers",
            "a burning Northern village behind advancing Nilfgaardian ranks, black banners with a golden sun, smoke and embers",
            "a shadowy imperial intelligence office, maps and sealed letters on a desk, a single candle, an agent in the doorway",
            "the scorching desert road of Vicovaro, an imperial legion marching past ancient sandstone ruins",
            "the deck of an imperial warship at sea, black sails with a golden sun, a burning coastal fort on the horizon",
        ),
        "Color palette: black, gold, dark steel, crimson accents.",
        "Размещение, Приказ, Шпионаж, Блокировка, Раскрытие, Ассимиляция, Захват, Порядок, Уязвимость, Яд",
        ("Кагыр Маур Дыффин", "Мэнно Коэгоорн", "Ардал аэп Даги", "Вилгефорц Ван Эмрейс", "Стефан Скеллен",
         "Мориц Рейвенхарт", "Деррик аэп Варрен", "Тибор Эггебрахт", "Эльан Вар Геллер", "Аурелия Мэйр",
         "Гвидо Тиль аэп Сельверт", "Леонарда Ван Даален", "Фаусто Дейн", "Сарвий Бреннард", "Эррил Фаррах"),
        ("Человек, Солдат", "Человек, Офицер", "Человек, Агент", "Человек, Аристократ", "Человек, Маг",
         "Человек, Рыцарь", "Человек, Шпион", "Человек, Инженер"),
        "о порядке, долге перед Императором, дисциплине и неизбежности победы Нильфгаарда",
        ("Империя не прощает. Империя помнит.", "Приказ есть приказ. Всё остальное — мнение.",
         "Белое Пламя пляшет на курганах врагов.", "Мы не завоёвываем. Мы наводим порядок.",
         "Север падёт — не сегодня, так завтра.", "Каждый шпион служит Империи. Иногда — сам того не зная."),
        (
            "Размещение: наложите *Блокировку* на вражеский отряд и нанесите ему 3 ед. урона.",
            "Приказ: раскройте случайную карту в руке противника.",
            "Размещение: *шпионаж*. Возьмите карту из вашей колоды.",
        ),
    ),
    "Monsters": Faction(
        "👾 Чудовища", "Чудовища",
        "Твари из болот, пещер и туманов: голод, когти, древние проклятия и Дикая Охота.",
        (
            "a foggy swamp at midnight, twisted dead trees like claws, glowing eyes hidden in green mist, bubbling black water",
            "a dark cave lair littered with bones and gnawed armor, dripping stalactites, a faint sickly green glow",
            "a frozen battlefield under a blood-red moon, spectral riders of the Wild Hunt in the storm clouds, crows over the dead",
            "an overgrown cursed cemetery with crooked gravestones, ghostly blue wisps and a broken crypt",
            "a ruined elven aqueduct turned into an insect hive, sticky webs, cocoons and eerie amber light",
            "a burning mountain peak with a dragon's hoard of gold and charred bones, smoke and ash in the air",
        ),
        "Color palette: sickly greens, bone white, blood red, abyssal black.",
        "Размещение, Приказ, Завещание, Поглощение, Рост, Доминирование, Кровотечение, Яд, Уязвимость",
        ("Кельтуллис", "Старая Сводница", "Имлерих", "Гнилоклык", "Шептунья с Топей", "Костолом",
         "Мать Туманов", "Вечноголодный Гарг", "Ночница Ведана", "Беззубый Ург", "Черноокая Морена",
         "Скрежет", "Плакальщица Курганов", "Древоглав", "Хозяин Пещеры"),
        ("Чудовище", "Дракон", "Трупоед", "Вампир", "Призрак", "Инсектоид", "Огр", "Реликт",
         "Проклятый", "Дикая Охота, Воин"),
        "о голоде, страхе, проклятиях, тьме и о том, что люди — лишь добыча",
        ("Ты боишься темноты? Правильно делаешь.", "Голод не знает ни чести, ни жалости.",
         "Когда туман сгущается — молись.", "Люди строят стены. Мы ждём, пока они уснут.",
         "Кости хрустят громче любых клятв.", "Охота началась задолго до твоего рождения."),
        (
            "Завещание: создайте копию этого отряда в случайном ряду.",
            "Размещение: *поглотите* союзный отряд и усильтесь на его силу.",
            "Рост: в конце хода усильтесь на 1 ед., если в ряду есть отряд сильнее.",
        ),
    ),
    "Skellige": Faction(
        "⚓ Скеллиге", "Скеллиге",
        "Островные кланы: берсерки, драккары, штормы и честь, добытая в бою.",
        (
            "a stormy northern coast, huge waves crashing against black cliffs, a dragon-headed longship in the surf",
            "a snowy mountain village of carved wooden longhouses, a roaring bonfire, clan banners and chimney smoke",
            "the deck of a longship in a raging sea storm, torn sail, lightning splitting the dark sky",
            "a mead hall feast, long tables, roaring hearth, drinking horns raised, carved pillars and shields on the walls",
            "a sacred grove of Freya with standing runestones and a misty fjord below",
            "a burning coastal raid at night, longships beached on the shore, flames reflecting in the dark water",
        ),
        "Color palette: icy blues, earthy browns, steel grey, sea-foam white.",
        "Размещение, Приказ, Берсерк, Адреналин, Жажда крови, Ветеран, Сброс, Кровотечение, Броня",
        ("Хьялмар ан Крайт", "Свеннбьорн Секира", "Бирна Бран", "Ульф Солёная Борода", "Сигрун Штормовая",
         "Торвальд Медвежья Лапа", "Харальд Кривой Клинок", "Ингрид Рыжая", "Бьорн Китобой", "Асгейр ан Тордаррох",
         "Хальвдан Громогласный", "Ровена Чёрный Парус", "Эйвинд Одноглазый", "Гудрун Ледяная", "Кнут Кровавый Рог"),
        ("Человек, Воин", "Человек, Берсерк", "Человек, Мореход", "Человек, Друид", "Человек, Клан Ан Крайт",
         "Человек, Ярл", "Человек, Налётчик", "Человек, Жрица"),
        "о море, чести, славной смерти в бою, клане и пиршественных чертогах",
        ("Море не прощает трусов.", "Умри с топором в руке — и скальды споют о тебе.",
         "Шторм — лучшее время для набега.", "Клан превыше крови. Кровь превыше золота.",
         "Мёд льётся рекой, пока точатся секиры.", "Кто боится волн, тот не видел берсерка."),
        (
            "Берсерк: когда сила этого отряда падает до 4 ед. или ниже, превратите его в Медведя.",
            "Размещение: сбросьте карту из руки и возьмите новую.",
            "Жажда крови 2: нанесите 3 ед. урона вражескому отряду.",
        ),
    ),
    "Northern": Faction(
        "⚜️ Королевства Севера", "Королевства Севера",
        "Короли и рыцари Темерии, Редании и Каэдвена: сталь, знамёна и осадные машины.",
        (
            "the war-torn courtyard of a besieged stone castle, trebuchets, arrows in the sky, blue and silver royal banners",
            "a royal throne hall lit by candles and stained-glass windows, heraldic tapestries and banners",
            "a misty battlefield at dawn, ranks of knights and crossbowmen, siege towers and fluttering lion banners",
            "a tournament field with colorful pavilions, a jousting lane and cheering crowds",
            "the muddy war camp of the Blue Stripes commandos in a pine forest, campfires and stacked spears",
            "the city walls of Vizima at sunset, a moat, a drawbridge and patrolling guards with halberds",
        ),
        "Color palette: royal blue, bright steel, red accents, warm golden sunlight.",
        "Размещение, Приказ, Заряд, Экипаж, Воодушевление, Щит, Броня, Усиление, Ходов до восстановления",
        ("Король Демавенд", "Вернон Роше", "Сэр Ян Натали", "Сигизмунд Дийкстра", "Бьянка Синие Полоски",
         "Сэр Эльдрик из Аэдирна", "Барон Олгерд Кровавый", "Леди Мирабелла Темерская", "Сэр Гаскон Драконоборец",
         "Капитан Рейнард Вейл", "Сэр Освальд Седой", "Граф Ульрих Бранвен", "Сестра Альмира", "Маршал Котт",
         "Сэр Тристан Каэдвенский"),
        ("Человек, Солдат", "Человек, Рыцарь", "Человек, Аристократ", "Человек, Машина", "Человек, Арбалетчик",
         "Человек, Чародейка", "Человек, Синие Полоски", "Человек, Офицер"),
        "о чести, верности короне, защите родной земли и рыцарском долге",
        ("За Темерию! До последнего вздоха.", "Корона тяжела, но меч тяжелее.",
         "Рыцарь без чести — просто человек в железе.", "Стены падают. Знамёна — никогда.",
         "Север помнит каждого, кто за него пал.", "Клятва дана — значит, держи строй."),
        (
            "Приказ: усильте союзный отряд на 2 ед. Заряд: 3.",
            "Размещение: наложите *Щит* на союзный отряд.",
            "Воодушевление: в начале хода добавьте себе 2 ед. брони.",
        ),
    ),
    "Syndicate": Faction(
        "💰 Синдикат", "Синдикат",
        "Преступный мир Новиграда: монеты, долги, наёмные ножи и награды за головы.",
        (
            "a narrow Novigrad alley at night, wet cobblestones reflecting orange lanterns, rain, shadowy figures in doorways",
            "a smoky gambling den lit by candles, tables piled with gold coins, dice and cards, thugs watching from the shadows",
            "the Novigrad harbour docks at sunset, merchant ships, crates of contraband and the city walls behind",
            "a decadent bathhouse with marble pools, steam, lanterns and masked guests whispering deals",
            "a dark underground fight pit surrounded by a shouting crowd throwing coins",
            "a counting room of a crime lord, ledgers, chests of gold, a huge vault door and candles",
        ),
        "Color palette: rich purple, gold, murky greens, deep shadows.",
        "Размещение, Приказ, Выгода, Подношение, Награда за голову, Накопление, Безумие, монеты",
        ("Франческо Красавчик", "Сиги Ройвен", "Карась Беззубый", "Мадам Ирис Шёлк", "Ловкач Тибальт",
         "Горбатый Бруно", "Сестра Кошель", "Лука Два Ножа", "Ветка Удавка", "Старый Меняла Гиль",
         "Жозефина Бархатная", "Риго Скупердяй", "Марко Лисий Хвост", "Тётушка Гризельда", "Фитиль"),
        ("Человек, Преступник", "Человек, Ростовщик", "Человек, Наёмник", "Краснолюд, Контрабандист",
         "Человек, Охотник за колдуньями", "Человек, Шулер", "Человек, Убийца", "Половинчик, Вор"),
        "о деньгах, долгах, сделках, предательстве и цене жизни",
        ("Всё имеет цену. Особенно молчание.", "Долг платежом красен — особенно с процентами.",
         "Честных людей нет. Есть недорогие.", "В Новиграде даже крысы платят налог.",
         "Кто держит кошель, тот держит город.", "Нож в спину — тоже деловое предложение."),
        (
            "Размещение: получите 4 монеты.",
            "Приказ: потратьте 3 монеты и нанесите 4 ед. урона вражескому отряду.",
            "Подношение 2: назначьте *награду за голову* вражеского отряда.",
        ),
    ),
    "Scoiatael": Faction(
        "🐿 Скоя'таэли", "Скоя'таэли",
        "Эльфы, краснолюды и дриады-партизаны: лесные засады, луки и древняя магия.",
        (
            "an ancient magical forest, colossal mossy oaks, crumbling white marble elven ruins, shafts of golden light",
            "a forest road in autumn, an ambush among red and orange trees, arrows flying, falling leaves",
            "moonlit elven ruins with waterfalls and glowing flowers in the valley of Dol Blathanna",
            "the heart of Brokilon forest, giant twisted trees, dryads' hanging bridges and luminous mushrooms",
            "a dwarven mountain forge in Mahakam, glowing furnaces, anvils and carved stone halls",
            "a misty river crossing at dawn, elven archers hidden among reeds and willow trees",
        ),
        "Color palette: forest greens, autumn browns, golden light, earthy tones.",
        "Размещение, Приказ, Гармония, Засада, Симбиоз, Перемещение, Усиление, Яд",
        ("Иорвет", "Тораль Иаллисен", "Эльдаин аэп Каэлл", "Аэлирэнн Ветреница", "Киаран аэп Эасниллен",
         "Брайна Сосновая Игла", "Финдабар Тихий Шаг", "Мильва Серебряная Стрела", "Варрен Дуб",
         "Ястреб из Дол Блатанны", "Иннира Ночная Роса", "Барклай Эльс", "Ольгерд Рыжебородый", "Сиана Лунный Луч",
         "Терн аэп Маэль"),
        ("Эльф, Солдат", "Эльф, Лучник", "Краснолюд, Солдат", "Дриада", "Эльф, Чародейка", "Краснолюд, Кузнец",
         "Гном, Инженер", "Эльф, Командир"),
        "о свободе Старших Народов, мести людям, лесе и древней памяти эльфов",
        ("Лес помнит каждого, кто рубил его деревья.", "Мы были здесь первыми. Мы будем здесь последними.",
         "Стрела не спрашивает, кто прав.", "Свобода пахнет хвоей и кровью.",
         "Люди называют это засадой. Мы — справедливостью.", "Va fail, dh'oine. Лес тебя не отпустит."),
        (
            "Гармония: усильте этот отряд на 1 ед.",
            "Засада: когда противник сыграет отряд, переверните эту карту и нанесите ему 4 ед. урона.",
            "Размещение: переместите вражеский отряд в другой ряд и нанесите ему 2 ед. урона.",
        ),
    ),
    "Neutral": Faction(
        "🐺 Нейтральная", "Нейтральная",
        "Ведьмаки, чародеи, наёмники и бродяги — без знамени и без хозяина.",
        (
            "a dim medieval tavern, heavy oak tables, candles, ale barrels and blurry patrons in the background",
            "a misty crossroads with an old signpost, a gallows and a dark forest behind",
            "the snowy ruins of an ancient witcher keep high in the mountains, broken walls and a cold wind",
            "a sorcerer's tower laboratory, floating books, glowing potions and a swirling portal",
            "a monster's nest in a burned barn, claw marks and torchlight, a contract notice nailed to a post",
            "a wide river valley with a ferry and a wooden watermill in the rain",
        ),
        "Color palette: dark leather browns, silver, muted greys, warm orange firelight.",
        "Размещение, Приказ, Обречённость, Рвение, Кровотечение, Щит, Усиление",
        ("Ламберт Злоязыкий", "Эскель Терпеливый", "Весемир Старший", "Странник из Ривии", "Золтан Хивай",
         "Лютик Певчий", "Ведьмак Аркас", "Чародейка Илзе", "Бродяга Пётр Седой", "Наёмница Ворона",
         "Мастер Лавк", "Охотник Брам", "Знахарка Морена", "Кот из Хаэрн Кадух", "Безымянный Странник"),
        ("Ведьмак", "Человек, Чародей", "Человек, Наёмник", "Краснолюд, Странник", "Человек, Бард",
         "Человек, Охотник", "Мутант", "Человек, Знахарь"),
        "о ремесле ведьмака, дороге, нейтралитете, монетах за контракт и выборе меньшего зла",
        ("Сталь — для людей, серебро — для чудовищ.", "Меньшее зло всё равно остаётся злом.",
         "Плати вперёд — и я убью твоё чудовище.", "Дорога длинная, а монет всегда мало.",
         "Мне всё равно, чьё знамя. Важно, чей кошель.", "Мутации не убивают чувства. Только прячут."),
        (
            "Размещение: нанесите 4 ед. урона вражескому отряду.",
            "Приказ: усильте союзный отряд на 3 ед.",
            "Размещение: наложите *Кровотечение* на 3 хода на вражеский отряд.",
        ),
    ),
}
FACTION_BY_NAME = {f.name: f for f in FACTIONS.values()}

# Внешность персонажа для арта: (мужские варианты, женские варианты), выбирается случайно.
ATTIRE = {
    "Nilfgaard": (
        ("an imperial Nilfgaardian officer in ornate black enamelled plate armor with gold filigree, a golden sun "
         "emblem on the breastplate and a heavy black cloak with a high collar",
         "a Nilfgaardian imperial spy in a tailored black-and-gold silk doublet, black leather gloves and a thin rapier",
         "an imperial battle mage in long black robes embroidered with golden suns, holding a staff with a glowing crystal"),
        ("a female imperial officer in fitted black enamelled plate armor with gold filigree, a golden sun emblem "
         "and a black cloak with a high collar",
         "a Nilfgaardian noblewoman in an elegant high-collared black-and-gold gown with a golden sun brooch",
         "an imperial sorceress in flowing black robes embroidered with golden suns, a glowing crystal in her hand"),
    ),
    "Monsters": (
        ("a terrifying monstrous being with pale veined skin, feral glowing eyes and bestial features, wearing grim "
         "armor of bone, chitin and rotten leather adorned with skulls",
         "a Wild Hunt warrior in spiked black frost-covered armor with a skull-like helmet held under the arm",
         "an ancient vampire lord with deathly pale skin, red eyes and fangs, in a tattered dark velvet coat"),
        ("a vampire noblewoman with deathly pale skin, red eyes and fangs, in a tattered dark crimson gown",
         "a swamp witch with bark-like skin, wild hair tangled with twigs and bones, wrapped in rotten rags and charms",
         "a ghostly noonwraith woman in a torn white dress, pale glowing skin and hollow burning eyes"),
    ),
    "Skellige": (
        ("a rugged Skellige island warrior with braided hair and war paint, wearing bear-fur mantles, chainmail and "
         "leather armor carved with Celtic knots and runes, holding an axe",
         "a Skellige raider with a shaved head and runic tattoos, a wolf pelt over the shoulders and a round wooden shield",
         "a Skellige druid in rough grey robes with a staff of driftwood, bone amulets and braided beard"),
        ("a fierce Skellige shieldmaiden with long braided hair and war paint, a fur mantle, chainmail and a round "
         "painted shield, an axe in her hand",
         "a Skellige priestess of Freya in white and blue robes with a silver torc and a crown of feathers",
         "a Skellige huntress in furs and leather with a longbow, runic tattoos on her arms and wind-blown braids"),
    ),
    "Northern": (
        ("a noble Northern knight in polished steel plate armor with a blue and silver heraldic tabard, a long sword "
         "and a kite shield with a royal coat of arms",
         "a Northern king in a golden crown, an ermine-trimmed royal blue mantle over gilded armor",
         "a Temerian Blue Stripes commando in a padded gambeson, chainmail, a blue striped tabard and a crossbow"),
        ("a female Northern knight in fitted polished plate armor with a blue and silver heraldic tabard, a long sword "
         "and a kite shield",
         "a Northern queen in a jewelled crown and an ermine-trimmed royal blue gown",
         "a Northern sorceress in an elegant blue and silver dress with a lily brooch, magic glowing at her fingertips"),
    ),
    "Syndicate": (
        ("a Novigrad street thug in a patched leather jerkin over a dirty linen shirt, a hood, crude tattoos and knife "
         "scars, brass knuckles and daggers tucked into his belt, cheap tarnished rings",
         "a sly smuggler in a long weathered greatcoat, a battered tricorn hat, a pistol-crossbow and pouches of contraband",
         "a twitchy fisstech addict with sunken bloodshot eyes, ragged layered clothes, a grimy scarf and a stolen purse"),
        ("a Novigrad courtesan in a laced corset dress with a loose silk shawl, kohl-lined eyes, red lips, cheap pearls "
         "and a hidden stiletto in her sleeve",
         "a female cutpurse in a short hooded cloak and a fitted leather bodice, lockpicks and throwing knives on her belt",
         "a tough female gang leader with an eyepatch, a man's long coat over a ruffled shirt and gold teeth"),
    ),
    "Scoiatael": (
        ("an elven guerilla fighter with sharp features and pointed ears, forest camouflage, green and brown leather "
         "armor decorated with leaves and feathers, a longbow",
         "a stocky dwarven warrior with a long braided beard, heavy riveted armor and a great war hammer",
         "an elven commander with a squirrel-tail pin, a green hood, twin curved blades and a scarred face"),
        ("an elven huntress with sharp features and pointed ears, green and brown leather armor decorated with leaves "
         "and feathers, a longbow drawn",
         "a dryad of Brokilon with bark-like tattoos on her skin, leaves woven in her hair and a living-wood bow",
         "an elven sorceress in a flowing pale green gown with silver leaf jewellery, pointed ears and glowing hands"),
    ),
    "Neutral": (
        ("a hardened witcher with scars and cat-like eyes, worn dark leather armor with steel studs, a silver wolf "
         "medallion and two swords over the shoulder",
         "a grizzled mercenary in mismatched plate and chainmail, a battered helmet under the arm and a broadsword",
         "a wandering bard in a feathered hat and a colorful slashed doublet, holding a lute"),
        ("a female witcher with scars and cat-like eyes, fitted dark leather armor, a silver medallion and two swords "
         "over the shoulder",
         "a powerful sorceress in an elegant black and white dress with an obsidian star necklace, magic swirling around",
         "a female mercenary in worn leather and chainmail with a crossbow and a scarred, determined face"),
    ),
}

ART_POSES = (
    "standing in a three-quarter view, one hand resting on the pommel of a sheathed sword",
    "raising a weapon in a battle-ready stance",
    "arms crossed over the chest, looking down at the viewer with confidence",
    "glancing over the shoulder with a wary look",
    "holding a flickering torch that lights the face from below",
    "gripping a weapon with both hands, leaning slightly forward",
    "with a cloak billowing in the wind, looking into the distance",
    "pointing forward as if commanding troops into battle",
    "kneeling on one knee, leaning on a planted sword",
    "walking toward the viewer through smoke and embers",
    "drawing a blade from its scabbard",
    "seated on a rough wooden throne, chin resting on a fist",
)
ART_MOODS = (
    "at dusk under a dramatic orange sky", "in heavy rain", "in drifting snowfall", "in thick rolling fog",
    "under a stormy sky with lightning", "at golden hour", "by moonlight", "amid swirling embers and smoke",
    "at cold grey dawn", "in a sudden gust of wind with flying leaves",
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
    send_screen(
        chat_id, "gate",
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
    send_screen(
        chat_id, "topup",
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
# 2б. КАРТИНКИ ЛАВКА НА КАЖДОМ ЭКРАНЕ
# ==========================================
# Рисуются один раз из админки (Fal Kontext по образцу WELCOME_IMAGE) и лежат в постоянном хранилище.
SCENE_DIR = os.path.join(os.path.dirname(DB_PATH) or BASE_DIR, "scenes")
CAPTION_LIMIT = 1000   # у Telegram 1024 символа на подпись к фото, оставляем запас
SCENES = {
    "gate": ("Вход в пещеру", "standing in the entrance of his cave with a raised palm, blocking the way, stern but friendly"),
    "create": ("Создание", "holding up an empty ornate wooden picture frame toward the viewer as if asking for a portrait"),
    "faction": ("Фракция", "standing in front of seven hanging war banners: black with a golden sun, blue with golden lilies, "
                           "green with leaves, purple with gold coins, dark red with a skull, sea-blue with an anchor, grey with a wolf"),
    "name": ("Имя", "writing with a long quill on an unrolled parchment scroll"),
    "category": ("Категория", "studying a huge old bestiary book full of drawings of races and creatures"),
    "power": ("Сила", "lifting a heavy two-handed sword with a proud grin, flexing"),
    "armor": ("Броня", "knocking with his knuckles on a battered steel kite shield"),
    "provision": ("Наём", "sitting at a table with bread, cheese, a ham and a tankard of ale, counting the food"),
    "frame": ("Редкость", "holding an ornate golden card frame in one hand and a bronze one in the other, comparing them"),
    "ability": ("Способность", "casting glowing magical runes from his fingertips"),
    "quote": ("Цитата", "reading aloud from an old book by candlelight with one finger raised"),
    "spirits": ("Духи", "gazing into a glowing crystal ball surrounded by faint ghostly spirits"),
    "wait": ("Рисует", "painting energetically at a wooden easel with a brush and palette, paint splashes around"),
    "error": ("Ошибка", "scratching his head over a knocked-over paint pot and a ruined canvas"),
    "deck": ("Колода", "shuffling a deck of Gwent cards at a tavern table, cards fanned out"),
    "profile": ("Профиль", "looking at himself in a tall ornate mirror and adjusting his collar"),
    "topup": ("Пополнение", "holding an open leather pouch overflowing with glowing golden stars"),
    "pouch": ("Звёзды получены", "happily tossing glowing golden stars into the air from a full pouch"),
    "invite": ("Друзья", "waving welcomingly at the entrance of his cave, inviting friends in"),
    "promo": ("Промокод", "holding a letter with a glowing magical wax seal, breaking it open"),
    "support": ("Поддержка", "holding a warm glowing lantern and offering a helping hand"),
    "terms": ("Условия", "holding up a long parchment of rules with a wax seal"),
    "edit": ("Правка", "retouching a painting with a fine brush and a magnifying glass"),
    "cancel": ("Отмена", "rolling up a canvas with a disappointed sigh"),
    "confused": ("Не понял", "shrugging with a puzzled face, a question mark of smoke above his head"),
    "admin": ("Админка", "sitting on a rough stone throne in his cave like a king, holding a goblet"),
}
_scene_file_ids: dict[tuple, str] = {}
_scene_lock = threading.Lock()


def scene_path(key: str) -> str:
    return os.path.join(SCENE_DIR, f"{key}.jpg")


def scene_source(key: str) -> Optional[str]:
    for path in (scene_path(key), os.path.join(BASE_DIR, "assets", "lavk", f"{key}.jpg")):
        if os.path.exists(path):
            return path
    return WELCOME_IMAGE if os.path.exists(WELCOME_IMAGE) else None


def is_photo(message) -> bool:
    return getattr(message, "content_type", None) == "photo"


SCENE_CACHE = os.path.join(SCENE_DIR, "file_ids.json")
SCENE_UPLOAD_TIMEOUT = 180  # фоновая загрузка картинки в Telegram (пользователь её не ждёт)


def _load_scene_cache():
    try:
        with open(SCENE_CACHE, encoding="utf-8") as f:
            return {tuple(json.loads(k)): v for k, v in json.load(f).items()}
    except (OSError, ValueError, TypeError):
        return {}


def _save_scene_cache():
    try:
        os.makedirs(SCENE_DIR, exist_ok=True)
        tmp = SCENE_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({json.dumps(list(k)): v for k, v in _scene_file_ids.items()}, f)
        os.replace(tmp, SCENE_CACHE)
    except OSError:
        logger.exception("Не удалось сохранить кэш картинок экранов")


def scene_upload_bytes(src: str) -> bytes:
    """Картинка экрана для загрузки: до 960 px и ~100–150 КБ — быстро даже по медленному каналу до Telegram."""
    with Image.open(src) as img:
        img = img.convert("RGB")
        img.thumbnail((960, 960), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=82, optimize=True)
        return out.getvalue()


def _scene_key(src: str) -> tuple:
    return (src, int(os.path.getmtime(src)))


def cached_scene_id(src: str) -> Optional[str]:
    with _scene_lock:
        if not _scene_file_ids:
            _scene_file_ids.update(_load_scene_cache())
        return _scene_file_ids.get(_scene_key(src))


def forget_scene(src: str):
    with _scene_lock:
        if _scene_file_ids.pop(_scene_key(src), None):
            _save_scene_cache()


def send_screen(chat_id: int, key: str, text: str, reply_markup=None):
    """Экран с картинкой Лавка сверху.

    Картинка уходит только по file_id — это мгновенно. Пока картинка ещё не загружена в Telegram,
    экран показывается текстом, а загрузка идёт в фоне через админа (warm_scenes). Так пользователь
    не ждёт медленной загрузки и не получает один и тот же экран дважды.
    """
    src = scene_source(key)
    file_id = cached_scene_id(src) if src else None
    if not file_id:
        if src:
            warm_scenes_async()
        return bot.send_message(chat_id, text, reply_markup=reply_markup)
    long_text = len(text) > CAPTION_LIMIT
    caption, markup = (None, None) if long_text else (text, reply_markup)
    try:
        msg = bot.send_photo(chat_id, file_id, caption=caption, reply_markup=markup)
    except apihelper.ApiTelegramException as e:
        # file_id устарел или не подходит — забываем его и показываем текст.
        logger.warning("Картинка экрана %s не отправилась: %s", key, e)
        forget_scene(src)
        warm_scenes_async()
        return bot.send_message(chat_id, text, reply_markup=reply_markup)
    except requests.ConnectionError:
        # До Telegram запрос не дошёл — повтор текстом дубля не создаст.
        return bot.send_message(chat_id, text, reply_markup=reply_markup)
    # При ReadTimeout сообщение, скорее всего, уже доставлено — второй раз не шлём (ошибка уйдёт в лог).
    if long_text:
        return bot.send_message(chat_id, text, reply_markup=reply_markup)
    return msg


_warm_lock = threading.Lock()
_warm_pending = threading.Event()


def scenes_warming() -> bool:
    """True, пока не все картинки экранов загружены в Telegram (первые минуты после пересборки)."""
    sources = {scene_source(k) for k in list(SCENES) + ["welcome"]} - {None}
    return any(not cached_scene_id(src) for src in sources)


def warm_scenes_async():
    _warm_pending.set()
    if not _warm_lock.locked():
        threading.Thread(target=warm_scenes, daemon=True).start()


def warm_scenes():
    """Загружает в Telegram картинки экранов, у которых ещё нет file_id, через чат админа."""
    if not _warm_lock.acquire(blocking=False):
        return
    try:
        while _warm_pending.is_set():
            _warm_pending.clear()
            sources = {scene_source(k) for k in list(SCENES) + ["welcome"]} - {None}
            for src in sorted(sources):
                if cached_scene_id(src):
                    continue
                uploaded = False
                for admin_id in ADMIN_IDS:
                    try:
                        raw = scene_upload_bytes(src)
                        source = photo_source(raw, "scene.jpg")
                        try:
                            msg = bot.send_photo(admin_id, source, disable_notification=True,
                                                 timeout=SCENE_UPLOAD_TIMEOUT)
                        except apihelper.ApiTelegramException as e:
                            if not isinstance(source, str) or not is_url_fetch_error(e):
                                raise
                            msg = bot.send_photo(admin_id, raw, disable_notification=True,
                                                 timeout=SCENE_UPLOAD_TIMEOUT)
                    except Exception as e:
                        logger.warning("Не удалось загрузить картинку %s через админа %s: %s", src, admin_id, e)
                        continue
                    with _scene_lock:
                        _scene_file_ids[_scene_key(src)] = msg.photo[-1].file_id
                        _save_scene_cache()
                    safe_call(bot.delete_message, admin_id, msg.message_id)
                    uploaded = True
                    break
                if not uploaded:
                    logger.warning("Картинка %s пока без file_id — экраны с ней идут текстом", src)
    finally:
        _warm_lock.release()


def edit_screen(chat_id: int, message, text: str, reply_markup=None):
    """Меняет текст экрана: у сообщения с картинкой — подпись, у текстового — сам текст."""
    message_id = message.message_id
    if is_photo(message):
        return safe_call(bot.edit_message_caption, text, chat_id, message_id, reply_markup=reply_markup)
    return safe_call(bot.edit_message_text, text, chat_id, message_id, reply_markup=reply_markup)


def draw_scene(key: str) -> None:
    """Рисует картинку экрана по образцу приветственной картинки Лавка."""
    if not os.path.exists(WELCOME_IMAGE):
        raise FalError(f"нет образца {os.path.basename(WELCOME_IMAGE)} рядом с main.py")
    with open(WELCOME_IMAGE, "rb") as f:
        reference = to_data_uri(f.read(), "image/jpeg")
    action = SCENES[key][1]
    art = fal_with_retry(FAL_EDIT_ENDPOINT, lambda: {
        "prompt": (f"Keep this exact character — the same face, hair, beard, body, clothes and painting style. "
                   f"Now show him {action}. Cozy candle-lit cave workshop of a painter in the world of The Witcher, "
                   "hand-painted Gwent card art style, warm light, detailed background."),
        "image_url": reference,
        "aspect_ratio": "4:3",
        "guidance_scale": 3.5,
        "output_format": "jpeg",
    })
    os.makedirs(SCENE_DIR, exist_ok=True)
    tmp = scene_path(key) + ".tmp"
    with open(tmp, "wb") as f:
        f.write(art)
    os.replace(tmp, scene_path(key))
    warm_scenes_async()

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
_gemini_blocked: dict[str, float] = {}  # модель -> до какого времени не трогаем
_gemini_lock = threading.Lock()


def _retry_delay(e: Exception) -> float:
    """Через сколько секунд Google разрешает повторить (из ответа 429)."""
    m = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", str(e))
    return float(m.group(1)) if m else 60.0


def gemini_generate(contents, config) -> Optional[str]:
    """Один ответ Gemini: перебирает модели, пропуская те, у которых исчерпан лимит.

    Модель с исчерпанной квотой отключается до сброса лимита — бот не тратит время
    на заведомо неудачные запросы. None — ни одна модель сейчас не доступна.
    """
    if gemini_client is None:
        return None
    for model in GEMINI_MODELS:
        with _gemini_lock:
            if time.time() < _gemini_blocked.get(model, 0):
                continue
        for attempt in (1, 2):
            try:
                response = gemini_client.models.generate_content(model=model, contents=contents, config=config)
                text = (response.text or "").strip()
                if text:
                    return text
                break  # пустой ответ — пробуем следующую модель
            except Exception as e:
                code = getattr(e, "code", None)
                if code == 429:
                    delay = _retry_delay(e)
                    with _gemini_lock:
                        _gemini_blocked[model] = time.time() + delay
                    logger.warning("Gemini %s: лимит исчерпан, отключаю на %s — пробую запасную модель",
                                   model, time.strftime("%H:%M:%S", time.gmtime(delay)) if delay < 86400 else "сутки")
                    break
                if code == 404:
                    # Такой модели нет (или нет у этого ключа) — больше не пробуем.
                    with _gemini_lock:
                        _gemini_blocked[model] = time.time() + 6 * 3600
                    logger.warning("Gemini %s недоступна: %s", model, str(e)[:200])
                    break
                if code in (400, 403):
                    logger.warning("Gemini %s отклонил запрос (%s): %s", model, code, str(e)[:200])
                    break
                if code == 504 or "timeout" in type(e).__name__.lower():
                    # Модель не уложилась в таймаут — повтор потратит ещё столько же, сразу берём следующую.
                    logger.warning("Gemini %s не ответил вовремя — пробую следующую модель", model)
                    break
                logger.warning("Ошибка Gemini %s (попытка %s/2): %s", model, attempt, str(e)[:300])
                if attempt == 1:
                    time.sleep(2)
    logger.warning("Gemini: все модели сейчас недоступны, обхожусь без него")
    return None


def gemini_raw(prompt, temperature: float, json_mode: bool = False) -> Optional[str]:
    """Текст ответа Gemini. None — не вышло."""
    config = genai_types.GenerateContentConfig(
        temperature=temperature, response_mime_type="application/json" if json_mode else None)
    return gemini_generate(prompt, config)


def ask_gemini(prompt: str, keep_markup: bool = False, temperature: float = 1.2) -> Optional[str]:
    text = gemini_raw(prompt, temperature)
    if text is None:
        return None
    text = text.strip('"«»')
    text = text.replace("**", "*") if keep_markup else text.replace("*", "")
    return text or None


def parse_json(text: Optional[str]):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        return json.loads(text)
    except ValueError:
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
    prompt = step.ai_prompt.format(
        faction=faction.name, mechanics=faction.mechanics, quote_theme=faction.quote_theme,
        # Случайная выборка примеров каждый раз — иначе Gemini крутится вокруг одних и тех же имён.
        names=", ".join(random.sample(faction.names, 4)),
        categories=", ".join(random.sample(faction.categories, 3)),
        quotes=" / ".join(random.sample(faction.quotes, 2)),
    )
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
    own_fallbacks = {"ability": faction.fallbacks, "name": faction.names, "category": faction.categories,
                     "quote": faction.quotes}
    return random.choice(own_fallbacks.get(step.key) or step.fallbacks)


STEPS = {
    STATE_NAME: Step(
        "name", "📜 Имя",
        "📜 <b>Как тебя звать-величать?</b>\n\n"
        "Имя пишется крупно в шапке карты. Можно настоящее, можно с прозвищем или титулом — как у героев Гвинта.\n"
        "<i>Например: «Кирилл Беспощадный», «Старый Волк», «Ярл Бьорн Седобородый», «Мастер Лавк».</i>\n"
        "До 35 символов.",
        text_validator(35), STATE_CATEGORY,
        ai_prompt=("Придумай новое имя для персонажа карты Гвинта за фракцию {faction}. Оно должно звучать "
                   "как имена этой фракции, например: {names}. Можно с прозвищем или титулом. "
                   "Не повторяй примеры и не бери известных персонажей. 1-4 слова, до 35 символов. Только имя."),
        fallbacks=("Грозный Воин", "Старый Волк"),
    ),
    STATE_CATEGORY: Step(
        "category", "🧬 Категория",
        "🧬 <b>Ты вообще кто такой? (Категория)</b>\n\n"
        "Мелкая строка под именем: раса и ремесло через запятую, 1–3 слова.\n"
        "<i>Например: «Человек, Солдат», «Эльф, Лучник», «Краснолюд, Кузнец», «Ведьмак», «Вампир», "
        "«Человек, Чародей».</i>",
        text_validator(30, max_words=3), STATE_POWER,
        ai_prompt=("Придумай категорию для карты Гвинта за фракцию {faction} в формате «Раса, Ремесло», "
                   "как у этой фракции, например: {categories}. 1-3 слова. Только категория."),
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
        ai_prompt=("Сгенерируй новую цитату персонажа для карты Гвинт за фракцию {faction} — {quote_theme}. "
                   "В духе мира Ведьмака, мрачно или с чёрным юмором, как: {quotes}. "
                   "Не повторяй примеры. Максимум 20 слов. Без кавычек."),
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
        send_screen(
            chat_id, "frame",
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
        send_screen(chat_id, step.key, prefix + step.prompt, reply_markup=gen_ai_markup(step.key))


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
    if scenes_warming():
        text += ("\n\n🕯 <i>Лавк только проснулся и развешивает картины по пещере — первые минуты часть экранов "
                 "может прийти без картинок. Бот при этом работает как обычно!</i>")
    send_screen(chat_id, "welcome", text, reply_markup=get_main_menu(chat_id))


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
    send_screen(chat_id, "profile", text, reply_markup=markup)


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
    send_screen(chat_id, "invite", text, reply_markup=markup)


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
    send_screen(
        chat_id, "topup",
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
    send_screen(chat_id, "promo", "🎟 <b>Впиши промокод</b> — Лавк проверит, настоящий ли он.\n\n"
                "<i>Промокоды Лавк раздаёт в своём канале и на праздники.</i>", reply_markup=get_cancel_menu())


def show_support(chat_id: int):
    send_screen(
        chat_id, "support",
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


def show_deck(chat_id: int, page: int = 0, message=None):
    """message — экран колоды, который можно перелистнуть на месте (кнопки ◀️ ▶️)."""
    total, markup = deck_markup(chat_id, page)
    if not total:
        send_screen(chat_id, "deck", "📜 <b>Твоя колода пуста, человече!</b>\n\n"
                                     f"Жми «{BTN_CREATE}» — и Лавк нарисует первую картищу.")
        return
    rank, _ = rank_info(total)
    text = (f"📜 <b>Твоя колода</b> — {plural_cards(total)}\n🏅 {rank}\n\n"
            "Выбирай картищу, чтобы посмотреть или заказать правку:")
    if message is not None and edit_screen(chat_id, message, text, reply_markup=markup) is not None:
        return
    send_screen(chat_id, "deck", text, reply_markup=markup)


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
    send_screen(
        message.chat.id, "support",
        "💳 <b>Вопросы по оплате</b>\n\n"
        f"Если звёзды списались, а мешочек не пополнился, или нужен возврат — напиши {SUPPORT_CONTACT} "
        f"и укажи свой ID <code>{message.chat.id}</code>. Лавк разберётся!",
    )


@bot.message_handler(commands=["terms"])
def cmd_terms(message):
    send_screen(
        message.chat.id, "terms",
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
    send_screen(chat_id, "wait" if chat_id in busy_users else "cancel", text, reply_markup=get_main_menu(chat_id))


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
        send_screen(chat_id, "wait", "⏳ Лавк ещё рисует твою прошлую картищу, подожди!")
        return
    if not check_sub(message.from_user.id):
        send_sub_request(chat_id)
        return
    if not can_afford(chat_id, GENERATION_PRICE) and not has_trial(chat_id):
        send_not_enough(chat_id, GENERATION_PRICE)
        return
    user_data[chat_id] = {}
    user_states[chat_id] = STATE_PHOTO
    send_screen(
        chat_id, "create",
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
        send_screen(chat_id, "topup", f"✍️ <b>Сколько звёзд насыпать?</b>\n\nВпиши число от {TOPUP_MIN} до {TOPUP_MAX}. "
                    "Своя сумма зачисляется один к одному — выгоднее брать наборы.", reply_markup=get_cancel_menu())
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
    send_screen(
        chat_id, "pouch",
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
    about = "\n".join(f"<b>{f.button}</b> — {f.about}" for f in FACTIONS.values())
    send_screen(
        chat_id, "faction",
        "🏴 <b>За кого воевать будешь?</b>\n"
        "<i>От фракции зависят шапка карты, наряд и фон арта, имена и способности.</i>\n\n"
        f"{about}\n\n<b>Выбирай знамя, человече!</b>",
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
    edit_screen(chat_id, call.message, f"✅ Фракция: <b>{esc(faction.button)}</b>\n<i>{esc(faction.about)}</i>")
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
    edit_screen(chat_id, call.message, f"✅ Редкость: <b>{esc(FRAMES[frame_key])}</b>")
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
    edit_screen(chat_id, call.message, random.choice(SPIRIT_PHRASES))
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
        send_screen(chat_id, "cancel", "Эх, порвали холст! Начинай заново, когда будешь готов.",
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
        # Листаем на месте только сам экран колоды; из карточки картищи — новое сообщение.
        # Старые (>48 ч) сообщения приходят как InaccessibleMessage без текста.
        shown = getattr(call.message, "caption", None) or getattr(call.message, "text", None) or ""
        show_deck(chat_id, int(value), call.message if shown.startswith("📜 Твоя колода") else None)
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
        markup = card_markup(card, with_deck=True)
        if card["file_id"]:
            bot.send_photo(chat_id, card["file_id"], caption=caption, reply_markup=markup)
            return
        # Доставка не была подтверждена — собираем карту заново из сохранённого арта.
        with RENDER_LOCK:
            full, _ = render_card(load_art(card), card["data"], trial=bool(card["data"].get("trial")))
        src = photo_source(full)
        try:
            msg = tg_retry(bot.send_photo, chat_id, src, caption=caption, reply_markup=markup,
                           timeout=MEDIA_TIMEOUT)
        except apihelper.ApiTelegramException as e:
            if not isinstance(src, str) or not is_url_fetch_error(e):
                raise
            msg = tg_retry(bot.send_photo, chat_id, full, caption=caption, reply_markup=markup,
                           timeout=MEDIA_TIMEOUT)
        db.set_card_file(card["id"], msg.photo[-1].file_id)
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
    quick = types.InlineKeyboardMarkup(row_width=1)
    quick.add(*[types.InlineKeyboardButton(label, callback_data=f"editq:{key}") for key, (label, _) in EDIT_QUICK.items()])
    quick.add(types.InlineKeyboardButton("❌ Отмена", callback_data="editno"))
    send_screen(
        chat_id, "edit",
        f"✏️ <b>Правка картищи «{esc(card['data'].get('name', ''))}»</b>\n\n"
        "Напиши Лавку, что поправить, — можно менять и героя, и фон. Например:\n"
        "<i>• «смени фон на заснеженный горный перевал»\n"
        "• «пусть за спиной горит деревня»\n"
        "• «добавь шрам на щеку и капюшон»\n"
        "• «дай в руки огненный меч»</i>\n\n"
        "Или выбери готовую правку кнопкой. При смене фона герой остаётся в точности как был, "
        "а лицо духи меняют, только если ты сам об этом попросишь.",
        reply_markup=quick,
    )


# Готовые правки — все меняют только фон, герой остаётся пиксель в пиксель. {bg} — случайный фон фракции.
EDIT_QUICK = {
    "bg": ("🌄 Новый фон в духе фракции", "{bg}."),
    "night": ("🌙 Ночная сцена при луне",
              "The same kind of place at night under a full moon, dark blue sky, cold moonlight and deep shadows."),
    "storm": ("⛈ Буря и молнии", "A raging storm: black clouds, forks of lightning, driving rain and wind."),
    "fire": ("🔥 Пожар и искры", "A burning battlefield: flames, smoke and flying embers glowing orange."),
}


def ask_edit_confirm(chat_id: int, text: str, preset: Optional[tuple] = None):
    user_data[chat_id]["edit_prompt"] = text
    user_data[chat_id]["edit_preset"] = preset
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


@bot.callback_query_handler(func=lambda call: call.data.startswith("editq:"))
def cb_edit_quick(call):
    chat_id = call.message.chat.id
    key = call.data.split(":", 1)[1]
    if user_states.get(chat_id) != STATE_EDIT_PROMPT or key not in EDIT_QUICK:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    safe_call(bot.answer_callback_query, call.id)
    card = db.get_card(user_data[chat_id]["edit_card_id"], chat_id)
    if card is None:
        reset_user(chat_id)
        return
    faction = FACTION_BY_NAME.get(card["data"].get("faction"), FACTIONS["Neutral"])
    label, scene = EDIT_QUICK[key]
    safe_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=None)
    ask_edit_confirm(chat_id, label, ("background", scene.format(bg=random.choice(faction.backgrounds))))


@bot.message_handler(func=lambda msg: user_states.get(msg.chat.id) == STATE_EDIT_PROMPT)
def process_edit_prompt(message):
    chat_id = message.chat.id
    text = message.text.strip()
    error = text_validator(300, min_len=3)(text)
    if error:
        bot.send_message(chat_id, error)
        return
    ask_edit_confirm(chat_id, text)


@bot.callback_query_handler(func=lambda call: call.data in ("editok", "editno"))
def cb_edit_confirm(call):
    chat_id = call.message.chat.id
    allowed = (STATE_EDIT_CONFIRM, STATE_EDIT_PROMPT) if call.data == "editno" else (STATE_EDIT_CONFIRM,)
    if user_states.get(chat_id) not in allowed:
        safe_call(bot.answer_callback_query, call.id, "Эта кнопка уже не работает.")
        return
    if call.data == "editno":
        safe_call(bot.answer_callback_query, call.id)
        reset_user(chat_id)
        safe_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=None)
        send_screen(chat_id, "cancel", "Эх, отложили кисть. Правку можно заказать в любой момент из «Моих картищ».",
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
    preset = user_data[chat_id].get("edit_preset")
    reset_user(chat_id)
    busy_users.add(chat_id)
    safe_call(bot.edit_message_reply_markup, chat_id, call.message.message_id, reply_markup=None)
    bot.send_message(chat_id, f"✅ <b>Правка принята:</b> {esc(instruction)}", reply_markup=get_main_menu(chat_id))
    threading.Thread(target=edit_card, args=(chat_id, card, instruction, "paid" if paid else "free", preset),
                     daemon=True).start()


# ==========================================
# 13. ГЕНЕРАЦИЯ И ПРАВКА АРТА (Fal.ai)
# ==========================================
class FalError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def to_data_uri(image_bytes: bytes, mime: str) -> str:
    # Fal принимает base64 data URI в полях-файлах, отдельная загрузка в storage не нужна.
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def http_retry(method: str, url: str, attempts: int = 3, **kwargs) -> requests.Response:
    """Повтор при обрыве сети, 429 и 5xx — самые частые причины «внезапных» сбоев генерации."""
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            if attempt == attempts:
                raise FalError("нет связи с художником", retryable=True) from e
            logger.warning("Fal.ai: сеть (%s), повтор %s/%s", e.__class__.__name__, attempt, attempts)
        else:
            if response.status_code != 429 and response.status_code < 500:
                return response
            if attempt == attempts:
                return response
            logger.warning("Fal.ai: код %s, повтор %s/%s", response.status_code, attempt, attempts)
        time.sleep(3 * attempt)
    raise AssertionError("unreachable")


def is_blank(image: bytes) -> bool:
    """Fal заменяет картинки, не прошедшие фильтр, чёрным квадратом."""
    try:
        with Image.open(io.BytesIO(image)) as img:
            return max(img.convert("L").resize((32, 32)).getextrema()) < 12
    except Exception:
        return True


def request_fal_image(endpoint: str, payload: dict, check_blank: bool = True) -> bytes:
    """Генерация через очередь Fal: запрос не висит на одном соединении, есть общий дедлайн."""
    if not FAL_KEY:
        raise FalError("не задан FAL_API_KEY")
    headers = {"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"}
    response = http_retry("POST", endpoint, json=payload, headers=headers, timeout=60)
    if not response.ok:
        logger.error("Fal.ai %s: %s", response.status_code, response.text[:1000])
        raise FalError(f"Fal.ai вернул код {response.status_code}", retryable=response.status_code >= 500)
    job = response.json()
    deadline = time.monotonic() + FAL_DEADLINE
    while True:
        if time.monotonic() > deadline:
            logger.error("Fal.ai: истёк дедлайн для %s", job.get("request_id"))
            try:
                requests.put(job["cancel_url"], headers=headers, timeout=15)
            except Exception:
                logger.exception("Fal.ai: не удалось отменить запрос")
            raise FalError("художник слишком долго не отвечал", retryable=True)
        time.sleep(2)
        try:
            status = requests.get(job["status_url"], headers=headers, timeout=30).json().get("status")
        except (requests.RequestException, ValueError):
            logger.warning("Fal.ai: не удалось получить статус, повторю")
            continue
        if status == "COMPLETED":
            break
    result = http_retry("GET", job["response_url"], headers=headers, timeout=60)
    try:
        result.raise_for_status()
        body = result.json()
        image_url = fal_image_url(body)
    except (requests.HTTPError, ValueError, KeyError, IndexError, TypeError):
        logger.error("Неожиданный ответ Fal.ai: %s %s", result.status_code, result.text[:1000])
        raise FalError("Fal.ai вернул ответ без картинки", retryable=result.status_code >= 500)
    art = http_retry("GET", image_url, timeout=90)
    if not art.ok:
        raise FalError("не удалось скачать картинку", retryable=True)
    if check_blank and (any(body.get("has_nsfw_concepts") or []) or is_blank(art.content)):
        logger.warning("Fal.ai: картинка отфильтрована (%s)", job.get("request_id"))
        raise FalError("духи забраковали рисунок", retryable=True)
    return art.content


def fal_with_retry(endpoint: str, make_payload: Callable[[], dict]) -> bytes:
    """Одна повторная попытка при временном сбое; payload собирается заново (новая поза, фон и seed)."""
    try:
        return request_fal_image(endpoint, make_payload())
    except FalError as e:
        if not e.retryable:
            raise
        logger.warning("Fal.ai: %s — пробую ещё раз", e)
        return request_fal_image(endpoint, make_payload())


FAL_CDN_URL = "https://v3.fal.media"
_fal_token = {"value": "", "expires": 0.0}
_fal_token_lock = threading.Lock()
_fal_upload_off_until = 0.0  # после сбоя не тратим время на загрузку, сразу шлём байты


def _fal_cdn_auth() -> str:
    """Короткоживущий токен Fal CDN v3 — тот же путь, что у официального fal_client."""
    with _fal_token_lock:
        if _fal_token["value"] and time.time() < _fal_token["expires"] - 60:
            return _fal_token["value"]
        r = requests.post(
            "https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3",
            json={},
            headers={"Authorization": f"Key {FAL_KEY}", "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        try:
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, AttributeError):
            expires = time.time() + 600
        _fal_token["value"] = f"{data['token_type']} {data['token']}"
        _fal_token["expires"] = expires
        return _fal_token["value"]


def fal_upload(data: bytes, content_type: str = "image/jpeg", name: str = "card.jpg") -> Optional[str]:
    """Кладёт файл в Fal CDN и возвращает публичную ссылку. None — не вышло.

    Канал Amvera → Telegram медленный, а до Fal быстрый. Если дать Telegram ссылку,
    он сам скачает картинку с Fal, и с Amvera уйдёт только короткий запрос.
    """
    global _fal_upload_off_until
    if not FAL_KEY or time.time() < _fal_upload_off_until:
        return None
    started = time.monotonic()
    try:
        r = requests.post(
            f"{FAL_CDN_URL}/files/upload",
            data=data,
            headers={"Authorization": _fal_cdn_auth(), "Content-Type": content_type,
                     "X-Fal-File-Name": name},
            timeout=(10, 40),
        )
        if r.status_code in (401, 403):
            with _fal_token_lock:
                _fal_token["value"] = ""
        r.raise_for_status()
        url = r.json()["access_url"]
        logger.info("Картинка %s выложена на Fal за %.1f с", name, time.monotonic() - started)
        return url
    except Exception as e:
        _fal_upload_off_until = time.time() + 600
        logger.warning("Не удалось выложить картинку на Fal (%s) — 10 минут шлю байтами", e)
        return None


def photo_source(data: bytes, name: str = "card.jpg"):
    """Ссылка на картинку (если удалось выложить на Fal) или сами байты как запасной путь."""
    return fal_upload(data, name=name) or data


def is_url_fetch_error(e: Exception) -> bool:
    """Telegram не смог скачать картинку по ссылке — тогда отправляем байты."""
    return (isinstance(e, apihelper.ApiTelegramException) and e.error_code == 400
            and any(k in str(e).lower() for k in ("http url", "url content", "wrong file", "webpage_", "failed to get")))


def not_sent(e: Exception) -> bool:
    """Соединение не установилось — Telegram запрос точно не получил."""
    if isinstance(e, requests.ConnectTimeout):
        return True
    text = repr(e)
    return any(k in text for k in ("NewConnectionError", "NameResolutionError", "Failed to establish",
                                   "Temporary failure in name resolution", "Connection refused"))


def tg_retry(func, *args, attempts: int = 3, idempotent: bool = False, **kwargs):
    """Повтор запроса к Telegram только там, где он не создаст дубль.

    Обрыв после отправки («Connection aborted», 5xx) значит, что фото часто уже доставлено —
    повтор отправки присылал человеку 2–3 одинаковые карты. Такие ошибки повторяем
    только для безопасных запросов (idempotent=True), для отправок — пробрасываем.
    """
    for attempt in range(1, attempts + 1):
        try:
            return func(*args, **kwargs)
        except apihelper.ApiTelegramException as e:
            retryable = e.error_code == 429 or (idempotent and e.error_code >= 500)
            if attempt == attempts or not retryable:
                raise
            delay = int((e.result_json or {}).get("parameters", {}).get("retry_after", 3 * attempt))
        except requests.ConnectionError as e:
            if attempt == attempts or not (idempotent or not_sent(e)):
                raise
            delay = 3 * attempt
        logger.warning("Telegram: повтор %s через %s с", getattr(func, "__name__", "запроса"), delay)
        time.sleep(delay)


def download_telegram_file(file_id: str) -> bytes:
    return tg_retry(lambda: bot.download_file(bot.get_file(file_id).file_path), idempotent=True)


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
    "GWENT: The Witcher Card Game premium card art. Semi-realistic dark fantasy concept art painting, "
    "traditional oil painting look with thick visible brush strokes and painted texture, gritty and weathered, "
    "realistic adult anatomy and proportions, a believable characterful face with natural skin texture, "
    "natural eyes of normal size, muted earthy desaturated colors, dramatic chiaroscuro lighting with strong rim light, "
    "Slavic medieval folklore atmosphere, epic cinematic composition like a Witcher 3 key art. "
)
ART_SCENE = (
    "Waist-up portrait, the character in the lower two thirds, face clearly visible and lit, room above the head. "
    # FLUX лучше слушается конкретного описания рук, чем общего «no extra fingers».
    "Anatomically correct hands: each hand has exactly five fingers — one thumb and four fingers — with natural "
    "knuckles and proportions; hands are relaxed or firmly gripping, partly in shadow or gloved where it suits the outfit. "
    "A fully painted richly detailed environment behind the character fills the whole image with depth, "
    "foreground and background elements, atmospheric perspective, no plain or empty backdrop. "
    # Отрицательный промпт при true_cfg=1 моделью игнорируется, поэтому запреты — прямо в описании.
    "Not anime, not manga, not cartoon, not cel-shaded, no glossy airbrushed skin, no big shiny eyes."
)
ART_NEGATIVE = (
    "extra fingers, six fingers, missing fingers, fused fingers, mutated hands, deformed hands, extra hands, "
    "extra arms, malformed limbs, "
    "anime, manga, cartoon, cel shading, chibi, big eyes, doll face, glossy airbrushed skin, "
    "photograph, 3d render, cgi, modern clothing, "
    "plain background, studio backdrop, white background, empty background, gradient background, "
    "text, letters, watermark, card frame, border, ui"
)


GENDER_TEXT = {
    "female": ("The character is a woman: female face, feminine body and proportions, slender shoulders, "
               "womanly figure, no beard. "),
    "male": "The character is a man with a masculine build. ",
}


def build_art_prompt(data: dict, role: Optional[str], gender: Optional[str]) -> str:
    key = data.get("faction_key") or next((k for k, f in FACTIONS.items() if f.name == data.get("faction")), "Neutral")
    faction = FACTIONS.get(key, FACTIONS["Neutral"])
    male, female = ATTIRE.get(key, ATTIRE["Neutral"])
    # Без пола — мужской вариант с явным «человек», как было раньше; с женским фото — женская одежда и фигура.
    attire = random.choice(female if gender == "female" else male)
    role_text = f" Their trade: {role}." if role and len(role) <= 60 else ""
    # Фон — раньше остального: модель читает начало промпта внимательнее.
    return (
        f"{ART_STYLE}Background: {random.choice(faction.backgrounds)}, {random.choice(ART_MOODS)}. "
        f"{GENDER_TEXT.get(gender, '')}Portrait of {attire}, {random.choice(ART_POSES)}.{role_text} "
        f"{faction.palette} {ART_SCENE}"
    )


# ---------- модели для артов ----------
# PuLID (FLUX.1) заточен под перенос лица, но руки и предметы рисует хуже новых моделей.
# FLUX.2 [pro] и Nano Banana 2 берут фото как образец и рисуют по описанию; лицо держат хуже/лучше —
# поэтому модель выбирается в админке после сравнения на реальных фото.
IDENTITY_PREFIX = (
    "Paint a brand-new dark fantasy portrait of the person from the reference photo. "
    "Keep their exact facial identity so they are clearly recognizable: face shape, eyes, eyebrows, nose, lips, "
    "jawline, hairline, skin tone and apparent age. Do NOT copy the photo's clothes, background, lighting, "
    "camera angle or framing; do not include the photo itself. "
)
ART_MODELS = {
    "pulid": ("FLUX.1 PuLID", "~$0.03", FAL_GENERATE_ENDPOINT),
    "flux2": ("FLUX.2 [pro]", "~$0.03–0.05", "https://queue.fal.run/fal-ai/flux-2-pro/edit"),
    "nb2": ("Nano Banana 2", "~$0.08", "https://queue.fal.run/fal-ai/nano-banana-2/edit"),
}
ART_MODEL_FILE = os.path.join(os.path.dirname(DB_PATH) or BASE_DIR, "art_model.txt")


def current_art_model() -> str:
    try:
        with open(ART_MODEL_FILE, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        key = os.getenv("ART_MODEL", "pulid")
    return key if key in ART_MODELS else "pulid"


def set_art_model(key: str):
    with open(ART_MODEL_FILE, "w", encoding="utf-8") as f:
        f.write(key)


def art_payload(model: str, prompt: str, photo: bytes, mime: str, photo_url: Optional[str]) -> dict:
    seed = random.randint(1, 2 ** 31 - 1)
    if model == "pulid":
        return {
            "prompt": prompt,
            "negative_prompt": ART_NEGATIVE,
            "reference_image_url": to_data_uri(photo, mime),
            "image_size": "portrait_4_3",
            "num_inference_steps": 30,
            # 4.0 — компромисс: ниже даёт живее мазки, но портит анатомию.
            "guidance_scale": 4.0,
            "true_cfg": 1.0,
            "id_weight": 0.8,
            # Лицо подмешивается позже — у модели больше свободы на рисованный стиль (ценой части сходства).
            "start_step": 4,
            # По умолчанию промпт обрезается до 128 токенов, и описание фона терялось.
            "max_sequence_length": "512",
            "seed": seed,
        }
    ref = photo_url or to_data_uri(photo, mime)
    if model == "flux2":
        return {"prompt": IDENTITY_PREFIX + prompt, "image_urls": [ref], "image_size": "portrait_4_3",
                "output_format": "jpeg", "safety_tolerance": "4", "seed": seed}
    return {"prompt": IDENTITY_PREFIX + prompt, "image_urls": [ref], "aspect_ratio": "3:4", "resolution": "1K",
            "num_images": 1, "output_format": "jpeg", "safety_tolerance": "4", "seed": seed}


def generate_art(model: str, prompt: str, photo: bytes, mime: str) -> bytes:
    """Арт выбранной моделью. Фото для новых моделей — ссылкой на Fal CDN (меньше запрос), иначе data URI."""
    photo_url = fal_upload(photo, mime, "reference.jpg") if model != "pulid" else None
    return fal_with_retry(ART_MODELS[model][2], lambda: art_payload(model, prompt, photo, mime, photo_url))


def status_ticker(chat_id: int, status, stop: threading.Event):
    """Раз в 10–15 секунд меняет фразу ожидания, чтобы было видно, что Лавк не завис."""
    last = None
    while not stop.wait(random.uniform(*WAIT_TICK)):
        phrase = random.choice([p for p in LAVK_WAIT_PHRASES if p != last])
        last = phrase
        edit_screen(chat_id, status, phrase + WAIT_INTRO)


# ---------- очередь генераций ----------
# Одновременно у Fal рисуется не больше GEN_SLOTS картищ, остальные ждут своей очереди.
# Ожидание в очереди не съедает дедлайн генерации, и сервер не захлёбывается при наплыве.
GEN_SLOTS = max(1, int(os.getenv("GEN_SLOTS", "4")))
_gen_queue: list[int] = []
_gen_active = 0
_gen_cv = threading.Condition()
# Сборка карты — ~0,25 с и ~60 МБ памяти; по одной, чтобы при наплыве не упереться в память.
RENDER_LOCK = threading.Lock()


def queue_text(place: int) -> str:
    minutes = max(1, round(place * 1.5 / GEN_SLOTS + 1))
    return (f"⏳ <b>У Лавка очередь из желающих!</b>\n\nТы в очереди: <b>{place}-й</b>. "
            f"Примерно через {minutes} мин. Лавк возьмётся за твою картищу.\n"
            "<i>Можно закрыть чат — карта придёт сама, место не пропадёт.</i>")


def wait_for_slot(chat_id: int, status) -> bool:
    """Ждёт свободного художника. True — пришлось постоять в очереди."""
    global _gen_active
    shown, queued = None, False
    with _gen_cv:
        _gen_queue.append(chat_id)
    try:
        while True:
            with _gen_cv:
                if _gen_active < GEN_SLOTS and _gen_queue[0] == chat_id:
                    _gen_queue.pop(0)
                    _gen_active += 1
                    return queued
                place = _gen_queue.index(chat_id) + 1
                if place == shown:
                    _gen_cv.wait(timeout=10)
                    continue
            # Сообщение правим вне блокировки, чтобы не задерживать очередь сетевым запросом.
            shown, queued = place, True
            edit_screen(chat_id, status, queue_text(place))
    except BaseException:
        with _gen_cv:
            if chat_id in _gen_queue:
                _gen_queue.remove(chat_id)
            _gen_cv.notify_all()
        raise


def release_slot():
    global _gen_active
    with _gen_cv:
        _gen_active -= 1
        _gen_cv.notify_all()


def run_art_job(chat_id: int, price: int, mode: str, job: Callable[[object], None]):
    """Общая обвязка: очередь, статус, возврат звёзд при ошибке, снятие флага занятости.

    mode: paid — списаны звёзды, free — безлимит/админ, trial — пробная картища.
    """
    stop = threading.Event()
    status = None
    slot = False
    try:
        with _gen_cv:
            busy = _gen_active >= GEN_SLOTS or bool(_gen_queue)
            place = len(_gen_queue) + 1
        first = queue_text(place) if busy else random.choice(LAVK_WAIT_PHRASES) + WAIT_INTRO
        status = send_screen(chat_id, "wait", first)
        if wait_for_slot(chat_id, status):
            edit_screen(chat_id, status, "🎨 <b>Твоя очередь!</b> " + random.choice(LAVK_WAIT_PHRASES) + WAIT_INTRO)
        slot = True
        threading.Thread(target=status_ticker, args=(chat_id, status, stop), daemon=True).start()
        started = time.monotonic()
        job(status)
        logger.info("Задача для %s выполнена за %.0f с", chat_id, time.monotonic() - started)
    except Exception as e:
        stop.set()
        if isinstance(e, FalError):
            text = f"⚠️ Духи не смогли нарисовать: {esc(e)}"
        else:
            # Текст исключения пользователю не показываем: в URL Telegram-файлов содержится токен бота.
            logger.exception("Ошибка генерации для %s", chat_id)
            text = "⚠️ Системная ошибка связи."
        note = refund(chat_id, price, mode)
        if status is not None:
            safe_call(bot.delete_message, chat_id, status.message_id)
        safe_call(send_screen, chat_id, "error", f"{text}{note}\n\nПопробуй ещё раз чуть позже, человече.",
                  reply_markup=get_main_menu(chat_id))
    finally:
        stop.set()
        if slot:
            release_slot()
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


def deliver_card(chat_id: int, art: bytes, card_data: dict, title: str, status,
                 parent_id: Optional[int] = None) -> int:
    """Собирает карту поверх арта, отправляет полную карту + карту в рамке и сохраняет в колоду."""
    trial = bool(card_data.get("trial"))
    with RENDER_LOCK:
        full, card_only = render_card(art, card_data, trial=trial)
    caption = f"{title}\n«{esc(strip_markup(card_data.get('name', '')))}» · {esc(card_data.get('faction', ''))}"
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            sources = list(pool.map(lambda a: photo_source(*a),
                                    [(full, "card_full.jpg"), (card_only, "card_frame.jpg")]))

        def media(srcs):
            return [types.InputMediaPhoto(srcs[0], caption=caption, parse_mode="HTML"),
                    types.InputMediaPhoto(srcs[1])]

        try:
            sent = tg_retry(bot.send_media_group, chat_id, media(sources), timeout=MEDIA_TIMEOUT)
        except apihelper.ApiTelegramException as e:
            if not any(isinstance(x, str) for x in sources) or not is_url_fetch_error(e):
                raise
            logger.warning("Telegram не скачал карту по ссылке Fal (%s) — отправляю байтами", e)
            sent = tg_retry(bot.send_media_group, chat_id, media([full, card_only]), timeout=MEDIA_TIMEOUT)
        file_id = sent[0].photo[-1].file_id
        logger.info("Карта для %s отправлена в Telegram за %.0f с", chat_id, time.monotonic() - started)
    except (requests.ReadTimeout, requests.ConnectionError) as e:
        # Файлы ушли, но Telegram не успел ответить (или связь оборвалась) — карта почти наверняка уже у человека.
        # Не повторяем (будет дубль) и не возвращаем звёзды: сохраняем в колоду без file_id,
        # из «Моих картищ» она перерисуется из сохранённого арта.
        logger.warning("Telegram не подтвердил доставку карты для %s: %s", chat_id, e.__class__.__name__)
        file_id = ""
    safe_call(bot.delete_message, chat_id, status.message_id)
    card_id = db.add_card(chat_id, card_data, file_id, parent_id=parent_id)
    save_art(card_id, art)
    try:
        if not file_id:
            bot.send_message(chat_id, "📨 Telegram долго принимал картищу. Если она не пришла выше — "
                                      "открой «📜 Мои картищи», она уже там.")
        if trial:
            note = ("🎁 <b>Это пробная картища</b> — поэтому на ней водяные знаки Лавка.\n"
                    f"Чистая картища без надписей — {GENERATION_PRICE}⭐. Пополни мешочек и закажи настоящую!")
        else:
            note = ("🃏 Картища легла в твою колоду — ищи её в «Моих картищах».\n"
                    "<i>Вторая картинка — сама карта в рамке, удобно для аватарки.</i>")
        # Главное меню — обычной клавиатурой, правка (или пополнение) — кнопкой под отдельным сообщением.
        bot.send_message(chat_id, note, reply_markup=get_main_menu(chat_id))
        action = "Пополнить мешочек?" if trial else "Хочешь поменять фон или что-то поправить на арте?"
        bot.send_message(chat_id, f"✏️ {action}", reply_markup=card_markup({"id": card_id, "data": card_data}))
    except Exception:
        # Карта уже доставлена и сохранена — сбой подсказки не должен вызывать возврат звёзд.
        logger.exception("Не удалось отправить подсказку после карты %s", card_id)
    return card_id


def ask_gemini_vision(image: bytes, prompt: str, as_json: bool = False):
    """Вопрос Gemini про картинку. None — Gemini недоступен или ответил непонятно."""
    config = genai_types.GenerateContentConfig(
        temperature=0, response_mime_type="application/json" if as_json else None)
    text = gemini_generate([genai_types.Part.from_bytes(data=image, mime_type="image/jpeg"), prompt], config)
    if text is None:
        return None
    return parse_json(text) if as_json else text


def detect_gender_and_role(photo: bytes, category: str) -> tuple[Optional[str], Optional[str]]:
    """Пол по фото и перевод класса — одним запросом, чтобы экономить бесплатный лимит Gemini."""
    answer = ask_gemini_vision(
        photo,
        "1) Is the main person in this photo a woman or a man? "
        f"2) Translate this fantasy character class into English, 1-4 words: «{category}». "
        'Reply as JSON {"gender": "female" or "male", "role": "<english class>"}.',
        as_json=True,
    )
    if not isinstance(answer, dict):
        return None, None
    gender = str(answer.get("gender", "")).strip().lower()
    role = str(answer.get("role", "")).strip() or None
    if role and CYRILLIC.search(role):
        role = None
    return (gender if gender in ("female", "male") else None), role


def as_jpeg(image: bytes) -> bytes:
    with Image.open(io.BytesIO(image)) as img:
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=92)
        return out.getvalue()


def generate_card(chat_id: int, data: dict, mode: str):
    def job(status):
        photo = download_telegram_file(data["photo_id"])
        gender, role = detect_gender_and_role(as_jpeg(photo), data.get("category", ""))
        model = current_art_model()
        logger.info("Генерация для %s: пол %s, модель %s", chat_id, gender, model)
        art = generate_art(model, build_art_prompt(data, role, gender), photo, data["photo_mime"])
        logger.info("Арт для %s нарисован", chat_id)
        card_data = {k: v for k, v in data.items()
                     if k not in ("photo_id", "photo_mime", "photo_doc", "faction_key")}
        if gender:
            card_data["gender"] = gender
        if mode == "trial":
            card_data["trial"] = True
        title = "🎁 <b>ПРОБНАЯ КАРТИЩА ГОТОВА!</b>" if mode == "trial" else "🔥 <b>ТВОЯ ИСТИННАЯ КАРТИЩА ГОТОВА!</b>"
        deliver_card(chat_id, art, card_data, title, status)
        if mode == "paid":
            award_referrer(chat_id)

    run_art_job(chat_id, GENERATION_PRICE, mode, job)


# ---------- правки ----------
# Тип правки решает, как беречь лицо:
#   background — героя не перерисовываем вовсе: вырезаем по маске и кладём на новый фон;
#   other      — Kontext правит одежду/оружие/эффекты, потом возвращаем исходное лицо на место;
#   face       — человек сам просит поменять лицо (шрам, борода…), лицо не восстанавливаем.
EDIT_CLASSIFY = (
    "You prepare edits for the FLUX Kontext image editing model on a dark fantasy Witcher-style portrait. "
    "Classify the user's request (it may be in Russian) and write the English instruction. Reply as JSON: "
    '{"kind": "background" | "face" | "other", "instruction": "..."}.\n'
    "- background: the request changes only the place, scenery, weather, sky or time of day. Then instruction is "
    "a vivid 2-3 sentence description of the new scene ONLY, without the character.\n"
    "- face: the request changes the face, head or hair (scar, beard, eyes, age, expression, hairstyle, makeup, "
    "tattoo on the face). Then instruction says precisely what to change on the face and to keep everything else.\n"
    "- other: everything else (clothes, armor, weapons, objects, pose, magic effects). Then instruction says "
    "precisely what to change.\n"
    "Request: "
)
FACE_WORDS = ("лиц", "шрам", "глаз", "бород", "усы", "усов", "волос", "причёс", "прическ", "нос", "губ", "улыб",
              "морщин", "макияж", "возраст", "старше", "моложе", "брови", "лыс", "уши", "ухо", "щек", "щёк", "зуб")
BG_WORDS = ("фон", "задн", "небо", "ночь", "ночн", "днём", "погод", "локац", "пейзаж", "место", "закат", "рассвет",
            "лес", "горы", "море", "замок", "город", "дождь", "снег", "туман", "пещер", "таверн")
KEEP_IDENTITY = ("Keep the exact same person: identical facial features, face shape, eyes, nose, mouth, skin tone, "
                 "hairstyle and expression. Keep the same position, scale, pose and camera framing.")
# Добавляется только к правкам, которые могут затронуть руки (оружие, предметы, позы).
HANDS_OK = "Any visible hands are anatomically correct, with five fingers each."
EDIT_STYLE = "Hand-painted GWENT card art style, painterly brush strokes."


CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def translate_edit(request: str, kind: str) -> str:
    """Запасной путь: простой перевод правки на английский (модели правок русского не понимают)."""
    task = ("Describe this new background scenery in English in one or two sentences, without any person"
            if kind == "background" else
            "Translate this image edit request into one clear English instruction for an image editing model")
    english = ask_gemini(f"{task}. Reply with the English text only.\n\n{request}", temperature=0.2)
    if english and not CYRILLIC.search(english):
        return english
    logger.error("Правка не переведена, отправляю как есть: %r", request)
    return english or request


def classify_edit(request: str) -> tuple[str, str]:
    answer = parse_json(gemini_raw(EDIT_CLASSIFY + request, temperature=0.3, json_mode=True))
    if (isinstance(answer, dict) and answer.get("kind") in ("background", "face", "other")
            and answer.get("instruction") and not CYRILLIC.search(str(answer["instruction"]))):
        return answer["kind"], str(answer["instruction"])
    low = request.lower()
    if any(w in low for w in FACE_WORDS):
        kind = "face"
    elif any(w in low for w in BG_WORDS):
        kind = "background"
    else:
        kind = "other"
    return kind, translate_edit(request, kind)


def fal_image_url(body: dict) -> str:
    return (body.get("images") or [{}])[0].get("url") or body["image"]["url"]


def subject_mask(image_uri: str) -> bytes:
    """Маска персонажа (белое — герой) от BiRefNet."""
    return request_fal_image(FAL_MASK_ENDPOINT, {
        "image_url": image_uri, "model": "General Use (Heavy)", "mask_only": True, "output_format": "png",
    }, check_blank=False)


def composite_subject(source: bytes, background: bytes, mask: bytes) -> bytes:
    """Кладёт исходного героя (пиксель в пиксель) на новый фон, чуть подстраивая общий тон под сцену."""
    src = Image.open(io.BytesIO(source)).convert("RGB")
    bg = Image.open(io.BytesIO(background)).convert("RGB").resize(src.size, Image.LANCZOS)
    m = Image.open(io.BytesIO(mask)).convert("L").resize(src.size, Image.LANCZOS)
    m = m.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.5))
    a, b = np.asarray(src, np.float32), np.asarray(bg, np.float32)
    alpha = np.asarray(m, np.float32)[..., None] / 255
    outside = 1 - alpha
    weight = max(float(outside.sum()), 1.0)
    old_tone = (a * outside).sum(axis=(0, 1)) / weight
    new_tone = (b * outside).sum(axis=(0, 1)) / weight
    # Фон уже дорисован под героя, поэтому тон подстраиваем лишь слегка.
    gain = np.clip(new_tone / np.maximum(old_tone, 1), 0.7, 1.3) ** 0.3
    out = np.clip(a * gain, 0, 255) * alpha + b * outside
    buf = io.BytesIO()
    Image.fromarray(out.astype(np.uint8)).save(buf, "JPEG", quality=93)
    return buf.getvalue()


def fill_mask_png(mask: bytes, size: tuple[int, int]) -> bytes:
    """Маска для FLUX Fill: белое — перерисовать (всё, кроме героя). Край заходит на героя на пару пикселей,
    чтобы вместе со старым фоном ушёл и его ореол вокруг волос и плеч."""
    m = Image.open(io.BytesIO(mask)).convert("L").resize(size, Image.LANCZOS)
    fill = m.point(lambda v: 255 if v < 128 else 0).filter(ImageFilter.MaxFilter(5))
    buf = io.BytesIO()
    fill.save(buf, "PNG")
    return buf.getvalue()


def replace_background(source: bytes, scene: str) -> bytes:
    """Новый фон дорисовывается вокруг героя (FLUX Fill), сам герой не перерисовывается.

    Раньше сцена рисовалась отдельно и фигура вклеивалась поверх — получалось, что поясной портрет
    «висит в воздухе». Fill видит героя и строит землю, перспективу и свет под него.
    """
    uri = to_data_uri(source, "image/jpeg")
    try:
        mask = subject_mask(uri)
    except FalError as e:
        # Без маски — запасной путь: обычная правка фона с защитой лица.
        logger.warning("Маска героя не получена (%s), правлю фон напрямую", e)
        return edit_with_identity(source, f"Change the background to: {scene}", restore=True)
    size = Image.open(io.BytesIO(source)).size
    mask_uri = to_data_uri(fill_mask_png(mask, size), "image/png")
    filled = fal_with_retry(FAL_FILL_ENDPOINT, lambda: {
        "prompt": (f"A dark fantasy portrait of a character in front of {scene} The scene continues naturally behind "
                   f"and around the character, with matching perspective, scale and lighting. {EDIT_STYLE}"),
        "image_url": uri,
        "mask_url": mask_uri,
        "output_format": "jpeg",
        # 2 по умолчанию — слишком строго для мрачного фэнтези (кровь, кости); 4 — умеренно.
        "safety_tolerance": FAL_SAFETY,
        "seed": random.randint(1, 2 ** 31 - 1),
    })
    return composite_subject(source, filled, mask)


def face_box(image: bytes) -> Optional[tuple[int, int, int, int]]:
    answer = ask_gemini_vision(
        image, 'Find the face of the main character. Reply as JSON {"box_2d": [ymin, xmin, ymax, xmax]} with '
               'coordinates normalized to 0-1000, or {"box_2d": null} if there is no face.', as_json=True)
    try:
        y0, x0, y1, x1 = [float(v) for v in answer["box_2d"]]
    except (TypeError, KeyError, ValueError):
        return None
    w, h = Image.open(io.BytesIO(image)).size
    box = (int(x0 * w / 1000), int(y0 * h / 1000), int(x1 * w / 1000), int(y1 * h / 1000))
    area = (box[2] - box[0]) * (box[3] - box[1]) / (w * h)
    return box if 0.004 < area < 0.5 else None


def restore_face(source: bytes, edited: bytes) -> bytes:
    """Возвращает исходное лицо поверх правки, если Kontext не сдвинул голову."""
    box = face_box(source)
    if box is None:
        return edited
    src = Image.open(io.BytesIO(source)).convert("RGB")
    out = Image.open(io.BytesIO(edited)).convert("RGB").resize(src.size, Image.LANCZOS)
    a = np.asarray(src.crop(box).convert("L").resize((32, 32)), np.float32).ravel()
    b = np.asarray(out.crop(box).convert("L").resize((32, 32)), np.float32).ravel()
    similarity = float(np.corrcoef(a, b)[0, 1]) if a.std() > 0 and b.std() > 0 else 0.0
    if similarity < 0.45:
        # Голова сдвинулась или повернулась — вклейка дала бы «маску», оставляем как есть.
        logger.info("Лицо не восстановлено: сходство области %.2f", similarity)
        return edited
    x0, y0, x1, y1 = box
    pad_x, pad_y = (x1 - x0) * 0.06, (y1 - y0) * 0.04
    mask = Image.new("L", src.size, 0)
    ImageDraw.Draw(mask).ellipse((x0 + pad_x, y0 + pad_y, x1 - pad_x, y1 - pad_y), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(2, (x1 - x0) * 0.08)))
    result = Image.composite(src, out, mask)
    buf = io.BytesIO()
    result.save(buf, "JPEG", quality=93)
    return buf.getvalue()


def edit_with_identity(source: bytes, instruction: str, restore: bool) -> bytes:
    uri = to_data_uri(source, "image/jpeg")
    keep = KEEP_IDENTITY if restore else "Keep everything else, the person's identity and the pose the same."
    edited = fal_with_retry(FAL_EDIT_ENDPOINT, lambda: {
        "prompt": f"{instruction} {keep} {HANDS_OK} {EDIT_STYLE}",
        "image_url": uri, "aspect_ratio": "3:4", "guidance_scale": 3.5, "output_format": "jpeg",
        "safety_tolerance": FAL_SAFETY,
    })
    return restore_face(source, edited) if restore else edited


def edit_card(chat_id: int, card: dict, instruction: str, mode: str, preset: Optional[tuple] = None):
    def job(status):
        source = as_jpeg(load_art(card))
        kind, english = preset or classify_edit(instruction)
        logger.info("Правка %s для %s: %s", kind, chat_id, english[:200])
        if kind == "background":
            art = replace_background(source, english)
        else:
            art = edit_with_identity(source, english, restore=(kind == "other"))
        card_data = dict(card["data"], edit=instruction)
        card_data.pop("trial", None)
        logger.info("Правка для %s нарисована", chat_id)
        deliver_card(chat_id, art, card_data, "✏️ <b>ПРАВКА ГОТОВА!</b>", status, parent_id=card["id"])

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
    markup.add(types.InlineKeyboardButton("🎨 Картинки Лавка", callback_data="adm:scenes"),
               types.InlineKeyboardButton("🧪 Модель артов", callback_data="adm:models"))
    return markup


def show_models(chat_id: int):
    active = current_art_model()
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(*[types.InlineKeyboardButton(("✅ " if key == active else "▫️ ") + f"{title} · {price}",
                                            callback_data=f"adm:model:{key}")
                 for key, (title, price, _) in ART_MODELS.items()])
    markup.add(types.InlineKeyboardButton("🧪 Сравнить все на моём фото", callback_data="adm:modeltest"))
    bot.send_message(
        chat_id,
        f"🧪 <b>Модель для новых картищ:</b> {ART_MODELS[active][0]}\n\n"
        "• <b>FLUX.1 PuLID</b> — лучше всех переносит лицо, но путается в руках и предметах.\n"
        "• <b>FLUX.2 [pro]</b> — новое поколение FLUX: чище руки, детали и свет, цена почти та же.\n"
        "• <b>Nano Banana 2</b> (Google) — хорошо держит внешность и понимает описание, дороже.\n\n"
        "<i>Правки картищ остаются на FLUX Kontext. Сначала сравни на своём фото, потом выбери.</i>",
        reply_markup=markup,
    )


def run_model_test(chat_id: int, photo: bytes, mime: str):
    """Один и тот же персонаж всеми моделями — чтобы сравнить сходство, руки и детали."""
    key = random.choice(list(FACTIONS))
    data = {"faction_key": key, "faction": FACTIONS[key].name}
    gender, _ = detect_gender_and_role(as_jpeg(photo), "")
    prompt = build_art_prompt(data, None, gender)
    status = bot.send_message(chat_id, f"🧪 Рисую всеми моделями: {FACTIONS[key].name}, пол {gender or '?'}… "
                                       "Обычно 30–90 секунд.")

    def one(model):
        started = time.monotonic()
        try:
            return model, generate_art(model, prompt, photo, mime), time.monotonic() - started
        except Exception as e:
            logger.warning("Тест модели %s: %s", model, e)
            return model, None, str(e)

    with ThreadPoolExecutor(max_workers=len(ART_MODELS)) as pool:
        results = list(pool.map(one, ART_MODELS))
    safe_call(bot.delete_message, chat_id, status.message_id)
    media, failed = [], []
    for model, art, info in results:
        title = ART_MODELS[model][0]
        if art is None:
            failed.append(f"❌ {title}: {esc(str(info)[:200])}")
            continue
        media.append(types.InputMediaPhoto(photo_source(art, f"test_{model}.jpg"),
                                           caption=f"{title} · {ART_MODELS[model][1]} · {info:.0f} с"))
    if media:
        try:
            bot.send_media_group(chat_id, media, timeout=MEDIA_TIMEOUT)
        except Exception:
            logger.exception("Не удалось отправить сравнение моделей")
            failed.append("⚠️ Telegram не принял картинки — попробуй ещё раз.")
    bot.send_message(chat_id, "\n".join(failed + ["Выбери модель для всех новых картищ:"]),
                     reply_markup=types.InlineKeyboardMarkup(row_width=1).add(
                         *[types.InlineKeyboardButton(title, callback_data=f"adm:model:{k}")
                           for k, (title, _, _) in ART_MODELS.items()]))


_scene_job = threading.Lock()


def show_scenes(chat_id: int):
    ready = [key for key in SCENES if os.path.exists(scene_path(key))]
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(types.InlineKeyboardButton(f"✨ Нарисовать недостающие ({len(SCENES) - len(ready)})",
                                          callback_data="adm:scenesmissing"))
    markup.add(types.InlineKeyboardButton("🔁 Перерисовать все", callback_data="adm:scenesall"))
    markup.add(*[
        types.InlineKeyboardButton(("✅ " if key in ready else "▫️ ") + title, callback_data=f"adm:scene:{key}")
        for key, (title, _) in SCENES.items()
    ])
    sample = "есть ✅" if os.path.exists(WELCOME_IMAGE) else f"❌ нет — загрузи {os.path.basename(WELCOME_IMAGE)} рядом с main.py"
    bot.send_message(
        chat_id,
        "🎨 <b>Картинки Лавка на экранах</b>\n\n"
        f"Готово: <b>{len(ready)}/{len(SCENES)}</b>. Пока картинки нет, на экране — приветственная.\n"
        f"Образец внешности Лавка (приветственная картинка): {sample}\n\n"
        "<i>Рисование одной картинки — ~15–30 с и примерно $0.04 на Fal. Нажми на экран, чтобы посмотреть, "
        "перерисовать или заменить своей картинкой.</i>",
        reply_markup=markup,
    )


def scene_actions(key: str):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔄 Перерисовать", callback_data=f"adm:scenegen:{key}"),
        types.InlineKeyboardButton("📤 Заменить своей", callback_data=f"adm:sceneup:{key}"),
    )
    markup.add(types.InlineKeyboardButton("◀️ Ко всем картинкам", callback_data="adm:scenes"))
    return markup


def run_scene_job(admin_id: int, keys: list[str]):
    if not _scene_job.acquire(blocking=False):
        bot.send_message(admin_id, "⏳ Лавк уже рисует картинки — дождись окончания.")
        return
    try:
        bot.send_message(admin_id, f"🎨 Рисую картинок: {len(keys)}. Это займёт пару минут…")
        failed = []

        def one(key):
            try:
                draw_scene(key)
            except Exception as e:
                logger.exception("Не удалось нарисовать картинку %s", key)
                failed.append(f"{SCENES[key][0]} ({e})")

        workers = [threading.Thread(target=one, args=(k,)) for k in keys]
        # По 4 одновременно — быстрее, но без перегрузки очереди Fal.
        for i in range(0, len(workers), 4):
            batch = workers[i:i + 4]
            for w in batch:
                w.start()
            for w in batch:
                w.join()
        text = f"✅ Готово: {len(keys) - len(failed)} из {len(keys)}."
        if failed:
            text += "\n⚠️ Не вышло: " + esc("; ".join(failed))
        bot.send_message(admin_id, text)
        if len(keys) == 1 and not failed:
            send_screen(admin_id, keys[0], f"🖼 <b>{SCENES[keys[0]][0]}</b> — так выглядит теперь.",
                        reply_markup=scene_actions(keys[0]))
        else:
            show_scenes(admin_id)
    finally:
        _scene_job.release()


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
    send_screen(chat_id, "admin", "👑 <b>Трон смотрителя пещеры</b>\n\nЧто делаем, господин?",
                reply_markup=admin_panel_markup())


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
            f"⏳ Рисуется сейчас: {_gen_active} из {GEN_SLOTS} · в очереди: {len(_gen_queue)}",
        )
    elif action == "find":
        user_states[chat_id] = STATE_ADMIN_FIND
        bot.send_message(chat_id, "🔎 Пришли ID или @username бойца.", reply_markup=get_cancel_menu())
    elif action == "promo":
        show_promos(chat_id)
    elif action == "scenes":
        show_scenes(chat_id)
    elif action == "models":
        show_models(chat_id)
    elif action == "model" and len(parts) == 3 and parts[2] in ART_MODELS:
        try:
            set_art_model(parts[2])
        except OSError:
            logger.exception("Не удалось сохранить выбор модели")
            bot.send_message(chat_id, "⚠️ Не получилось сохранить выбор.")
            return
        logger.info("Модель артов: %s", parts[2])
        bot.send_message(chat_id, f"✅ Новые картищи рисует <b>{ART_MODELS[parts[2]][0]}</b>.")
    elif action == "modeltest":
        user_states[chat_id] = STATE_ADMIN_MODELTEST
        bot.send_message(chat_id, f"📸 Пришли фото человека — нарисую его всеми {len(ART_MODELS)} моделями "
                                  "(≈$0.15 с Fal).", reply_markup=get_cancel_menu())
    elif action in ("scenesmissing", "scenesall"):
        keys = [k for k in SCENES if action == "scenesall" or not os.path.exists(scene_path(k))]
        if not keys:
            bot.send_message(chat_id, "✅ Все картинки уже нарисованы.")
            return
        threading.Thread(target=run_scene_job, args=(chat_id, keys), daemon=True).start()
    elif action in ("scene", "scenegen", "sceneup") and len(parts) == 3 and parts[2] in SCENES:
        key = parts[2]
        if action == "scene":
            title = SCENES[key][0]
            if os.path.exists(scene_path(key)):
                send_screen(chat_id, key, f"🖼 <b>{title}</b>", reply_markup=scene_actions(key))
            else:
                bot.send_message(chat_id, f"🖼 <b>{title}</b> — ещё не нарисована.", reply_markup=scene_actions(key))
        elif action == "scenegen":
            threading.Thread(target=run_scene_job, args=(chat_id, [key]), daemon=True).start()
        else:
            user_states[chat_id] = STATE_ADMIN_SCENE
            user_data[chat_id] = {"scene_key": key}
            bot.send_message(chat_id, f"📤 Пришли картинку для экрана «{SCENES[key][0]}» — фото или файлом.",
                             reply_markup=get_cancel_menu())
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


@bot.message_handler(content_types=["photo", "document", "text"],
                     func=lambda msg: user_states.get(msg.chat.id) == STATE_ADMIN_MODELTEST and is_admin(msg.from_user.id))
def admin_model_test(message):
    chat_id = message.chat.id
    if message.photo:
        file_id, mime = message.photo[-1].file_id, "image/jpeg"
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id, mime = message.document.file_id, message.document.mime_type
    else:
        bot.send_message(chat_id, "⚠️ Нужно фото. Пришли его или жми «Отмена».")
        return
    reset_user(chat_id)
    bot.send_message(chat_id, "👌 Принял.", reply_markup=get_main_menu(chat_id))

    def job():
        try:
            run_model_test(chat_id, download_telegram_file(file_id), mime)
        except Exception:
            logger.exception("Сравнение моделей упало")
            safe_call(bot.send_message, chat_id, "⚠️ Сравнение не удалось, смотри лог.")

    threading.Thread(target=job, daemon=True).start()


@bot.message_handler(content_types=["photo", "document", "text"],
                     func=lambda msg: user_states.get(msg.chat.id) == STATE_ADMIN_SCENE and is_admin(msg.from_user.id))
def admin_scene_upload(message):
    chat_id = message.chat.id
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
    else:
        bot.send_message(chat_id, "⚠️ Нужна картинка. Пришли фото или жми «Отмена».")
        return
    key = user_data[chat_id]["scene_key"]
    try:
        with Image.open(io.BytesIO(download_telegram_file(file_id))) as img:
            os.makedirs(SCENE_DIR, exist_ok=True)
            img.convert("RGB").save(scene_path(key), "JPEG", quality=92)
        warm_scenes_async()
    except Exception:
        logger.exception("Не удалось сохранить картинку экрана %s", key)
        bot.send_message(chat_id, "⚠️ Не получилось прочитать картинку. Попробуй другую.")
        return
    reset_user(chat_id)
    bot.send_message(chat_id, "✅ Сохранил!", reply_markup=get_main_menu(chat_id))
    send_screen(chat_id, key, f"🖼 <b>{SCENES[key][0]}</b> — так выглядит теперь.", reply_markup=scene_actions(key))


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
    send_screen(chat_id, "pouch" if status == "ok" else "promo", text, reply_markup=get_main_menu(chat_id))


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
    elif state in (STATE_ADMIN_SCENE, STATE_ADMIN_MODELTEST):
        bot.send_message(chat_id, "⚠️ Нужна картинка. Пришли фото или жми «Отмена».")
    elif state in STEPS or state in (STATE_EDIT_PROMPT, STATE_TOPUP_AMOUNT, STATE_PROMO, STATE_ADMIN_PROMO):
        bot.send_message(chat_id, "⚠️ Лавк ждёт текст!")
    elif chat_id in busy_users:
        bot.send_message(chat_id, "⏳ Лавк рисует, подожди чуть-чуть!")
    else:
        send_screen(chat_id, "confused", "Ууу, Лавк не понял. Жми кнопки в меню, человече!",
                    reply_markup=get_main_menu(chat_id))


def check_telegram_network():
    """Замер связи с Telegram при запуске; при сломанном IPv6 переключает запросы на IPv4.

    Если у сервера есть IPv6-адрес, но маршрут до Telegram по нему не работает, каждое новое соединение
    сначала ждёт таймаут IPv6 и лишь потом идёт по IPv4 — это даёт задержки по 10–20 с на ровном месте.
    """
    host = "api.telegram.org"
    timings = {}
    for family, name in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        try:
            address = socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM)[0][4][0]
            started = time.monotonic()
            with socket.create_connection((address, 443), timeout=5):
                timings[name] = time.monotonic() - started
        except OSError as e:
            timings[name] = None
            logger.info("Telegram по %s недоступен: %s", name, e)
    v4, v6 = timings.get("IPv4"), timings.get("IPv6")
    logger.info("Связь с Telegram: IPv4 %s, IPv6 %s", f"{v4:.2f} с" if v4 is not None else "нет",
                f"{v6:.2f} с" if v6 is not None else "нет")
    if v4 is not None and (v6 is None or v6 > v4 + 0.5):
        import urllib3.util.connection as urllib3_connection
        urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
        logger.info("Запросы к внешним сервисам идут только по IPv4")
    started = time.monotonic()
    try:
        bot.get_me()
        logger.info("Запрос к Telegram API занял %.2f с", time.monotonic() - started)
    except Exception as e:
        logger.warning("Запрос к Telegram API не прошёл: %s", e)


if __name__ == "__main__":
    # В фоне: проверка сети не должна задерживать запуск бота, даже если DNS или Telegram отвечают медленно.
    threading.Thread(target=check_telegram_network, daemon=True).start()
    logger.info("Lavk is listening as @%s...", bot_username())
    # Картинки экранов загружаются в Telegram заранее, в фоне — пользователи их не ждут.
    warm_scenes_async()
    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=30,
        skip_pending=True,
        allowed_updates=["message", "callback_query", "pre_checkout_query"],
    )
