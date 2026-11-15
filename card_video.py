"""Анимированная картища: видео живёт только внутри рамки карты.

Схема:
1. video_source_image — из чистого арта собирается кадр 9:16 для видеомодели (Veo принимает только
   16:9 и 9:16). Видимая в рамке часть арта стоит по центру, сверху и снизу — продолжение арта
   (или размытый фон, если арта не хватает). Эти поля потом отрезаются.
2. card_overlay — полная карта (рамка + панель со способностью и цитатой), где окно арта прозрачное.
3. compose_card_video — ffmpeg вырезает из ролика видимую часть, вписывает её в окно
   и кладёт карту сверху. Звук ролика сохраняется. make_gif — та же анимация в GIF (без звука).
"""
import io
import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageFilter

import card_render as cr

# Окно арта на холсте карты — как в card_render.draw_card.
WINDOW_W, WINDOW_H = round(990 * cr.CARD_SCALE[0]), round(1422 * cr.CARD_SCALE[1])
FIT_CENTERING = (0.5, 0.4)          # то же, что ImageOps.fit в draw_card
SOURCE_SIZE = (1080, 1920)          # кадр для Veo, 9:16
GIF_WIDTH, GIF_FPS = 800, 12        # GIF заметно тяжелее MP4: уменьшаем и реже кадры


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


def media_duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, timeout=30)
    try:
        return float(out.stdout.decode().strip())
    except ValueError:
        return 0.0


def has_audio_stream(path: str) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", path], capture_output=True, timeout=30)
    return bool(out.stdout.strip())


def make_loop(raw_video: bytes, length: float = 7.0, timeout: int = 180) -> bytes:
    """Бесшовная петля: последний отрезок ролика плавно перетекает в начало (и картинка, и звук).

    Из ролика длиной L получается петля length = L - d, где d — длина перехода: выход начинается
    с секунды d исходника, а его последние d секунд смешаны с началом. Конец петли совпадает с её началом.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "src.mp4"), os.path.join(tmp, "loop.mp4")
        with open(src, "wb") as f:
            f.write(raw_video)
        total = media_duration(src)
        if total <= 1.0:
            return raw_video
        d = total - length if total - length >= 0.4 else min(1.0, total / 4)
        d = min(d, total / 3)
        has_audio = has_audio_stream(src)
        graph = (f"[0:v]fps=30,format=yuv420p,split[va][vb];"
                 f"[va]trim=start={d:.3f},setpts=PTS-STARTPTS[vmain];"
                 f"[vb]trim=end={d:.3f},setpts=PTS-STARTPTS[vhead];"
                 f"[vmain][vhead]xfade=transition=fade:duration={d:.3f}:offset={total - 2 * d:.3f}[v]")
        maps = ["-map", "[v]"]
        if has_audio:
            # acrossfade молча отдаёт пустую дорожку, когда второй кусок равен длине склейки,
            # поэтому склейка вручную: конец затухает, начало нарастает поверх него.
            fade_at = total - 2 * d
            graph += (f";[0:a]asplit[aa][ab];"
                      f"[aa]atrim=start={d:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={fade_at:.3f}:d={d:.3f}[amain];"
                      f"[ab]atrim=end={d:.3f},asetpts=PTS-STARTPTS,afade=t=in:d={d:.3f},"
                      f"adelay={round(fade_at * 1000)}:all=1[ahead];"
                      f"[amain][ahead]amix=inputs=2:duration=first:normalize=0[a]")
            maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", graph, *maps,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", dst]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0 and has_audio:
            # Без звуковой дорожки в исходнике — повтор только с видео.
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", graph.split(";[0:a]")[0],
                   "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", dst]
            result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg loop: {result.stderr.decode(errors='replace')[-800:]}")
        with open(dst, "rb") as f:
            return f.read()


def card_overlay(data: dict) -> tuple[Image.Image, tuple[int, int]]:
    """Полная карта (как картинка карты: рамка + панель со способностью и цитатой) с прозрачным окном арта.
    Возвращает слой и левый верхний угол окна арта в нём.

    Карта собирается дважды — на чёрном и на белом арте. Где пиксели совпали, там рамка или
    текст (непрозрачно); где разошлись — сквозь них видно арт. Так прозрачность получается
    точной и на сглаженных краях рамки, без ручной разметки окна.
    """
    def render(color):
        art = Image.new("RGB", (WINDOW_W, WINDOW_H), color)
        canvas = cr.asset("background.jpg").copy()
        bottom = cr.draw_panel(canvas, data)
        cr.draw_card(canvas, art, data)
        crop_bottom = min(canvas.height, max(cr.CARD_BOTTOM, bottom) + cr.CROP_MARGIN)
        if (crop_bottom - cr.CROP_TOP) % 2:      # H.264 требует чётные стороны
            crop_bottom += 1 if crop_bottom < canvas.height else -1
        box = (cr.CROP_X[0], cr.CROP_TOP, cr.CROP_X[1], crop_bottom)
        return np.asarray(canvas.crop(box).convert("RGB"), dtype=np.float32), box

    (black, box), (white, _) = render((0, 0, 0)), render((255, 255, 255))
    alpha = 1.0 - (white - black).max(axis=2, keepdims=True) / 255.0
    alpha = np.clip(alpha, 0.0, 1.0)
    color = np.where(alpha > 1e-3, black / np.maximum(alpha, 1e-3), 0.0)
    rgba = np.concatenate([np.clip(color, 0, 255), alpha * 255.0], axis=2)
    window = (cr.CARD_POS[0] - box[0], cr.CARD_POS[1] - box[1])
    return Image.fromarray(rgba.round().astype(np.uint8), "RGBA"), window


def compose_card_video(raw_video: bytes, overlay: Image.Image, window_pos: tuple[int, int],
                       window: tuple[float, float, float, float], timeout: int = 180) -> bytes:
    """Готовое MP4: ролик в окне арта, карта с панелью поверх, звук как в исходнике.

    window_pos — где окно арта в слое карты; window — где окно в кадре ролика (доли).
    """
    fx, fy, fw, fh = window
    out_w, out_h = overlay.size
    win_w, win_h = WINDOW_W, WINDOW_H
    win_x, win_y = window_pos
    with tempfile.TemporaryDirectory() as tmp:
        src, frame, dst = (os.path.join(tmp, n) for n in ("src.mp4", "frame.png", "card.mp4"))
        with open(src, "wb") as f:
            f.write(raw_video)
        overlay.save(frame)
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


def make_gif(mp4: bytes, timeout: int = 180) -> bytes:
    """GIF из готового видео: своя палитра на весь ролик, чтобы цвета арта не «полосили»."""
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "card.mp4"), os.path.join(tmp, "card.gif")
        with open(src, "wb") as f:
            f.write(mp4)
        graph = (f"fps={GIF_FPS},scale={GIF_WIDTH}:-2:flags=lanczos,split[a][b];"
                 "[a]palettegen=max_colors=256:stats_mode=diff[p];"
                 "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle")
        result = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", graph,
                                 "-loop", "0", dst], capture_output=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg gif: {result.stderr.decode(errors='replace')[-800:]}")
        with open(dst, "rb") as f:
            return f.read()
