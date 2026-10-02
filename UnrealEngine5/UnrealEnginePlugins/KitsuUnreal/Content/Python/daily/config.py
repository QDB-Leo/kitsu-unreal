from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


# ── Codec preset ──────────────────────────────────────────────────────────────

class CodecPreset(BaseModel):
    name: str
    codec: str
    pix_fmt: str
    crf: int | None = None
    ffmpeg_args: str = ""


# ── OCIO ──────────────────────────────────────────────────────────────────────

class OCIOTransformConfig(BaseModel):
    type: Literal["colorconvert", "display", "look"]
    src: str
    dst: str | None = None
    display: str | None = None
    view: str | None = None
    looks: list[str] = []

    @model_validator(mode="after")
    def check_required_fields(self) -> "OCIOTransformConfig":
        if self.type == "colorconvert" and self.dst is None:
            raise ValueError("colorconvert transform requires 'dst'")
        if self.type == "display" and (self.display is None or self.view is None):
            raise ValueError("display transform requires 'display' and 'view'")
        if self.type == "look" and self.dst is None:
            raise ValueError("look transform requires 'dst'")
        return self


class OCIOConfig(BaseModel):
    config: str = "$OCIO"
    transform: OCIOTransformConfig


# ── Output ────────────────────────────────────────────────────────────────────

class OutputConfig(BaseModel):
    codec: str = "h264_hq"
    resolution: tuple[int, int] | None = None  # None = use source EXR resolution
    framerate: str = "24"
    directory: Path | None = None
    trim_overscan: bool = True
    # KitsuUnreal patch: raise on an unreadable frame instead of encoding a black frame
    strict_frames: bool = False
    threads: int = Field(default_factory=lambda: os.cpu_count() or 1)

    @field_validator("framerate", mode="before")
    @classmethod
    def _framerate_to_str(cls, v: Any) -> Any:
        # Numeric overrides (e.g. CLI --output-framerate 25, or the web form)
        # arrive as int/float after coercion; framerate is stored as a string.
        if isinstance(v, (int, float)):
            return str(v)
        return v


# ── Cropmask ──────────────────────────────────────────────────────────────────

class CropmaskConfig(BaseModel):
    enable: bool = False
    aspect: float = 1.85
    opacity: float = 0.7


# ── Slate ─────────────────────────────────────────────────────────────────────

class SlateConfig(BaseModel):
    enable: bool = False
    frame_path: Path | None = None
    duration_frames: int = 24
    fit: Literal["horizontal", "vertical"] = "horizontal"
    ocio_transform: bool = False

    @field_validator("frame_path")
    @classmethod
    def frame_path_must_exist(cls, v: Path | None) -> Path | None:
        if v is not None and not v.exists():
            raise ValueError(f"Slate frame not found: {v}")
        return v


# ── Text elements ─────────────────────────────────────────────────────────────

class TextElementConfig(BaseModel):
    content: str
    enable: bool = True
    anchor: Literal[
        "top-left", "top-center", "top-right",
        "bottom-left", "bottom-center", "bottom-right",
        "center",
    ] = "bottom-left"
    offset: tuple[int, int] = (20, 20)
    font: Path | None = None
    font_size: float = 24.0
    color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    shadow: bool = False
    shadow_offset: tuple[int, int] = (2, 2)
    shadow_color: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.7)
    default: str = ""
    format: str | None = None

    @field_validator("font")
    @classmethod
    def font_must_exist(cls, v: Path | None) -> Path | None:
        if v is not None and not v.exists():
            raise ValueError(f"Font not found: {v}")
        return v


# ── Top-level config ──────────────────────────────────────────────────────────

class DailyConfig(BaseModel):
    ocio: OCIOConfig
    output: OutputConfig = OutputConfig()
    cropmask: CropmaskConfig = CropmaskConfig()
    slate: SlateConfig = SlateConfig()
    text_enable: bool = True
    text_font: Path | None = None   # None = bundled Vera.ttf
    text_overlays: list[TextElementConfig] = []

    # From yaml (optional explicit path to ffmpeg binary)
    ffmpeg: str | None = None

    # Populated at load time, not from yaml
    codecs: dict[str, CodecPreset] = {}
    cli_text: dict[str, str] = {}
    input_path: Path | None = None
    output_path_override: Path | None = None
    # KitsuUnreal patch: encode only these frame numbers (None = every frame on disk)
    frame_numbers: list[int] | None = None

    model_config = {"arbitrary_types_allowed": True}


# ── OCIO path resolution ──────────────────────────────────────────────────────

def resolve_ocio_path(config: DailyConfig) -> Path:
    """Resolve OCIO config path: literal path → $OCIO env var → error."""
    raw = config.ocio.config
    if raw.startswith("$"):
        env_var = raw[1:]
        value = os.environ.get(env_var)
        if not value:
            raise RuntimeError(
                f"OCIO config set to '{raw}' but ${env_var} is not set. "
                "Use --ocio, set $OCIO, or update ocio.config in daily.yaml."
            )
        raw = value
    path = Path(raw)
    if not path.exists():
        raise RuntimeError(f"OCIO config not found: {path}")
    return path


def resolve_ffmpeg(config: DailyConfig) -> str:
    """Return absolute path to ffmpeg binary.

    Resolution order:
      --ffmpeg CLI flag / daily.yaml ffmpeg key  →  PATH search  →  error
    """
    candidate = config.ffmpeg
    if candidate:
        p = Path(candidate)
        if not p.exists():
            raise RuntimeError(f"ffmpeg binary not found at configured path: {p}")
        return str(p)

    found = shutil.which("ffmpeg")
    if found:
        return found

    raise RuntimeError(
        "ffmpeg not found. Either:\n"
        "  • Add ffmpeg to your PATH, or\n"
        "  • Set 'ffmpeg: /path/to/ffmpeg' in daily.yaml, or\n"
        "  • Pass --ffmpeg /path/to/ffmpeg on the command line."
    )


# ── YAML loaders ──────────────────────────────────────────────────────────────

def _bundled(name: str) -> Path:
    """Return path to a project-level config/font file (root/config or root/fonts)."""
    return Path(__file__).parent.parent / name


def load_codecs(path: Path | None = None) -> dict[str, CodecPreset]:
    if path is None:
        path = _bundled("config/codecs.yaml")
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {key: CodecPreset(name=key, **val) for key, val in raw.items()}


def user_config_path(path: Path | None = None) -> Path:
    """Resolve which daily.yaml load_user_config would read: explicit → cwd → bundled."""
    if path is not None:
        return path
    cwd_cfg = Path("daily.yaml")
    if cwd_cfg.exists():
        return cwd_cfg
    return _bundled("config/daily.yaml")


def load_user_config(path: Path | None = None) -> dict[str, Any]:
    """Load daily.yaml, preferring cwd → bundled default."""
    cfg_path = user_config_path(path)
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def text_overlays_path(path: Path | None = None) -> Path:
    """Resolve which text_overlays.yaml is used: explicit → cwd → bundled."""
    if path is not None:
        return path
    cwd_cfg = Path("text_overlays.yaml")
    if cwd_cfg.exists():
        return cwd_cfg
    return _bundled("config/text_overlays.yaml")


def _resolve_font(raw: Any, yaml_dir: Path) -> Path | None:
    """Resolve a font reference from text_overlays.yaml.

    - bare filename ("Vera.ttf")        → bundled fonts dir (package resource)
    - relative path ("subdir/Foo.ttf")  → relative to the text_overlays.yaml
    - absolute path                     → used as-is
    """
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        return p
    if str(p) == p.name:
        return _bundled("fonts") / p.name
    return Path(_resolve_yaml_path(str(p), yaml_dir))


def load_text_overlays(
    path: Path | None = None,
) -> tuple[Path | None, bool | None, list[Any]]:
    """Load text_overlays.yaml, preferring explicit path → cwd → bundled default.

    Returns (font_path, enable, elements).
    - font_path: resolved Path to the font file, or None if not specified.
    - enable: bool from the yaml 'enable' key, or None if not specified.
    - elements: list of raw element dicts.

    Font references are resolved relative to the text_overlays.yaml location (a
    bare filename resolves to the bundled fonts dir), so they don't depend on
    the working directory.
    """
    cfg_path = text_overlays_path(path)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    yaml_dir = cfg_path.resolve().parent

    if isinstance(raw, list):
        elements = raw
        return None, None, _resolve_element_fonts(elements, yaml_dir)

    font = _resolve_font(raw.get("font"), yaml_dir)
    enable: bool | None = raw.get("enable")
    elements = _resolve_element_fonts(raw.get("elements", []), yaml_dir)

    return font, enable, elements


def _resolve_element_fonts(elements: list[Any], yaml_dir: Path) -> list[Any]:
    """Resolve any per-element 'font' override relative to the yaml location."""
    for el in elements:
        if isinstance(el, dict) and el.get("font"):
            resolved = _resolve_font(el["font"], yaml_dir)
            if resolved is not None:
                el["font"] = str(resolved)
    return elements


def _coerce(value: str) -> Any:
    """Best-effort type coercion for string values from CLI flags."""
    if value.lower() in ("true", "yes"):   return True
    if value.lower() in ("false", "no"):   return False
    if value.lower() in ("null", "none"):  return None
    try: return int(value)
    except ValueError: pass
    try: return float(value)
    except ValueError: pass
    return value


def _apply_dotpath(data: dict, key: str, value: Any) -> None:
    """Set a nested dict value via a dot-separated key path."""
    parts = key.split(".")
    d = data
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value


def _resolve_yaml_path(raw: Any, config_dir: Path, *, allow_env: bool = False) -> Any:
    """Resolve a relative path *relative to the daily.yaml location*.

    Anchoring on the config file (and its parent, so a config in ``config/`` can
    reference sibling dirs like ``example/``) makes paths independent of where
    the tool was launched. Absolute paths, env refs (``$OCIO``), and paths that
    don't resolve are returned unchanged so existing behaviour/validation stands.
    """
    if not isinstance(raw, str) or not raw:
        return raw
    if allow_env and raw.startswith("$"):
        return raw
    p = Path(raw)
    if p.is_absolute():
        return raw
    for base in (config_dir, config_dir.parent):
        cand = base / p
        if cand.exists():
            return str(cand.resolve())
    return raw


def build_config(
    user_yaml: dict[str, Any],
    codecs: dict[str, CodecPreset],
    text_overlays: list[Any] | None = None,
    text_font: Path | None = None,
    text_enable: bool | None = None,
    *,
    cli_overrides: dict[str, Any] | None = None,
    config_dir: Path | None = None,
) -> DailyConfig:
    """Merge yaml + CLI overrides into a validated DailyConfig.

    config_dir, when given, is the directory of the daily.yaml; relative
    slate/OCIO paths are resolved against it so they work regardless of cwd.
    """
    data = dict(user_yaml)
    if text_overlays is not None:
        data["text_overlays"] = text_overlays
    if text_font is not None:
        data["text_font"] = str(text_font)
    if text_enable is not None:
        data["text_enable"] = text_enable
    overrides = cli_overrides or {}

    # Apply generic dot-path overrides from auto-generated CLI flags
    for dot_path, value in overrides.get("set_overrides", {}).items():
        _apply_dotpath(data, dot_path, value)

    # Shorthands that don't map directly to yaml keys
    if "codec" in overrides:
        _apply_dotpath(data, "output.codec", overrides["codec"])
    if "output_dir" in overrides:
        _apply_dotpath(data, "output.directory", str(overrides["output_dir"]))

    # Resolve relative slate/OCIO paths against the daily.yaml location so they
    # don't depend on the working directory.
    if config_dir is not None:
        slate = data.get("slate")
        if isinstance(slate, dict) and slate.get("frame_path"):
            slate["frame_path"] = _resolve_yaml_path(slate["frame_path"], config_dir)
        ocio = data.get("ocio")
        if isinstance(ocio, dict) and ocio.get("config"):
            ocio["config"] = _resolve_yaml_path(ocio["config"], config_dir, allow_env=True)

    cfg = DailyConfig.model_validate(data)
    # Inject non-yaml fields
    object.__setattr__(cfg, "codecs", codecs)
    object.__setattr__(cfg, "cli_text", overrides.get("cli_text", {}))
    object.__setattr__(cfg, "input_path", overrides.get("input_path"))
    object.__setattr__(cfg, "output_path_override", overrides.get("output_path_override"))

    # Re-validate codec now that codecs dict is populated
    if codecs and cfg.output.codec not in codecs:
        available = ", ".join(codecs)
        raise ValueError(
            f"Codec '{cfg.output.codec}' not in codecs.yaml. Available: {available}"
        )
    return cfg


# Video container extensions that mark an --output value as a file (not a directory)
_VIDEO_SUFFIXES = {".mov", ".mp4", ".mxf", ".mkv"}


def build_daily_config(
    *,
    input_path: str | Path,
    output: str | Path | None = None,
    codec: str | None = None,
    text: dict[str, str] | None = None,
    set_overrides: dict[str, Any] | None = None,
    config_path: Path | None = None,
    codecs_path: Path | None = None,
    text_overlays_path: Path | None = None,
) -> DailyConfig:
    """Load the YAML config files, merge overrides, and return a DailyConfig.

    Shared by the CLI (``cmd_encode``) and the web UI so both build configs the
    same way.

    - ``input_path``: EXR directory, single file, or glob pattern.
    - ``output``: a *.mov/.mp4/.mxf/.mkv path is treated as a fixed output file;
      anything else is treated as an output directory. ``None`` writes next to
      the source frames.
    - ``codec``: shorthand for ``output.codec`` (overrides set_overrides if given).
    - ``text``: custom ``--text key=value`` overlay values.
    - ``set_overrides``: dot-path → value overrides (e.g. ``ocio.transform.src``).
      Non-bool values are coerced via :func:`_coerce`.
    """
    user_yaml = load_user_config(config_path)
    codecs = load_codecs(codecs_path)
    text_font, text_enable, text_overlays = load_text_overlays(text_overlays_path)
    config_dir = user_config_path(config_path).resolve().parent

    cli_overrides: dict[str, Any] = {
        "input_path": Path(input_path),
        "cli_text": dict(text or {}),
    }

    if codec:
        cli_overrides["codec"] = codec

    if output:
        out = Path(output)
        if out.suffix.lower() in _VIDEO_SUFFIXES:
            cli_overrides["output_path_override"] = out
        else:
            cli_overrides["output_dir"] = out

    coerced: dict[str, Any] = {}
    for dot_path, val in (set_overrides or {}).items():
        if val is None:
            continue
        # Strings from CLI flags get best-effort coercion; richer values from
        # the web UI (bool, int, float, list/tuple) pass through unchanged.
        coerced[dot_path] = _coerce(val) if isinstance(val, str) else val
    cli_overrides["set_overrides"] = coerced

    return build_config(
        user_yaml, codecs, text_overlays, text_font, text_enable,
        cli_overrides=cli_overrides, config_dir=config_dir,
    )
