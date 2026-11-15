"""Анимированная картища: видео живёт только внутри рамки карты.

Схема:
1. video_source_image — из чистого арта собирается кадр 9:16 для видеомодели (Veo принимает только
   16:9 и 9:16). Видимая в рамке часть арта стоит по центру, сверху и снизу — продолжение арта
   (или размытый фон, если арта не хватает). Эти поля потом отрезаются.
2. card_overlay — карта в рамке, где окно арта прозрачное: рамка, ромб силы, броня, провизия.
3. compose_card_video — ffmpeg вырезает из ролика видимую часть, вписывает её в окно
   и кладёт рамку сверху. Звук ролика сохраняется.
"""
import io
import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageFilter

import card_render as cr

# Окно арта внутри картинки «карта в рамке» (CARD_ONLY_BOX) — как в card_render.draw_card.
WINDOW_W, WINDOW_H = round(990 * cr.CARD_SCALE[0]), round(1422 * cr.CARD_SCALE[1])
WINDOW_X, WINDOW_Y = cr.CARD_POS[0] - cr.CARD_ONLY_BOX[0], cr.CARD_POS[1] - cr.CARD_ONLY_BOX[1]
FIT_CENTERING = (0.5, 0.4)          # то же, что ImageOps.fit в draw_card
SOURCE_SIZE = (1080, 1920)          # кадр для Veo, 9:16
OUT_SCALE = 2                       # итоговое видео вдвое крупнее картинки-карты: на телефоне выглядит чётко


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def visible_box(art_w: int, art_h: int) -> tuple[float, float, float, float]:
    """Часть арта, которую ImageOps.fit оставляет в окне карты: (x, y, w, h) в пикселях арта."""
    aspect = WINDOW_W / WINDOW_H
    if art_w / art_h > aspect:
        cw, ch = art_h * aspect, art_h
    else:
        cw, ch = art_w, art_w / aspect
    return (art_w - cw) * FIT_CENTERING[0], (art_h - ch) * FIT_CENTERING[1], cw, ch


def video_source_image(art_bytes: bytes) -> tuple[bytes, tuple[float, float, float, float]]:
    """JPEG 9:16 для видеомодели и положение окна карты в нём (доли от ширины/высоты кадра)."""
    art = Image.open(io.BytesIO(art_bytes)).convert("RGB")
    sw, sh = SOURCE_SIZE
    vx, vy, vw, vh = visible_box(*art.size)
    # Окно карты шире, чем 9:16, поэтому занимает всю ширину кадра; по высоте — по центру.
    scale = sw / vw
    win_h = vh * scale
    top = (sh - win_h) / 2

    # Фон на случай, если арта выше/ниже окна не хватает: тот же арт, растянутый и размытый.
    canvas = art.resize((sw, sh), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
    canvas = Image.eval(canvas, lambda v: int(v * 0.8))
    scaled = art.resize((max(1, round(art.width * scale)), max(1, round(art.height * scale))), Image.LANCZOS)
    canvas.paste(scaled, (round(-vx * scale), round(top - vy * scale)))

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=94)
    return out.getvalue(), (0.0, top / sh, 1.0, win_h / sh)


def card_overlay(data: dict) -> Image.Image:
    """Карта в рамке, где окно арта прозрачное.

    Карта собирается дважды — на чёрном и на белом арте. Где пиксели совпали, там рамка или
    значки (непрозрачно); где разошлись — сквозь них видно арт. Так прозрачность получается
    точной и на сглаженных краях рамки, без ручной разметки окна.
    """
    def render(color):
        art = Image.new("RGB", (WINDOW_W, WINDOW_H), color)
        canvas = cr.asset("background.jpg").copy()
        cr.draw_card(canvas, art, data)
        return np.asarray(canvas.crop(cr.CARD_ONLY_BOX).convert("RGB"), dtype=np.float32)

    black, white = render((0, 0, 0)), render((255, 255, 255))
    alpha = 1.0 - (white - black).max(axis=2, keepdims=True) / 255.0
    alpha = np.clip(alpha, 0.0, 1.0)
    color = np.where(alpha > 1e-3, black / np.maximum(alpha, 1e-3), 0.0)
    rgba = np.concatenate([np.clip(color, 0, 255), alpha * 255.0], axis=2)
    return Image.fromarray(rgba.round().astype(np.uint8), "RGBA")


def compose_card_video(raw_video: bytes, overlay: Image.Image, window: tuple[float, float, float, float],
                       timeout: int = 180) -> bytes:
    """Готовое MP4: ролик в окне карты, рамка поверх, звук как в исходнике."""
    fx, fy, fw, fh = window
    k = OUT_SCALE
    out_w, out_h = overlay.width * k, overlay.height * k
    win_w, win_h, win_x, win_y = WINDOW_W * k, WINDOW_H * k, WINDOW_X * k, WINDOW_Y * k
    with tempfile.TemporaryDirectory() as tmp:
        src, frame, dst = (os.path.join(tmp, n) for n in ("src.mp4", "frame.png", "card.mp4"))
        with open(src, "wb") as f:
            f.write(raw_video)
        overlay.resize((out_w, out_h), Image.LANCZOS).save(frame)
        graph = (
            f"[0:v]crop=iw*{fw:.6f}:ih*{fh:.6f}:iw*{fx:.6f}:ih*{fy:.6f},"
            f"scale={win_w}:{win_h}:flags=lanczos,setsar=1[art];"
            f"color=c=black:s={out_w}x{out_h}:r=30[bg];"
            f"[bg][art]overlay={win_x}:{win_y}:shortest=1[v0];"
            f"[v0][1:v]overlay=0:0:format=auto,format=yuv420p[v]"
        )
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", src, "-loop", "1", "-i", frame,
            "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-r", "30",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", dst,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg: {result.stderr.decode(errors='replace')[-800:]}")
        with open(dst, "rb") as f:
            return f.read()


def video_size() -> tuple[int, int]:
    w, h = cr.CARD_ONLY_BOX[2] - cr.CARD_ONLY_BOX[0], cr.CARD_ONLY_BOX[3] - cr.CARD_ONLY_BOX[1]
    return w * OUT_SCALE, h * OUT_SCALE
