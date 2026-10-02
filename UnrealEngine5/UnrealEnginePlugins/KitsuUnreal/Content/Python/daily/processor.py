from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Protocol, TYPE_CHECKING

import OpenEXR
import numpy as np
from PIL import Image

from .text import FrameContext, TextContentResolver, TextRenderer

if TYPE_CHECKING:
    from .color import OCIOProcessor
    from .config import DailyConfig, TextElementConfig


# ── EXR reader ────────────────────────────────────────────────────────────────

class FrameReadError(Exception):
    pass


_HEADER_SKIP = {
    "channels", "compression", "dataWindow", "displayWindow",
    "lineOrder", "pixelAspectRatio", "screenWindowCenter", "screenWindowWidth", "type",
}


def _box_ints(box) -> tuple[int, int, int, int]:
    """Extract (xmin, ymin, xmax, ymax) from an openexr 3.x window tuple.

    openexr 3.x returns dataWindow/displayWindow as
    (array([xmin, ymin], int32), array([xmax, ymax], int32)).
    """
    return int(box[0][0]), int(box[0][1]), int(box[1][0]), int(box[1][1])


def get_source_resolution(path: Path) -> tuple[int, int]:
    """Return (width, height) of the display window from an EXR header."""
    try:
        f = OpenEXR.File(str(path), separate_channels=True)
    except Exception as e:
        raise FrameReadError(f"Cannot open {path}: {e}") from e
    header = f.header()
    win = header.get("displayWindow") or header.get("dataWindow")
    if win is None:
        raise FrameReadError(f"No window attribute in {path}")
    x0, y0, x1, y1 = _box_ints(win)
    return (x1 - x0 + 1, y1 - y0 + 1)


def read_exr(path: Path, trim_overscan: bool = True) -> tuple[np.ndarray, dict[str, str]]:
    """Read EXR → float32 (H, W, 3) array.

    When trim_overscan is True (default), pixels outside the display window are
    removed. When False, the full data window buffer is returned as-is.
    """
    try:
        f = OpenEXR.File(str(path), separate_channels=True)
    except Exception as e:
        raise FrameReadError(f"Cannot open {path}: {e}") from e

    try:
        channels = f.channels()
        r = channels["R"].pixels.astype(np.float32)
        g = channels["G"].pixels.astype(np.float32)
        b = channels["B"].pixels.astype(np.float32)
    except KeyError as e:
        raise FrameReadError(f"Missing channel {e} in {path}") from e
    except Exception as e:
        raise FrameReadError(f"Cannot read RGB channels from {path}: {e}") from e

    buf = np.stack([r, g, b], axis=2)

    header = f.header()

    if trim_overscan:
        dw = header.get("dataWindow")
        disw = header.get("displayWindow")
        if dw is not None and disw is not None:
            dx0, dy0, dx1, dy1 = _box_ints(dw)
            wx0, wy0, wx1, wy1 = _box_ints(disw)
            if (dx0, dy0, dx1, dy1) != (wx0, wy0, wx1, wy1):
                y0 = max(0, wy0 - dy0)
                y1 = min(buf.shape[0], wy1 - dy0 + 1)
                x0 = max(0, wx0 - dx0)
                x1 = min(buf.shape[1], wx1 - dx0 + 1)
                buf = buf[y0:y1, x0:x1]

    metadata = {k: str(v) for k, v in header.items() if k not in _HEADER_SKIP}
    return buf, metadata


# ── Op protocol ───────────────────────────────────────────────────────────────

class ImageOp(Protocol):
    def __call__(self, buf: np.ndarray, ctx: FrameContext) -> np.ndarray: ...


# ── Individual ops ────────────────────────────────────────────────────────────

class OCIOOp:
    def __init__(self, processor: OCIOProcessor):
        self._proc = processor

    def __call__(self, buf: np.ndarray, ctx: FrameContext) -> np.ndarray:
        return self._proc.apply(buf)


class ResizeOp:
    """Scale to fit output resolution, letterbox/pillarbox with black if needed."""

    def __init__(self, target_w: int, target_h: int):
        self.tw = target_w
        self.th = target_h

    def __call__(self, buf: np.ndarray, ctx: FrameContext) -> np.ndarray:
        src_h, src_w = buf.shape[:2]
        if src_w == self.tw and src_h == self.th:
            return buf

        scale = min(self.tw / src_w, self.th / src_h)
        new_w = round(src_w * scale)
        new_h = round(src_h * scale)

        # Resize via uint8 intermediate — display-referred data, precision loss negligible
        img = Image.fromarray((buf.clip(0.0, 1.0) * 255).astype(np.uint8))
        img = img.resize((new_w, new_h), Image.LANCZOS)

        result = np.zeros((self.th, self.tw, 3), dtype=np.float32)
        x_off = (self.tw - new_w) // 2
        y_off = (self.th - new_h) // 2
        result[y_off: y_off + new_h, x_off: x_off + new_w] = (
            np.array(img).astype(np.float32) / 255.0
        )
        return result


class CropmaskOp:
    """Overlay dark horizontal bars to enforce a target aspect ratio."""

    def __init__(self, target_aspect: float, opacity: float, canvas_w: int, canvas_h: int):
        self.opacity = opacity
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        target_h = round(canvas_w / target_aspect)
        self.bar_h = max(0, (canvas_h - target_h) // 2)

    def __call__(self, buf: np.ndarray, ctx: FrameContext) -> np.ndarray:
        if self.bar_h <= 0:
            return buf
        result = buf.copy()
        result[: self.bar_h] *= 1.0 - self.opacity
        result[self.canvas_h - self.bar_h :] *= 1.0 - self.opacity
        return result


class FrameTextOp:
    """Render per-frame text (timecode, framecounter, filename, metadata.*)."""

    def __init__(
        self,
        renderer: TextRenderer,
        resolver: TextContentResolver,
        elements: list[TextElementConfig],
    ):
        self._renderer = renderer
        self._resolver = resolver
        self._elements = elements

    def __call__(self, buf: np.ndarray, ctx: FrameContext) -> np.ndarray:
        if not self._elements:
            return buf
        return self._renderer.composite(buf, self._elements, self._resolver, ctx)


# ── Pipeline ──────────────────────────────────────────────────────────────────

class FrameProcessor:
    def __init__(self, ops: list[ImageOp], trim_overscan: bool = True):
        self._ops = ops
        self._trim_overscan = trim_overscan

    def process(self, frame_path: Path, ctx: FrameContext) -> np.ndarray:
        buf, metadata = read_exr(frame_path, trim_overscan=self._trim_overscan)
        ctx = dataclasses.replace(ctx, exr_metadata=metadata)
        for op in self._ops:
            buf = op(buf, ctx)
        return buf


def build_ops(
    config: DailyConfig,
    ocio_processor: OCIOProcessor,
    renderer: TextRenderer,
    resolver: TextContentResolver,
    resolution: tuple[int, int],
) -> list[ImageOp]:
    """Assemble the fixed-order op list from config."""
    tw, th = resolution
    ops: list[ImageOp] = []

    ops.append(OCIOOp(ocio_processor))
    ops.append(ResizeOp(tw, th))

    if config.cropmask.enable:
        ops.append(CropmaskOp(config.cropmask.aspect, config.cropmask.opacity, tw, th))

    if config.text_enable:
        enabled = [el for el in config.text_overlays if el.enable]
        if enabled:
            ops.append(FrameTextOp(renderer, resolver, enabled))

    return ops
