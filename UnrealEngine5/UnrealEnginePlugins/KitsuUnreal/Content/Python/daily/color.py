from __future__ import annotations

from pathlib import Path

import numpy as np
import PyOpenColorIO as ocio

from .config import OCIOTransformConfig


class ColorError(Exception):
    pass


def list_ocio_options(config_path: Path) -> dict:
    """Enumerate the colourspaces, displays, views, and looks in an OCIO config.

    Returns a dict with keys ``colorspaces`` (list[str]), ``displays`` (list[str]),
    ``views`` (dict mapping each display name to its list of view names), and
    ``looks`` (list[str]). On any failure (missing or invalid config) every value
    is empty so callers — e.g. the web UI dropdowns — can degrade gracefully
    instead of crashing.
    """
    empty: dict = {"colorspaces": [], "displays": [], "views": {}, "looks": []}
    try:
        cfg = ocio.Config.CreateFromFile(str(config_path))
    except Exception:
        return empty

    try:
        colorspaces = list(cfg.getColorSpaceNames())
        displays = list(cfg.getDisplays())
        views = {d: list(cfg.getViews(d)) for d in displays}
        looks = [look.getName() for look in cfg.getLooks()]
    except Exception:
        return empty

    return {
        "colorspaces": colorspaces,
        "displays": displays,
        "views": views,
        "looks": looks,
    }


class OCIOProcessor:
    """Applies an OCIO color transform to float32 numpy arrays (H, W, 3).

    Raises ColorError on misconfigured colorspaces instead of silently
    producing wrong-colored output.
    """

    def __init__(self, transform_config: OCIOTransformConfig, config_path: Path):
        try:
            self._ocio_cfg = ocio.Config.CreateFromFile(str(config_path))
        except Exception as e:
            raise ColorError(f"Failed to load OCIO config {config_path}: {e}") from e

        self._transform_config = transform_config
        self._cpu = self._build_cpu_processor()

    def _build_cpu_processor(self) -> ocio.CPUProcessor:
        tc = self._transform_config
        try:
            match tc.type:
                case "colorconvert":
                    transform = ocio.ColorSpaceTransform(
                        src=tc.src,
                        dst=tc.dst,
                    )
                case "display":
                    transform = ocio.DisplayViewTransform(
                        src=tc.src,
                        display=tc.display,
                        view=tc.view,
                    )
                    # Apply explicit look(s) on top of the display/view pipeline.
                    # DisplayViewTransform alone only runs the look baked into the
                    # view, so override it via the viewing pipeline when looks are
                    # given.
                    if tc.looks:
                        vp = ocio.LegacyViewingPipeline()
                        vp.setDisplayViewTransform(transform)
                        vp.setLooksOverrideEnabled(True)
                        vp.setLooksOverride(",".join(tc.looks))
                        processor = vp.getProcessor(self._ocio_cfg)
                        return processor.getDefaultCPUProcessor()
                case "look":
                    transform = ocio.LookTransform(
                        src=tc.src,
                        dst=tc.dst,
                        looks=",".join(tc.looks),
                    )
                case _:
                    raise ColorError(f"Unknown OCIO transform type: {tc.type!r}")

            processor = self._ocio_cfg.getProcessor(transform)
            return processor.getDefaultCPUProcessor()

        except ocio.Exception as e:
            raise ColorError(
                f"OCIO transform setup failed ({tc.type}, src={tc.src!r}): {e}"
            ) from e

    def apply(self, buf: np.ndarray) -> np.ndarray:
        """Apply transform in-place on a copy; returns float32 (H, W, 3)."""
        result = buf.copy()
        try:
            self._cpu.applyRGB(result)
        except Exception as e:
            raise ColorError(f"OCIO transform failed: {e}") from e
        return result
