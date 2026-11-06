#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_icon.py — графическое ядро, диагностика и CLI создателя иконок .ico.

Архитектура:
    Исходник -> Срезы и подготовка (_prepare) -> Окно кадрирования (_window)
             -> Суперсемплированная маска (_filled_shape) + Неоновая рамка (_border_layer)
             -> Независимый рендер каждого размера (_render) -> Упаковка в .ICO (write_ico)
             -> Детальная телеметрия (build_frames_detailed) и самодиагностика (run_icon_checks)

Модуль не зависит от GUI-библиотек и может использоваться:
  1. Напрямую из командной строки (CLI);
  2. Как движок для pywebview-интерфейса (icon_launcher.py + icon_gui.html);
  3. Как библиотека в других Python-скриптах.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import struct
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

# Опциональная регистрация плагинов HEIC/HEIF и AVIF, если они установлены в окружении
for _plugin_mod, _reg_fn in (
    ("pillow_heif", "register_heif_opener"),
    ("pillow_avif", None),
):
    try:
        _mod = __import__(_plugin_mod)
        if _reg_fn and hasattr(_mod, _reg_fn):
            getattr(_mod, _reg_fn)()
    except ImportError:
        pass

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------
REF = 512          # опорный холст: radius, border_width и border_inset заданы в его
                   # пикселях и пропорционально пересчитываются под каждый размер
SS = 4             # во сколько раз крупнее рисуем маски, чтобы края были гладкими
MAX_SOURCE = 4096  # исходники длиннее этого ужимаются один раз (на результат не влияет)
DEFAULT_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
WINDOWS_CORE_SIZES = (16, 32, 48, 256)
SHAPES = ("rounded", "circle", "square")
FITS = ("cover", "contain")
RESAMPLE = Image.Resampling.LANCZOS

ColorRGBA = tuple[int, int, int, int]


# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------
@dataclass
class IconSettings:
    """Все параметры иконки. Значения по умолчанию повторяют внешний вид
    классического пресета (скругление 96, рамка #2de2e6, ширина 8, отступ 4)."""

    # --- кадрирование ---
    trim_top: float = 0.0       # срезать сверху (доля высоты, 0.09 = 9%)
    trim_bottom: float = 0.0    # срезать снизу (водяные знаки, подписи)
    trim_left: float = 0.0
    trim_right: float = 0.0
    fit: str = "cover"          # cover: заполнить квадрат (края обрезаются);
                                # contain: вписать картинку целиком (будут поля)
    zoom: float = 1.0           # >1 приближает, <1 отдаляет (появляются поля)
    offset_x: float = 0.0       # сдвиг окна: -1 левый край ... 0 центр ... +1 правый
    offset_y: float = 0.0       # то же по вертикали: -1 верх ... +1 низ
    background: str | None = None   # цвет под картинкой и в полях; None = прозрачный

    # --- форма ---
    shape: str = "rounded"      # rounded | circle | square
    radius: float = 96.0        # скругление углов (px на холсте 512, 0..256)

    # --- рамка ---
    border: bool = True
    border_color: str = "#2de2e6"   # #RRGGBB или #RRGGBBAA
    border_alpha: int = 180         # 0..255, если в цвете нет своей прозрачности
    border_width: float = 8.0       # толщина (px на холсте 512), не тоньше 1 px
    border_inset: float = 4.0       # отступ рамки от края (px на холсте 512)

    # --- выход ---
    sizes: list[int] = field(default_factory=lambda: list(DEFAULT_SIZES))

    def validate(self) -> IconSettings:
        """Проверяет значения и приводит sizes к виду «отсортированные без повторов»."""

        def num(name: str, lo: float, hi: float) -> None:
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{name}: нужно число от {lo} до {hi}, получено {v!r}")

        for name in ("trim_top", "trim_bottom", "trim_left", "trim_right"):
            num(name, 0.0, 0.9)
        if self.trim_top + self.trim_bottom > 0.9 or self.trim_left + self.trim_right > 0.9:
            raise ValueError("Срезы с двух противоположных сторон съедают больше 90% картинки")
        num("zoom", 0.2, 20.0)
        num("offset_x", -1.0, 1.0)
        num("offset_y", -1.0, 1.0)
        num("radius", 0.0, REF // 2)
        num("border_width", 0.0, REF // 2)
        num("border_inset", 0.0, REF // 2)
        num("border_alpha", 0, 255)
        self.border_alpha = int(round(self.border_alpha))

        if self.fit not in FITS:
            raise ValueError(f"fit: допустимо {', '.join(FITS)}, получено {self.fit!r}")
        if self.shape not in SHAPES:
            raise ValueError(f"shape: допустимо {', '.join(SHAPES)}, получено {self.shape!r}")
        if not isinstance(self.border, bool):
            raise ValueError(f"border: ожидается bool, получено {self.border!r}")

        parse_color(self.background)
        if parse_color(self.border_color, self.border_alpha) is None:
            raise ValueError("border_color: цвет рамки не может быть пустым")

        try:
            sizes = sorted({int(s) for s in self.sizes})
        except (TypeError, ValueError):
            raise ValueError(f"sizes: нужен список целых чисел, получено {self.sizes!r}") from None
        if not sizes or sizes[0] < 8 or sizes[-1] > 256:
            raise ValueError("sizes: размеры должны быть в пределах 8..256 (ограничение формата ICO)")
        self.sizes = sizes
        return self

    @classmethod
    def from_dict(cls, data: dict) -> IconSettings:
        """Создаёт и валидирует IconSettings из словаря."""
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        if "background" in filtered and filtered["background"] in ("", "none", "transparent"):
            filtered["background"] = None
        if "border" in filtered:
            filtered["border"] = bool(filtered["border"])
        for float_key in (
            "trim_top",
            "trim_bottom",
            "trim_left",
            "trim_right",
            "zoom",
            "offset_x",
            "offset_y",
            "radius",
            "border_width",
            "border_inset",
        ):
            if float_key in filtered and filtered[float_key] is not None:
                filtered[float_key] = float(filtered[float_key])
        if "border_alpha" in filtered and filtered["border_alpha"] is not None:
            filtered["border_alpha"] = int(round(float(filtered["border_alpha"])))
        return cls(**filtered).validate()


def parse_color(value: str | None, default_alpha: int = 255) -> ColorRGBA | None:
    """'#2de2e6', '#2de2e6b4', '#fff' -> (r, g, b, a). None/'none'/'transparent' -> None."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("", "none", "transparent", "нет"):
        return None
    m = re.fullmatch(r"#?([0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})", s)
    if not m:
        raise ValueError(
            f"Не понимаю цвет «{value}». Нужен вид #RGB, #RRGGBB или #RRGGBBAA, например #2de2e6"
        )
    h = m.group(1)
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    a = int(h[6:8], 16) if len(h) == 8 else int(default_alpha)
    return (r, g, b, a)


def rgba_to_hex(rgba: ColorRGBA) -> str:
    """Преобразует (r, g, b, a) в 6-символьный #rrggbb."""
    return f"#{rgba[0]:02x}{rgba[1]:02x}{rgba[2]:02x}"


# --------------------------------------------------------------------------
# Профили настроек (JSON)
# --------------------------------------------------------------------------
def save_profile(st: IconSettings, path: str | Path) -> Path:
    """Валидирует и сохраняет настройки иконки в JSON-файл."""
    st.validate()
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(asdict(st), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return out_path


def load_profile(path: str | Path) -> IconSettings:
    """Загружает настройки иконки из JSON-файла и проверяет их корректность."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Файл профиля не найден: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"Не удалось прочитать JSON-профиль «{p.name}»: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"Профиль «{p.name}» должен содержать JSON-объект с настройками")

    known = {f.name for f in fields(IconSettings)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"Неизвестные поля в профиле «{p.name}»: {', '.join(unknown)}")

    return IconSettings.from_dict(raw)


# --------------------------------------------------------------------------
# Загрузка и подготовка исходника (всеядный декодер форматов)
# --------------------------------------------------------------------------
COMMON_IMAGE_EXTS = (
    ".png", ".apng",
    ".jpg", ".jpeg", ".jfif", ".jpe", ".pjpeg", ".pjp", ".mpo",
    ".webp", ".avif", ".heic", ".heif",
    ".bmp", ".dib",
    ".gif",
    ".tif", ".tiff",
    ".ico", ".cur", ".icns",
    ".tga", ".dds", ".psd", ".qoi", ".pcx", ".blp",
    ".ppm", ".pgm", ".pbm", ".pnm", ".pfm",
    ".jp2", ".j2k", ".jpc", ".jpf", ".jpx", ".j2c",
    ".sgi", ".rgb", ".rgba", ".bw", ".ras", ".xbm", ".xpm",
    ".svg",
)


def get_supported_extensions() -> list[str]:
    """Возвращает отсортированный список всех расширений изображений (Pillow + браузерные)."""
    Image.init()
    reg = {ext.lower() for ext in Image.registered_extensions().keys()}
    reg.update(COMMON_IMAGE_EXTS)
    # Исключаем чисто документные форматы, которые не являются растровыми картинками по умолчанию
    reg.discard(".pdf")
    reg.discard(".eps")
    reg.discard(".ps")
    return sorted(reg)


def _normalize_opened_image(img: Image.Image) -> Image.Image:
    """Выбирает самый крупный кадр для мультиразмерных форматов (.ico, .icns, .tiff),
    безопасно применяет EXIF-поворот (даже при битых тегах в .jfif) и приводит любые
    цветовые режимы (включая 16-битные I;16 и float F) к RGBA."""
    # 1. Если это многокадровый контейнер (.ico, .icns, .tiff), берём самый крупный кадр
    try:
        n_frames = getattr(img, "n_frames", 1)
        if n_frames and n_frames > 1 and getattr(img, "format", "") in ("ICO", "ICNS", "TIFF", "MPO"):
            best_idx = 0
            best_area = img.width * img.height
            for idx in range(n_frames):
                img.seek(idx)
                area = img.width * img.height
                if area > best_area:
                    best_area = area
                    best_idx = idx
            img.seek(best_idx)
        img.load()
    except Exception:
        img.load()

    # 2. Безопасный EXIF-поворот (некоторые .jfif из интернета имеют нестандартный EXIF-блок)
    try:
        transposed = ImageOps.exif_transpose(img)
        if transposed is not None:
            img = transposed
    except Exception:
        pass

    # 3. Корректный перевод 16-битных (I;16, I) и float (F) изображений в 8-битный диапазон
    if img.mode in ("I;16", "I;16L", "I;16B", "I"):
        img = img.point(lambda v: v * (1.0 / 256.0)).convert("L")
    elif img.mode == "F":
        img = img.convert("L")

    return img.convert("RGBA")


def load_source(path: str | Path) -> Image.Image:
    """Открывает картинку любого поддерживаемого формата (.png, .jpg, .jfif, .webp, .ico, .psd, .tga и др.)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Файл не найден: {p}")
    try:
        img = Image.open(p)
        return _normalize_opened_image(img)
    except Exception as e:
        raise ValueError(f"Не удалось открыть «{p.name}» как картинку: {e}") from e


def load_source_bytes(raw_bytes: bytes, label: str = "upload") -> Image.Image:
    """Декодирует картинку из байтов в памяти (например, переданную из браузера/UI)."""
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        return _normalize_opened_image(img)
    except Exception as e:
        raise ValueError(f"Не удалось декодировать изображение «{label}»: {e}") from e


def make_demo_image(size: int = 512) -> Image.Image:
    """Генерирует демонстрационную киберпанк-иконку, если входной файл ещё не выбран."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(12 + 28 * t)
        g = int(16 + 24 * t)
        b = int(34 + 68 * t)
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))
    sun_box = [int(size * 0.20), int(size * 0.14), int(size * 0.80), int(size * 0.74)]
    draw.ellipse(sun_box, fill=(255, 46, 77, 255))
    for frac, h_frac in ((0.54, 0.025), (0.61, 0.035), (0.68, 0.045)):
        y0 = int(size * frac)
        y1 = int(size * (frac + h_frac))
        draw.rectangle([int(size * 0.18), y0, int(size * 0.82), y1], fill=(24, 24, 58, 255))
    draw.polygon(
        [(0, int(size * 0.78)), (int(size * 0.34), int(size * 0.44)), (int(size * 0.72), int(size * 0.78))],
        fill=(26, 36, 78, 255),
    )
    draw.polygon(
        [(int(size * 0.28), int(size * 0.80)), (int(size * 0.66), int(size * 0.38)), (size, int(size * 0.80))],
        fill=(16, 22, 52, 255),
    )
    draw.rectangle([0, int(size * 0.76), size, size], fill=(10, 12, 24, 255))
    draw.line([(0, int(size * 0.76)), (size, int(size * 0.76))], fill=(45, 226, 230, 255), width=max(2, size // 128))
    return img


def _prepare(img: Image.Image, st: IconSettings, max_dim: int = MAX_SOURCE) -> tuple[Image.Image, float]:
    """Срезы по краям + одноразовое ужатие гигантов. Возвращает (картинка, масштаб),
    где масштаб нужен только чтобы пересчитать размеры окна обратно в пиксели оригинала."""
    w, h = img.size
    left, right = int(w * st.trim_left), w - int(w * st.trim_right)
    top, bottom = int(h * st.trim_top), h - int(h * st.trim_bottom)
    if right - left < 1 or bottom - top < 1:
        raise ValueError("После срезов от картинки ничего не осталось")
    if (left, top, right, bottom) != (0, 0, w, h):
        img = img.crop((left, top, right, bottom))
    scale = 1.0
    if max(img.size) > max_dim:
        scale = max_dim / max(img.size)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))), RESAMPLE
        )
    return img, scale


def _window(size: tuple[int, int], st: IconSettings) -> tuple[float, float, float]:
    """Квадратное «окно» (left, top, side) в координатах подготовленной картинки.
    В режиме cover масштаб не опускается ниже 1.0, чтобы картинка всегда
    заполняла форму целиком без плоских срезов по краям круга/скругления."""
    w, h = size
    base = min(w, h) if st.fit == "cover" else max(w, h)
    eff_zoom = max(1.0, st.zoom) if st.fit == "cover" else st.zoom
    side = base / eff_zoom
    left = (w - side) * (1 + st.offset_x) / 2
    top = (h - side) * (1 + st.offset_y) / 2
    return left, top, side


# --------------------------------------------------------------------------
# Рендер одного кадра (с кэшированием суперсемплированных масок и рамок)
# --------------------------------------------------------------------------
def _window_to_square(
    src: Image.Image,
    win: tuple[float, float, float],
    n: int,
    bg: ColorRGBA | None,
) -> Image.Image:
    """Вырезает окно и масштабирует его до n x n за ОДИН проход ресемплинга
    через предумноженную альфу (RGBa). Принимает как RGBA, так и уже сконвертированный RGBa."""
    left, top, side = win
    k = n / side
    w, h = src.size
    x0, y0 = max(left, 0.0), max(top, 0.0)
    x1, y1 = min(left + side, float(w)), min(top + side, float(h))
    layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    if x1 > x0 and y1 > y0:
        dx0, dy0 = round((x0 - left) * k), round((y0 - top) * k)
        dx1, dy1 = round((x1 - left) * k), round((y1 - top) * k)
        tw, th = max(1, dx1 - dx0), max(1, dy1 - dy0)
        src_rgba = src if src.mode == "RGBa" else src.convert("RGBa")
        piece = src_rgba.resize((tw, th), RESAMPLE, box=(x0, y0, x1, y1)).convert("RGBA")
        layer.paste(piece, (dx0, dy0))
    if bg is not None:
        layer = Image.alpha_composite(Image.new("RGBA", (n, n), bg), layer)
    return layer


@lru_cache(maxsize=256)
def _filled_shape(n: int, shape: str, radius_px: float, inset_px: int) -> Image.Image:
    """Суперсемплированная маска формы n x n с отступом inset_px (кэшируется)."""
    big = n * SS
    mask = Image.new("L", (big, big), 0)
    a = inset_px * SS
    box = [a, a, big - 1 - a, big - 1 - a]
    if box[2] > box[0] and box[3] > box[1]:
        d = ImageDraw.Draw(mask)
        if shape == "circle":
            d.ellipse(box, fill=255)
        elif shape == "square":
            d.rectangle(box, fill=255)
        else:
            clamped_r = min(radius_px, n / 2.0)
            d.rounded_rectangle(box, radius=max(0.0, clamped_r - inset_px) * SS, fill=255)
    return mask.reduce(SS)


@lru_cache(maxsize=256)
def _cached_border_overlay(
    n: int,
    shape: str,
    radius_q: float,
    inset: int,
    width: int,
    border_rgba: ColorRGBA,
) -> tuple[Image.Image, Image.Image]:
    """Возвращает (outer_mask, border_overlay_rgba), кэшируя суперсемплированные маски."""
    outer = _filled_shape(n, shape, radius_q, inset)
    inner = _filled_shape(n, shape, radius_q, inset + width)
    r, g, b, a = border_rgba
    b_layer = Image.new("RGBA", (n, n), (r, g, b, 0))
    b_layer.putalpha(ImageChops.invert(inner).point(lambda v: v * a // 255))
    return outer, b_layer


def _border_layer(n: int, st: IconSettings, border_rgba: ColorRGBA) -> Image.Image:
    """Прозрачный слой с кольцом рамки: (внешняя форма) минус (внутренняя форма)."""
    s = n / REF
    width = max(1, int(st.border_width * s + 0.5))
    inset = int(st.border_inset * s + 0.5)
    radius_q = round(st.radius * s, 2)
    ring = ImageChops.subtract(
        _filled_shape(n, st.shape, radius_q, inset),
        _filled_shape(n, st.shape, radius_q, inset + width),
    )
    r, g, b, a = border_rgba
    layer = Image.new("RGBA", (n, n), (r, g, b, 0))
    layer.putalpha(ring.point(lambda v: v * a // 255))
    return layer


def _apply_mask_and_border(
    art: Image.Image,
    st: IconSettings,
    n: int,
    border_rgba: ColorRGBA,
) -> Image.Image:
    """Накладывает неоновую рамку и обрезает кадр n x n по суперсемплированной маске формы."""
    s = n / REF
    radius_q = round(st.radius * s, 2)
    if st.border and st.border_width > 0:
        width = max(1, int(st.border_width * s + 0.5))
        inset = int(st.border_inset * s + 0.5)
        outer, b_layer = _cached_border_overlay(n, st.shape, radius_q, inset, width, border_rgba)
        art = Image.alpha_composite(art, b_layer)
        art.putalpha(ImageChops.multiply(art.getchannel("A"), outer))
    else:
        outer = _filled_shape(n, st.shape, radius_q, 0)
        art.putalpha(ImageChops.multiply(art.getchannel("A"), outer))
    return art


def _render(
    src: Image.Image,
    st: IconSettings,
    n: int,
    bg_rgba: ColorRGBA | None,
    border_rgba: ColorRGBA,
) -> Image.Image:
    art = _window_to_square(src, _window(src.size, st), n, bg_rgba)
    return _apply_mask_and_border(art, st, n, border_rgba)


# --------------------------------------------------------------------------
# Публичный API и диагностика (по образцу entropy_engine.py)
# --------------------------------------------------------------------------
def build_frames(img: Image.Image, st: IconSettings) -> tuple[dict[int, Image.Image], float]:
    """Принимает PIL-картинку и настройки, возвращает ({размер: кадр RGBA}, сторона окна в px оригинала)."""
    st.validate()
    bg_rgba = parse_color(st.background)
    border_rgba = parse_color(st.border_color, st.border_alpha)
    assert border_rgba is not None

    src, scale = _prepare(img.convert("RGBA"), st)
    src_rgba = src.convert("RGBa")
    side = _window(src.size, st)[2] / scale
    frames = {n: _render(src_rgba, st, n, bg_rgba, border_rgba) for n in st.sizes}
    return frames, side


def build_frames_detailed(
    img: Image.Image,
    st: IconSettings,
) -> tuple[dict[int, Image.Image], bytes, dict]:
    """Аналог collect_detailed() из entropy_engine.py:
    строит все кадры, упаковывает .ICO в память и возвращает
    (frames, ico_bytes, telemetry_dict) с побайтовой разбивкой каждого кадра,
    реальной толщиной рамки в пикселях и временем рендера."""
    t0 = time.perf_counter()
    frames, window_side = build_frames(img, st)
    sizes = sorted(frames)
    blobs: list[bytes] = []
    breakdown: list[dict] = []

    for n in sizes:
        buf = io.BytesIO()
        frames[n].save(buf, format="PNG", optimize=True)
        blob = buf.getvalue()
        blobs.append(blob)
        s = n / REF
        b_width_px = max(1, int(st.border_width * s + 0.5)) if (st.border and st.border_width > 0) else 0
        b_inset_px = int(st.border_inset * s + 0.5) if (st.border and st.border_width > 0) else 0
        breakdown.append(
            {
                "size": n,
                "png_bytes": len(blob),
                "border_px": b_width_px,
                "inset_px": b_inset_px,
            }
        )

    header_bytes = 6 + 16 * len(sizes)
    offset = header_bytes
    out = [struct.pack("<HHH", 0, 1, len(sizes))]
    for n, blob in zip(sizes, blobs):
        dim = 0 if n >= 256 else n
        out.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    out.extend(blobs)
    ico_bytes = b"".join(out)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    max_target = max(sizes) if sizes else 256
    telemetry = {
        "window_side": round(window_side),
        "scale_ratio": round(window_side / max_target, 2),
        "render_ms": round(elapsed_ms, 1),
        "breakdown": breakdown,
        "png_total_bytes": sum(len(b) for b in blobs),
        "header_bytes": header_bytes,
        "ico_total_bytes": len(ico_bytes),
    }
    return frames, ico_bytes, telemetry


def run_icon_checks(img: Image.Image, st: IconSettings) -> list[dict]:
    """Техническая самодиагностика движка и аудит текущей иконки (по образцу run_checks).
    Возвращает список словарей: {'kind': str, 'status': 'ok'|'warn'|'fail', ...}."""
    results: list[dict] = []

    def add(kind: str, status: str, **data: object) -> None:
        results.append({"kind": kind, "status": status, "ok": status != "fail", **data})

    st.validate()
    frames, ico_bytes, tel = build_frames_detailed(img, st)
    max_size = max(st.sizes)
    win_side = tel["window_side"]
    ratio = tel["scale_ratio"]

    # 1. Достаточность разрешения исходника (нет ли апскейла/мыла на максимальном кадре)
    if win_side >= max_size:
        add("source_resolution", "ok", window_side=win_side, max_size=max_size, ratio=ratio)
    else:
        add("source_resolution", "warn", window_side=win_side, max_size=max_size, ratio=ratio)

    # 2. Читаемость мелкого кадра 16x16 (не съедает ли рамка и отступ полезную площадь)
    s16 = 16 / REF
    w16 = max(1, int(st.border_width * s16 + 0.5)) if (st.border and st.border_width > 0) else 0
    i16 = int(st.border_inset * s16 + 0.5) if (st.border and st.border_width > 0) else 0
    inner_16 = max(0, 16 - 2 * (w16 + i16))
    add(
        "small_frame_legibility",
        "ok" if inner_16 >= 10 else ("warn" if inner_16 >= 6 else "fail"),
        inner_px=inner_16,
        border_px=w16,
        inset_px=i16,
    )

    # 3. Антиалиасинг и суперсемплинг масок (SS=4)
    sample_n = 64
    mask_test = _filled_shape(sample_n, "circle", 32.0, 0)
    hist = mask_test.histogram()
    intermediate_pixels = sum(hist[1:255])
    corner_alpha = mask_test.getpixel((0, 0))
    add(
        "supersample_aa",
        "ok" if (intermediate_pixels > 0 and corner_alpha == 0) else "fail",
        ss=SS,
        aa_pixels=intermediate_pixels,
    )

    # 4. Концентричность внешнего и внутреннего контуров рамки
    if st.shape == "rounded" and st.border and st.border_width > 0:
        r_outer = min(st.radius, REF / 2.0) - st.border_inset
        r_inner = max(0.0, r_outer - st.border_width)
        add(
            "concentric_geometry",
            "ok" if r_outer >= 0 else "warn",
            r_outer=round(max(0.0, r_outer), 1),
            r_inner=round(r_inner, 1),
        )
    else:
        add("concentric_geometry", "ok", shape=st.shape)

    # 5. Равномерность заполнения краёв на старшем кадре (проверка 4 сторон внутри рамки)
    top_frame = frames[max_size]
    s_top = max_size / REF
    ins_top = int(st.border_inset * s_top + 0.5) if (st.border and st.border_width > 0) else 0
    wid_top = max(1, int(st.border_width * s_top + 0.5)) if (st.border and st.border_width > 0) else 0
    probe_offset = min(max_size // 2 - 1, ins_top + wid_top + 2)
    mid = max_size // 2
    edge_alphas = [
        top_frame.getpixel((mid, probe_offset))[3],
        top_frame.getpixel((mid, max_size - 1 - probe_offset))[3],
        top_frame.getpixel((probe_offset, mid))[3],
        top_frame.getpixel((max_size - 1 - probe_offset, mid))[3],
    ]
    filled_sides = sum(1 for a in edge_alphas if a > 0)
    add(
        "edge_coverage",
        "ok" if filled_sides in (0, 4) else "warn",
        filled_sides=filled_sides,
    )

    # 6. Целостность бинарного контейнера .ICO (ICONDIR + ICONDIRENTRY + сигнатуры PNG)
    reserved, ico_type, count = struct.unpack_from("<HHH", ico_bytes, 0)
    header_ok = (reserved == 0 and ico_type == 1 and count == len(st.sizes))
    png_sigs_ok = True
    for idx in range(count):
        _, _, _, _, planes, bpp, sz, off = struct.unpack_from("<BBBBHHII", ico_bytes, 6 + idx * 16)
        if planes != 1 or bpp != 32 or ico_bytes[off:off + 8] != b"\x89PNG\r\n\x1a\n":
            png_sigs_ok = False
            break
    add(
        "ico_binary_spec",
        "ok" if (header_ok and png_sigs_ok) else "fail",
        count=count,
        total_bytes=len(ico_bytes),
    )

    # 7. Наличие базовых размеров Windows (16, 32, 48, 256)
    missing_core = [s for s in WINDOWS_CORE_SIZES if s not in st.sizes]
    add(
        "windows_core_sizes",
        "ok" if not missing_core else "warn",
        missing=missing_core,
        present=st.sizes,
    )

    # 8. Идемпотентность сериализации JSON-профиля
    reconstructed = IconSettings.from_dict(asdict(st))
    add("profile_roundtrip", "ok" if reconstructed == st else "fail")

    return results


def frames_to_ico_bytes(frames: dict[int, Image.Image]) -> bytes:
    """Упаковывает словарь кадров {размер: Image} в бинарный формат .ICO (в памяти)."""
    if not frames:
        raise ValueError("frames_to_ico_bytes: словарь кадров пуст")
    sizes = sorted(frames)
    blobs = []
    for n in sizes:
        buf = io.BytesIO()
        frames[n].save(buf, format="PNG", optimize=True)
        blobs.append(buf.getvalue())
    offset = 6 + 16 * len(sizes)
    out = [struct.pack("<HHH", 0, 1, len(sizes))]
    for n, blob in zip(sizes, blobs):
        dim = 0 if n >= 256 else n
        out.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    out.extend(blobs)
    return b"".join(out)


def write_ico(frames: dict[int, Image.Image], path: str | Path) -> Path:
    """Записывает .ico-файл на диск."""
    data = frames_to_ico_bytes(frames)
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


def write_png_dir(frames: dict[int, Image.Image], directory: str | Path, prefix: str = "icon") -> list[Path]:
    """Сохраняет каждый кадр отдельным PNG-файлом (<prefix>_<N>x<N>.png) в указанную папку."""
    if not frames:
        raise ValueError("write_png_dir: словарь кадров пуст")
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for n in sorted(frames):
        p = out_dir / f"{prefix}_{n}x{n}.png"
        frames[n].save(p, format="PNG", optimize=True)
        saved.append(p)
    return saved


def make_preview_image(frames: dict[int, Image.Image]) -> Image.Image:
    """Генерирует лист превью (PIL.Image): ряды 1:1 на тёмном и светлом фоне + 4x лупа мелких размеров."""
    if not frames:
        raise ValueError("make_preview_image: словарь кадров пуст")
    sizes = sorted(frames)
    pad, label_h = 20, 18
    try:
        font = ImageFont.load_default(size=11)
    except TypeError:
        font = ImageFont.load_default()
    rows = [
        ((30, 30, 32), (200, 200, 200), 1, False),
        ((243, 243, 243), (60, 60, 60), 1, False),
        ((30, 30, 32), (200, 200, 200), 4, True),
        ((243, 243, 243), (60, 60, 60), 4, True),
    ]
    layout = []
    for bg, fg, scale, small in rows:
        ss = [s for s in sizes if s <= 64] if small else sizes
        if not ss:
            continue
        width = pad + sum(s * scale + pad for s in ss)
        height = pad + max(s * scale for s in ss) + label_h
        layout.append((bg, fg, scale, ss, width, height))
    sheet_w = max(row[4] for row in layout)
    sheet = Image.new("RGB", (sheet_w, sum(row[5] for row in layout)))
    draw = ImageDraw.Draw(sheet)
    y = 0
    for bg, fg, scale, ss, _, height in layout:
        draw.rectangle([0, y, sheet_w - 1, y + height - 1], fill=bg)
        tallest = max(s * scale for s in ss)
        x = pad
        for s in ss:
            im = frames[s]
            if scale != 1:
                im = im.resize((s * scale, s * scale), Image.Resampling.NEAREST)
            sheet.paste(im, (x, y + pad + tallest - s * scale), im)
            draw.text((x, y + pad + tallest + 3), f"{s}px", fill=fg, font=font)
            x += s * scale + pad
        y += height
    return sheet


def make_preview(frames: dict[int, Image.Image], path: str | Path) -> Path:
    """Сохраняет лист превью в PNG-файл."""
    sheet = make_preview_image(frames)
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, format="PNG", optimize=True)
    return out_path


# --------------------------------------------------------------------------
# Командная строка (CLI)
# --------------------------------------------------------------------------
def _parse_sizes_arg(raw_items: Sequence[str]) -> list[int]:
    result: list[int] = []
    for item in raw_items:
        for part in item.replace(",", " ").split():
            try:
                result.append(int(part))
            except ValueError:
                raise argparse.ArgumentTypeError(
                    f"Некорректный размер «{part}»: ожидается целое число 8..256"
                ) from None
    if not result:
        raise argparse.ArgumentTypeError("Нужно указать хотя бы один размер")
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="make_icon.py",
        description="Генератор чётких мультиразмерных иконок .ico (движок + диагностика + CLI).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python icon_launcher.py                              # запуск GUI (pywebview)\n"
            "  python make_icon.py logo.png                         # создать logo.ico\n"
            "  python make_icon.py logo.png -o app.ico --trim-bottom 0.09 --zoom 1.15\n"
            "  python make_icon.py logo.png --shape circle --no-border --preview\n"
            "  python make_icon.py logo.png --save-profile neon.json\n"
            "  python make_icon.py other.png --profile neon.json --border-color \"#ff2a85\""
        ),
    )

    p.add_argument("input", nargs="?", help="Путь к исходной картинке (PNG, JPG, WEBP и др.).")
    p.add_argument("-g", "--gui", action="store_true", help="Запустить графический интерфейс (icon_launcher.py).")
    p.add_argument("-o", "--output", help="Путь к выходному .ico файлу (по умолчанию: <имя_исходника>.ico).")
    p.add_argument(
        "--preview",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Сохранить PNG-лист превью всех размеров (без аргумента: <имя_выхода>_preview.png).",
    )
    p.add_argument("--png-dir", metavar="DIR", help="Сохранить каждый размер отдельным PNG в указанную папку.")

    prof = p.add_argument_group("Профили настроек")
    prof.add_argument("--profile", metavar="JSON", help="Загрузить базовые настройки из JSON-профиля.")
    prof.add_argument("--save-profile", metavar="JSON", help="Сохранить итоговые настройки в JSON-профиль.")

    crop = p.add_argument_group("Кадрирование и геометрия")
    crop.add_argument("--trim", type=float, default=None, metavar="FRAC", help="Срезать долю (0..0.45) со всех сторон.")
    crop.add_argument("--trim-top", type=float, default=None, metavar="FRAC")
    crop.add_argument("--trim-bottom", type=float, default=None, metavar="FRAC")
    crop.add_argument("--trim-left", type=float, default=None, metavar="FRAC")
    crop.add_argument("--trim-right", type=float, default=None, metavar="FRAC")
    crop.add_argument("--fit", choices=FITS, default=None)
    crop.add_argument("--zoom", type=float, default=None)
    crop.add_argument("--offset-x", type=float, default=None, metavar="X")
    crop.add_argument("--offset-y", type=float, default=None, metavar="Y")
    crop.add_argument("--background", "--bg", dest="background", default=None, metavar="COLOR")

    shp = p.add_argument_group("Форма маски")
    shp.add_argument("--shape", choices=SHAPES, default=None)
    shp.add_argument("--radius", type=float, default=None)

    brd = p.add_argument_group("Рамка")
    brd_toggle = brd.add_mutually_exclusive_group()
    brd_toggle.add_argument("--border", dest="border", action="store_true", default=None)
    brd_toggle.add_argument("--no-border", dest="border", action="store_false")
    brd.add_argument("--border-color", default=None, metavar="COLOR")
    brd.add_argument("--border-alpha", type=int, default=None, metavar="0..255")
    brd.add_argument("--border-width", type=float, default=None, metavar="PX")
    brd.add_argument("--border-inset", type=float, default=None, metavar="PX")

    out = p.add_argument_group("Размеры и вывод")
    out.add_argument("--sizes", nargs="+", default=None, metavar="N")
    out.add_argument("-q", "--quiet", action="store_true")

    return p


def resolve_settings(args: argparse.Namespace) -> IconSettings:
    st = load_profile(args.profile) if args.profile else IconSettings()
    if args.trim is not None:
        st.trim_top = st.trim_bottom = st.trim_left = st.trim_right = args.trim

    for name in (
        "trim_top",
        "trim_bottom",
        "trim_left",
        "trim_right",
        "fit",
        "zoom",
        "offset_x",
        "offset_y",
        "background",
        "shape",
        "radius",
        "border",
        "border_color",
        "border_alpha",
        "border_width",
        "border_inset",
    ):
        val = getattr(args, name, None)
        if val is not None:
            setattr(st, name, val)

    if args.sizes is not None:
        st.sizes = _parse_sizes_arg(args.sizes)

    return st.validate()


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_argv)

    try:
        st = resolve_settings(args)

        if args.save_profile:
            prof_path = save_profile(st, args.save_profile)
            if not args.quiet:
                print(f"Профиль сохранён: {prof_path}")

        if args.gui or (not raw_argv) or (args.input is None and args.save_profile is None):
            from icon_launcher import launch_gui
            return launch_gui(initial_input=args.input, initial_settings=st)

        if args.input is None:
            return 0

        src_path = Path(args.input)
        img = load_source(src_path)
        frames, ico_bytes, tel = build_frames_detailed(img, st)

        out_ico = Path(args.output) if args.output else Path(f"{src_path.stem}.ico")
        out_ico.parent.mkdir(parents=True, exist_ok=True)
        out_ico.write_bytes(ico_bytes)

        preview_path: Path | None = None
        if args.preview is not None:
            preview_path = (
                Path(args.preview)
                if args.preview
                else out_ico.with_name(f"{out_ico.stem}_preview.png")
            )
            make_preview(frames, preview_path)

        png_files: list[Path] = []
        if args.png_dir:
            png_files = write_png_dir(frames, args.png_dir, prefix=out_ico.stem)

        if not args.quiet:
            sizes_str = ", ".join(str(s) for s in st.sizes)
            ico_kb = len(ico_bytes) / 1024
            print(
                f"Готово: {out_ico} ({ico_kb:.1f} КБ за {tel['render_ms']:.1f} мс)\n"
                f"  Исходник: {src_path.name} ({img.width}x{img.height} px, окно кадрирования ~{tel['window_side']} px)\n"
                f"  Форма: {st.shape}"
                + (f" (радиус {st.radius:g})" if st.shape == "rounded" else "")
                + (
                    f", рамка {st.border_color} (толщина {st.border_width:g}, отступ {st.border_inset:g})"
                    if st.border and st.border_width > 0
                    else ", без рамки"
                )
                + f"\n  Размеры ({len(st.sizes)}): {sizes_str}"
            )
            if preview_path is not None:
                print(f"  Превью: {preview_path}")
            if png_files:
                print(f"  PNG-кадры ({len(png_files)} шт.): {Path(args.png_dir)}")

        return 0

    except (FileNotFoundError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())