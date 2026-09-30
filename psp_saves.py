#!/usr/bin/env python3
"""
psp_saves.py - import PPSSPP save folders into RomM so webstation (emulator streaming) can restore them.

Unofficial community helper, MIT licensed. Python 3.8+, standard library only.

  python3 psp_saves.py SAVEDATA_DIR                       # show which save belongs to which RomM game
  python3 psp_saves.py SAVEDATA_DIR --zip OUT             # build the archives
  python3 psp_saves.py SAVEDATA_DIR --zip OUT --upload    # ...and upload them to RomM

  ROMM_URL / ROMM_TOKEN   RomM address and an API token (needed for matching by RomM and for --upload)
  [ROMS_DIR]              optional: match against a local roms/psp folder instead of RomM's game list
  --map SERIAL=ROM        manual match, ROM = file name without extension (as in RomM)
  --library-path PATH     library_path of your webstation container in config.yml (default /romm/library)
  --force                 upload even if the game already has a ppsspp save in RomM

Archive format = what webstation's PPSSPP writes itself (verified with RomM 5.3.1):
  "<ROM> [ppsspp <date time>].saves.zip" containing SAVEDATA/<FOLDER>/<files> + .broker-manifest.json,
  uploaded with emulator=ppsspp and no slot.
"""
import argparse, json, os, re, struct, time, urllib.parse, urllib.request, uuid, zipfile
from collections import defaultdict


def read_sfo(path):
    """Parse a PSF/SFO file into {key: value}."""
    with open(path, "rb") as f:
        data = f.read()
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


def rom_region(stem):
    m = re.search(r"\[([A-Z]{4}\d{5})\]", stem)
    if m:
        return serial_region(m.group(1)), m.group(1)
    for tag in re.findall(r"\(([^)]*)\)", stem):
        for part in tag.split(","):
            r = REGION_OF_TAG.get(part.strip().lower())
            if r:
                return r, ""
    return "", ""


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("savedata")
    ap.add_argument("roms", nargs="?", help="optional local roms/psp folder (default: RomM's game list)")
    ap.add_argument("--zip", metavar="OUT_DIR")
    ap.add_argument("--layout", choices=["savedata", "flat"], default="savedata")
    ap.add_argument("--library-path", default="/romm/library",
                    help="library path as the webstation container sees it (config.yml library_path)")
    ap.add_argument("--upload", action="store_true", help="upload the zips to RomM (needs ROMM_URL/TOKEN)")
    ap.add_argument("--force", action="store_true", help="upload even if a ppsspp save already exists")
    ap.add_argument("--map", action="append", default=[], metavar="SERIAL=ROM",
                    help='manual match, e.g. --map "ULUS10041=Grand Theft Auto - Liberty City Stories (USA)"')
    a = ap.parse_args()

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

    games = defaultdict(lambda: {"folders": [], "title": ""})
    for d in sorted(os.listdir(a.savedata)):
        p = os.path.join(a.savedata, d)
        if not os.path.isdir(p) or d.startswith(".") or d.upper().startswith(SYSTEM_PREFIXES):
            continue
        serial = d[:9].upper()
        sfo = read_sfo(os.path.join(p, "PARAM.SFO")) if os.path.isfile(os.path.join(p, "PARAM.SFO")) else {}
        games[serial]["folders"].append(d)
        games[serial]["title"] = games[serial]["title"] or sfo.get("TITLE", "")

    manual = dict(m.split("=", 1) for m in a.map)
    for serial, g in games.items():
        rom = manual.get(serial)
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
        region, rom_serial = rom_region(rom)
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
            region, rom_serial = rom_region(rom)
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