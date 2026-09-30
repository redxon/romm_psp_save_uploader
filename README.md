# psp_saves.py: bring existing PPSSPP saves into RomM for webstation

PSP saves aren't single files. Each one is a folder (`SAVEDATA/ULUS10336DATA00/` with
`PARAM.SFO`, `DATA.BIN`, icons), so RomM's upload dialog can't take them and webstation
shows "No saves yet". This script packs each game's save folders into the exact archive
format webstation's PPSSPP writes itself, matches them to your RomM games, and uploads them.

Unofficial community helper, MIT licensed. Python 3.8+, standard library only, no installs.

## Requirements

- RomM with emulator streaming via **webstation** (tested with RomM 5.3.1) and PSP set up in
  `streaming.platforms` with `emulator: ppsspp`
- An API token: RomM → profile → API tokens (read ROMs/platforms, write assets)
- Your PPSSPP `SAVEDATA` folder (from a PC, Android, a handheld, a Syncthing folder, …)

## Usage

```bash
export ROMM_URL="https://romm.example.com"      # or http://<ip>:<port>
export ROMM_TOKEN="rmm_…"

# 1. Dry run: which save belongs to which game?
python3 psp_saves.py /path/to/PSP/SAVEDATA

# 2. Build the archives and upload them
python3 psp_saves.py /path/to/PSP/SAVEDATA --zip ./psp-saves --upload
```

Test with **one game** first: copy a single save folder into an empty directory and point
the script at that. Then start the game via Stream in RomM. The save should be offered on the
launch screen.

With Docker instead of a local Python (mount the save folder read-only):

```bash
docker run --rm -v "$PWD":/w -w /w -v "/path/to/PSP/SAVEDATA":/savedata:ro \
  -e ROMM_URL -e ROMM_TOKEN python:3.12-slim \
  python psp_saves.py /savedata --zip ./psp-saves --upload
```

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
  serial online, then add `--map "ULUS10041=<ROM file name without extension>"` (the exact file
  name as it appears in RomM).
- **Duplicates:** games that already have a `ppsspp` save in RomM are skipped. Use `--force` to
  upload anyway.

## Options

| Option | |
|---|---|
| `ROMS_DIR` (2nd argument) | Match against a local `roms/psp` folder instead of RomM's game list |
| `--zip DIR` | Where to write the archives (the source folder is only read) |
| `--upload` | Upload to RomM (`emulator=ppsspp`, no slot) |
| `--map SERIAL=ROM` | Manual match, can be repeated |
| `--layout flat` | Archives without the `SAVEDATA/` level (for experiments with other clients) |
| `--library-path` | `library_path` of your webstation container in `config.yml`. It's written into the archive's manifest; default `/romm/library` |
| `--force` | Upload even if a `ppsspp` save already exists |

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