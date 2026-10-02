from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from .config import TextElementConfig
    from .timecode import TimecodeHelper


# ── Frame context ─────────────────────────────────────────────────────────────

@dataclass
class FrameContext:
    frame_path: Path
    frame_number: int      # e.g. 1001
    frame_index: int       # 0-based position in sequence
    seq_start: int         # first frame of sequence
    seq_end: int           # last frame of sequence
    sequence_name: str     # e.g. "M02-0014"
    exr_metadata: dict[str, str]
    filename: str          # basename of frame file


# ── Content resolver ──────────────────────────────────────────────────────────

class TextContentResolver:
    """Resolves a content-type string to a display string for a given frame."""

    def __init__(self, tc_helper: TimecodeHelper, cli_text: dict[str, str]):
        self._tc = tc_helper
        self._cli = cli_text
        self._render_time = datetime.now()

    def resolve(
        self, content: str, ctx: FrameContext, fmt: str | None = None, default: str = ""
    ) -> str:
        match content:
            case "timecode":
                return self._tc.tc_from_frame(ctx.frame_number)
            case "framecounter":
                return str(ctx.frame_number)
            case "framerange":
                return f"{ctx.seq_start}-{ctx.seq_end}"
            case "time_of_day":
                return self._render_time.strftime(fmt or "%H:%M:%S")
            case "date":
                return self._render_time.strftime(fmt or "%Y-%m-%d")
            case "filename":
                return ctx.filename
            case "sequence_name":
                return ctx.sequence_name
            case _ if content.startswith("metadata."):
                key = content.removeprefix("metadata.")
                return ctx.exr_metadata.get(key, default)
            case _:
                return self._cli.get(content, default)


# ── Renderer ──────────────────────────────────────────────────────────────────

# Pillow anchor codes: first char = h-align (l/m/r), second = v-align (a/m/d/s/b).
# We use 'a' (ascender) for top and 's' (descender) for bottom.
_ANCHOR_MAP: dict[str, str] = {
    "top-left":      "la",
    "top-center":    "ma",
    "top-right":     "ra",
    "bottom-left":   "ls",
    "bottom-center": "ms",
    "bottom-right":  "rs",
    "center":        "mm",
}


def _to_pil_color(rgba: tuple[float, ...]) -> tuple[int, int, int, int]:
    return tuple(round(c * 255) for c in rgba)  # type: ignore[return-value]


def _xy_for_anchor(
    anchor_key: str,
    offset: tuple[int, int],
    canvas_w: int,
    canvas_h: int,
) -> tuple[int, int]:
    ox, oy = offset
    match anchor_key:
        case "top-left":      return ox, oy
        case "top-center":    return canvas_w // 2, oy
        case "top-right":     return canvas_w - ox, oy
        case "bottom-left":   return ox, canvas_h - oy
        case "bottom-center": return canvas_w // 2, canvas_h - oy
        case "bottom-right":  return canvas_w - ox, canvas_h - oy
        case "center":        return canvas_w // 2, canvas_h // 2
        case _:               return ox, oy


def _load_font(font_path: Path | None, size: float, default: Path | None = None) -> ImageFont.FreeTypeFont:
    path = font_path or default
    if path is None:
        raise ValueError("No font specified. Set 'font' in text_overlays.yaml.")
    return ImageFont.truetype(str(path), size=int(size))


_REFERENCE_HEIGHT = 1080  # font_size and offset values are defined at this height

_BOTTOM_ANCHORS = {"bottom-left", "bottom-center", "bottom-right"}


class TextRenderer:
    """Composites text elements onto a float32 numpy array in-place."""

    def __init__(self, canvas_size: tuple[int, int], default_font: Path | None = None):
        self.width, self.height = canvas_size
        self._scale = self.height / _REFERENCE_HEIGHT
        self._default_font = default_font

    def _stack_offsets(self, elements: list[TextElementConfig]) -> list[int]:
        """Compute per-element Y stack offsets for elements sharing an anchor.

        For top anchors: first element is topmost, subsequent ones stack downward.
        For bottom anchors: last element sits at the base margin, earlier ones
        stack upward — so yaml order reads top-to-bottom on screen for both cases.
        """
        from collections import defaultdict

        anchor_groups: dict[str, list[int]] = defaultdict(list)
        for i, el in enumerate(elements):
            anchor_groups[el.anchor].append(i)

        offsets = [0] * len(elements)
        for anchor, indices in anchor_groups.items():
            line_heights = [elements[i].font_size * self._scale * 1.3 for i in indices]
            if anchor in _BOTTOM_ANCHORS:
                # Last in yaml = at base position; work backwards to assign upward offsets
                cum = 0.0
                for rev_pos, idx in enumerate(reversed(indices)):
                    offsets[idx] = round(-cum)
                    cum += line_heights[len(indices) - 1 - rev_pos]
            else:
                cum = 0.0
                for pos, idx in enumerate(indices):
                    offsets[idx] = round(cum)
                    cum += line_heights[pos]
        return offsets

    def composite(
        self,
        buf: np.ndarray,
        elements: list[TextElementConfig],
        resolver: TextContentResolver,
        ctx: FrameContext,
    ) -> np.ndarray:
        overlay = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        active = [el for el in elements if el.enable]
        stack_offsets = self._stack_offsets(active)
        for el, y_stack in zip(active, stack_offsets):
            text = resolver.resolve(el.content, ctx, el.format, default=el.default)
            if not text.strip():
                continue
            self._draw_element(draw, el, text, y_stack)
        return _composite_overlay(buf, overlay)

    def _draw_element(
        self,
        draw: ImageDraw.ImageDraw,
        el: TextElementConfig,
        text: str,
        y_stack: int = 0,
    ) -> None:
        scaled_size = el.font_size * self._scale
        scaled_offset = (
            round(el.offset[0] * self._scale),
            round(el.offset[1] * self._scale),
        )
        scaled_shadow_offset = (
            round(el.shadow_offset[0] * self._scale),
            round(el.shadow_offset[1] * self._scale),
        )
        font = _load_font(el.font, scaled_size, default=self._default_font)
        pillow_anchor = _ANCHOR_MAP.get(el.anchor, "la")
        xy = _xy_for_anchor(el.anchor, scaled_offset, self.width, self.height)
        xy = (xy[0], xy[1] + y_stack)
        color = _to_pil_color(el.color)
        if el.shadow:
            shadow_xy = (xy[0] + scaled_shadow_offset[0], xy[1] + scaled_shadow_offset[1])
            draw.text(
                shadow_xy,
                text,
                font=font,
                anchor=pillow_anchor,
                fill=_to_pil_color(el.shadow_color),
            )
        draw.text(xy, text, font=font, anchor=pillow_anchor, fill=color)


def _composite_overlay(buf: np.ndarray, overlay: Image.Image) -> np.ndarray:
    """Alpha-composite a PIL RGBA overlay onto a float32 numpy frame."""
    ov = np.array(overlay).astype(np.float32) / 255.0   # H, W, 4
    alpha = ov[:, :, 3:4]
    rgb = ov[:, :, :3]
    return buf * (1.0 - alpha) + rgb * alpha
