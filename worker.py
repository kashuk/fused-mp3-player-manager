"""Detached downloader: bestaudio → MP3 (VBR best) via bundled ffmpeg, then copy onto the player.

argv: url job_id title out_mp3 dest [live]
With "live" the stream is recorded for LIVE_CLIP_SECONDS via ffmpeg instead of downloaded.
Writes .fused/cache/jobs/<job_id>/status.json atomically per tick and mirrors
progress to the shell's download manager over HTTP (best effort).
"""
import json
import os
import shutil
import sys
import time
import urllib.request

APP = os.path.dirname(os.path.abspath(__file__))
url, job_id, title, out_mp3, dest = sys.argv[1:6]
is_live = len(sys.argv) > 6 and sys.argv[6] == "live"
LIVE_CLIP_SECONDS = int(os.environ.get("MP3_LIVE_CLIP_SECONDS") or 30 * 60)
jdir = os.path.join(APP, ".fused", "cache", "jobs", job_id)
status_path = os.path.join(jdir, "status.json")
cancel_path = os.path.join(jdir, "cancel")
origin = os.environ.get("FUSED_RENDER_ORIGIN")


try:
    from yt_dlp.utils import DownloadCancelled as _Base
except Exception:  # pragma: no cover
    _Base = Exception


class Cancelled(_Base):
    pass


_last_post = 0.0
cancel_requested = False


def write_status(state, done=0, total=None, detail="", **extra):
    global _last_post, cancel_requested
    obj = {"state": state, "done": done, "total": total, "detail": detail,
           "updated": time.time(), **extra}
    tmp = status_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, status_path)
    terminal = state in ("done", "failed", "cancelled")
    if origin and (terminal or time.time() - _last_post > 1.0):
        _last_post = time.time()
        try:
            body = json.dumps({"id": job_id, "title": f"MP3: {title}", "kind": "download",
                               "state": state, "done": done, "total": total,
                               "detail": detail}).encode()
            req = urllib.request.Request(f"{origin}/api/jobs", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "X-Fused": "1"})
            with urllib.request.urlopen(req, timeout=3) as r:
                reply = json.loads(r.read() or b"{}")
                if reply.get("cancel_requested"):
                    cancel_requested = True
        except Exception:
            pass


def check_cancel():
    if cancel_requested or os.path.exists(cancel_path):
        raise Cancelled()


def on_progress(d):
    check_cancel()
    st = d.get("status")
    if st == "downloading":
        write_status("running", d.get("downloaded_bytes") or 0,
                     d.get("total_bytes") or d.get("total_bytes_estimate"),
                     "Downloading audio…", phase="download")
    elif st == "finished":
        write_status("running", d.get("downloaded_bytes") or 0, d.get("total_bytes"),
                     "Converting to MP3…", phase="convert")


def on_pp(d):
    check_cancel()
    if d.get("postprocessor") == "ExtractAudio" and d.get("status") == "started":
        write_status("running", 0, None, "Converting to MP3…", phase="convert")


def _clock(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def record_live(ffmpeg):
    """Record LIVE_CLIP_SECONDS of a live stream straight into out_mp3."""
    import subprocess
    import yt_dlp

    write_status("running", 0, None, "Resolving stream…", phase="resolve")
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                           "format": "bestaudio/best"}) as y:
        info = y.extract_info(url, download=False)
        cover = None
        if info.get("thumbnail"):
            # yt-dlp carries its own CA bundle; the venv's urllib may not.
            try:
                cover = os.path.splitext(out_mp3)[0] + ".jpg"
                with open(cover, "wb") as f:
                    f.write(y.urlopen(info["thumbnail"]).read())
            except Exception:
                cover = None
    stream = info.get("url")
    if not stream:
        raise RuntimeError("Could not find an audio stream for this live video.")
    headers = "".join(f"{k}: {v}\r\n" for k, v in (info.get("http_headers") or {}).items())
    check_cancel()
    tmp = out_mp3 + ".part.mp3"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-i", stream]
    if cover:
        cmd += ["-i", cover]
    cmd += ["-t", str(LIVE_CLIP_SECONDS), "-map", "0:a:0", "-codec:a", "libmp3lame", "-q:a", "0",
            "-metadata", f"title={title}", "-metadata", "artist=" + (info.get("uploader") or info.get("channel") or ""),
            "-id3v2_version", "3"]
    if cover:
        cmd += ["-map", "1:v:0", "-codec:v", "mjpeg", "-disposition:v", "attached_pic",
                "-metadata:s:v", "title=Album cover"]
    cmd += ["-progress", "pipe:1", "-f", "mp3", tmp]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    total = LIVE_CLIP_SECONDS
    try:
        for line in proc.stdout:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    done = min(int(line.split("=", 1)[1]) / 1_000_000, total)
                except ValueError:
                    continue
                write_status("running", done, total,
                             f"Recording {_clock(done)} of {_clock(total)}…", phase="record")
            try:
                check_cancel()
            except Cancelled:
                proc.kill()
                proc.wait()
                raise
    finally:
        err = proc.stderr.read() if proc.stderr else ""
        proc.wait()
    if proc.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError("ffmpeg failed: " + (err.strip().splitlines() or ["unknown error"])[-1])
    os.replace(tmp, out_mp3)
    if cover:
        try:
            os.remove(cover)
        except OSError:
            pass


def main():
    import imageio_ffmpeg
    import yt_dlp

    os.makedirs(os.path.dirname(out_mp3), exist_ok=True)
    base, _ = os.path.splitext(out_mp3)
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "outtmpl": base + ".%(ext)s",
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            # YouTube thumbnails arrive as webp; ID3 wants jpg. Embed as cover art.
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ],
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_pp],
        "overwrites": True,
    }
    if is_live and not os.path.exists(out_mp3):
        record_live(opts["ffmpeg_location"])
    elif not os.path.exists(out_mp3):
        write_status("running", 0, None, "Resolving stream…", phase="resolve")
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
        if not os.path.exists(out_mp3):
            raise RuntimeError("yt-dlp finished but MP3 not found at expected path.")
    check_cancel()
    size = os.path.getsize(out_mp3)
    rel = os.path.relpath(out_mp3, APP)
    if not dest:
        # no player connected: keep the MP3 in cache so it can be played here and copied later
        write_status("done", size, size, "Ready to play", mp3_rel=rel, size=size, copied=False)
        return
    write_status("running", 0, size, "Copying to player…", phase="copy")
    if not os.path.isdir(os.path.dirname(dest)):
        raise RuntimeError("Player is not connected — MP3 kept in cache, plug in and retry.")
    tmp = dest + ".part"
    shutil.copyfile(out_mp3, tmp)
    os.replace(tmp, dest)
    write_status("done", size, size, "On player", mp3_rel=rel,
                 size=size, filename=os.path.basename(dest), copied=True)


if __name__ == "__main__":
    try:
        main()
    except Cancelled:
        stem = os.path.splitext(os.path.basename(out_mp3))[0]
        for p in os.listdir(os.path.dirname(out_mp3)):
            if p.startswith(stem) and (p.endswith(".part") or p.endswith(".ytdl") or p.endswith(".part.mp3")):
                try:
                    os.remove(os.path.join(os.path.dirname(out_mp3), p))
                except OSError:
                    pass
        write_status("cancelled", 0, None, "Cancelled")
    except Exception as e:  # surface yt-dlp's message verbatim
        msg = str(e)
        write_status("failed", 0, None, msg[:600], error=msg[:2000])
        sys.exit(1)
