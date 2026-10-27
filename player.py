"""runPython entry for the MP3 player manager.

Actions:
  list   → tracks on the player + disk usage (connected=False when unplugged)
  delete → remove one track (and its AppleDouble shadow) from the player
  info   → YouTube metadata for a URL (title, duration, thumbnail, job_id)
  start  → spawn worker.py: download → MP3 → copy onto the player
  cancel → drop a cancel flag the worker honours
  set_root → save the player folder (url = path; empty resets to default)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

APP = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(APP, ".fused", "cache", "jobs")
MP3S = os.path.join(APP, ".fused", "cache", "mp3")
ART = os.path.join(APP, ".fused", "cache", "art")
ART_VERSION = "v1"
CACHE_VERSION = "v1"
SETTINGS = os.path.join(APP, ".fused", "data", "settings.json")


def _default_root() -> str:
    """First mounted volume with a top-level "music" folder (the player)."""
    mounts = os.path.join(os.sep + "Volumes")
    try:
        for vol in sorted(os.listdir(mounts)):
            cand = os.path.join(mounts, vol, "music")
            if os.path.isdir(cand):
                return cand
    except OSError:
        pass
    return ""


def _saved_root() -> str:
    try:
        with open(SETTINGS) as f:
            return str(json.load(f).get("root") or "")
    except (OSError, ValueError):
        return ""


def _save_root(root: str) -> None:
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    if root:
        with open(SETTINGS, "w") as f:
            json.dump({"root": root}, f)
    elif os.path.exists(SETTINGS):
        os.remove(SETTINGS)


def job_id_for(video_id: str) -> str:
    h = hashlib.sha1(f"{CACHE_VERSION}:{video_id}:mp3".encode()).hexdigest()[:12]
    return f"yt-mp3-{h}"


def safe_name(title: str) -> str:
    t = re.sub(r'[\\/:*?"<>|%\x00-\x1f]+', " ", title).strip()
    return (t or "audio")[:120]


def _syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _apic(path: str) -> tuple[bytes, str] | None:
    """Return (image bytes, mime) from the first ID3v2 APIC frame, or None."""
    with open(path, "rb") as f:
        head = f.read(10)
        if len(head) < 10 or head[:3] != b"ID3":
            return None
        ver = head[3]
        size = _syncsafe(head[6:10])
        tag = f.read(size)
    if head[5] & 0x40:  # extended header
        if ver == 4:
            tag = tag[_syncsafe(tag[:4]):]
        else:
            tag = tag[4 + int.from_bytes(tag[:4], "big"):]
    pos = 0
    while pos + 10 <= len(tag):
        fid = tag[pos:pos + 4]
        if fid == b"\x00\x00\x00\x00":
            break
        fsize = _syncsafe(tag[pos + 4:pos + 8]) if ver == 4 else int.from_bytes(tag[pos + 4:pos + 8], "big")
        flags = tag[pos + 9]
        body = tag[pos + 10:pos + 10 + fsize]
        pos += 10 + fsize
        if fid != b"APIC" or not body:
            continue
        if ver == 4 and flags & 0x01:  # data length indicator
            body = body[4:]
        if ver == 4 and flags & 0x02:  # unsynchronised frame
            body = body.replace(b"\xff\x00", b"\xff")
        enc = body[0]
        end = body.index(b"\x00", 1)
        mime = body[1:end].decode("latin-1") or "image/jpeg"
        rest = body[end + 2:]  # skip picture type byte
        if enc in (1, 2):  # UTF-16 description, terminated by 00 00
            i = 0
            while i + 1 < len(rest) and rest[i:i + 2] != b"\x00\x00":
                i += 2
            rest = rest[i + 2:]
        else:
            rest = rest[rest.index(b"\x00") + 1:]
        if rest[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif rest[:4] == b"\x89PNG":
            mime = "image/png"
        return rest, mime
    return None


def _art_for(path: str, st: os.stat_result) -> str | None:
    """Cover art extracted to .fused/cache/art; returns app-relative path or None."""
    key = hashlib.sha1(f"{ART_VERSION}:{path}:{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()[:16]
    for ext in ("jpg", "png"):
        p = os.path.join(ART, f"{key}.{ext}")
        if os.path.exists(p):
            return os.path.relpath(p, APP)
    miss = os.path.join(ART, f"{key}.none")
    if os.path.exists(miss):
        return None
    os.makedirs(ART, exist_ok=True)
    try:
        got = _apic(path)
    except Exception:
        got = None
    if not got:
        open(miss, "wb").close()
        return None
    data, mime = got
    ext = "png" if "png" in mime else "jpg"
    out = os.path.join(ART, f"{key}.{ext}")
    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, out)
    return os.path.relpath(out, APP)


def _connected(root: str) -> bool:
    return os.path.isdir(root)


def _list(root: str) -> dict:
    out = {"root": root, "connected": _connected(root), "tracks": [], "usage": None}
    if not out["connected"]:
        return out
    try:
        u = shutil.disk_usage(root)
        out["usage"] = {"total": u.total, "used": u.used, "free": u.free}
    except OSError:
        pass
    for name in sorted(os.listdir(root), key=str.lower):
        if name.startswith(".") or not name.lower().endswith(".mp3"):
            continue
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        m = re.search(r"\[([A-Za-z0-9_-]{11})\]\.mp3$", name, re.I)
        out["tracks"].append({
            "name": name,
            "title": name[: m.start()].rstrip() if m else name[:-4],
            "size": st.st_size,
            "mtime": st.st_mtime,
            "video_id": m.group(1) if m else None,
            "art": _art_for(p, st),
        })
    return out


def _safe_target(root: str, name: str) -> str:
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("Bad track name.")
    real_root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, name))
    if not target.startswith(real_root + os.sep):
        raise ValueError("Refusing to touch a path outside the music folder.")
    return target


def _on_device(root: str, vid: str) -> str | None:
    if not vid or not _connected(root):
        return None
    for name in os.listdir(root):
        if not name.startswith(".") and name.lower().endswith(".mp3") and f"[{vid}]" in name:
            return name
    return None


def main(action: str = "list", url: str = "", name: str = "", root: str = "") -> dict:
    if action == "set_root":
        # empty value resets to the default
        _save_root((url or "").strip().rstrip("/"))
        action = "list"
        root = ""
    root = (root or _saved_root() or _default_root()).rstrip("/")
    url = (url or "").strip()

    if action == "list":
        return _list(root)

    if action == "delete":
        if not _connected(root):
            raise ValueError("Player is not connected.")
        target = _safe_target(root, name)
        if not os.path.isfile(target):
            raise ValueError(f"No such track: {name}")
        os.remove(target)
        shadow = os.path.join(os.path.dirname(target), "._" + os.path.basename(target))
        try:
            os.remove(shadow)
        except FileNotFoundError:
            pass
        return {"deleted": name, **_list(root)}

    if not url:
        raise ValueError("Paste a YouTube URL first.")

    if action == "info":
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "skip_download": True, "extract_flat": False, "playlist_items": "1"}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            if not entries:
                raise ValueError("Playlist has no entries.")
            info = entries[0]
        vid = info.get("id") or hashlib.sha1(url.encode()).hexdigest()[:11]
        jid = job_id_for(vid)
        fname = f"{safe_name(info.get('title') or vid)} [{vid}].mp3"
        mp3 = os.path.join(MP3S, fname)
        return {
            "id": vid,
            "job_id": jid,
            "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url") or url,
            "filename": fname,
            "mp3_rel": os.path.relpath(mp3, APP),
            "cached": os.path.exists(mp3),
            "on_device": _on_device(root, vid),
            "connected": _connected(root),
        }

    if action == "start":
        if not _connected(root):
            raise ValueError("Player is not connected — plug it in first.")
        info = main("info", url, root=root)
        jid = info["job_id"]
        jdir = os.path.join(JOBS, jid)
        os.makedirs(jdir, exist_ok=True)
        os.makedirs(MP3S, exist_ok=True)
        status_path = os.path.join(jdir, "status.json")
        info["status_rel"] = os.path.relpath(status_path, APP)
        try:
            with open(status_path) as f:
                prev = json.load(f)
            if prev.get("state") in ("queued", "running") and time.time() - prev.get("updated", 0) < 30:
                info["reattached"] = True
                return info
        except (OSError, ValueError):
            pass
        for f in ("cancel", "status.json"):
            try:
                os.remove(os.path.join(jdir, f))
            except FileNotFoundError:
                pass
        _write_json(status_path, {"state": "queued", "done": 0, "total": None,
                                  "detail": "Starting…", "updated": time.time()})
        log = open(os.path.join(jdir, "worker.log"), "ab")
        subprocess.Popen(
            [sys.executable, os.path.join(APP, "worker.py"),
             info["webpage_url"], jid, info["title"] or info["id"],
             os.path.join(APP, info["mp3_rel"]),
             os.path.join(root, info["filename"])],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            cwd=APP, start_new_session=True, close_fds=True,
        )
        return info

    if action == "cancel":
        m = re.search(r"yt-mp3-[0-9a-f]{12}", url)
        jid = m.group(0) if m else main("info", url, root=root)["job_id"]
        jdir = os.path.join(JOBS, jid)
        os.makedirs(jdir, exist_ok=True)
        with open(os.path.join(jdir, "cancel"), "w") as f:
            f.write("1")
        return {"job_id": jid, "cancelled": True}

    raise ValueError(f"Unknown action {action!r}")


def _write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
