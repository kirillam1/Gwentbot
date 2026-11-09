"""Однократная сборка шаблонов карты из скриншотов-примеров (нужен numpy, на хосте не запускается).

Использование:
    python tools/build_card_templates.py gold1.jpg gold2.jpg assets/card

gold1/gold2 — два скриншота золотых карт с одинаковой рамкой (2752x1536). Всё, что в них
совпадает попиксельно, — это рамка; различаются только арт и текст.
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

CARD = (257, 129, 1108, 1322)
WIN = (279, 147, 1092, 1306)
DIAMOND_C, DIAMOND_R = (390, 262), 128
BADGE_FIELD = (927, 1105, 1081, 1296)
BREAD_ZONE = (925, 950, 1085, 1106)
# Уголок над ромбом, камень, завитки (координаты скриншота 2752x1536).
ORNAMENT_BOXES = ((279, 147, 492, 216), (279, 147, 338, 362))
PANEL = (1141, 123, 2492, 1333)
HEADER = (1141, 123, 2492, 356)
HEADER_TEXT = (1180, 150, 2290, 345)
ICON_BOX = (2296, 160, 2490, 352)
BODY = (1158, 366, 2476, 1318)
BODY_GRAIN = (1200, 1212, 2440, 1300)
HEADER_GRAIN = (1760, 150, 2260, 340)
SEPARATOR_BOX = (1440, 1000, 2240, 1040)
BUTTON_NUMBERS = ((925, 1424, 1016, 1470), (1418, 1424, 1512, 1470), (2004, 1424, 2094, 1470))


def L(arr):
    return arr[..., :3].astype(np.float32).mean(axis=2)


def mask_img(mask):
    return Image.fromarray((mask.astype(np.uint8) * 255), "L")


def dilate(mask, size):
    return np.asarray(mask_img(mask).filter(ImageFilter.MaxFilter(size))) > 127


def erode(mask, size):
    return np.asarray(mask_img(mask).filter(ImageFilter.MinFilter(size))) > 127


def _box_blur_axis(arr, r, axis):
    pad = [(0, 0)] * arr.ndim
    pad[axis] = (r + 1, r)
    padded = np.pad(arr, pad, mode="edge")
    c = np.cumsum(padded, axis=axis, dtype=np.float64)
    n = arr.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def blur_f(arr, radius):
    """Приближённое гауссово размытие float-массива: три прохода box-фильтра."""
    r = max(1, int(round(radius * 0.8)))
    out = arr.astype(np.float32)
    for _ in range(3):
        out = _box_blur_axis(_box_blur_axis(out, r, 0), r, 1)
    return out


def max_filter_f(arr, size):
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8), "L").filter(ImageFilter.MaxFilter(size))
    return np.asarray(img).astype(np.float32)


def tile(patch, h, w):
    """Зеркальная мозаика из патча до размера h×w (для зерна фактуры)."""
    ph, pw = patch.shape[:2]
    rows = []
    flip_y = False
    while sum(r.shape[0] for r in rows) < h:
        row, flip_x, width = [], False, 0
        p = patch[::-1] if flip_y else patch
        while width < w:
            row.append(p[:, ::-1] if flip_x else p)
            width += pw
            flip_x = not flip_x
        rows.append(np.concatenate(row, axis=1))
        flip_y = not flip_y
    return np.concatenate(rows, axis=0)[:h, :w]


def inpaint(arr, box, mask, grain_box=None, radius=18, grain_gain=1.0, known=None):
    """Нормализованная свёртка + зерно фактуры. arr — (H,W,3) float, mask/known — bool в координатах box."""
    x0, y0, x1, y1 = box
    region = arr[y0:y1, x0:x1].copy()
    known = (~mask if known is None else known & ~mask).astype(np.float32)
    num = blur_f(region * known[..., None], radius)
    den = blur_f(known, radius)[..., None]
    fill = num / np.maximum(den, 1e-3)
    # Где вокруг нет известных пикселей — берём всё более широкие проходы.
    r = radius
    while (den < 0.05).any() and r < max(region.shape[:2]):
        r *= 2
        wide_den = blur_f(known, r)[..., None]
        wide = blur_f(region * known[..., None], r) / np.maximum(wide_den, 1e-6)
        fill = np.where(den < 0.05, wide, fill)
        den = np.maximum(den, wide_den * 10)
    if grain_box is not None:
        gx0, gy0, gx1, gy1 = grain_box
        patch = arr[gy0:gy1, gx0:gx1]
        grain = patch - blur_f(patch, 4)
        fill = fill + tile(grain, y1 - y0, x1 - x0) * grain_gain
    alpha = blur_f(mask.astype(np.float32), 1.5)[..., None]
    arr[y0:y1, x0:x1] = region * (1 - alpha) + fill * alpha


def resynthesize(arr, box, grain_box, feather=14, radius=34):
    """Убирает текст с пергамента: маска тёмных штрихов вместе со светлым ореолом апскейла,
    заливка нормализованной свёрткой и зерно из чистого участка."""
    x0, y0, x1, y1 = box
    lum = L(arr[y0:y1, x0:x1])
    local = blur_f(max_filter_f(lum, 15), 10)
    text = dilate(lum < local - 14, 17)
    edge = np.zeros_like(text)
    edge[:feather, :] = edge[-feather:, :] = True
    edge[:, :feather] = edge[:, -feather:] = True
    # Закрашиваем строки целиком, иначе между словами остаются полосы от ореолов.
    rows = text[:, feather:-feather].mean(axis=1) > 0.02
    rows = np.convolve(rows.astype(float), np.ones(25), mode="same") > 0
    text = text | (rows[:, None] & ~edge)
    inpaint(arr, box, text & ~edge, grain_box, radius=radius, grain_gain=1.0)
    # Пятнистость старого пергамента — процедурный шум двух масштабов.
    rng = np.random.default_rng(7)
    h, w = y1 - y0, x1 - x0
    mottle = blur_f(rng.normal(0, 1, (h, w)), 28) * 160 + blur_f(rng.normal(0, 1, (h, w)), 70) * 260
    m = blur_f((text & ~edge).astype(np.float32), 6)
    arr[y0:y1, x0:x1] += (mottle * m)[..., None] * np.array([1.0, 0.95, 0.85])


def flood(mask, seeds):
    """Компоненты связности mask, достижимые из seeds."""
    img = mask_img(mask).copy()  # fromarray в Pillow 13 делит память с массивом, заливка её не меняет
    for seed in seeds:
        if img.getpixel(seed) == 255:
            ImageDraw.floodfill(img, seed, 128)
    return np.asarray(img) == 128


def diamond_mask(shape, offset, center, r):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    return (np.abs(xx + offset[0] - center[0]) + np.abs(yy + offset[1] - center[1])) <= r


def gradient_map(lum, stops):
    xs = [s[0] for s in stops]
    return np.stack([np.interp(lum, xs, [s[1][c] for s in stops]) for c in range(3)], axis=2)




def build(gold1, gold2, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    A = np.asarray(Image.open(gold1).convert("RGB")).astype(np.float32)
    B = np.asarray(Image.open(gold2).convert("RGB")).astype(np.float32)
    equal = np.abs(A - B).max(axis=2) <= 22

    # ---------- рамка ----------
    cx0, cy0, cx1, cy1 = CARD
    h, w = cy1 - cy0, cx1 - cx0
    rel = lambda box: (box[0] - cx0, box[1] - cy0, box[2] - cx0, box[3] - cy0)  # noqa: E731
    eq = equal[cy0:cy1, cx0:cx1]
    win = np.zeros((h, w), bool)
    wx0, wy0, wx1, wy1 = rel(WIN)
    win[wy0:wy1, wx0:wx1] = True
    border = ~win
    band = win & ~erode(win, 2 * 22 + 1)

    candidate = border | eq
    candidate = dilate(erode(candidate, 3), 3)
    candidate = erode(dilate(candidate, 5), 5) | border

    alpha = border | (band & candidate)
    # Фон артов у ромба почти совпадает, поэтому орнаменты выделяем по геометрии и цвету, а не по сравнению.
    crop = A[cy0:cy1, cx0:cx1]
    lum_c = L(crop)
    sat_c = crop.max(axis=2) - crop.min(axis=2)
    gold = ((crop[..., 0] - crop[..., 2]) > 40) & (lum_c > 60)
    for box in ORNAMENT_BOXES:
        ox0, oy0, ox1, oy1 = rel(box)
        zone = np.zeros((h, w), bool)
        zone[oy0:oy1, ox0:ox1] = True
        alpha |= dilate(gold & zone, 7) & zone
    ox0, oy0, ox1, oy1 = rel(BREAD_ZONE)
    zone = np.zeros((h, w), bool)
    zone[oy0:oy1, ox0:ox1] = True
    same = np.abs(A - B).max(axis=2)[cy0:cy1, cx0:cx1] <= 20
    sticker = ((sat_c > 45) & (lum_c > 45) | (lum_c > 120)) & same & zone
    sticker = erode(dilate(sticker, 9), 9)
    sticker = ~flood(~sticker & zone | ~zone, [(w // 2, h // 2)]) & zone
    alpha |= dilate(sticker, 9) & zone
    bx0, by0, bx1, by1 = BADGE_FIELD
    alpha |= diamond_mask((h, w), (cx0, cy0), DIAMOND_C, DIAMOND_R + 3)
    alpha[by0 - cy0 - 4:by1 - cy0, bx0 - cx0 - 4:bx1 - cx0] = True
    print("frame opaque share inside window:", round(float(alpha[win].mean()), 3))

    work = A.copy()
    # Поле ромба: перерисовываем всё внутри, кроме орнаментов у вершин.
    dc = DIAMOND_C
    dbox = (dc[0] - 112, dc[1] - 112, dc[0] + 113, dc[1] + 113)
    dh, dw = dbox[3] - dbox[1], dbox[2] - dbox[0]
    yy, xx = np.mgrid[0:dh, 0:dw]
    dx, dy = xx + dbox[0] - dc[0], yy + dbox[1] - dc[1]
    l1 = np.abs(dx) + np.abs(dy)
    tips = ((np.abs(dx) < 26) & (np.abs(dy) > 74)) | ((np.abs(dy) < 26) & (np.abs(dx) > 74))
    fill_mask = (l1 <= 92) & ~tips
    lum = L(work[dbox[1]:dbox[3], dbox[0]:dbox[2]])
    known = (l1 > 92) & (l1 <= 104) & (lum < 75) & ~tips
    inpaint(work, dbox, fill_mask, HEADER_GRAIN, radius=40, grain_gain=0.55, known=known)
    # Поле стоимости найма.
    fbox = (bx0 + 4, by0 + 4, bx1 - 4, by1 - 4)
    fh, fw = fbox[3] - fbox[1], fbox[2] - fbox[0]
    field_mask = np.zeros((fh, fw), bool)
    field_mask[12:-12, 12:-12] = True
    lum = L(work[fbox[1]:fbox[3], fbox[0]:fbox[2]])
    known = ~field_mask & (lum < 75)
    inpaint(work, fbox, field_mask, HEADER_GRAIN, radius=40, grain_gain=0.55, known=known)
    frame = work[cy0:cy1, cx0:cx1]

    a8 = (blur_f(alpha.astype(np.float32), 0.8) * 255).clip(0, 255).astype(np.uint8)
    Image.fromarray(np.dstack([frame.clip(0, 255).astype(np.uint8), a8]), "RGBA").save(
        os.path.join(out_dir, "frame_gold.png"), optimize=True)

    # Бронзовая версия: рамка — приглушённая бронза, поле ромба и найма — медь.
    lum = L(frame)
    bronze = gradient_map(lum, [(0, (10, 8, 6)), (60, (66, 46, 32)), (130, (136, 98, 70)),
                                (200, (192, 156, 120)), (255, (232, 210, 182))])
    copper = gradient_map(lum, [(0, (24, 10, 4)), (45, (116, 56, 24)), (90, (164, 88, 40)),
                                (160, (208, 150, 98)), (255, (242, 216, 182))])
    copper_zone = diamond_mask((h, w), (cx0, cy0), DIAMOND_C, 104)
    copper_zone[by0 - cy0 + 6:by1 - cy0 - 6, bx0 - cx0 + 6:bx1 - cx0 - 6] = True
    keep = np.zeros((h, w), bool)
    keep[BREAD_ZONE[1] - cy0:BREAD_ZONE[3] - cy0, BREAD_ZONE[0] - cx0:BREAD_ZONE[2] - cx0] = True
    keep &= ~copper_zone
    gem = np.zeros((h, w), bool)
    gem[150 - cy0:212 - cy0, 286 - cx0:338 - cx0] = True
    sat = frame.max(axis=2) - frame.min(axis=2)
    keep |= gem & (sat > 60)
    recolored = np.where(copper_zone[..., None], copper, bronze)
    soft = blur_f(keep.astype(np.float32), 2)[..., None]
    bronze_frame = recolored * (1 - soft) + frame * soft
    Image.fromarray(np.dstack([bronze_frame.clip(0, 255).astype(np.uint8), a8]), "RGBA").save(
        os.path.join(out_dir, "frame_bronze.png"), optimize=True)

    # ---------- фон и панель ----------
    bg = A.copy()
    x0, y0, x1, y1 = ICON_BOX
    region = bg[y0:y1, x0:x1]
    icon_mask = dilate((L(region) > 70) | ((region[..., 0] - region[..., 2]) > 35), 11)
    sun_alpha = blur_f(icon_mask.astype(np.float32), 1.2)
    Image.fromarray(np.dstack([region.astype(np.uint8), (sun_alpha * 255).clip(0, 255).astype(np.uint8)]),
                    "RGBA").save(os.path.join(out_dir, "icon_nilfgaard.png"), optimize=True)
    # Шапка: имя, категория и значок уходят вместе с низкочастотной перерисовкой тёмной фактуры.
    header_box = (HEADER_TEXT[0], HEADER_TEXT[1], ICON_BOX[2] - 6, HEADER_TEXT[3])
    hx0, hy0, hx1, hy1 = header_box
    region = bg[hy0:hy1, hx0:hx1]
    light = dilate(L(region) > 80, 13)
    inpaint(bg, header_box, light, HEADER_GRAIN, radius=24, grain_gain=0.9)

    # Пергамент: перерисовываем целиком — текст и разделитель исчезают без следов.
    resynthesize(bg, BODY, BODY_GRAIN)

    # Кнопки внизу: убираем стоимость, бот впишет свою по редкости.
    for nx0, ny0, nx1, ny1 in BUTTON_NUMBERS:
        box = (nx0 - 10, ny0 - 6, nx1 + 10, ny1 + 6)
        bh, bw = box[3] - box[1], box[2] - box[0]
        m = np.zeros((bh, bw), bool)
        m[6:-6, 10:-10] = True
        inpaint(bg, box, m, None, radius=12)

    Image.fromarray(bg.clip(0, 255).astype(np.uint8), "RGB").save(
        os.path.join(out_dir, "background.jpg"), quality=95, subsampling=0)

    # ---------- разделитель между способностью и цитатой ----------
    x0, y0, x1, y1 = SEPARATOR_BOX
    lum = L(A[y0:y1, x0:x1])
    local = blur_f(max_filter_f(lum, 11), 6)
    sep_alpha = ((local - lum - 4) * 6).clip(0, 255)
    sep_alpha *= (np.abs(np.arange(y1 - y0) - 19) <= 9)[:, None]
    sep = np.zeros((y1 - y0, x1 - x0, 4), np.uint8)
    sep[..., 0], sep[..., 1], sep[..., 2] = 52, 40, 30
    sep[..., 3] = sep_alpha.astype(np.uint8)
    Image.fromarray(sep, "RGBA").save(os.path.join(out_dir, "separator.png"), optimize=True)


if __name__ == "__main__":
    build(*sys.argv[1:4])
