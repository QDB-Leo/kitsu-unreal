from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from .processor import read_exr

if TYPE_CHECKING:
    from .color import OCIOProcessor
    from .config import SlateConfig

class SlateGenerator:
    """Fits a slate image frame onto the output canvas.

    fit="horizontal": scale so width matches canvas; black bars top/bottom if needed.
    fit="vertical":   scale so height matches canvas; black bars left/right if needed.

    The slate image comes from slate.frame_path in daily.yaml; when it is unset
    or missing, a blank (black) slate is produced.
    Pass ocio_processor to colour-transform a linear EXR slate; PNG slates should
    always be loaded without a transform.
    """

    def generate(
        self,
        config: SlateConfig,
        output_size: tuple[int, int],
        ocio_processor: OCIOProcessor | None = None,
    ) -> np.ndarray:
        canvas_w, canvas_h = output_size
        canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)

        path = config.frame_path
        if path is None or not path.exists():
            return canvas

        if path.suffix.lower() == ".exr":
            arr, _ = read_exr(path, trim_overscan=False)
        else:
            img = Image.open(str(path)).convert("RGB")
            arr = np.array(img).astype(np.float32) / 255.0

        if ocio_processor is not None:
            arr = ocio_processor.apply(arr)

        img_h, img_w = arr.shape[:2]
        if config.fit == "horizontal":
            scale = canvas_w / img_w
        else:
            scale = canvas_h / img_h

        new_w = round(img_w * scale)
        new_h = round(img_h * scale)

        pil = Image.fromarray((arr.clip(0.0, 1.0) * 255).astype(np.uint8))
        pil = pil.resize((new_w, new_h), Image.LANCZOS)
        arr = np.array(pil).astype(np.float32) / 255.0

        x = (canvas_w - new_w) // 2
        y = (canvas_h - new_h) // 2
        y1, y2 = max(0, y), min(canvas_h, y + new_h)
        x1, x2 = max(0, x), min(canvas_w, x + new_w)
        sy1, sx1 = y1 - y, x1 - x
        canvas[y1:y2, x1:x2] = arr[sy1:sy1 + (y2 - y1), sx1:sx1 + (x2 - x1)]

        return canvas
