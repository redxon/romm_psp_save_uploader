# psp_saves.py: bring existing PPSSPP saves into RomM for webstation

PSP saves aren't single files. Each one is a folder (`SAVEDATA/ULUS10336DATA00/` with
`PARAM.SFO`, `DATA.BIN`, icons), so RomM's upload dialog can't take them and webstation
shows "No saves yet". This script packs each game's save folders into the exact archive
format webstation's PPSSPP writes itself, matches them to your RomM games, and uploads them.

Unofficial community helper, MIT licensed. Runs with Python 3.8+ (standard library only) or Docker.

## Requirements

- RomM with emulator streaming via **webstation** (tested with RomM 5.3.1) and PSP set up in
  `streaming.platforms` with `emulator: ppsspp`
- An API token: RomM → profile → API tokens (read ROMs/platforms, write assets)
- Your PPSSPP `SAVEDATA` folder (from a PC, Android, a handheld, a Syncthing folder, …)

## Setup

1. Download `psp_saves.py`, `.env.example` and `compose.yaml` into one folder.
2. Copy `.env.example` to `.env` and fill in at least:
   - `ROMM_URL`: your RomM address
   - `ROMM_TOKEN`: RomM → profile → API tokens
   - `SAVEDATA_DIR`: your PPSSPP `SAVEDATA` folder (subfolders look like `ULUS10336DATA00`)

Every setting is documented in `.env.example`. Both ways of running below read the same `.env`,
and anything given on the command line overrides it.

## Usage

**With Python** (3.8 or newer, nothing to install):

```bash
python3 psp_saves.py              # dry run: shows which save goes to which game
python3 psp_saves.py --zip        # also writes the archives to OUT_DIR
python3 psp_saves.py --upload     # writes and uploads them
```

**With Docker** (no Python needed; the official `python:3.12-slim` image runs the script):

```bash
docker compose run --rm psp-saves              # dry run
docker compose run --rm psp-saves --upload     # writes and uploads
```

Your `SAVEDATA` folder is mounted read-only. With Docker, `ROMM_URL` is resolved from inside the
container: `localhost` there is the container itself, so use the host's IP or hostname, or join
RomM's Docker network (commented example in `compose.yaml`) and use `http://romm:8080`.
On Linux, files written to `OUT_DIR` by the container belong to root.

Without Compose:

```bash
docker run --rm --env-file .env -e SAVEDATA_DIR=/savedata -e OUT_DIR=/out \
  -v "$PWD/psp_saves.py":/app/psp_saves.py:ro \
  -v "/path/to/PSP/SAVEDATA":/savedata:ro -v "$PWD/psp-saves":/out \
  python:3.12-slim python /app/psp_saves.py --upload
```

**Test with one game first:** run `python3 psp_saves.py --upload --only ULUS10336` (the serial
from the dry run), then start that game via *Stream* in RomM. The save should be offered on the
launch screen. `--only` is also the way to retry a single game later without touching the others.

## How it matches

- **Serial:** the first 9 characters of a save folder (`ULUS10336`) are the game serial. Every
  folder of one game goes into one archive.
- **Game:** identified by the `TITLE` in the save's own `PARAM.SFO` and matched against your
  RomM game list, ignoring punctuation, ™/® and region tags. A ROM named `… [ULUS10336]` is
  checked against the serial exactly.
- **Region:** a PSP game only finds saves under its **own** serial (US, EU and JP releases
  differ). The `check` column warns about this:

| check | meaning |
|---|---|
| `serial ok` / `region ok` | save and ROM are the same release |
| `region ?` | the ROM name has no region; works only if it's the same release as the save |
| `REGION!` / `SERIAL!` | the save is from another release (e.g. UMD save, PSN ROM); the game will most likely not see it |
| `skipped` | several saves for one ROM; the one matching the ROM's region is kept |

- **No match?** This happens with Japanese save titles or very different names. Look up the
  serial online, then add it to `SERIAL_MAP` in `.env` (several entries separated by `;`):
  `SERIAL_MAP=ULUS10041=Grand Theft Auto - Liberty City Stories (USA)`. The value is the ROM
  file name without extension, exactly as it appears in RomM.
- **Duplicates:** games that already have a `ppsspp` save in RomM are skipped. Use `--force` to
  upload anyway.

## Settings

| `.env` | Command line | |
|---|---|---|
| `ROMM_URL`, `ROMM_TOKEN` | – | RomM address and API token |
| `SAVEDATA_DIR` | 1st argument | PPSSPP `SAVEDATA` folder (only read) |
| `OUT_DIR` | `--zip DIR` | Where archives are written (default `./psp-saves`) |
| `ROMS_DIR` | 2nd argument | Match against a local `roms/psp` folder instead of RomM's game list |
| `SERIAL_MAP` | `--map SERIAL=ROM` | Manual matches (`;`-separated in `.env`, repeatable on the CLI) |
| `UPLOAD` | `--upload` | Upload to RomM (`emulator=ppsspp`, no slot); implies building the archives |
| `FORCE` | `--force` | Upload even if the game already has a `ppsspp` save |
| `ONLY` | `--only SERIAL` | Process only these games (`;`/`,`-separated in `.env`; repeatable on the CLI, where it replaces the `.env` value). A full folder name like `ULUS10041DATA00` works too |
| `LAYOUT` | `--layout` | `savedata` (webstation format) or `flat` (without `SAVEDATA/`) |
| `LIBRARY_PATH` | `--library-path` | `library_path` of your webstation container, written into the manifest |
| – | `--env-file FILE` | Use another settings file (default `./.env`, then `.env` next to the script) |

Environment variables that are already set win over `.env`, so CI or one-off overrides like
`FORCE=true python3 psp_saves.py --upload` work too.

## Archive format

The format was copied from a save that webstation created itself:

```
<ROM> [ppsspp <YYYY-MM-DD HH-MM-SS>].saves.zip
├── SAVEDATA/ULUS10336DATA00/PARAM.SFO
├── SAVEDATA/ULUS10336DATA00/DATA.BIN
└── .broker-manifest.json      (emulator, platform, rom_id, rom_file, file list)
```

What RomM actually cares about is the **emulator field `ppsspp`**. Uploading the same zip through
the web UI without choosing that emulator stores it with `emulator: null`, and webstation won't
offer it.

## Limitations

- **Webstation only.** RomM stores saves per emulator, and in-browser play (EmulatorJS), Argosy and
  the iOS app use their own emulator names and formats. Whether they pick these archives up is
  untested. Reports welcome.
- **Unofficial format.** This depends on webstation's archive format (manifest `version: 1`). A
  broker update could change it.
- **Save states** (`PPSSPP_STATE`) aren't handled, only in-game saves.