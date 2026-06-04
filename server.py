"""
StreamGrab Backend — Flask + yt-dlp
Endpoints:
  POST /api/fetch   → get video info + formats
  POST /api/dl      → stream download to client
  GET  /api/ping    → health check
"""

import os, re, uuid, mimetypes
from flask import Flask, request, jsonify, Response, stream_with_context, send_from_directory

from flask_cors import CORS
import yt_dlp

app = Flask(__name__, static_folder="static")
CORS(app)

# ── Serve frontend ──
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)

# ── Cookies file (optional — helps with age-restricted / login-required content)
COOKIES_FILE = os.environ.get("COOKIES_FILE", None)  # set path if needed

def base_ydl_opts(extra={}):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    opts.update(extra)
    return opts


# ──────────────────────────────────────────────
# 1. FETCH INFO
# ──────────────────────────────────────────────
@app.route("/api/fetch", methods=["POST"])
def fetch_info():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        with yt_dlp.YoutubeDL(base_ydl_opts({"skip_download": True})) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500

    # Build format list
    formats = []
    seen = set()
    raw_fmts = info.get("formats") or []

    for f in raw_fmts:
        ext   = f.get("ext", "mp4")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")
        abr    = f.get("abr")
        fid    = f.get("format_id", "")

        # Video+audio formats
        if vcodec != "none" and acodec != "none" and height:
            label = f"{height}p ({ext.upper()})"
            if label not in seen:
                seen.add(label)
                formats.append({
                    "id":    fid,
                    "label": label,
                    "type":  "video",
                    "height": height,
                    "ext":   ext,
                    "size":  f.get("filesize") or f.get("filesize_approx"),
                })

    # Video-only + best audio merge options (yt-dlp handles merge)
    for f in raw_fmts:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")
        ext    = f.get("ext", "mp4")
        fid    = f.get("format_id", "")

        if vcodec != "none" and acodec == "none" and height:
            label = f"{height}p HD ({ext.upper()}) + audio"
            if label not in seen:
                seen.add(label)
                formats.append({
                    "id":    f"{fid}+bestaudio",
                    "label": label,
                    "type":  "video",
                    "height": height,
                    "ext":   "mp4",
                    "size":  f.get("filesize") or f.get("filesize_approx"),
                })

    # Audio-only
    for f in raw_fmts:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        abr    = f.get("abr")
        ext    = f.get("ext", "m4a")
        fid    = f.get("format_id", "")

        if vcodec == "none" and acodec != "none" and abr:
            label = f"Audio {int(abr)}kbps ({ext.upper()})"
            if label not in seen:
                seen.add(label)
                formats.append({
                    "id":    fid,
                    "label": label,
                    "type":  "audio",
                    "ext":   ext,
                    "size":  f.get("filesize") or f.get("filesize_approx"),
                })

    # MP3 audio-only option (always add)
    if not any(f["label"].startswith("MP3") for f in formats):
        formats.append({
            "id":    "bestaudio/best",
            "label": "MP3 Audio (best quality)",
            "type":  "audio",
            "ext":   "mp3",
            "size":  None,
        })

    # Sort: best video first, then audio
    formats.sort(key=lambda x: (0 if x["type"]=="video" else 1, -(x.get("height") or 0)))

    return jsonify({
        "title":      info.get("title", "Untitled"),
        "thumbnail":  info.get("thumbnail"),
        "duration":   info.get("duration"),          # seconds
        "uploader":   info.get("uploader"),
        "platform":   info.get("extractor_key"),
        "webpage_url":info.get("webpage_url", url),
        "formats":    formats[:20],                  # max 20 options
    })


# ──────────────────────────────────────────────
# 2. DOWNLOAD (stream to browser)
# ──────────────────────────────────────────────
@app.route("/api/dl", methods=["POST"])
def download():
    data      = request.get_json(silent=True) or {}
    url       = (data.get("url") or "").strip()
    fmt_id    = (data.get("format_id") or "bestvideo+bestaudio/best").strip()
    to_mp3    = data.get("to_mp3", False)

    if not url:
        return jsonify({"error": "No URL"}), 400

    # Temp file path
    tmp_name = f"/tmp/sg_{uuid.uuid4().hex}"

    ydl_opts = base_ydl_opts({
        "outtmpl": tmp_name + ".%(ext)s",
        "format":  fmt_id,
    })

    if to_mp3:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Find actual output file
            filepath = ydl.prepare_filename(info)
            if to_mp3:
                filepath = re.sub(r"\.\w+$", ".mp3", filepath)
            # Fallback glob
            if not os.path.exists(filepath):
                import glob
                matches = glob.glob(tmp_name + ".*")
                if not matches:
                    return jsonify({"error": "Download produced no file"}), 500
                filepath = matches[0]
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    ext      = os.path.splitext(filepath)[1].lstrip(".")
    mime     = mimetypes.guess_type(f"x.{ext}")[0] or "application/octet-stream"
    title    = info.get("title", "download")
    # Sanitize filename
    safe_title = re.sub(r'[^\w\-. ]', '_', title)[:80]
    filename = f"{safe_title}.{ext}"

    def generate():
        try:
            with open(filepath, "rb") as fh:
                while chunk := fh.read(1024 * 512):  # 512 KB chunks
                    yield chunk
        finally:
            try: os.remove(filepath)
            except: pass

    file_size = os.path.getsize(filepath)

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type":        mime,
        "Content-Length":      str(file_size),
        "X-Filename":          filename,
    }

    return Response(
        stream_with_context(generate()),
        headers=headers,
        direct_passthrough=True,
    )


# ──────────────────────────────────────────────
# 3. HEALTH CHECK
# ──────────────────────────────────────────────
@app.route("/api/ping")
def ping():
    return jsonify({"status": "ok", "engine": "yt-dlp"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
