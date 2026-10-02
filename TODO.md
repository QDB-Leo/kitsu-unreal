# TODO

## Open

- [ ] Proxies for png/jpg sequences: the vendored daily reads EXR only, so those are skipped today (a single image is still uploaded).
- [ ] Send the vendored daily patches upstream (see `daily/VENDORED.md`).
- [ ] The EXR header says `unreal/layerData/rgba/disableTonecurve = 0` on every MRG render, although the tone curve is disabled in the graph and the pixels confirm it (values well above 1.0). Find where MRG takes that metadata from (probably a legacy MRQ setting) and whether it's a UE bug.
- [ ] `kitsu_read.py`: gazu wrapper for an Editor Utility Widget ("Execute Python Script") showing a task's comments. Not used at the moment.

## UE 5.7 issues worked around

- The `unreal/frameRate` EXR header is 24 unless the graph overrides the frame rate: `FMovieGraphFilenameResolveParams::MakeResolveParams` never sets `DefaultFrameRate`, which defaults to 24. The proxy takes the frame rate from the job instead (`get_render_frame_rate`). Worth reporting to Epic.
- During a render with a custom playback range, MRG sets that range on the sequence itself, so `on_job_finished` sees the custom range. The sequence's range is read in `on_job_start` (`get_sequence_frame_range`).
- Python can't read a `UserDefinedEnum`'s display names: `KismetNodeHelperLibrary` isn't exposed and `DisplayNameMap` is protected. `call_method` on the library's default object reaches `GetEnumeratorUserFriendlyName` by name (`get_enum_display_name`).
- Unreal API calls are refused off the game thread: everything that reads the job happens in the callbacks, and the background publish only uses the values collected there.

## Choices

- Proxy look: ACES 1.0 SDR (`sRGB - Display` / `ACES 1.0 - SDR Video`), kept over ACES 2.0 SDR after review. ACES 2.0 would need the ACES 2.0 studio config.
