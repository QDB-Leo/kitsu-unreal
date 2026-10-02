# Vendored: Rapheus/daily

Source: https://github.com/Rapheus/daily at `7993333` (MIT, see LICENSE).
Library modules only: `cli.py`, `webui.py` and `__main__.py` are left out (they need rich / gradio).
The plugin's config is in `../daily_config`, and `kitsu_utils.generate_proxy` is the only caller.

Local patches (worth sending upstream):
- `timecode.py`: the `timecode` package counts frames from 1. Upstream passed the frame number directly,
  which put every timecode one frame early and crashed on frame 0, Unreal's default start frame.
- `sequence.py`: a single EXR without a frame number (a still) was not recognized as an EXR sequence.
- `config.py` / `daily.py`: `output.strict_frames` raises on an unreadable frame instead of encoding a black frame.
- `config.py` / `daily.py` / `sequence.py`: `frame_numbers` (set in code, not in yaml) encodes only those frames of the sequence found on disk.

`fonts/Vera.ttf` in `../daily_config` is Bitstream Vera (freely redistributable), from the same repo.
