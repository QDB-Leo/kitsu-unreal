from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .config import CodecPreset


class EncoderError(Exception):
    pass


class FFmpegEncoder:
    """Context manager that pipes float32 frames to ffmpeg.

    Frames must be in display-referred sRGB with values in [0, 1].
    They are converted to uint16 rgb48le before piping so ffmpeg receives
    a lossless 16-bit intermediate regardless of the target pix_fmt.

    Pass verbose=True to inherit the parent stderr so ffmpeg output is
    printed directly to the terminal.
    """

    def __init__(
        self,
        preset: CodecPreset,
        output_path: Path,
        width: int,
        height: int,
        framerate: str,
        ffmpeg_bin: str,
        start_timecode: str | None = None,
        verbose: bool = False,
    ):
        self._preset = preset
        self._output = output_path
        self._width = width
        self._height = height
        self._framerate = framerate
        self._ffmpeg = ffmpeg_bin
        self._start_timecode = start_timecode
        self._verbose = verbose
        self._proc: subprocess.Popen | None = None
        self._aborted = False

    def _build_cmd(self) -> list[str]:
        cmd = [
            self._ffmpeg,
            "-y",
            "-f", "rawvideo",
            "-pixel_format", "rgb48le",
            "-video_size", f"{self._width}x{self._height}",
            "-framerate", self._framerate,
            "-i", "pipe:0",
        ]

        if self._start_timecode:
            cmd += ["-timecode", self._start_timecode]

        cmd += ["-c:v", self._preset.codec]
        cmd += ["-pix_fmt", self._preset.pix_fmt]

        if self._preset.crf is not None:
            cmd += ["-crf", str(self._preset.crf)]

        if self._preset.ffmpeg_args:
            cmd += shlex.split(self._preset.ffmpeg_args)

        cmd.append(str(self._output))
        return cmd

    def __enter__(self) -> "FFmpegEncoder":
        self._proc = subprocess.Popen(
            self._build_cmd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None if self._verbose else subprocess.DEVNULL,
        )
        return self

    def write_frame(self, buf: np.ndarray) -> None:
        """Write a single float32 (H, W, 3) frame to the encoder."""
        frame = (buf.clip(0.0, 1.0) * 65535.0).astype(np.uint16)
        try:
            self._proc.stdin.write(frame.tobytes())
        except BrokenPipeError:
            raise EncoderError("ffmpeg pipe broken — re-run with -v for details") from None

    def abort(self) -> None:
        """Forcibly kill ffmpeg, discarding any buffered frames.

        Used to cancel an in-progress encode. After this, __exit__ skips the
        normal finalize/return-code check.
        """
        self._aborted = True
        if self._proc is not None:
            self._proc.kill()

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        self._proc.wait()

        # A killed encode has a non-zero return code by design — don't treat it
        # as a failure.
        if self._aborted:
            return

        if exc_type is None and self._proc.returncode != 0:
            raise EncoderError(
                f"ffmpeg exited {self._proc.returncode} — re-run with -v for details"
            )
