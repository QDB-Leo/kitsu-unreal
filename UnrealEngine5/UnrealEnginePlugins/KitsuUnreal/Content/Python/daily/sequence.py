from __future__ import annotations

import glob as _glob
from dataclasses import dataclass
from pathlib import Path

import pyseq


@dataclass
class FrameInfo:
    path: Path
    index: int    # 0-based position within the sequence
    number: int   # actual frame number (e.g. 1001)


@dataclass
class SequenceInfo:
    name: str
    frames: list[FrameInfo]
    start: int
    end: int

    def __len__(self) -> int:
        return len(self.frames)


_EXR_EXTS = {".exr"}


def _is_glob(path_str: str) -> bool:
    return any(c in path_str for c in ("*", "?", "["))


def _seq_name(seq: pyseq.Sequence) -> str:
    head: str = seq.head() or ""
    return head.rstrip("._- ") or Path(seq[0].name).stem


def _is_exr_seq(seq: pyseq.Sequence) -> bool:
    # KitsuUnreal patch: a single file without a frame number (a still) has no tail
    return Path(seq[0].name).suffix.lower() in _EXR_EXTS


def _build_sequence_info(seq: pyseq.Sequence) -> SequenceInfo:
    # KitsuUnreal patch: a still has no frame number, number it from 0
    frames = [
        FrameInfo(path=Path(item.path), index=i, number=item.frame if isinstance(item.frame, int) else i)
        for i, item in enumerate(seq)
    ]
    return SequenceInfo(
        name=_seq_name(seq),
        frames=frames,
        start=frames[0].number,
        end=frames[-1].number,
    )


def keep_frames(seq: SequenceInfo, numbers: set[int]) -> SequenceInfo | None:
    """KitsuUnreal patch: *seq* restricted to the given frame numbers, None if none are left."""
    kept = [f for f in seq.frames if f.number in numbers]
    if not kept:
        return None
    frames = [FrameInfo(path=f.path, index=i, number=f.number) for i, f in enumerate(kept)]
    return SequenceInfo(name=seq.name, frames=frames, start=frames[0].number, end=frames[-1].number)


def discover_sequences(input_path: Path) -> list[SequenceInfo]:
    """Return all EXR sequences found at *input_path*.

    - Glob pattern  → expand recursively, group into sequences via pyseq.
    - Directory     → scan for all EXR sequences inside it.
    - File          → find the sequence that file belongs to.
    """
    if _is_glob(str(input_path)):
        matched = _glob.glob(str(input_path), recursive=True)
        exr_files = [f for f in matched if f.lower().endswith(".exr")]
        if not exr_files:
            raise FileNotFoundError(f"No EXR files matched: {input_path}")
        # Group by parent directory — pyseq groups by filename pattern only,
        # so a flat list would merge same-named sequences from different directories.
        by_dir: dict[str, list[str]] = {}
        for f in exr_files:
            key = str(Path(f).parent)
            by_dir.setdefault(key, []).append(f)
        all_seqs = []
        for dir_files in by_dir.values():
            raw = pyseq.get_sequences(dir_files)
            all_seqs.extend(s for s in raw if _is_exr_seq(s) and len(s) > 0)
        if not all_seqs:
            raise FileNotFoundError(f"No EXR sequences matched: {input_path}")
        return [_build_sequence_info(s) for s in all_seqs]

    if input_path.is_dir():
        raw = pyseq.get_sequences(str(input_path))
        seqs = [s for s in raw if _is_exr_seq(s) and len(s) > 0]
        if not seqs:
            raise FileNotFoundError(f"No EXR sequences found in {input_path}")
        return [_build_sequence_info(s) for s in seqs]

    if input_path.is_file():
        raw = pyseq.get_sequences(str(input_path.parent))
        target = input_path.name
        for seq in raw:
            if not _is_exr_seq(seq):
                continue
            if any(item.name == target for item in seq):
                return [_build_sequence_info(seq)]
        raise FileNotFoundError(
            f"Could not find an EXR sequence containing {input_path}"
        )

    raise FileNotFoundError(f"Path not found and not a glob pattern: {input_path}")
