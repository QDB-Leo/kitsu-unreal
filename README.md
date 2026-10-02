# kitsu-unreal
**KitsuUnreal**: an Unreal Engine 5.7 plugin that publishes Movie Render Graph (MRG) renders to [Kitsu](https://www.cg-wire.com/kitsu) through [gazu](https://github.com/cgwire/gazu).

When an MRG render finishes, the plugin builds an mp4 proxy from the EXRs (ACES output transform, overscan trimmed) and publishes it as a preview on the matching Kitsu task, with the status picked in the render graph.

See [TODO.md](TODO.md) for open work and the UE 5.7 issues worked around.


# Setup

## Requirements

- Unreal Engine 5.7, with the Python Editor Script Plugin, Movie Render Graph and Cine Assembly Tools.
- `ffmpeg` and `ffprobe` on the `PATH`.
- A Kitsu server, and an account or bot token allowed to publish on the project.

## Install

Copy (or link) `UnrealEngine5/UnrealEnginePlugins/KitsuUnreal` into the project's `Plugins` folder.

Unreal installs the Python dependencies at editor startup, from `PythonRequirements` in `KitsuUnreal.uplugin` (gazu, OpenEXR, OpenColorIO, numpy, Pillow, PyYAML, pydantic, pyseq, timecode and their dependencies).
Every package, dependencies included, is pinned with its hashes, so it installs with strict hash checking (`bPipStrictHashCheck`, on by default in the Python plugin settings).
Versions shared with engine plugins (PythonFoundationPackages: requests, urllib3, typing-extensions...) match their pins. To add or bump a package, edit its `name==version` in the `.uplugin` and run `python scripts/update_pip_hashes.py`.

## Kitsu credentials

Credentials are never in git. The plugin reads, in this order:
- `Config/KitsuID.ini`: a personal account, for one machine (copy `KitsuID.ini.example`),
- `Config/KitsuBot.ini`: a bot token, shared by every machine and the farm (copy `KitsuBot.ini.example`).

```ini
[Kitsu]
host=https://your-kitsu-host/api
token=...
```

It connects in the background at editor startup, and reconnects before an upload if the session was lost.

## Render graph

In the MRG graph config used for Kitsu renders:
- write EXRs in ACEScg with the tone curve disabled: the proxy applies the ACES output transform,
- add `MRG_Kitsu_Callback` as a script,
- add two variables, `KitsuTaskType` and `KitsuTaskStatus`, typed with the enums from `/KitsuUnreal/Enums`, and expose them so each job of the Movie Render Queue can set them.


# How a render reaches Kitsu

`MRG_Kitsu_Callback` (a `MovieGraphScriptBase`, in `Content/Python/mrg_callbacks.py`) runs when an MRG job finishes. A cancelled or failed render publishes nothing.

## Project, sequence, shot

The job's sequence must be a **CineAssembly**:
- Kitsu project = the assembly's production name (Cine Assembly Tools production settings),
- Kitsu sequence = the assembly's `Sequence` metadata,
- Kitsu shot = the assembly's asset name.

A plain Level Sequence has no production, so it is not published.
Names must match Kitsu; a match that only differs by case or surrounding spaces is used, with a warning to fix the name in Kitsu.
If the shot doesn't exist in Kitsu, the preview goes on the sequence's task.
A master sequence (one with a Cinematic Shot track) is published on the `Bout a Bout` task type, status `wip`.

## Task type and status: the enums

The task to publish on and the status to set come from the job's `KitsuTaskType` and `KitsuTaskStatus` graph variables. They are enums, so the Movie Render Queue shows them as dropdowns.

| Enum (`Content/Enums`) | Entries | Kitsu |
|---|---|---|
| `KitsuTaskType` | Layout, Lighting, Rendering, Render ENG, Render ES | task type **name**, matched exactly |
| `KitsuTaskStatus` | WIP, WFA, Done | status **short name**, lowercased (`WIP` → `wip`) |

The plugin reads the selected entry's **display name** from the enum asset at runtime, so there is no list to keep in sync in the code:
- to add a task type or a status, add an entry to the enum asset, named like the Kitsu task type or status short name;
- renaming an entry changes what gets published.

UE 5.7's Python can't read a `UserDefinedEnum`'s display names directly (`KismetNodeHelperLibrary`, what the "Enum to String" node calls, isn't exposed). `kitsu_utils.get_enum_display_name` calls it through `call_method` on its default object.

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
The Kitsu comment is the job's comment, plus the host name, color transform and render time.

## Editor and farm

In the editor, the proxy and upload run on a background thread, so the editor stays usable; the result is in the Output Log, and closing the editor waits for running publishes.
With `-unattended` (Deadline), they run before the job ends, since the farm may quit Unreal right after the render.


# Branches and deploying to production

This repo is the source of truth; production gets the plugin through Perforce, one way only.

- `dev`: day-to-day work, tested in a dev project that loads the plugin straight from this repo.
- `main`: exactly what is submitted to the production depot. Merge `dev` into it when a version is ready.

`scripts/sync_to_p4.ps1` copies `main` into the production project's `Plugins` folder of your P4 workspace, in a new pending changelist. It never submits: you review the CL in P4V and submit it yourself. Never edit the plugin in the P4 workspace, change it here.

The target is `-Target`, or the `P4_PLUGINS_TARGET` environment variable (set it once per machine). The P4 connection comes from your usual p4 settings (`P4PORT`, `P4USER`, `P4CLIENT`).

```powershell
[Environment]::SetEnvironmentVariable('P4_PLUGINS_TARGET', 'D:\P4\MyProject\Plugins', 'User')   # once

git checkout main; git merge dev; git push
.\scripts\sync_to_p4.ps1                     # dry run: checks + plan (add / edit / delete), touches nothing
.\scripts\sync_to_p4.ps1 -Apply              # fills a NEW pending CL
# review and submit the CL in P4V, then:
.\scripts\sync_to_p4.ps1 -TagSubmitted 1712  # tags the synced commit p4-CL1712 and pushes the tag
```

What it ships and checks:
- only files tracked by git; `*.ini.example` stay out of production,
- `Config/KitsuBot.ini` (in P4) and `Config/KitsuID.ini` (local, p4ignored) are never touched,
- git must be on `main`, clean and pushed; the P4 workspace synced to head for the plugin, with no plugin file already opened or edited without being opened,
- drift: since the last `p4-CL*` tag, any difference in P4 that git didn't introduce means production was edited outside git, and the run stops (`-AllowDrift` overwrites it). That's what the tag is for.


# Repository

| Path | |
|---|---|
| `UnrealEngine5/UnrealEnginePlugins/KitsuUnreal` | the plugin |
| `.../Content/Python/kitsu_utils.py` | job info, enums, proxy, Kitsu upload |
| `.../Content/Python/mrg_callbacks.py` | `MRG_Kitsu_Callback`, background publishing |
| `.../Content/Python/daily`, `daily_config` | vendored Rapheus/daily and its settings (see `daily/VENDORED.md`) |
| `scripts/sync_to_p4.ps1` | git → P4 deployment |
| `scripts/update_pip_hashes.py` | refreshes the pip hashes in the `.uplugin` |


# License

MIT, see [LICENSE](LICENSE). Bundled third-party files keep their own licenses:
- `Content/Python/daily`: [Rapheus/daily](https://github.com/Rapheus/daily), MIT (`daily/LICENSE`),
- `Content/Python/cg-config-v2.2.0_aces-v1.3_ocio-v2.4.ocio`: ACES CG config from [OpenColorIO-Config-ACES](https://github.com/AcademySoftwareFoundation/OpenColorIO-Config-ACES), BSD-3-Clause,
- `Content/Python/daily_config/fonts/Vera.ttf`: Bitstream Vera, [Bitstream Vera license](https://www.gnome.org/fonts/).
