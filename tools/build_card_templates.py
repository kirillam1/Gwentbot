"""Собирает шаблоны панели карты из скриншотов экрана просмотра карты GWENT (2756×1268).

Запуск (один раз, на машине разработчика):
    python tools/build_card_templates.py <папка_со_скриншотами> <gwentCardExporter/assets> assets/card

Скриншоты ищутся по временным меткам из SHOTS. Спрайты карты (рамки, ромбы силы,
провизия, броня, цифры) копируются из gwentCardExporter без изменений.
"""
import glob
import os
import shutil
import sys

import numpy as np
from PIL import Image, ImageFilter

SHOTS = {"nr": "03-23-52", "st": "03-24-00", "sy": "03-24-13", "ng": "03-24-22", "sk1": "03-24-33",
         "mo1": "03-24-58", "sk2": "03-25-58", "mo2": "03-26-49", "sk3": "03-29-18"}
PANEL_X = (871, 1866)
PANEL_TOP, BODY_TOP = 191, 342
PANEL_BOTTOM = {"nr": 891, "st": 609, "sy": 1022, "ng": 1032, "sk1": 770, "mo1": 871, "sk2": 730,
                "mo2": 780, "sk3": 840}
BOTTOM_ROWS = 24           # нижняя кромка панели (чистая во всех скриншотах)
HEADER_BANDS = ((193, 270), (270, 332))   # строка названия и строка категории
TEXT_START = 888
ICON_START = 1748
# фракция -> (скриншот, период узора шапки по горизонтали или None)
FACTIONS = {"northern_realms": ("nr", None), "scoiatael": ("st", None, True), "syndicate": ("sy", None),
            "nilfgaard": ("ng", None), "skellige": ("sk3", 255), "monster": ("mo2", None)}
NEUTRAL_TINT = (84, 74, 60)
SPRITES = ["Gold.png", "Bronze.png", "Armor.png", "Provisions.png", "Common.png", "Rare.png",
           "Epic.png", "Legendary.png"] + [f"{t}{d}.png" for t in "sp" for d in range(10)]
SPRITE_FACTIONS = ["Monster", "Neutral", "Nilfgaard", "Northern Realms", "Scoiatael", "Skellige", "Syndicate"]


def load_shots(folder):
    shots = {}
    for key, stamp in SHOTS.items():
        path = sorted(glob.glob(os.path.join(folder, "**", f"*{stamp}*.jpg"), recursive=True))[0]
        shots[key] = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    return shots


def text_end(a, y0, y1):
    """Правая граница белого текста шапки (название/категория/цифра) в полосе строк."""
    white = a[y0:y1, TEXT_START:ICON_START].min(axis=2) > 165
    xs = np.where(white.sum(axis=0) > 0)[0]
    return TEXT_START + int(xs.max()) if len(xs) else TEXT_START


def fill_band(a, y0, y1, period):
    """Заменяет текст в полосе копиями чистого участка узора между текстом и значком."""
    c0, c1 = text_end(a, y0, y1) + 16, ICON_START - 8
    width = c1 - c0
    out = a.copy()
    for x in range(TEXT_START, c0):
        if period and period <= width:
            src = x + ((c0 - x + period - 1) // period) * period
        else:
            # зеркальное замощение: стыков нет, узор шапок симметричен
            d = c0 - x - 1
            k, r = divmod(d, width)
            src = c1 - 1 - r if k % 2 else c0 + r
        out[y0:y1, x] = a[y0:y1, src]
    return out


def build_header(a, period, mirror_name=False):
    out = a
    for y0, y1 in HEADER_BANDS:
        out = fill_band(out, y0, y1, period)
    if mirror_name:
        # над текстом (строки 193–208) узор чистый; ниже — отражение строки категории
        y = HEADER_BANDS[0][1]
        for row in range(208, y):
            out[row, TEXT_START:ICON_START] = out[2 * y - row - 1, TEXT_START:ICON_START]
    # шов между полосами смягчаем лёгким размытием по вертикали
    y = HEADER_BANDS[0][1]
    seam = out[y - 3:y + 3, TEXT_START:ICON_START]
    out[y - 3:y + 3, TEXT_START:ICON_START] = seam.mean(axis=0, keepdims=True) * 0.5 + seam * 0.5
    x0, x1 = PANEL_X
    return Image.fromarray(out[PANEL_TOP:BODY_TOP, x0:x1 + 1].clip(0, 255).astype(np.uint8))


def neutral_header(img):
    a = np.asarray(img).astype(np.float32)
    lum = a.mean(axis=2, keepdims=True)
    inner = np.zeros(lum.shape[:2], np.float32)
    inner[4:-12, 4:-4] = 1
    inner = np.asarray(Image.fromarray((inner * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(3)))[..., None] / 255
    tinted = lum / lum[20:-20, 40:-200].mean() * np.array(NEUTRAL_TINT, np.float32)
    keep_icon = np.zeros_like(inner)
    keep_icon[:, ICON_START - PANEL_X[0] - 10:] = 1
    # значок и рамку оставляем как есть, перекрашиваем только фон
    gold = ((a[..., 0] - a[..., 2]) > 45)[..., None] & (keep_icon > 0)
    mix = np.where(gold, 0, inner)
    out = a * (1 - mix) + tinted * mix
    return Image.fromarray(out.clip(0, 255).astype(np.uint8))


def ink_mask(a):
    """Текст и разделитель заметно темнее окружающего пергамента; рамку панели не трогаем."""
    lum = a.mean(axis=2)
    img = Image.fromarray(lum.clip(0, 255).astype(np.uint8))
    bg = np.asarray(img.filter(ImageFilter.MaxFilter(17)).filter(ImageFilter.GaussianBlur(12))).astype(np.float32)
    m = np.zeros(lum.shape, bool)
    m[:, 14:-12] = (lum < bg - 16)[:, 14:-12]
    return np.asarray(Image.fromarray(m.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(9))) > 0


def build_body(shots):
    """Пергамент без текста: медиана по скриншотам, текст исключён маской.

    «ng» не берём: в его тексте вложенное изображение карты.
    """
    x0, x1 = PANEL_X
    shots = {k: a for k, a in shots.items() if k != "ng"}
    height = max(PANEL_BOTTOM[k] for k in shots) - BOTTOM_ROWS - BODY_TOP
    stack = np.full((len(shots), height, x1 - x0 + 1, 3), np.nan, np.float32)
    for i, (key, a) in enumerate(shots.items()):
        h = PANEL_BOTTOM[key] - BOTTOM_ROWS - BODY_TOP
        reg = a[BODY_TOP:BODY_TOP + h, x0:x1 + 1]
        mask = ink_mask(reg)
        reg = reg.copy()
        reg[mask] = np.nan
        stack[i, :h] = reg
    valid = ~np.isnan(stack[..., 0])
    count = valid.sum(axis=0)
    med = np.zeros(stack.shape[1:], np.float32)
    has = count > 0
    with np.errstate(all="ignore"):
        med[has] = np.nanmedian(stack[:, has], axis=0)
    # дыры: нормализованная свёртка (только низкие частоты — пергамент там ровный)
    filled = med.copy()
    weight = has.astype(np.float32)
    for radius in (6, 16, 40):
        num = np.stack([_blur(filled[..., c] * weight, radius) for c in range(3)], axis=-1)
        den = _blur(weight, radius)[..., None]
        est = num / np.maximum(den, 1e-3)
        hole = (weight == 0) & (den[..., 0] > 0.05)
        filled[hole] = est[hole]
        weight = np.where(hole, 1.0, weight)
    holes = float((count == 0).mean())
    return Image.fromarray(filled.clip(0, 255).astype(np.uint8)), holes


def _blur(a, radius):
    k = 2 * radius + 1
    pad = np.pad(a, radius, mode="edge")
    c = np.cumsum(np.cumsum(pad, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s / (k * k)


def build_bottom(shots):
    x0, x1 = PANEL_X
    rows = [a[PANEL_BOTTOM[k] - BOTTOM_ROWS:PANEL_BOTTOM[k] + 2, x0:x1 + 1] for k, a in shots.items()]
    return Image.fromarray(np.median(np.stack(rows), axis=0).astype(np.uint8))


def build_separator(a):
    """Разделитель из «st»: альфа = затемнение относительно пергамента."""
    reg = a[498:522, 1040:1694]
    lum = reg.mean(axis=2)
    bg = np.median(np.concatenate([lum[:3], lum[-3:]]), axis=0, keepdims=True)
    alpha = ((bg - lum) / np.maximum(bg, 1) * 255 * 1.6).clip(0, 255)
    alpha[alpha < 12] = 0
    rgba = np.zeros(reg.shape[:2] + (4,), np.uint8)
    rgba[..., :3] = (40, 33, 26)
    rgba[..., 3] = alpha.astype(np.uint8)
    img = Image.fromarray(rgba, "RGBA")
    return img.crop(img.getbbox())


def build_background(shots):
    """Фон экрана из «sk2»: под панелью — отражение нижнего фона (он сильно размыт)."""
    a = shots["sk2"].copy()
    x0, x1 = PANEL_X[0] - 12, PANEL_X[1] + 14
    bottom = PANEL_BOTTOM["sk2"] + 14
    for y in range(PANEL_TOP - 8, bottom):
        a[y, x0:x1] = a[min(a.shape[0] - 1, 2 * bottom - y), x0:x1]
    img = Image.fromarray(a.astype(np.uint8))
    blurred = img.filter(ImageFilter.GaussianBlur(10))
    mask = Image.new("L", img.size, 0)
    mask.paste(255, (x0 - 10, PANEL_TOP - 18, x1 + 10, bottom + 10))
    mask = mask.filter(ImageFilter.GaussianBlur(8))
    return Image.composite(blurred, img, mask)


def main(shot_dir, sprite_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    shots = load_shots(shot_dir)
    for name, (key, period, *mirror) in FACTIONS.items():
        build_header(shots[key], period, *mirror).save(os.path.join(out_dir, f"header_{name}.png"))
    neutral_header(Image.open(os.path.join(out_dir, "header_skellige.png")).convert("RGB")).save(
        os.path.join(out_dir, "header_neutral.png"))
    body, holes = build_body(shots)
    body.save(os.path.join(out_dir, "panel_body.png"))
    build_bottom(shots).save(os.path.join(out_dir, "panel_bottom.png"))
    build_separator(shots["st"]).save(os.path.join(out_dir, "separator.png"))
    build_background(shots).save(os.path.join(out_dir, "background.jpg"), quality=92)
    sprite_out = os.path.join(out_dir, "sprites")
    os.makedirs(sprite_out, exist_ok=True)
    names = SPRITES + [f"{f}_{p}.png" for f in SPRITE_FACTIONS for p in ("diamond", "prov")]
    for n in names:
        shutil.copy(os.path.join(sprite_dir, n), os.path.join(sprite_out, n.replace(" ", "_")))
    print("body holes", round(holes, 4))


if __name__ == "__main__":
    main(*sys.argv[1:4])
