"""Сборка изображения карты в стиле GWENT: арт в рамке + панель с текстом."""
import io
import os
import re
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
FONT_REGULAR = "PT_Sans-Narrow-Web-Regular.ttf"
FONT_BOLD = "PT_Sans-Narrow-Web-Bold.ttf"
FONT_ITALIC = "PT_Sans-Web-Italic.ttf"
FONT_ICONS = "DejaVuSans.ttf"

CANVAS_W, CANVAS_H = 1600, 900
CARD_X, CARD_Y, CARD_W, CARD_H = 60, 80, 520, 740
BORDER = 12
PANEL_X, PANEL_W = 630, 910
HEADER_H = 150
PAD = 44

# header — цвет плашки с именем, accent — цвет значка фракции.
FACTION_STYLES = {
    "Нильфгаард": {"header": (40, 35, 30), "accent": (228, 180, 76), "icon": "☀"},
    "Чудовища": {"header": (96, 24, 22), "accent": (238, 150, 120), "icon": "☠"},
    "Скеллиге": {"header": (64, 42, 92), "accent": (200, 172, 236), "icon": "⚓"},
    "Королевства Севера": {"header": (30, 60, 100), "accent": (160, 198, 244), "icon": "⚜"},
    "Синдикат": {"header": (156, 82, 26), "accent": (252, 210, 150), "icon": "⚖"},
    "Скоя'таэли": {"header": (46, 78, 36), "accent": (182, 226, 130), "icon": "☘"},
    "Нейтральная": {"header": (74, 66, 58), "accent": (226, 212, 182), "icon": "⚔"},
}
DEFAULT_FACTION = FACTION_STYLES["Нейтральная"]

FRAME_STYLES = {
    "Золотая": {"light": (240, 204, 118), "dark": (138, 96, 36)},
    "Бронзовая": {"light": (196, 136, 88), "dark": (100, 60, 32)},
}

DARK_FILL = (30, 27, 24)
TEXT_LIGHT = (238, 228, 204)
PROVISION_COLOR = (244, 202, 112)
PARCHMENT = (218, 203, 170)
INK = (42, 33, 25)
KEYWORD_RED = (124, 22, 18)
KEYWORD_DARK = (22, 17, 12)
QUOTE_INK = (62, 50, 38)

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


def fit_font(text: str, name: str, start: int, minimum: int, max_width: int):
    size = start
    while size > minimum and font(name, size).getlength(text) > max_width:
        size -= 2
    return font(name, size)


# ---------- фактура ----------
def noise_overlay(size: tuple[int, int], strength: int = 18) -> Image.Image:
    noise = Image.effect_noise((size[0] // 2, size[1] // 2), strength).resize(size).filter(ImageFilter.GaussianBlur(1))
    return noise


def textured(size: tuple[int, int], color: tuple[int, int, int], vignette: float = 0.35) -> Image.Image:
    base = Image.new("RGB", size, color)
    noise = noise_overlay(size)
    base = Image.blend(base, Image.merge("RGB", (noise, noise, noise)), 0.08)
    # Затемнение к краям, как у старого пергамента.
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle((size[0] * 0.06, size[1] * 0.08, size[0] * 0.94, size[1] * 0.92), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(min(size) * 0.12))
    dark = Image.new("RGB", size, tuple(int(c * (1 - vignette)) for c in color))
    return Image.composite(base, dark, mask)


def background() -> Image.Image:
    bg = Image.new("RGB", (CANVAS_W, CANVAS_H), (16, 13, 11))
    glow = Image.new("L", (CANVAS_W, CANVAS_H), 0)
    ImageDraw.Draw(glow).ellipse((200, 100, CANVAS_W - 200, CANVAS_H - 100), fill=255)
    glow = glow.filter(ImageFilter.GaussianBlur(220))
    bg = Image.composite(Image.new("RGB", bg.size, (44, 36, 29)), bg, glow)
    noise = noise_overlay(bg.size, 30)
    return Image.blend(bg, Image.merge("RGB", (noise, noise, noise)), 0.04)


def drop_shadow(canvas: Image.Image, box: tuple[int, int, int, int], radius: int = 18, offset: int = 10):
    shadow = Image.new("L", canvas.size, 0)
    x0, y0, x1, y1 = box
    ImageDraw.Draw(shadow).rounded_rectangle((x0 + offset, y0 + offset, x1 + offset, y1 + offset), 10, fill=190)
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius))
    canvas.paste(Image.new("RGB", canvas.size, (0, 0, 0)), (0, 0), shadow)


# ---------- карта ----------
def diamond(cx: int, cy: int, r: int):
    return [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]


def shield(cx: int, cy: int, s: int):
    return [(cx - s, cy - s), (cx + s, cy - s), (cx + s, cy + s * 0.2), (cx, cy + s * 1.15), (cx - s, cy + s * 0.2)]


def centered_text(draw: ImageDraw.ImageDraw, center: tuple[int, int], text: str, fnt, fill):
    draw.text(center, text, font=fnt, fill=fill, anchor="mm", stroke_width=2, stroke_fill=(10, 8, 6))


def draw_card(canvas: Image.Image, art: Image.Image, data: dict):
    frame = FRAME_STYLES.get(data.get("frame"), FRAME_STYLES["Бронзовая"])
    x0, y0, x1, y1 = CARD_X, CARD_Y, CARD_X + CARD_W, CARD_Y + CARD_H
    drop_shadow(canvas, (x0, y0, x1, y1))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((x0, y0, x1, y1), 8, fill=frame["dark"])
    draw.rounded_rectangle((x0 + 3, y0 + 3, x1 - 3, y1 - 3), 7, fill=frame["light"])
    draw.rectangle((x0 + BORDER - 2, y0 + BORDER - 2, x1 - BORDER + 2, y1 - BORDER + 2), fill=frame["dark"])

    inner = (CARD_W - 2 * BORDER, CARD_H - 2 * BORDER)
    art = ImageOps.fit(art.convert("RGB"), inner, Image.LANCZOS, centering=(0.5, 0.3))
    canvas.paste(art, (x0 + BORDER, y0 + BORDER))
    draw = ImageDraw.Draw(canvas)

    # Сила — ромб в левом верхнем углу.
    cx, cy, r = x0 + 82, y0 + 82, 68
    draw.polygon(diamond(cx, cy, r + 4), fill=frame["dark"])
    draw.polygon(diamond(cx, cy, r), fill=frame["light"])
    draw.polygon(diamond(cx, cy, r - 8), fill=DARK_FILL)
    draw.polygon(diamond(cx, cy, r - 14), outline=frame["dark"], width=2)
    centered_text(draw, (cx, cy + 2), str(data.get("power", "")), font(FONT_BOLD, 88), TEXT_LIGHT)

    # Броня — щит под ромбом (только если она есть).
    armor = str(data.get("armor", "0"))
    if armor.isdigit() and int(armor) > 0:
        sx, sy, s = cx, cy + r + 46, 34
        draw.polygon(shield(sx, sy, s + 4), fill=frame["dark"])
        draw.polygon(shield(sx, sy, s), fill=frame["light"])
        draw.polygon(shield(sx, sy, s - 5), fill=DARK_FILL)
        centered_text(draw, (sx, sy + 2), armor, font(FONT_BOLD, 44), TEXT_LIGHT)

    # Стоимость найма — плашка в правом нижнем углу.
    bw, bh = 106, 126
    bx1, by1 = x1 - BORDER + 2, y1 - BORDER + 2
    bx0, by0 = bx1 - bw, by1 - bh
    draw.rectangle((bx0 - 4, by0 - 4, bx1, by1), fill=frame["dark"])
    draw.rectangle((bx0, by0, bx1, by1), fill=frame["light"])
    draw.rectangle((bx0 + 6, by0 + 6, bx1 - 6, by1 - 6), fill=DARK_FILL)
    centered_text(draw, ((bx0 + bx1) // 2, (by0 + by1) // 2 + 2), str(data.get("provision", "")),
                  font(FONT_BOLD, 90), PROVISION_COLOR)


def layout_body(data: dict, width: int, ability_size: int):
    tokens = parse_ability(data.get("ability", ""))
    ability_lines = wrap_rich(tokens, ability_size, width)
    quote_size = max(26, ability_size - 6)
    quote_font = font(FONT_ITALIC, quote_size)
    quote = data.get("quote", "").strip()
    quote_lines = wrap_plain(quote, quote_font, width) if quote else []
    ability_lh, quote_lh = int(ability_size * 1.22), int(quote_size * 1.3)
    height = len(ability_lines) * ability_lh
    if quote_lines:
        height += 60 + len(quote_lines) * quote_lh
    return ability_lines, ability_lh, quote_lines, quote_font, quote_lh, height


def draw_panel(canvas: Image.Image, data: dict):
    style = FACTION_STYLES.get(data.get("faction"), DEFAULT_FACTION)
    text_w = PANEL_W - 2 * PAD

    ability_size = 40
    while True:
        ability_lines, ability_lh, quote_lines, quote_font, quote_lh, body_h = layout_body(data, text_w, ability_size)
        if HEADER_H + body_h + 2 * PAD <= CARD_H or ability_size <= 26:
            break
        ability_size -= 2
    panel_h = max(HEADER_H + body_h + 2 * PAD, 360)
    x0, y0 = PANEL_X, CARD_Y
    x1, y1 = x0 + PANEL_W, y0 + panel_h

    drop_shadow(canvas, (x0, y0, x1, y1))
    canvas.paste(textured((PANEL_W, panel_h - HEADER_H), PARCHMENT, 0.18), (x0, y0 + HEADER_H))
    header = textured((PANEL_W, HEADER_H), style["header"], 0.45)
    canvas.paste(header, (x0, y0))
    draw = ImageDraw.Draw(canvas)
    draw.line((x0, y0 + HEADER_H, x1, y0 + HEADER_H), fill=(20, 16, 12), width=3)
    draw.rectangle((x0, y0, x1, y1), outline=(56, 46, 36), width=3)

    # Шапка: имя, категория, значок фракции.
    icon_size = 92
    name_max = PANEL_W - 2 * PAD - icon_size - 30
    name = strip_markup(data.get("name", "")).upper()
    name_font = fit_font(name, FONT_BOLD, 66, 34, name_max)
    draw.text((x0 + PAD, y0 + 62), name, font=name_font, fill=(246, 240, 228), anchor="ls",
              stroke_width=1, stroke_fill=(15, 12, 10))
    category_font = fit_font(data.get("category", ""), FONT_REGULAR, 36, 24, name_max)
    draw.text((x0 + PAD, y0 + 112), data.get("category", ""), font=category_font, fill=(226, 216, 196), anchor="ls")
    icon_center = (x1 - PAD - icon_size // 2, y0 + HEADER_H // 2)
    draw.text((icon_center[0] + 3, icon_center[1] + 3), style["icon"], font=font(FONT_ICONS, icon_size),
              fill=(10, 8, 6), anchor="mm")
    draw.text(icon_center, style["icon"], font=font(FONT_ICONS, icon_size), fill=style["accent"], anchor="mm",
              stroke_width=1, stroke_fill=(20, 16, 12))

    # Текст способности.
    y = y0 + HEADER_H + PAD
    for line in ability_lines:
        for word, word_style, offset in line:
            draw.text((x0 + PAD + offset, y), word, font=style_font(word_style, ability_size),
                      fill=style_color(word_style), anchor="la")
        y += ability_lh

    # Разделитель и цитата.
    if quote_lines:
        y += 24
        sep_w = int(text_w * 0.7)
        sx = x0 + PAD + 10
        for i in range(sep_w):
            fade = min(i, sep_w - i) / (sep_w * 0.25)
            alpha = min(1.0, fade)
            color = tuple(int(PARCHMENT[c] + (110 - PARCHMENT[c]) * alpha) for c in range(3))
            draw.line((sx + i, y, sx + i, y + 2), fill=color)
        y += 30
        for line in quote_lines:
            draw.text((x0 + PAD, y), line, font=quote_font, fill=QUOTE_INK, anchor="la")
            y += quote_lh


def render_card(art_bytes: bytes, data: dict) -> bytes:
    art = Image.open(io.BytesIO(art_bytes))
    canvas = background()
    draw_card(canvas, art, data)
    draw_panel(canvas, data)
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue()
