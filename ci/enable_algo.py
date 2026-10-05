#!/usr/bin/env python3
"""Switch on 'Allow algorithmic trading' in every MT5 data folder inside the Wine prefix."""
import glob, os, re, sys

BASE = os.path.expanduser("~/.wine-mt5/drive_c")
dirs = [d for d in glob.glob(BASE + "/users/*/AppData/Roaming/MetaQuotes/Terminal/*") if re.fullmatch(r"[0-9A-Fa-f]{32}", os.path.basename(d))]
dirs.append(BASE + "/Program Files/MetaTrader 5")          # portable-style folder, harmless if unused
WANT = {"AllowLiveTrading": "1", "Enabled": "1"}


def read(path):
    raw = open(path, "rb").read()
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le"), "utf16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf8bom"
    return raw.decode("utf-8", errors="ignore"), "utf8"


def write(path, text, enc):
    data = text.encode("utf-16-le") if enc == "utf16" else text.encode("utf-8")
    if enc == "utf16":
        data = b"\xff\xfe" + data
    elif enc == "utf8bom":
        data = b"\xef\xbb\xbf" + data
    open(path, "wb").write(data)


def patch(text):
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    out, i, found = [], 0, False
    while i < len(lines):
        out.append(lines[i])
        if lines[i].strip().lower() == "[experts]":
            found = True
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("["):
                block.append(lines[i]); i += 1
            have = set()
            for b in block:
                k = b.split("=", 1)[0].strip()
                if k in WANT:
                    out.append(f"{k}={WANT[k]}"); have.add(k)
                else:
                    out.append(b)
            for k, v in WANT.items():
                if k not in have:
                    out.append(f"{k}={v}")
            continue
        i += 1
    if not found:
        out += ["", "[Experts]"] + [f"{k}={v}" for k, v in WANT.items()]
    return nl.join(out) + nl


done = 0
for d in dirs:
    if not os.path.isdir(d):
        continue
    cfg = os.path.join(d, "config")
    os.makedirs(cfg, exist_ok=True)
    path = os.path.join(cfg, "common.ini")
    if os.path.exists(path):
        text, enc = read(path)
    else:
        text, enc = "", "utf16"
    write(path, patch(text), enc)
    done += 1
    print("patched", path, "|", enc)
if not done:
    print("WARNING: no MT5 data folder found")
    sys.exit(0)
