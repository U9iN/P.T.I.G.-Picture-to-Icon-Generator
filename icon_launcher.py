#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
icon_launcher.py — лаунчер и мост pywebview для генератора иконок.

Запуск:
    pip install pillow pywebview
    python icon_launcher.py
"""
from __future__ import annotations

import base64
import io
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from PIL import Image

import mimetypes
from make_icon import (
    COMMON_IMAGE_EXTS,
    REF,
    RESAMPLE,
    IconSettings,
    _apply_mask_and_border,
    _prepare,
    _window,
    _window_to_square,
    build_frames,
    build_frames_detailed,
    load_profile,
    load_source,
    load_source_bytes,
    make_demo_image,
    make_preview,
    parse_color,
    run_icon_checks,
    save_profile,
    write_png_dir,
)

SCRIPT_DIR = Path(__file__).resolve().parent
MEIPASS_DIR = Path(getattr(sys, "_MEIPASS", SCRIPT_DIR))
EXE_DIR = Path(sys.executable).resolve().parent
HTML_CANDIDATES = (
    MEIPASS_DIR / "icon_gui.html",
    EXE_DIR / "icon_gui.html",
    SCRIPT_DIR / "icon_gui.html",
    Path.cwd() / "icon_gui.html",
)
PREVIEW_SIZES = (256, 64, 48, 32, 24, 16)
PREVIEW_MAX_SOURCE = 1024


def _find_html_file() -> Path | None:
    for candidate in HTML_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _missing_html_fallback(expected_path: Path) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<style>
  body {{
    background: #0a0c10; color: #c9d1d9;
    font-family: 'JetBrains Mono', Consolas, monospace;
    padding: 32px; line-height: 1.6;
  }}
  .box {{
    background: #161b22; border: 1px solid #ff2e4d;
    border-radius: 8px; padding: 20px; max-width: 680px;
  }}
  h2 {{ color: #ff2e4d; margin-top: 0; }}
  code {{
    display: block; background: #0a0c10; color: #2de2e6;
    padding: 10px; border-radius: 6px; margin: 10px 0;
    word-break: break-all;
  }}
</style>
</head>
<body>
  <div class="box">
    <h2>Не найден файл интерфейса icon_gui.html</h2>
    <p>Положите файл <b>icon_gui.html</b> в одну папку со скриптами <b>icon_launcher.py</b> и <b>make_icon.py</b>:</p>
    <code>{expected_path}</code>
  </div>
</body>
</html>"""


def _image_to_data_url_and_size(img: Image.Image) -> tuple[str, int]:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    raw = buf.getvalue()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}", len(raw)


def _make_client_source_url(img: Image.Image, max_dim: int = 1024) -> str:
    if max(img.size) > max_dim:
        k = max_dim / max(img.size)
        im = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), RESAMPLE)
    else:
        im = img
    url, _ = _image_to_data_url_and_size(im)
    return url


# ---------------------------------------------------------------------------
# js_api bridge — каждый публичный метод доступен из JS как
# window.pywebview.api.<name>(...), возвращая JSON-сериализуемый словарь.
# ---------------------------------------------------------------------------
class IconMakerAPI:
    def __init__(
        self,
        initial_input: str | Path | None = None,
        initial_settings: IconSettings | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._window = None
        self._settings = (initial_settings or IconSettings()).validate()
        if initial_input:
            p = Path(initial_input)
            self._src_img = load_source(p)
            self._src_name = p.name
            self._src_stem = p.stem
        else:
            self._src_img = make_demo_image(512)
            self._src_name = "demo_synthwave.png"
            self._src_stem = "icon"
        self._client_src_url: str = _make_client_source_url(self._src_img)
        self._prep_key: tuple | None = None
        self._prep_img: Image.Image | None = None
        self._prep_rgba: Image.Image | None = None
        self._prep_scale: float = 1.0

    def attach_window(self, window) -> None:
        self._window = window

    def _set_source_locked(self, img: Image.Image, name: str, stem: str) -> None:
        self._src_img = img
        self._src_name = name
        self._src_stem = stem or "icon"
        self._client_src_url = _make_client_source_url(img)
        self._prep_key = None

    def _render_preview_locked(
        self,
        st: IconSettings,
        include_source: bool = False,
        include_telemetry: bool = False,
    ) -> dict:
        t0 = time.perf_counter()
        trim_key = (
            id(self._src_img),
            st.trim_top,
            st.trim_bottom,
            st.trim_left,
            st.trim_right,
        )
        if self._prep_key != trim_key or self._prep_img is None or self._prep_rgba is None:
            prep_img, scale = _prepare(self._src_img, st, max_dim=PREVIEW_MAX_SOURCE)
            self._prep_key = trim_key
            self._prep_img = prep_img
            self._prep_rgba = prep_img.convert("RGBa")
            self._prep_scale = scale

        prep_img = self._prep_img
        prep_rgba = self._prep_rgba
        scale = self._prep_scale
        bg_rgba = parse_color(st.background)
        border_rgba = parse_color(st.border_color, st.border_alpha)
        assert border_rgba is not None

        win = _window(prep_img.size, st)
        base_256 = _window_to_square(prep_rgba, win, 256, bg_rgba)
        base_256_rgba = base_256.convert("RGBa")

        frames_b64 = {}
        preview_sizes_bytes: dict[int, int] = {}
        for n in PREVIEW_SIZES:
            if n == 256:
                art_n = base_256.copy()
            else:
                art_n = base_256_rgba.resize((n, n), RESAMPLE).convert("RGBA")
            frame = _apply_mask_and_border(art_n, st, n, border_rgba)
            url, raw_len = _image_to_data_url_and_size(frame)
            frames_b64[str(n)] = url
            preview_sizes_bytes[n] = raw_len

        win_side = win[2] / scale
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        out = {
            "ok": True,
            "settings": asdict(st),
            "frames": frames_b64,
            "window_side": round(win_side),
            "src_name": self._src_name,
            "src_width": self._src_img.width,
            "src_height": self._src_img.height,
            "render_ms": round(elapsed_ms, 1),
        }
        if include_source:
            out["src_data_url"] = self._client_src_url
        if include_telemetry:
            breakdown = []
            for n in sorted(PREVIEW_SIZES):
                s = n / REF
                bw = max(1, int(st.border_width * s + 0.5)) if (st.border and st.border_width > 0) else 0
                bi = int(st.border_inset * s + 0.5) if (st.border and st.border_width > 0) else 0
                breakdown.append(
                    {
                        "size": n,
                        "png_bytes": preview_sizes_bytes[n],
                        "border_px": bw,
                        "inset_px": bi,
                    }
                )
            png_total = sum(preview_sizes_bytes.values())
            hdr = 6 + 16 * len(PREVIEW_SIZES)
            out["telemetry"] = {
                "window_side": round(win_side),
                "scale_ratio": round(win_side / 256.0, 2),
                "render_ms": round(elapsed_ms, 1),
                "breakdown": breakdown,
                "png_total_bytes": png_total,
                "header_bytes": hdr,
                "ico_total_bytes": png_total + hdr,
            }
        return out

    def get_initial_state(self) -> dict:
        with self._lock:
            return self._render_preview_locked(self._settings, include_source=True, include_telemetry=True)

    def render_preview(self, settings_dict: dict | None = None) -> dict:
        with self._lock:
            try:
                if settings_dict is not None:
                    self._settings = IconSettings.from_dict(settings_dict)
                return self._render_preview_locked(self._settings, include_source=False, include_telemetry=False)
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def inspect_icon(self, settings_dict: dict | None = None) -> dict:
        """Возвращает детальную телеметрию по всем выбранным размерам .ICO (build_frames_detailed)."""
        with self._lock:
            try:
                if settings_dict is not None:
                    self._settings = IconSettings.from_dict(settings_dict)
                _, _, tel = build_frames_detailed(self._src_img, self._settings)
                return {"ok": True, "telemetry": tel}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def run_checks(self, settings_dict: dict | None = None) -> dict:
        """Запускает самодиагностику движка и аудит текущей иконки (аналог run_checks из gui_launcher.py)."""
        with self._lock:
            try:
                if settings_dict is not None:
                    self._settings = IconSettings.from_dict(settings_dict)
                checks = run_icon_checks(self._src_img, self._settings)
                _, _, tel = build_frames_detailed(self._src_img, self._settings)
                return {"ok": True, "checks": checks, "telemetry": tel}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def load_image_data(
        self,
        filename: str,
        data_url: str,
        settings_dict: dict | None = None,
    ) -> dict:
        """Принимает картинку, перетащенную в окно (Drag & Drop) или выбранную через input."""
        with self._lock:
            try:
                payload = data_url.split(",", 1)[1] if "," in data_url else data_url
                raw = base64.b64decode(payload)
                img = load_source_bytes(raw, label=filename)
                p = Path(filename) if filename else Path("icon.png")
                self._set_source_locked(img, p.name, p.stem)
                if settings_dict is not None:
                    self._settings = IconSettings.from_dict(settings_dict)
                return self._render_preview_locked(self._settings, include_source=True, include_telemetry=True)
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def open_image_dialog(self, settings_dict: dict | None = None) -> dict:
        if self._window is None:
            return {"ok": False, "cancelled": True}
        import webview

        ext_glob = ";".join(f"*{ext}" for ext in COMMON_IMAGE_EXTS)
        file_types = (
            f"Все изображения ({ext_glob})",
            "Популярные (*.png;*.jpg;*.jpeg;*.jfif;*.webp;*.avif;*.bmp;*.gif;*.ico;*.svg)",
            "Профессиональные и текстуры (*.psd;*.tga;*.dds;*.tif;*.tiff;*.icns;*.qoi;*.pcx;*.ppm;*.jp2)",
            "Все файлы (*.*)",
        )
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=file_types)
        if not res:
            return {"ok": False, "cancelled": True}
        chosen = Path(res[0] if isinstance(res, (list, tuple)) else res)
        with self._lock:
            try:
                img = load_source(chosen)
                self._set_source_locked(img, chosen.name, chosen.stem)
                if settings_dict is not None:
                    self._settings = IconSettings.from_dict(settings_dict)
                return self._render_preview_locked(self._settings, include_source=True, include_telemetry=True)
            except Exception as pil_err:
                # Фоллбэк через движок Chromium (Edge WebView2) для форматов вроде .svg или .avif без плагина
                try:
                    raw = chosen.read_bytes()
                    ext = chosen.suffix.lower()
                    mime = {
                        ".svg": "image/svg+xml",
                        ".avif": "image/avif",
                        ".jfif": "image/jpeg",
                        ".jpe": "image/jpeg",
                        ".webp": "image/webp",
                    }.get(ext) or mimetypes.guess_type(chosen.name)[0] or "application/octet-stream"
                    b64 = base64.b64encode(raw).decode("ascii")
                    return {
                        "ok": True,
                        "needs_browser_raster": True,
                        "filename": chosen.name,
                        "data_url": f"data:{mime};base64,{b64}",
                    }
                except Exception:
                    return {"ok": False, "error": str(pil_err)}

    def load_profile_dialog(self) -> dict:
        if self._window is None:
            return {"ok": False, "cancelled": True}
        import webview

        file_types = ("JSON-профиль (*.json)", "Все файлы (*.*)")
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=file_types)
        if not res:
            return {"ok": False, "cancelled": True}
        chosen = Path(res[0] if isinstance(res, (list, tuple)) else res)
        with self._lock:
            try:
                self._settings = load_profile(chosen)
                out = self._render_preview_locked(self._settings, include_source=False, include_telemetry=True)
                out["profile_name"] = chosen.name
                return out
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def save_profile_dialog(self, settings_dict: dict) -> dict:
        if self._window is None:
            return {"ok": False, "cancelled": True}
        import webview

        try:
            st = IconSettings.from_dict(settings_dict)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        file_types = ("JSON-профиль (*.json)",)
        res = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=f"{self._src_stem}_profile.json",
            file_types=file_types,
        )
        if not res:
            return {"ok": False, "cancelled": True}
        target = Path(res[0] if isinstance(res, (list, tuple)) else res)
        try:
            saved = save_profile(st, target)
            return {"ok": True, "path": str(saved)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def save_ico_dialog(self, settings_dict: dict) -> dict:
        if self._window is None:
            return {"ok": False, "cancelled": True}
        import webview

        try:
            st = IconSettings.from_dict(settings_dict)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        file_types = ("Иконка Windows (*.ico)",)
        res = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=f"{self._src_stem}.ico",
            file_types=file_types,
        )
        if not res:
            return {"ok": False, "cancelled": True}
        target = Path(res[0] if isinstance(res, (list, tuple)) else res)
        with self._lock:
            try:
                frames, ico_bytes, tel = build_frames_detailed(self._src_img, st)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(ico_bytes)
                kb = len(ico_bytes) / 1024
                return {
                    "ok": True,
                    "path": str(target),
                    "kb": round(kb, 1),
                    "count": len(frames),
                    "telemetry": tel,
                }
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def save_preview_dialog(self, settings_dict: dict) -> dict:
        if self._window is None:
            return {"ok": False, "cancelled": True}
        import webview

        try:
            st = IconSettings.from_dict(settings_dict)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        file_types = ("PNG-изображение (*.png)",)
        res = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=f"{self._src_stem}_preview.png",
            file_types=file_types,
        )
        if not res:
            return {"ok": False, "cancelled": True}
        target = Path(res[0] if isinstance(res, (list, tuple)) else res)
        with self._lock:
            try:
                frames, _ = build_frames(self._src_img, st)
                out = make_preview(frames, target)
                return {"ok": True, "path": str(out)}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def export_png_dir_dialog(self, settings_dict: dict) -> dict:
        if self._window is None:
            return {"ok": False, "cancelled": True}
        import webview

        try:
            st = IconSettings.from_dict(settings_dict)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return {"ok": False, "cancelled": True}
        folder = Path(res[0] if isinstance(res, (list, tuple)) else res)
        with self._lock:
            try:
                frames, _ = build_frames(self._src_img, st)
                saved = write_png_dir(frames, folder, prefix=self._src_stem)
                return {"ok": True, "path": str(folder), "count": len(saved)}
            except Exception as e:
                return {"ok": False, "error": str(e)}


def launch_gui(
    initial_input: str | Path | None = None,
    initial_settings: IconSettings | None = None,
) -> int:
    try:
        import webview
    except ImportError:
        print(
            "Ошибка: пакет pywebview не установлен.\n"
            "Установите его командой:  pip install pywebview",
            file=sys.stderr,
        )
        return 1

    html_file = _find_html_file()
    if html_file is not None:
        html_content = html_file.read_text(encoding="utf-8")
    else:
        expected = SCRIPT_DIR / "icon_gui.html"
        print(f"[icon_gui] Внимание: файл не найден по пути {expected}", file=sys.stderr)
        html_content = _missing_html_fallback(expected)

    api = IconMakerAPI(initial_input=initial_input, initial_settings=initial_settings)
    win = webview.create_window(
        title="P.T.I.G. / Picture-to-Icon Generator",
        html=html_content,
        js_api=api,
        width=1160,
        height=860,
        min_size=(880, 680),
        background_color="#0a0c10",
    )
    api.attach_window(win)
    try:
        webview.start(gui="edgechromium", debug=False)
    except Exception:
        print("[icon_gui] Edge WebView2 недоступен, пробуем стандартный движок...")
        webview.start(debug=False)
    return 0


def main() -> None:
    sys.exit(launch_gui())


if __name__ == "__main__":
    main()