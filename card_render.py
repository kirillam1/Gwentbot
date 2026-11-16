"""Сборка изображения карты GWENT в том виде, как она выглядит на экране просмотра карты в игре.

Карта собирается из игровых спрайтов (рамка, ромб силы, провизия, броня, цифры), панель —
из шаблонов, извлечённых из скриншотов (их собирает tools/build_card_templates.py).
Все координаты — в системе скриншота 2756×1268; готовая картинка — его обрезка.
"""
import io
import os
import random
import re
import zipfile
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(BASE_DIR, "assets")
# Хостинги без загрузки папок (Amvera через браузер): те же файлы можно положить одним assets.zip рядом с кодом.
ASSET_ZIP = os.path.join(BASE_DIR, "assets.zip")

FONT_NAME = "GWENT-ExtraBold.ttf"
FONT_NUMBER = "gnum.ttf"
FONT_REGULAR = "PFDinTextCondPro-Regular.ttf"
FONT_BOLD = "PFDinTextCondPro-Bold.ttf"
FONT_QUOTE = FONT_REGULAR
QUOTE_SLANT = 0.2

# ---------- карта (спрайты 990×1422) ----------
CARD_POS, CARD_SCALE = (286, 197), (0.5586, 0.5858)   # в игре карта чуть вытянута по вертикали
DIAMOND_POS, DIAMOND_SCALE = (284, 198), 0.608          # ромб силы в игре чуть крупнее, чем в спрайте
POWER_CENTER, POWER_SCALE, POWER_TINT = (377, 288), 0.88, (205, 198, 178)
ARMOR_POS, ARMOR_SCALE = (675, 200), 0.626
ARMOR_CENTER, ARMOR_DIGIT_SCALE = (752, 288), 0.86
PROV_ICON_POS, PROV_ICON_SCALE = (718, 774), 0.52
PROV_BOX_POS, PROV_BOX_SCALE = (717, 878), 0.48
PROV_CENTER, PROV_SCALE, PROV_TINT = (770, 941), (0.52, 0.54), (246, 234, 155)
DIGIT_GAP = -6                      # между цифрами двузначного числа, в пикселях спрайта
POWER_MAX_W, ARMOR_MAX_W, PROV_MAX_W = 112, 100, 92

# ---------- панель ----------
PANEL_X, PANEL_TOP, BODY_TOP, PANEL_W = 871, 191, 342, 996
BOTTOM_ROWS = 24
HEADER_DIGIT_CENTER_X, HEADER_DIGIT_TOP, HEADER_DIGIT_H = 943, 220, 82
NAME_X, NAME_CAP_TOP, NAME_SIZE, NAME_MAX_RIGHT = 1015, 213, 59, 1735
CATEGORY_X, CATEGORY_CAP_TOP, CATEGORY_SIZE = 1013, 275, 50
TEXT_X, TEXT_RIGHT = 898, 1823
QUOTE_WRAP = 900                    # цитату игра переносит чуть уже, чем потом выравнивает
ABILITY_SIZE, ABILITY_PITCH, ABILITY_CAP_TOP = 51, 60.75, 367
SEPARATOR_GAP, SEPARATOR_CENTER_X = 79, 1366       # от верха прописных последней строки до разделителя
QUOTE_SIZE, QUOTE_PITCH, QUOTE_GAP = 42, 50.75, 36
BOTTOM_PAD_QUOTE, BOTTOM_PAD_ABILITY = 66, 72        # от верха прописных последней строки до низа панели
MAX_PANEL_BOTTOM = 1190
CROP_X, CROP_TOP, CROP_MARGIN = (250, 1896), 165, 31
CARD_BOTTOM = 1030
CARD_ONLY_BOX = (258, 170, 866, 1058)           # вторая картинка: карта в рамке без панели
WATERMARK_TEXT = "ЛАВК · ПРОБНАЯ"

INK = (36, 24, 12)
KEYWORD_RED = (98, 18, 12)
QUOTE_INK = (38, 25, 12)
WHITE = (255, 255, 255)
OUTLINE = (8, 6, 5)

FACTIONS = {
    "Нильфгаард": ("nilfgaard", "Nilfgaard"),
    "Чудовища": ("monster", "Monster"),
    "Скеллиге": ("skellige", "Skellige"),
    "Королевства Севера": ("northern_realms", "Northern_Realms"),
    "Синдикат": ("syndicate", "Syndicate"),
    "Скоя'таэли": ("scoiatael", "Scoiatael"),
    "Нейтральная": ("neutral", "Neutral"),
}
DEFAULT_FACTION = FACTIONS["Нейтральная"]
FRAMES = {"Золотая": ("Gold.png", "Legendary.png"), "Бронзовая": ("Bronze.png", "Common.png")}

# Термины GWENT, которые в тексте способности выделяются (сравнение по началу слова).
KEYWORD_STEMS = (
    "размещени", "приказ", "взаимосвяз", "выносливост", "яд", "кровотечени", "уязвимост",
    "детонаци", "месть", "хитрост", "бессмерти", "порядок", "гармони", "дуэл", "заряд",
    "щит", "блокировк", "казн", "призыв", "брони", "броню", "броня", "поглот", "удалите", "создайте",
    "обреченност", "преданност", "адреналин", "столкновени", "завещани", "засад", "шпионаж", "экипаж",
    "воодушевлени", "берсерк", "ветеран", "доминировани", "ассимиляц", "симбиоз", "рвени", "подношени",
)
LABEL_RE = re.compile(r"(^|(?<=[.!?]\s)|(?<=\n))([^\s.!?:*][^.!?:*\n]{0,40})(?=:)")

NORMAL, KEYWORD = "normal", "keyword"


@lru_cache(maxsize=64)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(io.BytesIO(asset_bytes(f"fonts/{name}")), size)


@lru_cache(maxsize=128)
def asset_bytes(rel: str) -> bytes:
    """Файл из папки assets, а если её нет — из assets.zip (пути внутри архива те же)."""
    path = os.path.join(ASSET_DIR, rel)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    with zipfile.ZipFile(ASSET_ZIP) as zf:
        return zf.read(rel)


def asset_exists(rel: str) -> bool:
    if os.path.exists(os.path.join(ASSET_DIR, rel)):
        return True
    if not os.path.exists(ASSET_ZIP):
        return False
    with zipfile.ZipFile(ASSET_ZIP) as zf:
        return rel in zf.namelist()


@lru_cache(maxsize=64)
def asset(name: str) -> Image.Image:
    rel = f"card/sprites/{name}"
    if not asset_exists(rel):
        rel = f"card/{name}"
    return Image.open(io.BytesIO(asset_bytes(rel))).convert("RGBA")


def strip_markup(text: str) -> str:
    return text.replace("*", "")


def to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def scaled(img: Image.Image, sx: float, sy: float | None = None) -> Image.Image:
    sy = sx if sy is None else sy
    return img.resize((max(1, round(img.width * sx)), max(1, round(img.height * sy))), Image.LANCZOS)


def cap_height(fnt) -> int:
    return -fnt.getbbox("Н", anchor="ls")[1]


# ---------- карта ----------
@lru_cache(maxsize=32)
def digit_sprite(kind: str, digit: str, tint: tuple | None) -> Image.Image:
    img = asset(f"{kind}{digit}.png")
    img = img.crop(img.getchannel("A").getbbox())
    if tint:
        # перекрашиваем под цвет цифр на экране, сохраняя светотень и обводку спрайта
        a = np.asarray(img).astype(np.float32)
        lum = a[..., :3].mean(axis=2, keepdims=True)
        ref = np.percentile(lum[a[..., 3] > 200], 90)
        a[..., :3] = np.clip(np.array(tint, np.float32) * lum / ref, 0, 255)
        img = Image.fromarray(a.astype(np.uint8), "RGBA")
    return img


def number_layer(value: int, kind: str, sx: float, sy: float, tint, max_w: int) -> Image.Image:
    glyphs = [digit_sprite(kind, d, tint) for d in str(value)]
    w = sum(g.width for g in glyphs) + DIGIT_GAP * (len(glyphs) - 1)
    h = max(g.height for g in glyphs)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    x = 0
    for g in glyphs:
        layer.alpha_composite(g, (x, h - g.height))
        x += g.width + DIGIT_GAP
    layer = scaled(layer, sx, sy)
    if layer.width > max_w:
        k = max_w / layer.width
        layer = scaled(layer, k)
    return layer


def paste_center(canvas: Image.Image, layer: Image.Image, center):
    canvas.alpha_composite(layer, (round(center[0] - layer.width / 2), round(center[1] - layer.height / 2)))


def draw_card(canvas: Image.Image, art: Image.Image, data: dict):
    _, sprite = FACTIONS.get(data.get("faction"), DEFAULT_FACTION)
    border, rarity = FRAMES.get(data.get("frame"), FRAMES["Бронзовая"])

    card_w, card_h = round(990 * CARD_SCALE[0]), round(1422 * CARD_SCALE[1])
    card = ImageOps.fit(art.convert("RGB"), (card_w, card_h), Image.LANCZOS, centering=(0.5, 0.4)).convert("RGBA")
    card.alpha_composite(asset(border).resize((card_w, card_h), Image.LANCZOS))
    canvas.alpha_composite(card, CARD_POS)

    diamond = asset(f"{sprite}_diamond.png").crop((0, 0, 330, 330))
    diamond = Image.alpha_composite(diamond, asset(rarity).crop((0, 0, 330, 330)))
    canvas.alpha_composite(scaled(diamond, DIAMOND_SCALE), DIAMOND_POS)
    power = to_int(data.get("power"))
    if power is not None:
        power = max(0, min(power, 99))
        paste_center(canvas, number_layer(power, "s", POWER_SCALE, POWER_SCALE, POWER_TINT, POWER_MAX_W), POWER_CENTER)

    armor = to_int(data.get("armor"))
    if armor and armor > 0:
        shield = asset("Armor.png").crop((730, 12, 973, 292))
        canvas.alpha_composite(scaled(shield, ARMOR_SCALE), ARMOR_POS)
        layer = number_layer(min(armor, 99), "s", ARMOR_DIGIT_SCALE, ARMOR_DIGIT_SCALE, None, ARMOR_MAX_W)
        paste_center(canvas, layer, ARMOR_CENTER)

    provision = to_int(data.get("provision"))
    if provision is not None:
        canvas.alpha_composite(scaled(asset("Provisions.png").crop((761, 950, 960, 1150)), PROV_ICON_SCALE),
                               PROV_ICON_POS)
        box = asset(f"{sprite}_prov.png").crop((753, 1138, 968, 1400))
        canvas.alpha_composite(scaled(box, PROV_BOX_SCALE), PROV_BOX_POS)
        layer = number_layer(max(0, min(provision, 99)), "p", *PROV_SCALE, PROV_TINT, PROV_MAX_W)
        paste_center(canvas, layer, PROV_CENTER)


# ---------- текст ----------
def outlined(text: str, fnt, width: int) -> Image.Image:
    """Белый текст с тёмной обводкой, как в шапке игры; слой выровнен по базовой линии (anchor ls)."""
    left, top, right, bottom = fnt.getbbox(text, anchor="ls", stroke_width=width)
    layer = Image.new("RGBA", (right - left + 2, bottom - top + 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((1 - left, 1 - top), text, font=fnt, fill=WHITE, anchor="ls",
                               stroke_width=width, stroke_fill=OUTLINE)
    layer.info["origin"] = (1 - left, 1 - top)
    return layer


def draw_header(canvas: Image.Image, data: dict):
    key, _ = FACTIONS.get(data.get("faction"), DEFAULT_FACTION)
    canvas.alpha_composite(asset(f"header_{key}.png"), (PANEL_X, PANEL_TOP))

    power = to_int(data.get("power"))
    if power is not None:
        text = str(max(0, min(power, 99)))
        probe = font(FONT_NUMBER, 100).getbbox(text, anchor="ls")
        fnt = font(FONT_NUMBER, round(100 * HEADER_DIGIT_H / max(1, probe[3] - probe[1])))
        layer = outlined(text, fnt, 5)
        ox, oy = layer.info["origin"]
        ink_top = fnt.getbbox(text, anchor="ls")[1]
        x = HEADER_DIGIT_CENTER_X - layer.width / 2
        canvas.alpha_composite(layer, (round(x), HEADER_DIGIT_TOP - (oy + ink_top)))

    name = strip_markup(data.get("name", "")).upper().strip()
    if name:
        size = NAME_SIZE
        while size > 36 and font(FONT_NAME, size).getlength(name) > NAME_MAX_RIGHT - NAME_X:
            size -= 1
        fnt = font(FONT_NAME, size)
        layer = outlined(name, fnt, 2)
        ox, oy = layer.info["origin"]
        baseline = NAME_CAP_TOP + cap_height(font(FONT_NAME, NAME_SIZE))
        ink_left = fnt.getbbox(name, anchor="ls")[0]
        canvas.alpha_composite(layer, (NAME_X - ox - ink_left, baseline - oy))

    category = strip_markup(data.get("category", "")).strip()
    if category:
        size = CATEGORY_SIZE
        while size > 30 and font(FONT_REGULAR, size).getlength(category) > NAME_MAX_RIGHT - CATEGORY_X:
            size -= 1
        fnt = font(FONT_REGULAR, size)
        layer = outlined(category, fnt, 2)
        ox, oy = layer.info["origin"]
        baseline = CATEGORY_CAP_TOP + cap_height(font(FONT_REGULAR, CATEGORY_SIZE))
        ink_left = fnt.getbbox(category, anchor="ls")[0]
        canvas.alpha_composite(layer, (CATEGORY_X - ox - ink_left, baseline - oy))


def parse_ability(text: str) -> list[tuple[str, str]]:
    """«Метка:» в начале предложения, *слово* и термины GWENT — красным жирным, как в игре."""
    segments: list[tuple[str, str]] = []
    for i, part in enumerate(re.split(r"\*([^*]+)\*", text)):
        if not part:
            continue
        if i % 2:
            segments.append((part, KEYWORD))
            continue
        pos = 0
        for match in LABEL_RE.finditer(part):
            label = match.group(2)
            if len(label.split()) > 4:
                continue
            start = match.start(2)
            if start > pos:
                before = part[pos:start]
                # каждая новая метка («Приказ:») — с новой строки
                if (segments or before.strip()) and not before.rstrip(" \t").endswith("\n"):
                    before = before.rstrip() + "\n"
                segments.append((before, NORMAL))
            # «Адреналин 4:» — число после метки в игре обычным шрифтом
            name, number = re.match(r"(.*?)(\s+\d+)?$", label).groups()
            segments.append((name, KEYWORD))
            if number:
                segments.append((number, NORMAL))
            pos = match.end(2)
        if pos < len(part):
            segments.append((part[pos:], NORMAL))

    styled: list[tuple[str, str]] = []
    for chunk, style in segments:
        for word in re.split(r"(\s+)", chunk):
            if not word:
                continue
            if style == NORMAL and not word.isspace():
                core = word.strip(".,;:!?«»\"()—-").lower()
                if core and any(core.startswith(stem) for stem in KEYWORD_STEMS):
                    # знаки препинания вокруг термина в игре обычные
                    lead = word[:len(word) - len(word.lstrip("«\"("))]
                    head = word[len(lead):].rstrip(".,;:!?»\")")
                    if lead:
                        styled.append((lead, NORMAL))
                    styled.append((head, KEYWORD))
                    if len(lead) + len(head) < len(word):
                        styled.append((word[len(lead) + len(head):], NORMAL))
                    continue
            styled.append((word, style))
    return styled


def style_font(style: str, size: int):
    return font(FONT_REGULAR if style == NORMAL else FONT_BOLD, size)


def wrap_rich(tokens, size: int, max_width: float):
    """Перенос по словам с разными начертаниями. Строки из (слово, стиль, x)."""
    units: list = []
    for word, style in tokens:
        if word.isspace():
            units.append(word)
        elif units and not isinstance(units[-1], str):
            units[-1].append((word, style))
        else:
            units.append([(word, style)])
    lines: list[list[tuple[str, str, float]]] = [[]]
    x = 0.0
    space = font(FONT_REGULAR, size).getlength(" ")
    for unit in units:
        if isinstance(unit, str):
            if "\n" in unit:
                lines.extend([] for _ in range(unit.count("\n")))
                x = 0
            elif lines[-1]:
                x += space
            continue
        width = sum(style_font(st, size).getlength(w) for w, st in unit)
        if lines[-1] and x + width > max_width:
            lines.append([])
            x = 0
        for w, st in unit:
            lines[-1].append((w, st, x))
            x += style_font(st, size).getlength(w)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def wrap_plain(text: str, fnt, max_width: float) -> list[list[str]]:
    lines, current = [], []
    for word in text.split():
        if current and fnt.getlength(" ".join(current + [word])) > max_width:
            lines.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(current)
    return lines


def layout(data: dict, k: float):
    """Раскладка текста при масштабе k (1.0 — как в игре). Возвращает позиции и низ панели."""
    a_size, q_size = round(ABILITY_SIZE * k), round(QUOTE_SIZE * k)
    a_pitch, q_pitch = ABILITY_PITCH * k, QUOTE_PITCH * k
    width = TEXT_RIGHT - TEXT_X
    lines = wrap_rich(parse_ability(data.get("ability", "")), a_size, width)
    quote = strip_markup(data.get("quote", "")).replace("...", "…").strip()
    q_font = font(FONT_QUOTE, q_size)
    q_lines = wrap_plain(quote, q_font, QUOTE_WRAP) if quote else []

    tops = [ABILITY_CAP_TOP + i * a_pitch for i in range(len(lines))]
    last = tops[-1] if tops else ABILITY_CAP_TOP - a_pitch
    sep_top = None
    q_tops = []
    if q_lines:
        if lines:
            sep_top = last + SEPARATOR_GAP * k
            first = sep_top + QUOTE_GAP * k
        else:
            first = ABILITY_CAP_TOP
        q_tops = [first + i * q_pitch for i in range(len(q_lines))]
        bottom = q_tops[-1] + BOTTOM_PAD_QUOTE * k
    else:
        bottom = last + BOTTOM_PAD_ABILITY * k
    return dict(a_size=a_size, q_size=q_size, lines=lines, tops=tops, q_lines=q_lines, q_tops=q_tops,
                sep_top=sep_top, bottom=round(bottom))


def draw_panel_body(canvas: Image.Image, bottom: int):
    body = asset("panel_body.png")
    height = bottom - BOTTOM_ROWS - BODY_TOP
    if height > body.height:
        # отражаем текстуру, если панель выше эталона
        body = Image.fromarray(np.concatenate([np.asarray(body), np.asarray(body)[::-1]], axis=0))
    canvas.alpha_composite(body.crop((0, 0, PANEL_W, height)), (PANEL_X, BODY_TOP))
    canvas.alpha_composite(asset("panel_bottom.png"), (PANEL_X, bottom - BOTTOM_ROWS))


def draw_text(canvas: Image.Image, lay: dict):
    draw = ImageDraw.Draw(canvas)
    a_cap = cap_height(font(FONT_REGULAR, lay["a_size"]))
    for line, top in zip(lay["lines"], lay["tops"]):
        for word, style, x in line:
            color = KEYWORD_RED if style == KEYWORD else INK
            draw.text((TEXT_X + x, round(top + a_cap)), word, font=style_font(style, lay["a_size"]),
                      fill=color, anchor="ls")

    if lay["sep_top"] is not None:
        sep = asset("separator.png")
        canvas.alpha_composite(sep, (SEPARATOR_CENTER_X - sep.width // 2, round(lay["sep_top"])))

    q_font = font(FONT_QUOTE, lay["q_size"])
    q_cap = cap_height(q_font)
    width = TEXT_RIGHT - TEXT_X
    space = q_font.getlength(" ")
    q_lines = lay["q_lines"]
    for i, (words, top) in enumerate(zip(q_lines, lay["q_tops"])):
        widths = [q_font.getlength(w) for w in words]
        if len(q_lines) == 1:
            # короткая цитата в игре стоит по центру
            gap, x = space, (width - sum(widths) - space * (len(words) - 1)) / 2
        else:
            last = i == len(q_lines) - 1
            gap = space if last or len(words) == 1 else (width - sum(widths)) / (len(words) - 1)
            x = 0.0
        # курсив в игре — наклонённый обычный шрифт (а не отдельное курсивное начертание)
        pad, base = 12, q_cap + 12
        mask = Image.new("L", (width + 2 * pad + 20, round(base + lay["q_size"] * 0.45)), 0)
        draw_q = ImageDraw.Draw(mask)
        x += pad
        for w, ww in zip(words, widths):
            draw_q.text((x, base), w, font=q_font, fill=255, anchor="ls")
            x += ww + gap
        mask = mask.transform(mask.size, Image.AFFINE, (1, QUOTE_SLANT, -QUOTE_SLANT * base, 0, 1, 0), Image.BICUBIC)
        canvas.paste(Image.new("RGBA", mask.size, QUOTE_INK + (255,)), (TEXT_X - pad, round(top) - 12), mask)


def draw_panel(canvas: Image.Image, data: dict) -> int:
    k = 1.0
    lay = layout(data, k)
    while lay["bottom"] > MAX_PANEL_BOTTOM and k > 0.6:
        k -= 0.02
        lay = layout(data, k)
    bottom = lay["bottom"]

    shadow = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(shadow).rectangle((PANEL_X + 4, PANEL_TOP + 8, PANEL_X + PANEL_W + 6, bottom + 10), fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(9))
    canvas.paste(Image.new("RGBA", canvas.size, (0, 0, 0, 255)), (0, 0), shadow)

    draw_header(canvas, data)
    draw_panel_body(canvas, bottom)
    draw_text(canvas, lay)
    return bottom


# Пробная картища: знак специально сделан трудным для нейросетей-«стиралок».
# Они хорошо убирают редкий ровный текст одного цвета; плохо — когда помеха покрывает
# всю картинку, каждый раз лежит по-новому и смешана с самим рисунком.
TRIAL_MAX_SIDE = 900          # пробная уходит в пониженном разрешении: даже стёртая — не годится для печати
TRIAL_JPEG_QUALITY = 80


def _tiled_text(size, text: str, fnt, angle: float, rng, colors, alpha: int) -> Image.Image:
    """Повторяющаяся надпись под углом со случайным сдвигом сетки; цвет строк чередуется."""
    w, h = size
    step_x = round(fnt.getlength(text + "  "))
    step_y = round(fnt.size * rng.uniform(2.0, 2.6))
    side = int((w ** 2 + h ** 2) ** 0.5) + 2 * step_x
    layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    off_x, off_y = rng.randrange(step_x), rng.randrange(step_y)
    stroke = max(1, fnt.size // 16)
    for row, y in enumerate(range(-off_y, side, step_y)):
        fill = colors[row % len(colors)]
        shift = rng.randrange(step_x)          # каждая строка со своим сдвигом — нет ровной решётки
        for x in range(-off_x - shift, side, step_x):
            draw.text((x, y), text, font=fnt, fill=fill + (alpha,),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, alpha * 2 // 3))
    layer = layer.rotate(angle, resample=Image.BICUBIC)
    left, top = (side - w) // 2, (side - h) // 2
    return layer.crop((left, top, left + w, top + h))


def _guilloche(size, rng, k: float) -> Image.Image:
    """Сетка тонких волнистых линий по всей картинке, как на банкнотах."""
    w, h = size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    width = max(1, round(k / 450))
    xs = np.arange(-20, w + 21, 6, dtype=float)
    for family, tilt in enumerate((rng.uniform(0.15, 0.4), -rng.uniform(0.15, 0.4))):
        spacing = k / rng.uniform(22, 30)
        amp = k / rng.uniform(30, 45)
        period = k / rng.uniform(3.5, 6)
        phase = rng.uniform(0, 2 * np.pi)
        y0 = -abs(tilt) * w - amp
        line = 0
        while y0 < h + abs(tilt) * w + amp:
            ys = y0 + xs * tilt + amp * np.sin(2 * np.pi * xs / period + phase + line * 0.35)
            color = (255, 255, 255, 70) if (line + family) % 2 else (20, 14, 8, 60)
            draw.line(list(zip(xs.tolist(), ys.tolist())), fill=color, width=width)
            y0 += spacing
            line += 1
    return layer


def _seal(size, center, k: float, rng) -> Image.Image:
    """Крупная печать «ЛАВК · ПРОБНАЯ» на арте (под лицом, на одежде)."""
    w, h = size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    r = k * 0.23
    cx, cy = center
    ring = max(3, round(k / 120))
    for rr, a in ((r, 150), (r * 0.82, 120)):
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(255, 255, 255, a), width=ring)
        draw.ellipse((cx - rr - ring, cy - rr - ring, cx + rr + ring, cy + rr + ring),
                     outline=(0, 0, 0, a // 2), width=max(1, ring // 2))
    big = font(FONT_NAME, round(r * 0.62))
    small = font(FONT_NAME, round(r * 0.22))
    for text, fnt, dy in (("ЛАВК", big, -r * 0.12), ("ПРОБНАЯ", small, r * 0.42)):
        tw = fnt.getlength(text)
        draw.text((cx - tw / 2, cy + dy - fnt.size * 0.6), text, font=fnt, fill=(255, 255, 255, 165),
                  stroke_width=max(2, fnt.size // 14), stroke_fill=(0, 0, 0, 110))
    return layer.rotate(rng.uniform(-14, 14), center=center, resample=Image.BICUBIC)


# Лицо на пробной картище оставляем чистым: человек должен увидеть себя в образе («вау»), а остальной
# арт под знаком. Где лицо — знает Gemini (data["face"], доли от арта); без него — типичное место на портрете.
DEFAULT_FACE = (0.36, 0.20, 0.64, 0.46)


def art_face_window(data: dict, art_size: tuple[int, int]) -> tuple[float, float, float, float]:
    """(cx, cy, rx, ry) окна вокруг лица в координатах холста — с учётом того, как draw_card вписывает арт."""
    face = data.get("face") or DEFAULT_FACE
    try:
        x0, y0, x1, y1 = (float(v) for v in face)
    except (TypeError, ValueError):
        x0, y0, x1, y1 = DEFAULT_FACE
    aw, ah = art_size
    card_w, card_h = round(990 * CARD_SCALE[0]), round(1422 * CARD_SCALE[1])
    k = max(card_w / aw, card_h / ah)                     # как ImageOps.fit: масштаб «заполнить»
    left, top = (aw * k - card_w) * 0.5, (ah * k - card_h) * 0.4
    px = lambda x: CARD_POS[0] + x * aw * k - left
    py = lambda y: CARD_POS[1] + y * ah * k - top
    cx, cy = (px(x0) + px(x1)) / 2, (py(y0) + py(y1)) / 2
    # С запасом: волосы, уши, подбородок. Но не больше трети ширины карты, чтобы арт не оголялся целиком.
    rx = min((px(x1) - px(x0)) * 0.75, card_w * 0.33)
    ry = min((py(y1) - py(y0)) * 0.75, card_h * 0.25)
    return cx, cy, max(rx, card_w * 0.12), max(ry, card_h * 0.08)


def _window_mask(size, window) -> np.ndarray:
    """1 — знак виден, 0 — чистое окно (эллипс с мягким краем)."""
    w, h = size
    cx, cy, rx, ry = window
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2)
    return np.clip((d - 0.85) / 0.35, 0.0, 1.0)


def watermark(img: Image.Image, tag: str = "", center=None, unit: float | None = None,
              window=None) -> Image.Image:
    """Водяной знак пробной картищи.

    Слои: сетка волнистых линий, крупный текст, мелкий текст с ID владельца под другим углом,
    печать по центру арта. Углы и сдвиги каждый раз случайные — общего «шаблона» для стирания нет;
    прозрачность «рябит» шумом, а часть слоёв не просто ложится сверху, а меняет яркость рисунка.
    """
    rng = random.Random()
    w, h = img.size
    k = unit or (w * h) ** 0.5     # unit — масштаб знака; один и тот же для обеих картинок карты
    center = center or (w / 2, h * 0.45)
    sign = rng.choice((-1, 1))

    big = font(FONT_NAME, max(30, round(k / 13)))
    small = font(FONT_REGULAR, max(14, round(k / 42)))
    mark = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mark.alpha_composite(_guilloche((w, h), rng, k))
    mark.alpha_composite(_tiled_text((w, h), WATERMARK_TEXT, big, sign * rng.uniform(22, 36), rng,
                                     [(255, 255, 255), (255, 214, 120)], 115))
    tag_text = f"ЛАВК  {tag}" if tag else "ЛАВК"
    mark.alpha_composite(_tiled_text((w, h), tag_text, small, -sign * rng.uniform(40, 60), rng,
                                     [(255, 255, 255), (30, 20, 10)], 95))
    mark.alpha_composite(_seal((w, h), center, k, rng))

    # Шум в прозрачности: края знака «рваные», у каждой картинки свои.
    arr = np.asarray(mark).astype(np.float32)
    noise = np.asarray(Image.effect_noise((w, h), 60).filter(ImageFilter.GaussianBlur(1)), dtype=np.float32)
    arr[..., 3] *= 0.75 + 0.5 * (noise / 255.0)
    if window is not None:
        arr[..., 3] *= _window_mask((w, h), window)
    mark = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")

    out = img.convert("RGBA")
    # Знак не только ложится поверх, но и меняет яркость рисунка под собой (как тиснение):
    # «стиралке» мало убрать белые буквы — под ними картинка уже другая.
    base = np.asarray(out).astype(np.float32)
    a = np.asarray(mark)[..., 3:4].astype(np.float32) / 255.0
    lum = np.asarray(mark.convert("L"), dtype=np.float32)[..., None] / 255.0
    base[..., :3] *= 1.0 - 0.35 * a * (1.0 - lum)
    out = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGBA")
    out.alpha_composite(mark)
    return out


# Telegram всё равно пережимает фото до 1280 px по длинной стороне — больше слать незачем,
# а каждый лишний килобайт на медленном канале до Telegram стоит секунды.
SEND_MAX_SIDE = 1280


def to_jpeg(img: Image.Image, max_side: int = SEND_MAX_SIDE, quality: int = 86) -> bytes:
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def render_card(art_bytes: bytes, data: dict, trial: bool = False, tag: str = "") -> tuple[bytes, bytes]:
    """Возвращает (полная карта с панелью, только карта в рамке без пергамента).

    tag — ID владельца, его вписывают в водяной знак пробной картищи.
    """
    art = Image.open(io.BytesIO(art_bytes))
    canvas = asset("background.jpg").copy()
    bottom = draw_panel(canvas, data)
    draw_card(canvas, art, data)
    crop_bottom = min(canvas.height, max(CARD_BOTTOM, bottom) + CROP_MARGIN)
    full = canvas.crop((CROP_X[0], CROP_TOP, CROP_X[1], crop_bottom))
    card_only = canvas.crop(CARD_ONLY_BOX)
    if trial:
        # Лицо — чистое окно; печать — ниже лица, на одежде.
        cx, cy, rx, ry = art_face_window(data, art.size)
        card_bottom = CARD_ONLY_BOX[3]
        unit = ((CARD_ONLY_BOX[2] - CARD_ONLY_BOX[0]) * (CARD_ONLY_BOX[3] - CARD_ONLY_BOX[1])) ** 0.5
        seal_r = unit * 0.23
        seal_y = min(cy + ry + seal_r * 1.05, card_bottom - seal_r * 1.1)
        for_full = (cx - CROP_X[0], cy - CROP_TOP, rx, ry)
        for_card = (cx - CARD_ONLY_BOX[0], cy - CARD_ONLY_BOX[1], rx, ry)
        full = watermark(full, tag, (cx - CROP_X[0], seal_y - CROP_TOP), unit, window=for_full)
        card_only = watermark(card_only, tag, (cx - CARD_ONLY_BOX[0], seal_y - CARD_ONLY_BOX[1]), unit,
                              window=for_card)
        return (to_jpeg(full, TRIAL_MAX_SIDE, TRIAL_JPEG_QUALITY),
                to_jpeg(card_only, TRIAL_MAX_SIDE, TRIAL_JPEG_QUALITY))
    return to_jpeg(full), to_jpeg(card_only)
