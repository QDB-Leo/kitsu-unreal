from __future__ import annotations

from timecode import Timecode


class TimecodeHelper:
    """Frame number ↔ SMPTE timecode string conversion.

    Frame numbers are treated as absolute (e.g. 1000 → "00:00:41:16" at 24fps),
    meaning frame 0 is the clock origin 00:00:00:00.

    KitsuUnreal patch: the `timecode` package counts frames from 1, so frame N is
    Timecode(frames=N + 1). Upstream passed N directly, which put every timecode
    one frame early and crashed on frame 0 (Unreal's default start frame).
    """

    def __init__(self, framerate: str):
        self.framerate = framerate

    def tc_from_frame(self, frame: int) -> str:
        """Absolute frame number → "HH:MM:SS:FF" string."""
        tc = Timecode(self.framerate, frames=max(frame, 0) + 1)
        return str(tc)

    def frame_from_tc(self, timecode_str: str) -> int:
        """Timecode string → absolute frame number (0 = 00:00:00:00)."""
        tc = Timecode(self.framerate, timecode_str)
        return tc.frames - 1

    def range_string(self, start: int, end: int) -> str:
        """Human-readable range for CLI output, e.g. '00:00:41:16 – 00:00:51:16'."""
        return f"{self.tc_from_frame(start)} - {self.tc_from_frame(end)}"
