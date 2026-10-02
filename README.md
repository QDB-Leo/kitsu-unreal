# kitsu-unreal
**KitsuUnreal**: an Unreal Engine 5.7 plugin that publishes Movie Render Graph (MRG) renders to [Kitsu](https://www.cg-wire.com/kitsu) through [gazu](https://github.com/cgwire/gazu).

When an MRG render finishes, the plugin either uploads the video file or builds an mp4 proxy from the EXRs (output transform from AcesCG applied) and publishes it as a preview on the matching Kitsu task, with the status picked in the render graph.

See [TODO.md](TODO.md) for open work and the UE 5.7 issues worked around.


# Setup

## Requirements

- Unreal Engine 5.7, with the Python Editor Script Plugin, Movie Render Graph and Cine Assembly Tools.
- `ffmpeg` and `ffprobe` on the `PATH`.
- A Kitsu server, and an account or bot token allowed to publish on the project.

## Install

Copy (or link) `UnrealEngine5/UnrealEnginePlugins/KitsuUnreal` into the project's `Plugins` folder.

## Kitsu credentials

The plugin reads, in this order:
- `Config/KitsuID.ini`: a personal account, for one machine (copy `KitsuID.ini.example`),
- `Config/KitsuBot.ini`: a bot token, shared by every machine and the farm (copy `KitsuBot.ini.example`).

```ini
[Kitsu]
host=https://your-kitsu-host/api
token=...
```

# How a render reaches Kitsu

`MRG_Kitsu_Callback` (a `MovieGraphScriptBase`, in `Content/Python/mrg_callbacks.py`) runs when an MRG job finishes. A cancelled or failed render publishes nothing.
The Kitsu comment is the job's comment, plus the host name, color transform if applied and render time.

## Project, sequence, shot

The job's sequence must be a **CineAssembly**:
- Kitsu project = the assembly's production name (Cine Assembly Tools production settings),
- Kitsu sequence = the assembly's `Sequence` metadata,
- Kitsu shot = the assembly's asset name.

A plain Level Sequence has no production, so it is not published.
Names must match Kitsu; a match that only differs by case or surrounding spaces is used, with a warning to fix the name in Kitsu.
The preview goes on the shot's task of the selected task type. If the shot has no such task, or doesn't exist in Kitsu, it goes on the sequence's task of that type instead:
that's how a sequence-level task type (e.g. an edit of the whole sequence, rendered from the master sequence) gets its previews.

## Render graph

In the MRG graph config used for Kitsu renders:
- add `MRG_Kitsu_Callback` as a script,
- add two variables, `KitsuTaskType` and `KitsuTaskStatus`, typed with the enums from `/KitsuUnreal/Enums`, and expose them so each job of the Movie Render Queue can set them.


## Task type and status: the enums

The task to publish on and the status to set come from the job's `KitsuTaskType` and `KitsuTaskStatus` graph variables. They are enums, so the Movie Render Queue shows them as dropdowns. You should customize them to match your Kitsu instance's Task Type and Status.

| Enum (`Content/Enums`) | Default Entries | Kitsu |
|---|---|---|
| `KitsuTaskType` | Layout, Lighting, Rendering, Render ENG, Render ES | task type **name**, matched exactly |
| `KitsuTaskStatus` | WIP, WFA, Done | status **short name**, lowercased (`WIP` → `wip`) |

The plugin reads the selected entry's **display name** from the enum asset at runtime, so there is no list to keep in sync in the code:
- to add a task type or a status, add an entry to the enum asset, named like the Kitsu task type or status short name;
- renaming an entry changes what gets published.

If the graph doesn't have the variable, the task type defaults to `Layout` and the status to `wip`.
If the variable is there but its value doesn't resolve, nothing is uploaded rather than publishing on the wrong task.

## Proxy

Built by a vendored [Rapheus/daily](https://github.com/Rapheus/daily) (`Content/Python/daily`, settings in `Content/Python/daily_config`):
- color: ACEScg to `sRGB - Display` / `ACES 1.0 - SDR Video`, with the bundled ACES CG config,
- Unreal's overscan cropped to the display window, then fit in 1920x1080 h264,
- frame rate of the job: the graph's Global Output frame rate if overridden, else the sequence's display rate (the `unreal/frameRate` EXR header is wrong in 5.7),
- frames: the whole Unreal sequence when all its frames are on disk, even if the job rendered only part of it (custom playback range); otherwise only the frames this job rendered. Frames on disk outside the sequence (leftovers of a longer render) are never used,
- only the first render layer gets a proxy. Without EXRs, a rendered movie or a single image is uploaded as is, and other image sequences (png, jpg) are skipped.

The mp4 must have exactly the expected frame count (ffprobe), or nothing is uploaded.

## Editor and farm

In the editor, the proxy and upload run on a background thread, so the editor stays usable; the result is in the Output Log, and closing the editor waits for running publishes.
With `-unattended` (Deadline), they run before the job ends, since the farm may quit Unreal right after the render.


# Scripts

A script is available to deploy the plugin from main branch to a local P4 environment, `scripts/sync_to_p4.ps1`: a dry run first, then a pending changelist that you review and submit yourself, then a tag on the synced commit so later runs can detect edits made in Perforce outside git.
See [scripts/README.md](scripts/README.md)


# Repository

| Path | |
|---|---|
| `UnrealEngine5/UnrealEnginePlugins/KitsuUnreal` | the plugin |
| `.../Content/Python/kitsu_utils.py` | job info, enums, proxy, Kitsu upload |
| `.../Content/Python/mrg_callbacks.py` | `MRG_Kitsu_Callback`, background publishing |
| `.../Content/Python/daily`, `daily_config` | vendored Rapheus/daily and its settings (see `daily/VENDORED.md`) |
| `scripts` | git → P4 deployment, pip hashes (see [scripts/README.md](scripts/README.md)) |


# License

MIT, see [LICENSE](LICENSE). Bundled third-party files keep their own licenses:
- `Content/Python/daily`: [Rapheus/daily](https://github.com/Rapheus/daily), MIT (`daily/LICENSE`),
- `Content/Python/cg-config-v2.2.0_aces-v1.3_ocio-v2.4.ocio`: ACES CG config from [OpenColorIO-Config-ACES](https://github.com/AcademySoftwareFoundation/OpenColorIO-Config-ACES), BSD-3-Clause,
- `Content/Python/daily_config/fonts/Vera.ttf`: Bitstream Vera, [Bitstream Vera license](https://www.gnome.org/fonts/).
