"""Сборка изображения карты в стиле GWENT поверх шаблона, извлечённого из скриншотов игры.

Шаблоны лежат в assets/card (их собирает tools/build_card_templates.py). Все координаты ниже —
в системе исходного скриншота 2752×1536; готовая картинка — его обрезка CROP.
"""
import io
import os
import re
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "assets", "fonts")
CARD_DIR = os.path.join(BASE_DIR, "assets", "card")

FONT_REGULAR = "RobotoCondensed-Regular.ttf"
FONT_BOLD = "RobotoCondensed-Bold.ttf"
FONT_ITALIC = "RobotoCondensed-Italic.ttf"
FONT_NAME = "Play-Bold.ttf"
FONT_DIGITS = "TekturCond-Bold.ttf"
FONT_ICONS = "DejaVuSans.ttf"

# ---------- геометрия шаблона ----------
CROP = (190, 70, 2562, 1392)
CARD_ORIGIN = (257, 129)
ART_WINDOW = (279, 147, 1092, 1306)
POWER_CENTER, POWER_HEIGHT = (392, 244), 88
PROVISION_CENTER, PROVISION_HEIGHT = (1004, 1204), 120
ARMOR_CENTER = (392, 440)
HEADER = (1141, 123, 2492, 356)
NAME_POS, NAME_CAP, NAME_SQUEEZE = (1235, 185), 62, 0.80
CATEGORY_POS, CATEGORY_SIZE = (1233, 272), 55
ICON_BOX = (2296, 160, 2490, 352)
TEXT_LEFT, TEXT_RIGHT = 1224, 2452
BODY_TOP, BODY_BOTTOM = 396, 1290
SEPARATOR_X = 1440

FACTION_STYLES = {
    "Нильфгаард": {"header": None, "accent": (228, 180, 76), "icon": "☀"},
    "Чудовища": {"header": (120, 30, 26), "accent": (238, 150, 120), "icon": "☠"},
    "Скеллиге": {"header": (84, 54, 120), "accent": (200, 172, 236), "icon": "⚓"},
    "Королевства Севера": {"header": (38, 76, 128), "accent": (160, 198, 244), "icon": "⚜"},
    "Синдикат": {"header": (176, 92, 30), "accent": (252, 210, 150), "icon": "⚖"},
    "Скоя'таэли": {"header": (56, 96, 44), "accent": (182, 226, 130), "icon": "☘"},
    "Нейтральная": {"header": (96, 84, 72), "accent": (226, 212, 182), "icon": "⚔"},
}
DEFAULT_FACTION = FACTION_STYLES["Нейтральная"]
FRAME_FILES = {"Золотая": "frame_gold.png", "Бронзовая": "frame_bronze.png"}

NAME_COLOR = (243, 238, 228)
CATEGORY_COLOR = (226, 220, 208)
POWER_COLOR = (236, 218, 182)
PROVISION_COLOR = (244, 204, 128)
INK = (30, 24, 20)
KEYWORD_RED = (112, 22, 20)
KEYWORD_DARK = (14, 11, 9)
QUOTE_INK = (40, 32, 26)

# Термины GWENT, которые в тексте способности выделяются жирным (сравнение по началу слова).
KEYWORD_STEMS = (
    "размещени", "приказ", "взаимосвяз", "выносливост", "яд", "кровотечени", "уязвимост",
    "детонаци", "месть", "хитрост", "бессмерти", "порядок", "гармони", "дуэл", "заряд",
    "щит", "блокировк", "заблокир", "усилени", "казн", "призыв",
)
LABEL_RE = re.compile(r"(^|(?<=[.!?]\s)|(?<=\n))([^\s.!?:*][^.!?:*\n]{0,40}:)")

NORMAL, KEYWORD, BOLD = "normal", "keyword", "bold"


@lru_cache(maxsize=64)
def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


@lru_cache(maxsize=8)
def template(name: str) -> Image.Image:
    return Image.open(os.path.join(CARD_DIR, name)).convert("RGBA")


def strip_markup(text: str) -> str:
    return text.replace("*", "")


# ---------- разметка текста способности ----------
def parse_ability(text: str) -> list[tuple[str, str]]:
    """*слово* и «Метка:» в начале предложения — красным, термины GWENT — жирным."""
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
                segments.append((part[pos:start], NORMAL))
            segments.append((label, KEYWORD))
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
                    styled.append((word, BOLD))
                    continue
            styled.append((word, style))
    return styled


def style_font(style: str, size: int):
    return font(FONT_REGULAR if style == NORMAL else FONT_BOLD, size)


def style_color(style: str):
    return {NORMAL: INK, KEYWORD: KEYWORD_RED, BOLD: KEYWORD_DARK}[style]


def wrap_rich(tokens: list[tuple[str, str]], size: int, max_width: int) -> list[list[tuple[str, str, int]]]:
    """Перенос по словам с разными шрифтами. Возвращает строки из (слово, стиль, x)."""
    lines: list[list[tuple[str, str, int]]] = [[]]
    x = 0
    space = font(FONT_REGULAR, size).getlength(" ")
    for word, style in tokens:
        if word.isspace():
            if "\n" in word:
                lines.extend([] for _ in range(word.count("\n")))
                x = 0
            elif lines[-1]:
                x += space
            continue
        width = style_font(style, size).getlength(word)
        if lines[-1] and x + width > max_width:
            lines.append([])
            x = 0
        lines[-1].append((word, style, int(x)))
        x += width
    while lines and not lines[-1]:
        lines.pop()
    return lines


def wrap_plain(text: str, fnt, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and fnt.getlength(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


# ---------- примитивы ----------
def cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Масштабирует с обрезкой по центру (чуть выше центра — там обычно лицо)."""
    return ImageOps.fit(img.convert("RGB"), size, Image.LANCZOS, centering=(0.5, 0.4))


def text_layer(text: str, fnt, fill, squeeze: float = 1.0, stroke: int = 0, stroke_fill=None):
    """Текст на прозрачном слое, обрезанный по глифам; squeeze сужает по горизонтали."""
    left, top, right, bottom = fnt.getbbox(text, stroke_width=stroke)
    layer = Image.new("RGBA", (right - left + 4, bottom - top + 4), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((2 - left, 2 - top), text, font=fnt, fill=fill,
                               stroke_width=stroke, stroke_fill=stroke_fill)
    layer = layer.crop(layer.getbbox() or (0, 0, 1, 1))
    if squeeze != 1.0:
        layer = layer.resize((max(1, round(layer.width * squeeze)), layer.height), Image.LANCZOS)
    return layer


def with_shadow(layer: Image.Image, offset: int = 3, alpha: int = 170) -> Image.Image:
    out = Image.new("RGBA", (layer.width + offset, layer.height + offset), (0, 0, 0, 0))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(layer.getchannel("A").point(lambda a: a * alpha // 255))
    out.alpha_composite(shadow, (offset, offset))
    out.alpha_composite(layer, (0, 0))
    return out


def gradient_fill(layer: Image.Image, top, bottom) -> Image.Image:
    """Перекрашивает непрозрачные пиксели слоя вертикальным градиентом (как цифры в игре)."""
    t = np.linspace(0, 1, layer.height)[:, None, None]
    rgb = (np.array(top) * (1 - t) + np.array(bottom) * t).repeat(layer.width, axis=1)
    out = Image.fromarray(rgb.astype(np.uint8), "RGB").convert("RGBA")
    out.putalpha(layer.getchannel("A"))
    return out


def digits(value: int, height: int, fill) -> Image.Image:
    text = str(value)
    size = height
    fnt = font(FONT_DIGITS, size)
    # подбираем кегль так, чтобы высота цифр совпала с оригиналом
    for _ in range(3):
        _, top, _, bottom = fnt.getbbox(text)
        size = max(10, round(size * height / max(1, bottom - top)))
        fnt = font(FONT_DIGITS, size)
    light = tuple(min(255, c + 14) for c in fill)
    dark = tuple(int(c * 0.82) for c in fill)
    layer = text_layer(text, fnt, (255, 255, 255), stroke=max(2, height // 30), stroke_fill=(0, 0, 0))
    body = gradient_fill(text_layer(text, fnt, (255, 255, 255)), light, dark)
    outline = Image.new("RGBA", layer.size, (22, 16, 12, 255))
    outline.putalpha(layer.getchannel("A"))
    pad = max(2, height // 30)
    outline.alpha_composite(body, ((layer.width - body.width) // 2, pad))
    return with_shadow(outline, offset=max(2, height // 28))


def paste_centered(canvas: Image.Image, layer: Image.Image, center: tuple[int, int]):
    canvas.alpha_composite(layer, (center[0] - layer.width // 2, center[1] - layer.height // 2))


def tint_header(canvas: Image.Image, color):
    """Перекрашивает тёмную фактурную шапку в цвет фракции, сохраняя фактуру."""
    box = HEADER
    region = np.asarray(canvas.crop(box).convert("RGB")).astype(np.float32)
    lum = region.mean(axis=2, keepdims=True)
    tinted = lum / max(1.0, float(lum.mean())) * np.array(color, dtype=np.float32) * 0.9
    # края шапки (рамка панели) оставляем как есть
    mask = np.zeros(lum.shape[:2], dtype=np.float32)
    mask[6:-8, 6:-6] = 1.0
    mask = np.asarray(Image.fromarray((mask * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(4))).astype(np.float32)[..., None] / 255
    out = region * (1 - mask) + np.clip(tinted, 0, 255) * mask
    canvas.paste(Image.fromarray(out.astype(np.uint8), "RGB").convert("RGBA"), box[:2])


def shield_layer(value: int) -> Image.Image:
    w, h = 120, 136
    layer = Image.new("RGBA", (w + 6, h + 6), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pts = [(3, 3), (w + 3, 3), (w + 3, h * 0.55), (w / 2 + 3, h + 3), (3, h * 0.55)]
    d.polygon(pts, fill=(34, 30, 26, 255), outline=(206, 170, 96, 255), width=5)
    paste_centered(layer, digits(value, 62, POWER_COLOR), (w // 2 + 3, int(h * 0.44)))
    return with_shadow(layer, 4)


def faction_icon(faction: str, style: dict) -> Image.Image:
    if faction == "Нильфгаард":
        return template("icon_nilfgaard.png")
    size = 150
    fnt = font(FONT_ICONS, size)
    return with_shadow(text_layer(style["icon"], fnt, style["accent"], stroke=2, stroke_fill=(24, 18, 12)), 4)


def to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------- карта ----------
def draw_card(canvas: Image.Image, art: Image.Image, data: dict):
    x0, y0, x1, y1 = ART_WINDOW
    canvas.paste(cover(art, (x1 - x0, y1 - y0)), (x0, y0))
    frame = template(FRAME_FILES.get(data.get("frame"), FRAME_FILES["Бронзовая"]))
    canvas.alpha_composite(frame, CARD_ORIGIN)

    power = to_int(data.get("power"))
    if power is not None:
        paste_centered(canvas, digits(power, POWER_HEIGHT, POWER_COLOR), POWER_CENTER)
    provision = to_int(data.get("provision"))
    if provision is not None:
        paste_centered(canvas, digits(provision, PROVISION_HEIGHT, PROVISION_COLOR), PROVISION_CENTER)
    armor = to_int(data.get("armor"))
    if armor and armor > 0:
        paste_centered(canvas, shield_layer(armor), ARMOR_CENTER)


# ---------- панель ----------
def draw_header(canvas: Image.Image, data: dict):
    faction = data.get("faction")
    style = FACTION_STYLES.get(faction, DEFAULT_FACTION)
    if style["header"]:
        tint_header(canvas, style["header"])

    icon = faction_icon(faction, style)
    ix0, iy0, ix1, iy1 = ICON_BOX
    paste_centered(canvas, icon, ((ix0 + ix1) // 2, (iy0 + iy1) // 2))

    max_w = ix0 - NAME_POS[0] - 30
    name = strip_markup(data.get("name", "")).upper().strip() or " "
    size = round(NAME_CAP / 0.71)
    layer = text_layer(name, font(FONT_NAME, size), NAME_COLOR, squeeze=NAME_SQUEEZE)
    if layer.width > max_w:
        layer = layer.resize((max_w, max(1, round(layer.height * max(0.7, max_w / layer.width)))), Image.LANCZOS)
    canvas.alpha_composite(with_shadow(layer, 3, 200), NAME_POS)

    category = strip_markup(data.get("category", "")).strip()
    if category:
        size = CATEGORY_SIZE
        while size > 34 and font(FONT_REGULAR, size).getlength(category) > max_w:
            size -= 2
        fnt = font(FONT_REGULAR, size)
        cap_top = fnt.getbbox("Ч")[1]
        draw = ImageDraw.Draw(canvas)
        draw.text((CATEGORY_POS[0] + 2, CATEGORY_POS[1] - cap_top + 2), category, font=fnt, fill=(8, 6, 5))
        draw.text((CATEGORY_POS[0], CATEGORY_POS[1] - cap_top), category, font=fnt, fill=CATEGORY_COLOR)


def layout_body(data: dict, size: int):
    width = TEXT_RIGHT - TEXT_LEFT
    lines = wrap_rich(parse_ability(data.get("ability", "")), size, width)
    quote = strip_markup(data.get("quote", "")).strip()
    quote_lines = wrap_plain(quote, font(FONT_ITALIC, size), width) if quote else []
    pitch = size * 1.2
    height = len(lines) * pitch
    if quote_lines:
        height += pitch * 0.5 + 43 + len(quote_lines) * pitch * 0.985
    return lines, quote_lines, pitch, height


def draw_body(canvas: Image.Image, data: dict):
    size = 63
    while True:
        lines, quote_lines, pitch, height = layout_body(data, size)
        if BODY_TOP + height <= BODY_BOTTOM or size <= 34:
            break
        size -= 1

    draw = ImageDraw.Draw(canvas)
    cap_top = font(FONT_REGULAR, size).getbbox("Н")[1]
    y = BODY_TOP
    for line in lines:
        for word, style, offset in line:
            draw.text((TEXT_LEFT + offset, y - cap_top), word, font=style_font(style, size), fill=style_color(style))
        y += pitch

    if quote_lines:
        if lines:
            sep = template("separator.png")
            sep_y = round(y - pitch + size * 0.6 + pitch * 0.5 + 18)
            sep_x = SEPARATOR_X + (TEXT_RIGHT + TEXT_LEFT) // 2 - (SEPARATOR_X + sep.width // 2) - 20
            canvas.alpha_composite(sep, (sep_x, sep_y - sep.height // 2))
            y = sep_y + 43
        qf = font(FONT_ITALIC, size)
        q_cap = qf.getbbox("Н")[1]
        for line in quote_lines:
            draw.text((TEXT_LEFT, y - q_cap), line, font=qf, fill=QUOTE_INK)
            y += pitch * 0.985


def render_card(art_bytes: bytes, data: dict) -> bytes:
    art = Image.open(io.BytesIO(art_bytes))
    canvas = Image.open(os.path.join(CARD_DIR, "background.jpg")).convert("RGBA")
    draw_card(canvas, art, data)
    draw_header(canvas, data)
    draw_body(canvas, data)
    out = io.BytesIO()
    canvas.crop(CROP).convert("RGB").save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue()
