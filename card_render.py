"""Сборка изображения карты GWENT в том виде, как она выглядит на экране просмотра карты в игре.

Карта собирается из игровых спрайтов (рамка, ромб силы, провизия, броня, цифры), панель —
из шаблонов, извлечённых из скриншотов (их собирает tools/build_card_templates.py).
Все координаты — в системе скриншота 2756×1268; готовая картинка — его обрезка.
"""
import io
import os
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


def watermark(img: Image.Image) -> Image.Image:
    """Крупные диагональные надписи поверх всей картинки — для пробной картищи."""
    w, h = img.size
    fnt = font(FONT_NAME, max(36, int((w * h) ** 0.5) // 11))
    text = WATERMARK_TEXT
    step_x = round(fnt.getlength(text + "   "))
    step_y = round(fnt.size * 2.6)
    side = int((w ** 2 + h ** 2) ** 0.5) + step_x
    layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for row, y in enumerate(range(0, side, step_y)):
        for x in range(-step_x + (row % 2) * step_x // 2, side, step_x):
            draw.text((x, y), text, font=fnt, fill=(255, 255, 255, 120),
                      stroke_width=max(2, fnt.size // 18), stroke_fill=(0, 0, 0, 90))
    layer = layer.rotate(30, resample=Image.BICUBIC)
    left, top = (side - w) // 2, (side - h) // 2
    out = img.convert("RGBA")
    out.alpha_composite(layer.crop((left, top, left + w, top + h)))
    return out


def to_jpeg(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue()


def render_card(art_bytes: bytes, data: dict, trial: bool = False) -> tuple[bytes, bytes]:
    """Возвращает (полная карта с панелью, только карта в рамке без пергамента)."""
    art = Image.open(io.BytesIO(art_bytes))
    canvas = asset("background.jpg").copy()
    bottom = draw_panel(canvas, data)
    draw_card(canvas, art, data)
    crop_bottom = min(canvas.height, max(CARD_BOTTOM, bottom) + CROP_MARGIN)
    full = canvas.crop((CROP_X[0], CROP_TOP, CROP_X[1], crop_bottom))
    card_only = canvas.crop(CARD_ONLY_BOX)
    if trial:
        full, card_only = watermark(full), watermark(card_only)
    return to_jpeg(full), to_jpeg(card_only)
