from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import numpy as np

from .color import OCIOProcessor
from .config import DailyConfig, resolve_ffmpeg, resolve_ocio_path
from .encode import FFmpegEncoder
from .processor import FrameContext, FrameProcessor, FrameReadError, build_ops, get_source_resolution
from .sequence import SequenceInfo, discover_sequences, keep_frames
from .slate import SlateGenerator
from .text import TextContentResolver, TextRenderer
from .timecode import TimecodeHelper

log = logging.getLogger(__name__)

ProgressCallback = Callable[..., None]  # (done: int, total: int, desc: str | None = None)


def _black_frame(width: int, height: int) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.float32)


def _output_path(seq: SequenceInfo, config: DailyConfig) -> Path:
    if config.output_path_override is not None:
        override = config.output_path_override
        # If override looks like a directory (no video extension), treat as dir
        if override.suffix.lower() not in {".mov", ".mp4", ".mxf", ".mkv"}:
            return override / f"{seq.name}_{config.output.codec}.mov"
        return override
    out_dir = config.output.directory if config.output.directory is not None \
              else seq.frames[0].path.parent
    return out_dir / f"{seq.name}_{config.output.codec}.mov"


def _deduplicate_paths(paths: list[Path]) -> list[Path]:
    counts = Counter(paths)
    seen: dict[Path, int] = {}
    result = []
    for p in paths:
        if counts[p] > 1:
            idx = seen.get(p, 0)
            seen[p] = idx + 1
            if idx == 0:
                result.append(p)
            else:
                result.append(p.with_name(f"{p.stem}-{idx:02d}{p.suffix}"))
        else:
            result.append(p)
    return result


def compute_output_paths(config: DailyConfig, sequences: list[SequenceInfo]) -> list[Path]:
    """Return the output path for each sequence, deduplicating collisions.

    Mirrors the path logic in run() so the web UI can show expected output
    paths in a preview without actually encoding.
    """
    override = config.output_path_override
    is_fixed_file = (
        override is not None
        and override.suffix.lower() in {".mov", ".mp4", ".mxf", ".mkv"}
    )
    raw = [_output_path(seq, config) for seq in sequences]
    return raw if is_fixed_file else _deduplicate_paths(raw)


def run(
    config: DailyConfig,
    progress_cb: ProgressCallback | None = None,
    verbose: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> list[Path]:
    """Encode all sequences described by config.

    progress_cb receives (completed_frames, total_frames) after each frame.
    should_stop, if given, is polled between frames; when it returns True the
    ffmpeg process is killed, the partial output is removed, and the run ends.
    Returns the list of output file paths that were fully produced.
    """
    if config.input_path is None:
        raise ValueError("config.input_path must be set before calling run()")

    ffmpeg_bin = resolve_ffmpeg(config)
    ocio_path = resolve_ocio_path(config)
    ocio = OCIOProcessor(config.ocio.transform, ocio_path)

    tc_helper = TimecodeHelper(config.output.framerate)
    resolver = TextContentResolver(tc_helper, config.cli_text)
    slate_gen = SlateGenerator() if config.slate.enable else None
    codec_preset = config.codecs[config.output.codec]
    trim_overscan = config.output.trim_overscan

    sequences = discover_sequences(config.input_path)
    # KitsuUnreal patch: only the requested frames (e.g. not leftovers of a longer render)
    if config.frame_numbers is not None:
        numbers = set(config.frame_numbers)
        sequences = [s for s in (keep_frames(s, numbers) for s in sequences) if s]
        if not sequences:
            raise FileNotFoundError(f"None of the requested frames are at {config.input_path}")
    log.info(f"Found {len(sequences)} sequence(s) at {config.input_path}")

    out_paths = compute_output_paths(config, sequences)

    # Overall progress across every sequence (and slate frames), so the bar
    # advances smoothly to 100% for the whole run instead of resetting per
    # sequence. slate frames are counted because they are written to ffmpeg too.
    slate_n = config.slate.duration_frames if config.slate.enable else 0
    grand_total = sum(len(seq) + slate_n for seq in sequences)
    done = 0
    completed: list[Path] = []
    stopped = False

    def _stop_requested() -> bool:
        return should_stop is not None and should_stop()

    for seq, out in zip(sequences, out_paths):
        if _stop_requested():
            stopped = True
            break

        out.parent.mkdir(parents=True, exist_ok=True)
        if progress_cb:
            progress_cb(done, grand_total, f"Encoding: {seq.name}")

        # Resolve output resolution: explicit config value or source EXR size
        if config.output.resolution is not None:
            width, height = config.output.resolution
        else:
            width, height = get_source_resolution(seq.frames[0].path)
            log.info(f"  resolution: {width}x{height} (from source)")

        renderer = TextRenderer((width, height), default_font=config.text_font)

        tc_start = (
            seq.start - config.slate.duration_frames
            if config.slate.enable
            else seq.start
        )
        start_tc = tc_helper.tc_from_frame(tc_start)
        end_tc = tc_helper.tc_from_frame(seq.end)

        ops = build_ops(config, ocio, renderer, resolver, (width, height))
        processor = FrameProcessor(ops, trim_overscan=trim_overscan)
        log.info(
            f"  {seq.frames[0].path}  frames {seq.start}-{seq.end} "
            f"({len(seq)} frames)  {start_tc} - {end_tc}"
        )
        log.info(f"  -> {out}")

        def _make_ctx(frame) -> FrameContext:
            return FrameContext(
                frame_path=frame.path,
                frame_number=frame.number,
                frame_index=frame.index,
                seq_start=seq.start,
                seq_end=seq.end,
                sequence_name=seq.name,
                exr_metadata={},
                filename=frame.path.name,
            )

        with FFmpegEncoder(
            codec_preset, out, width, height, config.output.framerate,
            ffmpeg_bin=ffmpeg_bin, start_timecode=start_tc, verbose=verbose,
        ) as enc:
            if slate_gen:
                slate_frame = slate_gen.generate(
                    config.slate,
                    (width, height),
                    ocio_processor=ocio if config.slate.ocio_transform else None,
                )
                for _ in range(config.slate.duration_frames):
                    if _stop_requested():
                        enc.abort()
                        stopped = True
                        break
                    enc.write_frame(slate_frame)
                    done += 1
                    if progress_cb:
                        progress_cb(done, grand_total)

            if not stopped and config.output.threads <= 1:
                for frame in seq.frames:
                    if _stop_requested():
                        enc.abort()
                        stopped = True
                        break
                    try:
                        buf = processor.process(frame.path, _make_ctx(frame))
                        enc.write_frame(buf)
                    except FrameReadError as e:
                        if config.output.strict_frames:
                            enc.abort()
                            raise
                        log.warning(str(e))
                        enc.write_frame(_black_frame(width, height))
                    done += 1
                    if progress_cb:
                        progress_cb(done, grand_total)
            elif not stopped:
                chunk_size = config.output.threads * 2
                with ThreadPoolExecutor(max_workers=config.output.threads) as pool:
                    for chunk_start in range(0, len(seq.frames), chunk_size):
                        if _stop_requested():
                            enc.abort()
                            stopped = True
                            break
                        chunk = seq.frames[chunk_start : chunk_start + chunk_size]
                        futures = [
                            pool.submit(processor.process, f.path, _make_ctx(f))
                            for f in chunk
                        ]
                        for frame, future in zip(chunk, futures):
                            if _stop_requested():
                                enc.abort()
                                stopped = True
                                break
                            try:
                                buf = future.result()
                            except FrameReadError as e:
                                if config.output.strict_frames:
                                    enc.abort()
                                    raise
                                log.warning(str(e))
                                buf = _black_frame(width, height)
                            enc.write_frame(buf)
                            done += 1
                            if progress_cb:
                                progress_cb(done, grand_total)
                        if stopped:
                            break

        if stopped:
            # The killed encoder leaves a truncated, unusable file behind.
            try:
                if out.exists():
                    out.unlink()
            except OSError:
                pass
            log.info("  stopped")
            break

        completed.append(out)
        log.info("  done")

    if stopped:
        log.info("Encode stopped by request")

    return completed
