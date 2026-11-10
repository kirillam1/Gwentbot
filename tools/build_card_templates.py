"""Сборка шаблонов карты из скриншотов окна просмотра карты GWENT (2756x1268) и игровых спрайтов.

Запускается один раз на машине разработчика (нужен numpy), результат лежит в assets/card:

    python tools/build_card_templates.py <папка со скриншотами> <папка gwentCardExporter/assets> assets/card

Скриншоты (имена — ключи SHOTS) должны быть в одном масштабе: панель начинается в (869, 192).
Спрайты ромбов, плашек найма, щита и цифр — из https://github.com/pramberg451/gwentCardExporter.
Все координаты ниже — в системе скриншота sk3 («Арнагад»).
"""
import glob
import json
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

SHOTS = {  # ключ -> подстрока имени файла
    "nr": "03-23-52", "st": "03-24-00", "sy": "03-24-13", "ng": "03-24-22", "sk1": "03-24-33",
    "mo1": "03-24-58", "sk2": "03-25-58", "mo2": "03-26-49", "sk3": "03-29-18",
}
# Смещение карты относительно sk3 (подобрано совмещением рамки) и низ панели.
CARD_OFF = {"nr": (3, -1), "st": (3, 0), "sy": (7, -2), "ng": (4, -6), "sk1": (5, -1),
            "mo1": (0, -1), "sk2": (0, -1), "mo2": (0, -1), "sk3": (0, 0)}
PANEL_BOTTOM = {"nr": 891, "st": 609, "sy": 1022, "ng": 1032, "sk1": 770, "mo1": 871, "sk2": 730, "mo2": 780, "sk3": 840}

FACTIONS = {  # имя в боте -> (спрайты экспортёра, скриншоты для шапки, скриншот со значком)
    "Королевства Севера": ("Northern Realms", ["nr"], "nr"),
    "Скоя'таэли": ("Scoiatael", ["st"], "st"),
    "Синдикат": ("Syndicate", ["sy"], "sy"),
    "Нильфгаард": ("Nilfgaard", ["ng"], "ng"),
    "Скеллиге": ("Skellige", ["sk1", "sk2", "sk3"], "sk2"),
    "Чудовища": ("Monster", ["mo1", "mo2"], "mo2"),
}

HEADER_BOX = (860, 180, 1880, 332)
TEXT_ZONE = (885, 198, 1770, 332)       # цифра силы, имя и категория
ICON_BOX = (1768, 196, 1868, 330)
BODY_X = (860, 1880)
BODY_TOP = 332
BODY_H = 700                             # высота эталонной текстуры пергамента
CARD_BOX = (262, 170, 860, 1048)         # область карты вместе с тенью
CARD_OUTER = (286, 194, 834, 1023)


def lum(a):
    return a[..., :3].mean(axis=2)


def dilate(mask, r):
    im = Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(2 * r + 1))
    return np.asarray(im) > 0


def blur(a, r):
    return np.asarray(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r))).astype(np.float32)


def norm_fill(img, valid, r=6, rounds=6):
    """Заполняет пиксели вне valid взвешенным средним соседей (нормированная свёртка)."""
    img = img.copy()
    valid = valid.copy()
    for _ in range(rounds):
        if valid.all():
            break
        w = valid.astype(np.float32)
        num = np.stack([blur_f(img[..., c] * w, r) for c in range(img.shape[2])], -1)
        den = blur_f(w, r)[..., None]
        fill = num / np.maximum(den, 1e-4)
        ok = (den[..., 0] > 0.02) & ~valid
        img[ok] = fill[ok]
        valid |= ok
        r *= 2
    return img


def blur_f(a, r):
    """Гауссово размытие float-массива (сепарабельная свёртка, края — отражение)."""
    k = np.arange(-int(3 * r) - 1, int(3 * r) + 2)
    k = np.exp(-k ** 2 / (2 * r * r))
    k /= k.sum()
    pad = len(k) // 2
    out = np.pad(a, ((pad, pad), (0, 0)), mode="reflect")
    out = np.apply_along_axis(lambda v: np.convolve(v, k, "valid"), 0, out)
    out = np.pad(out, ((0, 0), (pad, pad)), mode="reflect")
    return np.apply_along_axis(lambda v: np.convolve(v, k, "valid"), 1, out).astype(np.float32)


def periodic_fill(img, valid, max_shift=400):
    """Заполняет дыры копиями той же строки, сдвинутыми на период узора по горизонтали."""
    h, w, _ = img.shape
    g = lum(img)
    best = None
    for p in range(40, max_shift):
        a, b = g[:, :-p], g[:, p:]
        m = valid[:, :-p] & valid[:, p:]
        if m.sum() < 2000:
            continue
        x, y = a[m] - a[m].mean(), b[m] - b[m].mean()
        c = float((x * y).sum() / np.sqrt((x * x).sum() * (y * y).sum()))
        if best is None or c > best[0]:
            best = (c, p)
    corr, period = best
    out = img.copy()
    filled = valid.copy()
    for k in range(1, w // period + 1):
        for sign in (1, -1):
            shift = sign * k * period
            src = np.roll(img, -shift, axis=1)
            srcv = np.roll(valid, -shift, axis=1)
            if shift > 0:
                srcv[:, w - shift:] = False
            else:
                srcv[:, :-shift] = False
            take = ~filled & srcv
            out[take] = src[take]
            filled |= take
    return out, filled, period, corr


def load_shots(folder):
    shots = {}
    for key, part in SHOTS.items():
        path = glob.glob(os.path.join(folder, "**", f"*{part}*.jpg"), recursive=True)[0]
        shots[key] = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    return shots


# ---------- шапки ----------
def text_mask(a):
    """Маска надписей в шапке: белый текст, его тёмная обводка и тень."""
    x0, y0, x1, y1 = HEADER_BOX
    reg = a[y0:y1, x0:x1]
    L = lum(reg)
    zone = np.zeros(L.shape, bool)
    zone[TEXT_ZONE[1] - y0:TEXT_ZONE[3] - y0, TEXT_ZONE[0] - x0:TEXT_ZONE[2] - x0] = True
    text = (L > 110) & zone
    near = dilate(text, 14) & zone
    bg = norm_fill(reg, ~dilate(text, 14), r=4)
    halo = near & (L < lum(bg) - 7)
    m = dilate(text, 2) | dilate(halo, 2)
    # у цифры силы толстая чёрная обводка и тень
    digit = np.zeros(L.shape, bool)
    digit[15:150, 15:135] = True
    dtext = text & digit
    m |= dilate(dtext, 6) | (dilate(dtext, 16) & (L < 45))
    return dilate(m, 1)


def build_header(shots, keys, icon_key):
    x0, y0, x1, y1 = HEADER_BOX
    stack, valid = [], []
    for k in keys:
        reg = shots[k][y0:y1, x0:x1]
        m = text_mask(shots[k])
        icon = np.zeros(m.shape, bool)
        icon[ICON_BOX[1] - y0:ICON_BOX[3] - y0, ICON_BOX[0] - x0 - 6:ICON_BOX[2] - x0] = True
        stack.append(reg)
        valid.append(~m & ~icon)
    stack, valid = np.stack(stack), np.stack(valid)
    out = np.zeros(stack.shape[1:], np.float32)
    ok = valid.any(0)
    with np.errstate(all="ignore"):
        for c in range(3):
            v = np.where(valid, stack[..., c], np.nan)
            out[..., c] = np.nan_to_num(np.nanmedian(v, axis=0))
    # дыры: периодический узор только внутри узорной области шапки
    inner = np.zeros(ok.shape, bool)
    inner[14:-2, 12:-14] = True
    pat_valid = ok | ~inner
    filled, fv, period, corr = periodic_fill(out, pat_valid)
    out = np.where(fv[..., None], filled, out)
    out = norm_fill(out, fv & (ok | ~inner | fv), r=3)
    # значок фракции — с выбранного скриншота, с мягкими краями
    src = shots[icon_key][y0:y1, x0:x1]
    feather = np.zeros(ok.shape, np.float32)
    ix0, iy0, ix1, iy1 = ICON_BOX[0] - x0 - 4, ICON_BOX[1] - y0, ICON_BOX[2] - x0, ICON_BOX[3] - y0
    feather[iy0:iy1, ix0:ix1] = 1
    feather = blur_f(feather, 3)[..., None]
    out = out * (1 - feather) + src * feather
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)), period, corr


# ---------- пергамент ----------
def body_sample(a, bottom):
    x0, x1 = BODY_X
    reg = Image.fromarray(a[BODY_TOP:bottom + 16, x0:x1].astype(np.uint8)).resize((x1 - x0, BODY_H), Image.BICUBIC)
    r = np.asarray(reg).astype(np.float32)
    L = lum(r)
    bg = blur_f(L, 10)
    ink = (L < bg - 14) | ((r[..., 0] - r[..., 1] > 28) & (L < 150))
    # разделитель: длинная тонкая горизонтальная линия
    line_rows = ((L[:, 220:800] < bg[:, 220:800] - 5).mean(1) > 0.5)
    ink[dilate(line_rows[:, None].repeat(4, 1), 6)[:, 0], 160:860] = True
    ink[:16] = False          # верхняя кромка
    ink[-30:] = False         # нижняя кромка
    ink[:, :18] = False
    ink[:, -18:] = False
    return r, ~dilate(ink, 3)


def build_body(shots):
    """Текстура пергамента растягивается игрой по высоте панели. Основа — пара скриншотов почти
    одинаковой высоты (sy, ng): там, где текст в одном, берём пиксели из другого."""
    base_keys = ("nr", "mo1")
    # в ng в тексте вклеена мини-карта — его не используем
    samples = {k: body_sample(shots[k], PANEL_BOTTOM[k]) for k in shots if k != "ng"}
    out = np.zeros_like(samples["sy"][0])
    cnt = np.zeros(out.shape[:2], np.float32)
    for k in base_keys:
        r, v = samples[k]
        out[v] += r[v]
        cnt[v] += 1
    ok = cnt > 0
    out[ok] /= cnt[ok][:, None]
    # дыры, где текст в обоих: медиана остальных (текстура совпадает приблизительно)
    rest = [samples[k] for k in samples if k not in base_keys]
    stack = np.stack([r for r, _ in rest]); valid = np.stack([v for _, v in rest])
    with np.errstate(all="ignore"):
        med = np.stack([np.nanmedian(np.where(valid, stack[..., c], np.nan), 0) for c in range(3)], -1)
    take = ~ok & valid.any(0)
    out[take] = med[take]
    ok |= take
    out = norm_fill(out, ok, r=3)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)), float(1 - ok.mean())


def build_separator(shots):
    """Разделитель между способностью и цитатой (st: y≈508)."""
    crops = []
    for k, y in (("st", 508), ("sy", 874), ("sk3", 691)):
        crops.append(shots[k][y - 10:y + 11, 1050:1690])
    a = np.median(np.stack(crops), 0)
    L = lum(a)
    bg = np.median(np.concatenate([L[:3], L[-3:]]), 0)[None, :]
    alpha = np.clip((bg - L) / 70.0, 0, 1)
    alpha[alpha < 0.06] = 0
    rgba = np.zeros(a.shape[:2] + (4,), np.uint8)
    rgba[..., :3] = (60, 46, 34)
    rgba[..., 3] = (alpha * 255).astype(np.uint8)
    im = Image.fromarray(rgba)
    return im.crop(im.getbbox())


# ---------- рамка карты ----------
def build_frame(shots):
    """Рамка: пиксели, одинаковые во всех скриншотах. Полупрозрачная «грязь» по краю арта —
    альфа из разброса значений (у арта разброс большой, у рамки — нулевой)."""
    x0, y0, x1, y1 = CARD_BOX
    stack = []
    for k, a in shots.items():
        dx, dy = CARD_OFF[k]
        stack.append(a[y0 + dy:y1 + dy, x0 + dx:x1 + dx])
    stack = np.stack(stack)
    med = np.median(stack, 0)
    spread = np.median(np.abs(stack - med[None]), 0).mean(-1)   # MAD
    art_mad = 38.0
    alpha = np.clip(1 - spread / art_mad, 0, 1)
    # внутри окна арта (дальше 40px от края) рамки нет
    h, w = alpha.shape
    ox0, oy0, ox1, oy1 = (CARD_OUTER[0] - x0, CARD_OUTER[1] - y0, CARD_OUTER[2] - x0, CARD_OUTER[3] - y0)
    inner = np.zeros(alpha.shape, bool)
    inner[oy0 + 34:oy1 - 34, ox0 + 34:ox1 - 34] = True
    alpha[inner] = 0
    alpha = blur_f(alpha, 0.8)
    alpha = np.clip((alpha - 0.15) / 0.85, 0, 1)
    # цвет «грязи»: решаем med = a*F + (1-a)*mean_art
    mean_art = np.median(stack[:, inner], axis=(0, 1))
    F = (med - (1 - alpha[..., None]) * mean_art) / np.maximum(alpha[..., None], 0.2)
    rgba = np.dstack([np.clip(F, 0, 255), alpha * 255]).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def main(shot_dir, sprite_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    shots = load_shots(shot_dir)
    report = {}
    for name, (sprite, keys, icon_key) in FACTIONS.items():
        im, period, corr = build_header(shots, keys, icon_key)
        im.save(os.path.join(out_dir, f"header_{sprite.replace(' ', '_').lower()}.png"))
        report[name] = (period, round(corr, 3))
    body, holes = build_body(shots)
    body.save(os.path.join(out_dir, "body.jpg"), quality=94)
    build_separator(shots).save(os.path.join(out_dir, "separator.png"))
    build_frame(shots).save(os.path.join(out_dir, "frame.png"))
    print(json.dumps(report, ensure_ascii=False), "body holes", round(holes, 4))


if __name__ == "__main__":
    main(*sys.argv[1:4])
