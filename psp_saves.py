#!/usr/bin/env python3
"""
psp_saves.py - import PPSSPP save folders into RomM so webstation (emulator streaming) can restore them.

Unofficial community helper, MIT licensed. Python 3.8+, standard library only.

All settings live in a .env file (see .env.example); command-line options override them.

  python3 psp_saves.py                  # dry run: which save belongs to which RomM game
  python3 psp_saves.py --zip            # also build the archives into OUT_DIR
  python3 psp_saves.py --upload         # build and upload them to RomM
  python3 psp_saves.py --upload --only ULUS10041   # just one game (repeatable)

Archive format = what webstation's PPSSPP writes itself (verified with RomM 5.3.1):
  "<ROM> [ppsspp <date time>].saves.zip" containing SAVEDATA/<FOLDER>/<files> + .broker-manifest.json,
  uploaded with emulator=ppsspp and no slot.
"""

import argparse, json, os, re, struct, time, urllib.parse, urllib.request, uuid, zipfile, zlib
from collections import defaultdict


def read_sfo(path):
    """Parse a PSF/SFO file into {key: value}."""
    with open(path, "rb") as f:
        return parse_sfo(f.read())


def parse_sfo(data):
    if data[:4] != b"\0PSF":
        return {}
    key_start, data_start, count = struct.unpack_from("<III", data, 8)
    out = {}
    for i in range(count):
        k_off, fmt, length, _max, d_off = struct.unpack_from("<HHIII", data, 20 + i * 16)
        k_end = data.index(b"\0", key_start + k_off)
        key = data[key_start + k_off:k_end].decode("ascii", "replace")
        raw = data[data_start + d_off:data_start + d_off + length]
        if fmt == 0x0404:                      # int32
            out[key] = struct.unpack("<I", raw[:4])[0]
        else:                                  # utf-8 string / binary
            out[key] = raw.split(b"\0")[0].decode("utf-8", "replace")
    return out


def title_key(name):
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name).lower().replace("&", " and ")
    t = t.replace("\u2122", "").replace("\u00ae", "")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


REGION_OF_CODE = {"U": "USA", "E": "EUR", "J": "JPN", "A": "ASIA", "K": "KOR", "H": "ASIA"}
REGION_OF_TAG = {"usa": "USA", "canada": "USA", "europe": "EUR", "uk": "EUR", "germany": "EUR", "france": "EUR",
                 "spain": "EUR", "italy": "EUR", "australia": "EUR", "japan": "JPN", "asia": "ASIA", "korea": "KOR"}
SYSTEM_PREFIXES = ("PPCD", "NPIA", "SYSDIR")    # PSN/system data, not games


def serial_region(serial):
    return REGION_OF_CODE.get(serial[2:3].upper(), "") if re.match(r"^[A-Z]{4}\d{5}", serial) else ""


def rom_region(stem, serial=""):
    """(region, serial) of a ROM: serial read from the file, else a [ULUS10041] tag, else region words."""
    m = re.search(r"\[([A-Z]{4}\d{5})\]", stem)
    serial = serial or (m.group(1) if m else "")
    if serial:
        return serial_region(serial), serial
    for tag in re.findall(r"\(([^)]*)\)", stem):
        for part in tag.split(","):
            r = REGION_OF_TAG.get(part.strip().lower())
            if r:
                return r, ""
    return "", ""


# --- serial from the ROM itself (ROMS_DIR only) ---------------------------------------------------
SECTOR = 2048
READ_ERRORS = (OSError, ValueError, IndexError, KeyError, struct.error, zlib.error)


def norm_serial(s):
    """'ULUS-10041|7B6A...|0001|G' or 'ULUS10041' -> 'ULUS10041', anything else -> None."""
    s = (s or "").split("|")[0].replace("-", "").strip().upper()
    return s if re.match(r"^[A-Z]{4}\d{5}$", s) else None


def iso_serial(read):
    """Serial of an ISO9660 image, given read(offset, size): root UMD_DATA.BIN, else PSP_GAME/PARAM.SFO."""
    pvd = read(16 * SECTOR, SECTOR)
    if pvd[1:6] != b"CD001":
        return None

    def extent(rec, limit):
        lba, size = struct.unpack_from("<I4xI", rec, 2)     # little-endian halves of the both-endian fields
        return read(lba * SECTOR, min(size, limit))

    def lookup(dir_rec, name):
        data, pos = extent(dir_rec, 1 << 20), 0
        while pos < len(data):
            n = data[pos]
            if n == 0:                         # records never cross a sector; rest of this one is padding
                pos = (pos // SECTOR + 1) * SECTOR
                continue
            ident = data[pos + 33:pos + 33 + data[pos + 32]].decode("ascii", "replace")
            if ident.split(";")[0].rstrip(".").upper() == name:
                return data[pos:pos + n]
            pos += n
        return None

    root = pvd[156:190]
    rec = lookup(root, "UMD_DATA.BIN")
    serial = norm_serial(extent(rec, 4096).decode("ascii", "replace")) if rec else None
    if not serial:
        game = lookup(root, "PSP_GAME")
        rec = game and lookup(game, "PARAM.SFO")
        serial = norm_serial(str(parse_sfo(extent(rec, 1 << 16)).get("DISC_ID", ""))) if rec else None
    return serial


def cso_reader(f):
    """read(offset, size) over a CSO v1/v2 image, decompressing only the blocks touched; None for ZSO/LZ4."""
    magic, _hsize, total, bsize, ver, align = struct.unpack("<4sIQIBB", f.read(22))
    if magic != b"CISO" or ver > 2 or not bsize:
        return None

    def block(i):
        f.seek(24 + 4 * i)
        a, b = struct.unpack("<II", f.read(8))
        start, end = (a & 0x7FFFFFFF) << align, (b & 0x7FFFFFFF) << align
        f.seek(start)
        raw = f.read(end - start)
        flag = a & 0x80000000
        if ver < 2 and flag or ver == 2 and len(raw) >= bsize:
            return raw[:bsize]                 # stored uncompressed (v1: high bit, v2: full-size block)
        if flag:                               # v2: LZ4 block, not in the stdlib
            raise ValueError("LZ4 block")
        return zlib.decompressobj(-15).decompress(raw)[:bsize]

    def read(offset, size):
        out, end = b"", min(offset + size, total)
        while offset < end:
            i = offset // bsize
            out += block(i)[offset - i * bsize:end - i * bsize]
            offset = (i + 1) * bsize
        return out
    return read


def pbp_serial(f):
    """Serial of a PSN EBOOT.PBP: the header's first offset points to an embedded PARAM.SFO."""
    hdr = f.read(40)
    if hdr[:4] != b"\0PBP":
        return None
    sfo_start, sfo_end = struct.unpack_from("<II", hdr, 8)
    f.seek(sfo_start)
    return norm_serial(str(parse_sfo(f.read(min(max(sfo_end - sfo_start, 0), 1 << 16))).get("DISC_ID", "")))


def rom_serial_source(path):
    """The file to read a serial from: the ROM itself (.iso/.cso/.pbp) or a game folder's EBOOT.PBP."""
    if os.path.isdir(path):
        eboot = next((n for n in os.listdir(path) if n.upper() == "EBOOT.PBP"), None)
        return os.path.join(path, eboot) if eboot else None
    return path if path.lower().endswith((".iso", ".cso", ".pbp")) else None


def read_rom_serial(path):
    """Serial stored inside a ROM file, or None if the format is unknown or the file unreadable."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            f.seek(0)
            if magic == b"\0PBP":
                return pbp_serial(f)
            if magic == b"CISO":
                read = cso_reader(f)
                return iso_serial(read) if read else None

            def read(offset, size):
                f.seek(offset)
                return f.read(size)
            return iso_serial(read)
    except READ_ERRORS:
        return None


def rom_serials(roms_dir, names, cache_path):
    """ROM stem -> serial for every readable ROM in roms_dir; cached by file name, size and mtime."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    new, out = {}, {}
    for stem, fn in names:
        src = rom_serial_source(os.path.join(roms_dir, fn))
        if not src:
            continue
        try:
            st = os.stat(src)
        except OSError:
            continue
        hit = cache.get(fn)
        if isinstance(hit, dict) and hit.get("size") == st.st_size and hit.get("mtime") == st.st_mtime:
            serial = hit.get("serial")
        else:
            serial = read_rom_serial(src)
        new[fn] = {"size": st.st_size, "mtime": st.st_mtime, "serial": serial}
        if serial:
            out.setdefault(stem, serial)
    if new != cache:
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(new, f, indent=1, sort_keys=True)
        except OSError as e:
            print(f"! could not write serial cache {cache_path}: {e}")
    return out


def api(path, method="GET", data=None, headers=None):
    url, token = os.environ.get("ROMM_URL", "").rstrip("/"), os.environ.get("ROMM_TOKEN", "")
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


def romm_psp_roms():
    """fs_name -> (id, display name) for the psp platform, or {} without API access."""
    if not (os.environ.get("ROMM_URL") and os.environ.get("ROMM_TOKEN")):
        return {}
    pid = next((p["id"] for p in api("/api/platforms")
                if "psp" in ((p.get("fs_slug") or "").lower(), (p.get("slug") or "").lower())), None)
    if pid is None:
        print("! no PSP platform found in RomM")
    out, off = {}, 0
    while pid is not None:
        page = api(f"/api/roms?platform_ids={pid}&limit=500&offset={off}")
        items = page.get("items", page) if isinstance(page, dict) else page
        for r in items:
            out[r.get("fs_name")] = (r["id"], r.get("name") or r.get("fs_name"), r.get("fs_name_no_ext"))
        if len(items) < 500:
            break
        off += 500
    return out


def load_env_file(path):
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes. Real env vars win."""
    if not path or not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            else:
                val = val.split(" #", 1)[0].strip()
            os.environ.setdefault(key, val)
    return True


def env_bool(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file")
    known, _ = pre.parse_known_args()
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [known.env_file] if known.env_file else [os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env")]
    for candidate in candidates:
        if load_env_file(candidate):
            break
    else:
        if known.env_file:
            raise SystemExit(f"env file not found: {known.env_file}")
    E = os.environ.get

    ap = argparse.ArgumentParser(description="Import PPSSPP saves into RomM for webstation. "
                                             "Settings come from .env; options below override them.")
    ap.add_argument("--env-file", help="settings file (default: ./.env, then .env next to the script)")
    ap.add_argument("savedata", nargs="?", default=E("SAVEDATA_DIR"), help="SAVEDATA folder [SAVEDATA_DIR]")
    ap.add_argument("roms", nargs="?", default=E("ROMS_DIR") or None,
                    help="optional local roms/psp folder instead of RomM's game list [ROMS_DIR]")
    ap.add_argument("--zip", nargs="?", const=E("OUT_DIR") or "./psp-saves", default=None, metavar="OUT_DIR",
                    help="build the archives [OUT_DIR, default ./psp-saves]")
    ap.add_argument("--layout", choices=["savedata", "flat"], default=E("LAYOUT") or "savedata")
    ap.add_argument("--library-path", default=E("LIBRARY_PATH") or "/romm/library",
                    help="library_path of the webstation container in config.yml [LIBRARY_PATH]")
    ap.add_argument("--upload", action="store_true", default=env_bool("UPLOAD"),
                    help="upload the archives to RomM, implies --zip [UPLOAD]")
    ap.add_argument("--force", action="store_true", default=env_bool("FORCE"),
                    help="upload even if a ppsspp save already exists [FORCE]")
    ap.add_argument("--map", action="append",
                    default=[m.strip() for m in E("SERIAL_MAP", "").split(";") if m.strip()],
                    metavar="SERIAL=ROM", help="manual match, repeatable [SERIAL_MAP, ';'-separated]")
    ap.add_argument("--only", action="append", metavar="SERIAL",
                    help="process only these games, repeatable; replaces ONLY from .env [ONLY, ';'/','-separated]")
    a = ap.parse_args()
    if not a.savedata:
        raise SystemExit("set SAVEDATA_DIR in .env or pass the SAVEDATA folder as first argument")
    if not os.path.isdir(a.savedata):
        raise SystemExit(f"SAVEDATA folder not found: {a.savedata}")
    if a.roms and not os.path.isdir(a.roms):
        raise SystemExit(f"ROMS_DIR not found: {a.roms}")
    if a.upload and not a.zip:
        a.zip = E("OUT_DIR") or "./psp-saves"
    if a.upload and not (E("ROMM_URL") and E("ROMM_TOKEN")):
        raise SystemExit("--upload needs ROMM_URL and ROMM_TOKEN in .env")
    bad = [m for m in a.map if "=" not in m]
    if bad:
        raise SystemExit(f"invalid SERIAL_MAP entry (expected SERIAL=ROM): {bad[0]}")

    roms, rom_file = {}, {}
    romm = romm_psp_roms()
    if a.roms:
        names = [(fn.rsplit(".", 1)[0] if os.path.isfile(os.path.join(a.roms, fn)) else fn, fn)
                 for fn in sorted(os.listdir(a.roms))]
    elif romm:
        names = sorted((v[2] or k.rsplit(".", 1)[0], k) for k, v in romm.items() if k)
    else:
        raise SystemExit("give a ROMS_DIR or set ROMM_URL and ROMM_TOKEN")
    for stem, fn in names:
        roms.setdefault(title_key(stem), stem)
        rom_file.setdefault(stem, fn)
    # serials read from the ROM files themselves match exactly; RomM's game list has no file access
    file_serial = {}
    if a.roms:
        cache = os.path.join(a.zip or E("OUT_DIR") or "./psp-saves", ".rom-serials.json")
        file_serial = rom_serials(a.roms, names, cache)
        print(f"serials read from {len(file_serial)} of {len(names)} ROMs in {a.roms}\n")
    serial_rom = {}
    for stem, s in sorted(file_serial.items()):
        serial_rom.setdefault(s, stem)

    # "ULUS10041" or a full folder name like "ULUS10041DATA00" both select the game ULUS10041
    only_list = a.only if a.only is not None else re.split(r"[;,\s]+", E("ONLY", ""))
    only = {x.strip().upper()[:9] for x in only_list if x.strip()}
    games = defaultdict(lambda: {"folders": [], "title": ""})
    for d in sorted(os.listdir(a.savedata)):
        p = os.path.join(a.savedata, d)
        if not os.path.isdir(p) or d.startswith(".") or d.upper().startswith(SYSTEM_PREFIXES):
            continue
        serial = d[:9].upper()
        if only and serial not in only:
            continue
        sfo = read_sfo(os.path.join(p, "PARAM.SFO")) if os.path.isfile(os.path.join(p, "PARAM.SFO")) else {}
        games[serial]["folders"].append(d)
        games[serial]["title"] = games[serial]["title"] or sfo.get("TITLE", "")

    if only:
        missing = sorted(only - set(games))
        if missing:
            print(f"! not found in {a.savedata}: {', '.join(missing)}")
        if not games:
            raise SystemExit("nothing to do: none of the --only serials have a save folder")

    manual = {k.strip().upper(): v.strip() for k, v in (m.split("=", 1) for m in a.map)}
    for serial, g in games.items():
        rom = manual.get(serial) or serial_rom.get(serial)
        if not rom and g["title"]:
            tk = title_key(g["title"])
            rom = roms.get(tk)
            if not rom:   # "Daxter" -> "Daxter (USA) ...", "DiRT 2" -> "Colin McRae - DiRT 2 (USA)"
                hits = {v for k, v in roms.items() if k.startswith(tk) or (len(tk) > 3 and f" {tk}" in f" {k}")}
                rom = hits.pop() if len(hits) == 1 else None
        g["rom"] = rom
    # one save per ROM: PSP saves are tied to the game's serial, so prefer the one matching the ROM's region
    by_rom = {}
    for serial, g in sorted(games.items()):
        if g["rom"]:
            by_rom.setdefault(g["rom"], []).append(serial)
    for rom, serials in by_rom.items():
        region, rom_serial = rom_region(rom, file_serial.get(rom, ""))
        serials.sort(key=lambda s: (s != rom_serial, serial_region(s) != region))
        for other in serials[1:]:
            games[other]["skip"] = f"other region than ROM (kept {serials[0]})"

    print(f"{'serial':10} {'title in save':36} {'check':9}  ROM")
    for serial, g in sorted(games.items()):
        rom = g["rom"]
        if not rom:
            check, shown = "", "-- no match: look up serial, then --map"
        elif g.get("skip"):
            check, shown = "skipped", f"{rom}  ({g['skip']})"
        else:
            region, rom_serial = rom_region(rom, file_serial.get(rom, ""))
            if rom_serial:
                check = "serial ok" if rom_serial == serial else "SERIAL!"
            elif region:
                check = "region ok" if region == serial_region(serial) else "REGION!"
            else:
                check = "region ?"
            shown = rom
        g["check"] = check
        print(f"{serial:10} {g['title'][:36]:36} {check:9}  {shown}")
    print("\nregion ?  = ROM name has no region; works only if the ROM is the same version as the save's serial"
          "\nREGION!/SERIAL! = the save belongs to a different release; the game will most likely not see it")

    if a.zip:
        os.makedirs(a.zip, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H-%M-%S")
        for serial, g in sorted(games.items()):
            if not g["rom"] or g.get("skip"):
                continue
            fname = rom_file.get(g["rom"]) if g["rom"] else None
            rid, rname, _ = romm.get(fname, (None, g["title"] or serial, None))
            if g["rom"] and romm and rid is None:
                print(f"  ! {fname}: not found in RomM, rom_id left empty")
            # exactly how webstation names its archives; RomM keys on the ".saves.zip" extension
            name = f"{(g['rom'] or serial)} [ppsspp {stamp}].saves.zip"
            path = os.path.join(a.zip, name)
            members = []
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for d in g["folders"]:
                    base = os.path.join(a.savedata, d)
                    for root, _, files in os.walk(base):
                        for f in sorted(files):
                            full = os.path.join(root, f)
                            rel = os.path.join(d, os.path.relpath(full, base)).replace(os.sep, "/")
                            arc = ("SAVEDATA/" + rel) if a.layout == "savedata" else rel
                            z.write(full, arc)
                            members.append({"path": arc, "kind": "save"})
                manifest = {"version": 1, "created_at": time.time(),
                            "session": {"emulator": "ppsspp", "core": None, "platform": "psp",
                                        "rom_id": rid, "rom": rname,
                                        "rom_file": f"{a.library_path}/roms/psp/{fname}" if fname else None,
                                        "state_slot": 1},
                            "files": members}
                z.writestr(".broker-manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            print(f"  wrote {name}" + (f"  (rom_id {rid})" if rid else ""))
            if a.upload:
                if not rid:
                    print("    skipped upload: no rom_id")
                    continue
                if not a.force:
                    d = api(f"/api/roms/{rid}")
                    if any(x.get("emulator") == "ppsspp" for x in (d.get("user_saves") or d.get("saves") or [])):
                        print("    skipped upload: game already has a ppsspp save (use --force)")
                        continue
                boundary = uuid.uuid4().hex
                body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"saveFile\"; filename=\"{name}\"\r\n"
                        f"Content-Type: application/zip\r\n\r\n").encode() + open(path, "rb").read() + \
                       f"\r\n--{boundary}--\r\n".encode()
                q = urllib.parse.urlencode({"rom_id": rid, "emulator": "ppsspp"})
                api(f"/api/saves?{q}", "POST", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
                print("    uploaded")

if __name__ == "__main__":
    main()