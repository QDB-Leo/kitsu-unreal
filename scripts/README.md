# Scripts

| Script | |
|---|---|
| `sync_to_p4.ps1` | deploys the plugin from `main` to the production Perforce depot |
| `update_pip_hashes.py` | refreshes the pip hashes of the Python requirements in the `.uplugin` |


## sync_to_p4.ps1: git → Perforce

This repo is the source of truth; production gets the plugin through Perforce, one way only.

- `dev`: day-to-day work, tested in a dev project that loads the plugin straight from this repo.
- `main`: exactly what is submitted to the production depot. Merge `dev` into it when a version is ready.

The script copies `main` into the production project's `Plugins` folder of your P4 workspace, in a new pending changelist. It never submits: you review the CL in P4V and submit it yourself. Never edit the plugin in the P4 workspace, change it here.

The target is `-Target`, or the `P4_PLUGINS_TARGET` environment variable (set it once per machine). The P4 connection comes from your usual p4 settings (`P4PORT`, `P4USER`, `P4CLIENT`).

```powershell
[Environment]::SetEnvironmentVariable('P4_PLUGINS_TARGET', 'D:\P4\MyProject\Plugins', 'User')   # once

git checkout main; git merge dev; git push
.\scripts\sync_to_p4.ps1                     # dry run: checks + plan (add / edit / delete), touches nothing
.\scripts\sync_to_p4.ps1 -Apply              # fills a NEW pending CL
# review and submit the CL in P4V, then:
.\scripts\sync_to_p4.ps1 -TagSubmitted 1712  # tags the synced commit p4-CL1712 and pushes the tag
```

P4 may renumber the CL on submit: tag with the number it reports.

What it ships and checks:
- only files tracked by git; `*.ini.example` stay out of production,
- `Config/KitsuBot.ini` (in P4) and `Config/KitsuID.ini` (local, p4ignored) are never touched,
- git must be on `main`, clean and pushed; the P4 workspace synced to head for the plugin, with no plugin file already opened or edited without being opened,
- drift: since the last `p4-CL*` tag, any difference in P4 that git didn't introduce means production was edited outside git, and the run stops (`-AllowDrift` overwrites it). That's what the tag is for.


## update_pip_hashes.py: Python requirements

Unreal installs the plugin's Python dependencies at editor startup, from `PythonRequirements` in `KitsuUnreal.uplugin`.
Every package, dependencies included, is pinned with its hashes, so it installs with strict hash checking (`bPipStrictHashCheck`, on by default in the Python plugin settings).
Versions shared with engine plugins (PythonFoundationPackages: requests, urllib3, typing-extensions...) match their pins.

To add or bump a package, edit its `name==version` in the `.uplugin` (keep its dependencies listed and pinned too), then:

```powershell
python scripts/update_pip_hashes.py
```

It fetches from PyPI the hashes of the wheels CPython 3.11 (UE 5.7's Python) can install: every platform for the `All` entry, Windows only for `Win64`.
