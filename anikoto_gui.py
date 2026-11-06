"""
Anikoto Downloader — Professional IDM-Style Edition
High-performance desktop anime downloader with advanced queueing,
stream decryption, real-time speed/ETA metrics, and subtitle embedding.

Built on debugged pipeline:
  watch page -> episode list -> server list -> crypto extraction -> yt-dlp -> ffmpeg remux
"""

import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
import urllib.parse
from urllib.parse import urlparse, parse_qs, urlsplit

# Try Windows DPI awareness to make UI crisp on high-DPI displays
try:
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import io
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk, ImageDraw, ImageFilter, ImageEnhance, ImageOps, ImageChops
except ImportError:
    print("Please install: pip install pillow")
    raise

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Please install: pip install requests beautifulsoup4")
    raise

try:
    import yt_dlp
except ImportError:
    print("Please install: pip install yt-dlp")
    raise

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:
    try:
        import importlib
        _c = importlib.import_module("Cryptodome.Cipher")
        _p = importlib.import_module("Cryptodome.Util.Padding")
        AES = _c.AES
        unpad = _p.unpad
    except ImportError:
        print("Please install: pip install pycryptodome")
        raise


# ------------------------------------------------------------- Settings Paths ---
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")


# ------------------------------------------------------------- Helpers ---
def sanitize_filename(name):
    """Remove illegal Windows characters and trim trailing dots/spaces."""
    clean = re.sub(r'[<>:"/\\|?*]', '', name).strip()
    clean = clean.rstrip('. ')
    return clean or "Episode"


def format_bytes(size):
    """Format bytes into readable string (KB, MB, GB)."""
    if not size or size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    s = float(size)
    while s >= 1024 and i < len(units) - 1:
        s /= 1024
        i += 1
    return f"{s:.1f} {units[i]}"


def get_disk_free(path):
    """Return free disk space on the given path as a formatted string."""
    try:
        target = path if os.path.exists(path) else os.path.dirname(os.path.abspath(path))
        usage = shutil.disk_usage(target)
        return format_bytes(usage.free)
    except Exception:
        return "Unknown"


def find_episode_file(folder, ep_num, title=None):
    """Find the downloaded video file (.mp4, .mkv, .webm) for an episode on disk."""
    if not folder or not os.path.isdir(folder):
        return None

    # 1. Exact expected name match
    if title:
        safe_title = sanitize_filename(title)
        for ext in ('.mp4', '.mkv', '.webm'):
            candidate = os.path.join(folder, f"E{ep_num} {safe_title}{ext}")
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 50_000:
                return candidate

    # 2. Match pattern ^E0*{ep_num}(?:[ ._-].*)?\.(mp4|mkv|webm)$
    pattern = re.compile(rf"^E0*{ep_num}(?:[ ._-].*)?\.(mp4|mkv|webm)$", re.IGNORECASE)
    try:
        for fname in os.listdir(folder):
            if pattern.match(fname):
                candidate = os.path.join(folder, fname)
                if os.path.isfile(candidate) and os.path.getsize(candidate) > 50_000:
                    return candidate
    except OSError:
        pass
    return None


# ------------------------------------------------------------- Crypto ---
def player_parameters(script):
    literal = r'("(?:\\.|[^"\\])*")'
    pattern = (
        r"\bconst\s+[\w$]+=" + literal
        + r",[\w$]+=" + literal
        + r",[\w$]+=" + literal
        + r",[\w$]+=(\d+);function\s+[\w$]+\("
    )
    for match in re.finditer(pattern, script):
        if "AES-CBC" in script[match.end():match.end() + 10000]:
            key, iv, secret = (json.loads(v) for v in match.groups()[:3])
            return key, iv, secret, int(match.group(4))
    raise ValueError("Crypto parameters not found")


def signed_url(url, secret, ttl):
    import hmac, hashlib, base64
    if "token" in parse_qs(urlsplit(url).query):
        return url
    path = re.search(r"/([a-f0-9]{32})/([a-f0-9]{32})/", urlsplit(url).path, re.I)
    if path is None:
        return url
    expires = int(time.time()) + ttl
    message = f"{expires}|{path[1].lower()}/{path[2].lower()}".encode()
    sig = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    enc = lambda v: base64.urlsafe_b64encode(v).decode().rstrip("=")
    token = enc(message) + "." + enc(sig)
    sep = "&" if urlsplit(url).query else "?"
    return url + sep + "token=" + token


def decrypt_source(encoded, key, iv):
    import base64
    key_bytes = key.encode("utf-8")[:32].ljust(32, b"\0")
    iv_bytes = iv.encode("utf-8")[:16].ljust(16, b"\0")
    encrypted = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
    return json.loads(unpad(cipher.decrypt(encrypted), AES.block_size).decode("utf-8"))


# ---------------------------------------------------------- FFmpeg Glue ---
_FFMPEG_DIR_CACHE = []

def find_ffmpeg_dir():
    """Locate a folder containing ffmpeg.exe+ffprobe.exe (bundled or on PATH) and ensure it is on PATH."""
    if _FFMPEG_DIR_CACHE:
        cached = _FFMPEG_DIR_CACHE[0]
        if cached and cached not in os.environ.get('PATH', ''):
            os.environ['PATH'] = cached + os.pathsep + os.environ.get('PATH', '')
        return cached or None

    search_dirs = []
    if hasattr(sys, '_MEIPASS'):
        search_dirs.append(sys._MEIPASS)
        search_dirs.append(os.path.join(sys._MEIPASS, 'ffmpeg'))
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        search_dirs.append(exe_dir)
        search_dirs.append(os.path.join(exe_dir, '_internal'))
        search_dirs.append(os.path.join(exe_dir, '_internal', 'ffmpeg'))
        search_dirs.append(os.path.join(exe_dir, 'ffmpeg'))
    app_dir = os.path.dirname(os.path.abspath(__file__))
    search_dirs.append(app_dir)
    search_dirs.append(os.path.join(app_dir, '_internal', 'ffmpeg'))
    search_dirs.append(os.path.join(app_dir, 'ffmpeg'))

    for base in search_dirs:
        if os.path.isdir(base):
            for dirpath, _dirs, files in os.walk(base):
                if 'ffmpeg.exe' in files and 'ffprobe.exe' in files:
                    _FFMPEG_DIR_CACHE.append(dirpath)
                    if dirpath not in os.environ.get('PATH', ''):
                        os.environ['PATH'] = dirpath + os.pathsep + os.environ.get('PATH', '')
                    return dirpath

    exe = shutil.which('ffmpeg')
    if exe:
        d = os.path.dirname(exe)
        _FFMPEG_DIR_CACHE.append(d)
        if d not in os.environ.get('PATH', ''):
            os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
        return d

    _FFMPEG_DIR_CACHE.append('')
    return None


def remux_and_embed(video_file, subs_file, ffmpeg_dir, log_hook=None, cache_dir=None):
    """Repair container + embed subtitles into ONE final mp4."""
    ffmpeg = os.path.join(ffmpeg_dir, 'ffmpeg.exe')
    if not os.path.exists(ffmpeg):
        ffmpeg = os.path.join(ffmpeg_dir, 'ffmpeg')
    if not os.path.exists(ffmpeg):
        if log_hook:
            log_hook("  FFmpeg executable not found for remuxing")
        return False

    if cache_dir and os.path.isdir(cache_dir):
        tmp = os.path.join(cache_dir, os.path.splitext(os.path.basename(video_file))[0] + " - remux.mp4")
    else:
        tmp = video_file[:-4] + " - remux.mp4"

    cmd = [ffmpeg, '-y', '-fflags', '+genpts+discardcorrupt', '-i', video_file]
    if subs_file and os.path.exists(subs_file) and os.path.getsize(subs_file) > 10:
        cmd += ['-i', subs_file,
                '-map', '0:v:0', '-map', '0:a?', '-map', '1:0',
                '-c:v', 'copy', '-c:a', 'copy',
                '-c:s', 'mov_text', '-metadata:s:s:0', 'language=eng',
                '-avoid_negative_ts', 'make_zero',
                '-max_muxing_queue_size', '4096',
                '-movflags', '+faststart', tmp]
    else:
        cmd += ['-map', '0:v:0', '-map', '0:a?',
                '-c:v', 'copy', '-c:a', 'copy',
                '-avoid_negative_ts', 'make_zero',
                '-max_muxing_queue_size', '4096',
                '-movflags', '+faststart', tmp]

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15 * 60,
                           creationflags=creationflags)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 50_000:
            os.replace(tmp, video_file)
            if subs_file and os.path.exists(subs_file):
                try:
                    os.remove(subs_file)
                except OSError:
                    pass
            # Clean paired VTT if present
            vtt = subs_file[:-4] + '.vtt' if subs_file.endswith('.srt') else None
            if vtt and os.path.exists(vtt):
                try:
                    os.remove(vtt)
                except OSError:
                    pass
            if log_hook:
                log_hook(f"  Single-file OK (subs embedded): {os.path.basename(video_file)}")
            return True
        else:
            if log_hook and r.stderr:
                err_lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
                if err_lines:
                    log_hook(f"  FFmpeg note: {err_lines[-1]}")
    except Exception as e:
        if log_hook:
            log_hook(f"  ffmpeg remux failed: {e}")

    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    return False


def cleanup_cache_dir(cache_dir):
    """Remove temporary cache directory and all its contents."""
    try:
        if cache_dir and os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)
    except Exception:
        pass


def purge_episode_cache(cache_dir, file_stem):
    """Delete cache fragments and tracks for a specific episode."""
    try:
        if cache_dir and os.path.isdir(cache_dir):
            for f in os.listdir(cache_dir):
                if f.startswith(file_stem):
                    try:
                        os.remove(os.path.join(cache_dir, f))
                    except OSError:
                        pass
    except Exception:
        pass


def cleanup_cache_if_empty(cache_dir):
    """Safely delete cache_dir only if there are no pending paused files inside."""
    try:
        if cache_dir and os.path.isdir(cache_dir):
            if not os.listdir(cache_dir):
                os.rmdir(cache_dir)
    except Exception:
        pass


def _clean_broken_partial(cache_dir, file_stem):
    """Clean broken partial fragments ONLY if there is no .ytdl index file.
    If .ytdl index exists, the resume state is valid and preserved!
    """
    try:
        if not os.path.isdir(cache_dir):
            return
        has_ytdl = any(f.startswith(file_stem) and f.endswith('.ytdl') for f in os.listdir(cache_dir))
        has_part = any(f.startswith(file_stem) and ('.part' in f or 'Frag' in f) for f in os.listdir(cache_dir))
        if has_part and not has_ytdl:
            purge_episode_cache(cache_dir, file_stem)
    except Exception:
        pass


def _download_ffmpeg(log_hook=None):
    """One-time automatic ffmpeg setup into the app folder."""
    import zipfile
    try:
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
        root = os.path.join(APP_DIR, 'ffmpeg')
        os.makedirs(root, exist_ok=True)
        dest = os.path.join(root, 'bundle.zip')
        if log_hook:
            log_hook("  ffmpeg missing — downloading bundled build (~100 MB, one-time)...")
        r = requests.get(url, stream=True, timeout=120)
        r.raise_for_status()
        total = int(r.headers.get('content-length', 0) or 0)
        done = 0
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(512 * 1024):
                f.write(chunk)
                done += len(chunk)
                if log_hook and total:
                    log_hook(f"  ffmpeg download: {done/1e6:.0f} / {total/1e6:.0f} MB")
        if log_hook:
            log_hook("  extracting ffmpeg...")
        with zipfile.ZipFile(dest) as z:
            z.extractall(root)
        try:
            os.remove(dest)
        except OSError:
            pass
        _FFMPEG_DIR_CACHE.clear()
        ok = find_ffmpeg_dir() is not None
        if log_hook:
            log_hook("  ffmpeg ready." if ok else "  ffmpeg setup failed.")
        return ok
    except Exception as e:
        if log_hook:
            log_hook(f"  ffmpeg auto-download failed: {e}")
        return False


def _vtt_to_srt(vtt_text):
    """Convert a WebVTT string to a SubRip (srt) string."""
    srt_blocks = []
    counter = 0
    # Normalize CRLF
    vtt_text = vtt_text.replace('\r\n', '\n').replace('\r', '\n')
    for block in re.split(r'\n\s*\n', vtt_text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        if lines[0].startswith(('WEBVTT', 'NOTE', 'STYLE')):
            lines = lines[1:]
            lines = [l for l in lines if l.strip()]
        if not lines:
            continue
        if '-->' not in lines[0]:
            lines = lines[1:]
        if not lines or '-->' not in lines[0]:
            continue
        timing = lines[0]
        parts = timing.split('-->')
        if len(parts) != 2:
            continue
        start = _ts(parts[0])
        end = _ts(parts[1].split()[0] if parts[1].split() else parts[1])
        if start is None or end is None:
            continue
        counter += 1
        body = '\n'.join(re.sub(r'<[^>]+>', '', l) for l in lines[1:])
        srt_blocks.append(f"{counter}\n{start} --> {end}\n{body}")
    return "\n\n".join(srt_blocks) + "\n"


def _ts(vtt_time):
    """Convert WebVTT timestamp (HH:MM:SS.mmm or MM:SS.mmm) to SRT timestamp (HH:MM:SS,mmm)."""
    m = re.search(r'(?:(\d+):)?(\d+):(\d+)[.,](\d+)', vtt_time.strip())
    if not m:
        return None
    h, mnt, sec, ms = m.groups()
    h = int(h) if h is not None else 0
    return f"{h:02d}:{int(mnt):02d}:{int(sec):02d},{(ms + '000')[:3]}"


# ------------------------------------------------------------ Scraping ---
class AnikotoScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        })

    def get_video_id(self, watch_url):
        r = self.session.get(watch_url, timeout=20)
        r.raise_for_status()
        self.last_html = r.text  # kept so the GUI can show poster / synopsis without a 2nd request
        parsed = urlparse(r.url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        search = re.search(rf'{domain}/anime/getinfo/(\d+)', r.text)
        if not search:
            # Fallback regex for relative path
            search = re.search(r'/anime/getinfo/(\d+)', r.text)
            if not search:
                raise ValueError("Could not find anime video ID on this watch page")
        return search.group(1), domain

    def parse_series_info(self, html):
        """Extract title, poster, synopsis, badges and meta rows from a watch page."""
        info = {"title": "", "poster": "", "synopsis": "", "meta": {}, "badges": []}
        if not html:
            return info
        try:
            soup = BeautifulSoup(html, "html.parser")
            t = soup.select_one("h1.d-title") or soup.select_one("h1.title") or soup.select_one("h1")
            if t:
                info["title"] = t.get_text(" ", strip=True)
            if not info["title"]:
                og = soup.find("meta", property="og:title")
                if og and og.get("content"):
                    info["title"] = re.sub(r"^Watch\s+|\s+Episode\s+\d+.*$", "", og["content"]).strip()

            img = soup.select_one("img[itemprop=image]") or soup.select_one(".poster img")
            src = (img.get("src") or img.get("data-src")) if img else ""
            if not src:
                og = soup.find("meta", property="og:image")
                src = og.get("content") if og else ""
            info["poster"] = src or ""

            syn = soup.select_one(".synopsis .content") or soup.select_one(".synopsis")
            if syn:
                info["synopsis"] = re.sub(r"\s*\[more\]\s*$", "", syn.get_text(" ", strip=True))

            for blk in soup.select("div.meta"):
                classes = blk.get("class") or []
                if "icons" in classes:
                    if info["badges"]:
                        continue
                    for i in blk.find_all("i"):
                        cls = i.get("class") or []
                        txt = i.get_text(strip=True)
                        if txt:
                            info["badges"].append(txt)
                        elif "sub" in cls:
                            info["badges"].append("SUB")
                        elif "dub" in cls:
                            info["badges"].append("DUB")
                    continue
                for row in blk.find_all("div", recursive=False):
                    span = row.find("span")
                    if not span:
                        continue
                    key = row.get_text(" ", strip=True).split(":")[0].strip()
                    val = re.sub(r"\s*,\s*", ", ", span.get_text(" ", strip=True)).strip()
                    if key and val and key not in info["meta"]:
                        info["meta"][key] = val
                if info["meta"]:
                    break
        except Exception:
            pass
        return info

    def get_episodes(self, domain, video_id):
        r = self.session.get(f"{domain}/ajax/episode/list/{video_id}?vrf=", timeout=20)
        r.raise_for_status()
        res_json = r.json()
        if not isinstance(res_json, dict) or 'result' not in res_json:
            raise ValueError("Invalid episode list response from server")

        soup = BeautifulSoup(res_json['result'], 'html.parser')
        episodes = soup.find_all('li', {'data-html': 'true'})
        if not episodes:
            episodes = soup.find_all('li')

        result = []
        for i, ep in enumerate(episodes, 1):
            a = ep.find('a')
            d_title = a.find('span', class_='d-title') if a else None
            title_text = (d_title.text.strip() if d_title and d_title.text.strip() else None)
            raw_title = title_text or ep.get("title") or (a.get("title") if a else None) or f"Episode {i}"
            data_ids = a.get("data-ids", "") if a else ""
            num_attr = a.get("data-num") if a else None
            try:
                ep_num = int(num_attr) if num_attr and num_attr.isdigit() else i
            except (ValueError, TypeError):
                ep_num = i

            result.append({
                "number": ep_num,
                "title": raw_title.strip(),
                "data_ids": data_ids,
            })
        return result

    def get_servers(self, domain, data_ids):
        r = self.session.get(f"{domain}/ajax/server/list", params={"servers": data_ids}, timeout=20)
        r.raise_for_status()
        res_json = r.json()
        if not isinstance(res_json, dict) or 'result' not in res_json:
            return []

        soup = BeautifulSoup(res_json['result'], 'html.parser')
        servers = []
        for div in soup.find_all('div', class_='type'):
            stype = div.get('data-type', '').lower()
            for li in div.find_all('li'):
                dlid = li.get('data-link-id', '')
                name = li.text.strip()
                if dlid:
                    servers.append({"type": stype, "name": name, "data_link_id": dlid})
        return servers

    def get_embed_url(self, domain, data_link_id):
        r = self.session.get(f"{domain}/ajax/server", params={"get": data_link_id}, timeout=20)
        r.raise_for_status()
        return r.json()['result']['url']

    def extract_m3u8(self, embed_url, domain, audio_type, source_filter=None):
        if not embed_url:
            return None

        if embed_url.startswith("//"):
            embed_url = "https:" + embed_url

        # Normalize Megaplay stream route to the clean videojs player endpoint.
        # This provides the clean, unobfuscated player scripts and valid crypto parameters
        # across ALL servers (HD-1, HD-2, Vidstream-1, Vidstream-2).
        if "megaplay.buzz/stream/" in embed_url:
            embed_url = embed_url.replace("megaplay.buzz/stream/", "megaplay.buzz/videojs/stream/")

        embed_parsed = urlparse(embed_url)
        embed_origin = f"{embed_parsed.scheme}://{embed_parsed.netloc}"

        main_r = self.session.get(embed_url, headers={"referer": f"{domain}/"}, timeout=20)
        soup = BeautifulSoup(main_r.text, "html.parser")

        # Discover all script tags and resolve full URLs
        script_srcs = [s.get("src") for s in soup.select("script[src]") if s.get("src")]
        candidate_scripts = [s for s in script_srcs if "e1-player" in s or "videojs" in s] or script_srcs

        key, iv, secret, ttl = None, None, None, None
        for s_url in candidate_scripts:
            full_script_url = urllib.parse.urljoin(embed_url, s_url)
            try:
                js = self.session.get(full_script_url, timeout=20).text
                if "AES-CBC" in js:
                    key, iv, secret, ttl = player_parameters(js)
                    break
            except Exception:
                continue

        if key is None or iv is None or secret is None or ttl is None:
            return None

        id_match = re.search(r' data-id="(\d+)"', main_r.text)
        if not id_match:
            return None
        vid_id = id_match.group(1)

        sources_url = f"{embed_origin}/stream/getSourcesNew"
        r = self.session.get(
            sources_url,
            params={"id": vid_id},
            headers={
                "referer": embed_url,
                "x-requested-with": "XMLHttpRequest"
            },
            timeout=20
        )
        if r.headers.get('Content-Type') and 'json' not in r.headers.get('Content-Type', '').lower():
            return None

        try:
            data = r.json()
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        tracks = data.get('tracks', []) if isinstance(data, dict) else []
        if 'enc' not in data and 'sources' in data:
            src = data['sources']
            if isinstance(src, dict):
                m3u8 = src.get('file')
                if m3u8:
                    return (signed_url(m3u8, secret, ttl), f"{embed_origin}/", tracks)
            elif isinstance(src, list) and src:
                first = src[0]
                if isinstance(first, dict) and first.get('file'):
                    return (signed_url(first['file'], secret, ttl), f"{embed_origin}/", tracks)

        if 'enc' in data:
            m3u8 = decrypt_source(data["enc"], key, iv)
            if isinstance(m3u8, list):
                m3u8 = next((s for s in m3u8 if isinstance(s, dict) and s.get("file")), None)
            if isinstance(m3u8, dict):
                m3u8 = m3u8.get("file")
            if m3u8:
                return (signed_url(m3u8, secret, ttl), f"{embed_origin}/", tracks)

        return None


# ---------------------------------------------------------- Downloader ---
class Cancelled(Exception):
    """Raised when user cancels the operation."""


class Downloader:
    def __init__(self):
        self.scraper = AnikotoScraper()
        self._cancelled = False
        self.was_cancelled = False
        self.current_cache_dir = None

    def cancel(self):
        self._cancelled = True
        self.was_cancelled = True
        # NOTE: Do NOT wipe cache on pause/cancel!
        # Partial fragments and .ytdl index remain safely in .anikoto_cache
        # so you can resume right where you left off!

    def download(self, url, save_path, audio_type, source_filter, quality, ep_range,
                 progress_hook=None, log_hook=None, ep_hook=None, options=None):
        self._cancelled = False
        self.was_cancelled = False
        if find_ffmpeg_dir() is None:
            _download_ffmpeg(log_hook)

        options = options or {}
        concurrent_frags = int(options.get('concurrent_fragments', 16))
        anti_skip = bool(options.get('anti_skip', True))
        timeout = int(options.get('socket_timeout', 20))
        retries = int(options.get('max_retries', 20))
        auto_remux = bool(options.get('auto_remux', True))
        output_format = options.get('output_format', 'mp4')

        out_dir = os.path.abspath(save_path)
        os.makedirs(out_dir, exist_ok=True)

        # Temporary cache directory for all partial fragments and working files
        cache_dir = os.path.join(out_dir, ".anikoto_cache")
        self.current_cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

        try:
            video_id, domain = self.scraper.get_video_id(url)
            episodes = self.scraper.get_episodes(domain, video_id)

            if ep_range:
                wanted = set(ep_range)
                episodes = [e for e in episodes if e['number'] in wanted]

            for ep in episodes:
                if self._cancelled:
                    if log_hook:
                        log_hook("Cancelled — partial download preserved for resuming.\n")
                    return

                safe_title = sanitize_filename(ep['title'])
                file_stem = f"E{ep['number']} {safe_title}"

                # Check if file already exists on disk
                existing = find_episode_file(out_dir, ep['number'], ep['title'])
                if existing and os.path.exists(existing):
                    size = os.path.getsize(existing)
                    if log_hook:
                        log_hook(f"[Ep {ep['number']}] already exists ({format_bytes(size)}) — skipping")
                    if ep_hook:
                        ep_hook(ep['number'], 'done', {'size': size})
                    continue

                if log_hook:
                    log_hook(f"\n[Ep {ep['number']}] {ep['title']}")
                if ep_hook:
                    ep_hook(ep['number'], 'start', {})

                servers = self.scraper.get_servers(domain, ep['data_ids'])
                got = False

                # Normalize audio track:
                # Anikoto servers host 'sub' (Japanese + subtitles) and 'dub' (English audio).
                # 'hsub' is Hardsub/Embedded subtitles, which maps to 'sub'.
                req_audio = (audio_type or "sub").strip().lower()
                target_audio = "sub" if req_audio == "hsub" else req_audio

                matching_servers = [s for s in servers if s.get('type', '').lower() == target_audio]
                if not matching_servers and servers:
                    fallback_type = servers[0].get('type', 'sub')
                    if log_hook:
                        log_hook(f"  Note: '{req_audio.upper()}' audio track not on server for Ep {ep['number']}; falling back to '{fallback_type.upper()}'")
                    matching_servers = servers

                for srv in matching_servers:
                    if self._cancelled:
                        if ep_hook:
                            ep_hook(ep['number'], 'cancelled', {})
                        return
                    if source_filter and source_filter != 'all':
                        base_name = srv['name'].split('-')[0].strip().lower()
                        if base_name != source_filter.lower():
                            continue

                    if log_hook:
                        log_hook(f"  Connecting: {srv['name']} ({srv['type'].upper()})")
                    try:
                        embed_url = self.scraper.get_embed_url(domain, srv['data_link_id'])
                        result = self.scraper.extract_m3u8(embed_url, domain, target_audio, source_filter)
                        if result is None:
                            continue
                        m3u8_url, referer, tracks = result

                        # Only clean cache if broken/missing .ytdl index; keep valid resume state intact!
                        _clean_broken_partial(cache_dir, file_stem)

                        def _hook(d):
                            if self._cancelled:
                                raise Cancelled()
                            if progress_hook:
                                progress_hook(d)

                        ffmpeg_dir = find_ffmpeg_dir()
                        # Honor user-selected quality: e.g. 1080p, 720p, 480p, 360p
                        fmt = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best" if quality else "best"

                        ydl_opts = {
                            # Route ALL in-progress fragments, parts, and ytdl index to cache_dir
                            'paths': {
                                'home': out_dir,
                                'temp': cache_dir,
                            },
                            'outtmpl': file_stem + '.%(ext)s',
                            'format': fmt,
                            'extractor_args': {'generic': {'impersonate': ['edge']}},
                            'http_headers': {
                                'user-agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                                'accept': "*/*",
                                'origin': referer,
                                'referer': referer,
                                'priority': "u=1, i",
                            },
                            'concurrent_fragment_downloads': concurrent_frags,
                            'socket_timeout': timeout,
                            'retries': retries,
                            'fragment_retries': 50,
                            'skip_unavailable_fragments': not anti_skip,
                            'hls_use_mpegts': True,
                            'retry_sleep': {'max': 4},
                            'nocheckcertificate': True,
                            'continuedl': True,  # Resumes from existing fragments!
                            'noplaylist': True,
                            'quiet': True,
                            'no_warnings': True,
                            'progress_hooks': [_hook],
                        }
                        if ffmpeg_dir and auto_remux:
                            ydl_opts['ffmpeg_location'] = ffmpeg_dir
                            ydl_opts['remux_video'] = output_format
                            ydl_opts['fixup'] = 'detect_or_warn'

                        attempt = 0
                        while True:
                            if self._cancelled:
                                raise Cancelled()
                            try:
                                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                    ydl.download([m3u8_url])
                                if self._cancelled:
                                    raise Cancelled()
                                break
                            except Cancelled:
                                raise
                            except Exception as ex:
                                if self._cancelled:
                                    raise Cancelled()
                                attempt += 1
                                if attempt >= 5:
                                    raise
                                if log_hook:
                                    log_hook(f"  Network hiccup ({attempt}/5) — reconnecting...")
                                time.sleep(2)

                        # Save subtitle files into cache_dir
                        sub_base = os.path.join(cache_dir, file_stem)
                        self.save_subtitles(tracks, sub_base, log_hook)

                        # Locate resulting video file
                        video_file = find_episode_file(out_dir, ep['number'], ep['title'])
                        if not video_file or not os.path.exists(video_file):
                            video_file = os.path.join(out_dir, file_stem + '.mp4')

                        srt_cache = sub_base + '.en.srt'
                        vtt_cache = sub_base + '.en.vtt'
                        subs = srt_cache if (os.path.exists(srt_cache) and os.path.getsize(srt_cache) > 30) \
                            else (vtt_cache if os.path.exists(vtt_cache) else None)

                        if ffmpeg_dir and os.path.exists(video_file):
                            if not remux_and_embed(video_file, subs, ffmpeg_dir, log_hook, cache_dir=cache_dir):
                                if subs and os.path.exists(subs):
                                    dest_sub = os.path.join(out_dir, os.path.basename(subs))
                                    try:
                                        shutil.copyfile(subs, dest_sub)
                                    except Exception:
                                        pass
                                    if log_hook:
                                        log_hook("  Subtitles kept as sidecar file")
                        elif log_hook and not ffmpeg_dir and subs and os.path.exists(subs):
                            dest_sub = os.path.join(out_dir, os.path.basename(subs))
                            try:
                                shutil.copyfile(subs, dest_sub)
                            except Exception:
                                pass
                            log_hook("  (FFmpeg not detected — subtitle kept as .srt sidecar)")

                        # Episode completed 100%! Purge this episode's temporary fragments from cache
                        purge_episode_cache(cache_dir, file_stem)

                        got = True
                        file_size = os.path.getsize(video_file) if os.path.exists(video_file) else 0
                        if ep_hook:
                            ep_hook(ep['number'], 'done', {'size': file_size})
                        break
                    except Cancelled:
                        continue
                    except Exception as e:
                        if log_hook:
                            log_hook(f"  Server error: {e}")
                        continue

                if not got and log_hook:
                    log_hook(f"  [Ep {ep['number']}] All servers failed.\n")
                    if ep_hook:
                        ep_hook(ep['number'], 'error', {})
        finally:
            # Clean up the cache directory ONLY if all downloads finished and it is empty
            cleanup_cache_if_empty(cache_dir)
            self.current_cache_dir = None

    def save_subtitles(self, tracks, video_path, log_hook=None):
        base = video_path[:-4] if video_path.lower().endswith(".mp4") else video_path
        try:
            if not tracks:
                if log_hook:
                    log_hook("  No subtitle tracks available for this stream")
                return

            def score_track(t):
                if not t.get('file'):
                    return -100
                lbl = (t.get('label') or '').strip().lower()
                kind = (t.get('kind') or '').strip().lower()
                if kind == 'thumbnails':
                    return -100
                score = 0
                if 'english' in lbl or 'eng' in lbl:
                    score += 50
                    if lbl in ('english', 'eng'):
                        score += 50
                    if 'ai' in lbl:
                        score -= 25
                    if t.get('default'):
                        score += 10
                return score

            valid_tracks = [t for t in tracks if score_track(t) > 0]
            if not valid_tracks:
                if log_hook:
                    log_hook("  No English subtitle track found")
                return

            best_track = max(valid_tracks, key=score_track)
            r = self.scraper.session.get(best_track['file'], headers={"referer": "https://megaplay.buzz/"}, timeout=20)
            if r.status_code != 200:
                if log_hook:
                    log_hook(f"  Subtitle download failed (HTTP {r.status_code})")
                return

            with open(f"{base}.en.vtt", 'wb') as f:
                f.write(r.content)

            try:
                srt_text = _vtt_to_srt(r.content.decode('utf-8', errors='ignore'))
                if srt_text.strip():
                    with open(f"{base}.en.srt", 'w', encoding='utf-8') as f:
                        f.write(srt_text)
            except Exception:
                pass

            if log_hook:
                label_display = best_track.get('label') or 'English'
                log_hook(f"  Subtitles saved ({label_display}): {os.path.basename(base)}.en.srt")
        except Exception as e:
            if log_hook:
                log_hook(f"  Subtitle download warning: {e}")


# ------------------------------------------------------------- Settings ---
def load_settings():
    cfg = {
        "url": "",
        "path": os.path.join(os.path.expanduser("~"), "Downloads"),
        "audio": "sub",
        "quality": "1080",
        "geometry": "1140x780",
        "concurrent_fragments": 16,
        "anti_skip": True,
        "auto_remux": True,
        "create_subfolder": False,
        "clean_cache": True,
        "socket_timeout": 20,
        "max_retries": 20,
        "sound_notification": True,
        "output_format": "mp4",
    }
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg.update(data)
    except Exception:
        pass
    return cfg


def save_settings(cfg):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def get_asset_file(name):
    candidates = [
        os.path.join(getattr(sys, '_MEIPASS', ''), 'assets', name),
        os.path.join(getattr(sys, '_MEIPASS', ''), name),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', name),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
        os.path.join(APP_DIR, 'assets', name),
        os.path.join(APP_DIR, name),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


import customtkinter as ctk

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# --------------------------------------------- Deep Oceanic Navy & Gradient Theme ---
BG_ROOT             = "#080D1A"   # Deep Midnight Oceanic Navy (Easy on eyes, zero eye burn!)
BG_SIDEBAR          = "#0B132B"   # Frost Navy Sidebar
BG_CARD             = "#0F1A36"   # Elevated Sapphire Navy Card
BG_CARD_INNER       = "#091024"   # Recessed Deep Navy for inputs & table
BORDER_CARD         = "#1B2A4E"   # Subtle Navy Border
BORDER_ACCENT       = "#0284C7"   # Sky Cyan Gradient Border
BORDER_ACTIVE       = "#2563EB"   # Active Electric Blue Border
ACCENT_BLUE         = "#2563EB"   # Electric Royal Blue
ACCENT_BLUE_HOVER   = "#1D4ED8"   # Deep Royal Blue Hover
ACCENT_BLUE_LIGHT   = "#132247"   # Secondary Navy Button Background
ACCENT_CYAN         = "#06B6D4"   # Bright Neon Cyan
ACCENT_CYAN_LIGHT   = "#38BDF8"   # Sky Cyan Text / Highlight
ACCENT_GREEN        = "#10B981"   # Emerald Green
ACCENT_AMBER        = "#F59E0B"   # Amber Warning / Downloading
ACCENT_RED          = "#F43F5E"   # Crimson Red
ACCENT_RED_HOVER    = "#E11D48"   # Dark Red Hover
ACCENT_RED_BG       = "#451221"   # Deep Wine Red Background for Stop Button
ACCENT_RED_BORDER   = "#881337"   # Red Border
TEXT_TITLE          = "#FFFFFF"   # Crisp Pure White
TEXT_MAIN           = "#E2E8F0"   # Slate 200 Crisp Text
TEXT_MUTED          = "#94A3B8"   # Slate 400 Cool Gray
TEXT_FAINT          = "#64748B"   # Slate 500 Subtle Labels
TEXT_CYAN           = "#38BDF8"   # Electric Sky Cyan
ROW_ALT             = "#0D1630"   # Table Alternating Row Tint
ROW_SELECT          = "#2563EB"   # Table Selected Row (Royal Blue)
ROW_TICKED          = "#172554"   # Table Ticked Row (Vibrant Sapphire Glow)


class AnikotoDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ANIKOTO Download Manager Pro")

        # Window Icon
        icon_candidates = [
            get_asset_file("icon.ico"),
            os.path.join(APP_DIR, "icon.ico"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico"),
        ]
        for ic in icon_candidates:
            if ic and os.path.exists(ic):
                try:
                    self.root.iconbitmap(ic)
                    break
                except Exception:
                    pass

        self.cfg = load_settings()
        self.root.geometry(self.cfg.get("geometry", "1140x780"))
        self.root.minsize(980, 640)
        self.root.configure(fg_color=BG_ROOT)

        # State Variables
        self.downloader = None
        self._is_downloading = False
        self._episodes = []          # List of dicts: {number, title, data_ids, status, size, eta}
        self._ticked = set()         # Selected episode numbers
        self._current_ep_num = None
        self._queue = []
        self._queue_pos = -1
        self._active_filter = "All"  # "All", "Queued", "Downloading", "Completed", "Failed"
        self._search_query = ""
        self._current_tab = None
        self._tab_anim_job = None
        self._arrow_anim_job = None
        self._is_animating_tab = False
        self._nav_arrows = {}
        self._fetch_spinner_active = False
        self._log_messages = []
        self._log_window = None
        self._poster_photo = None
        self._banner_photo = None
        self._cat_photo = None
        self._slide_timer_job = None
        self._slides_photos = []
        self._slides_meta = []

        self._setup_styles()
        self._build_ui()
        self._setup_shortcuts()
        self._show_tab("downloads")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------- Style Configuration ---
    def _setup_styles(self):
        s = ttk.Style()
        s.theme_use("clam")

        s.configure("Oceanic.Treeview",
                    background=BG_CARD_INNER,
                    foreground=TEXT_MAIN,
                    fieldbackground=BG_CARD_INNER,
                    rowheight=36,
                    borderwidth=0,
                    font=("Segoe UI", 10),
                    relief="flat")
        s.configure("Oceanic.Treeview.Heading",
                    background="#111D3B",
                    foreground=ACCENT_CYAN_LIGHT,
                    font=("Segoe UI Bold", 10),
                    borderwidth=0,
                    relief="flat",
                    padding=(10, 8))
        s.map("Oceanic.Treeview.Heading",
              background=[("active", "#1A2C59")])
        s.map("Oceanic.Treeview",
              background=[("selected", ROW_SELECT)],
              foreground=[("selected", "#FFFFFF")])

    # ------------------------------------------------------------- UI Architecture ---
    def _build_ui(self):
        # 1. Left Slim Sidebar (Modern Icon Rail - 74px)
        self.sidebar = ctk.CTkFrame(self.root, width=74, corner_radius=0,
                                    fg_color=BG_SIDEBAR, border_color=BORDER_CARD, border_width=1)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self._build_sidebar()

        # 2. Right Main Content Area
        self.main_content = ctk.CTkFrame(self.root, fg_color="transparent")
        self.main_content.pack(side="right", fill="both", expand=True, padx=16, pady=12)

        # Tab Frames (Solid BG_ROOT so panels never see-through or ghost during transitions)
        self.frame_downloads = ctk.CTkFrame(self.main_content, fg_color=BG_ROOT)
        self.frame_settings = ctk.CTkFrame(self.main_content, fg_color=BG_ROOT)
        self.frame_about = ctk.CTkFrame(self.main_content, fg_color=BG_ROOT)

        self._build_downloads_tab()
        self._build_settings_tab()
        self._build_about_tab()

    # ------------------------------------------------------------- Left Sidebar ---
    def _build_sidebar(self):
        # Top Brand Logo Section (Clear brand badge + ANIKOTO title)
        brand_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand_frame.pack(pady=(14, 16))

        logo_badge = ctk.CTkButton(
            brand_frame, text="▲", width=42, height=42, corner_radius=10,
            font=("Segoe UI Black", 18), fg_color="#111E3E",
            text_color=ACCENT_CYAN_LIGHT, hover_color="#182A57",
            border_color="#1E3A8A", border_width=1,
            command=lambda: self._show_tab("downloads")
        )
        logo_badge.pack()

        lbl_brand = ctk.CTkLabel(
            brand_frame, text="ANIKOTO", font=("Segoe UI Black", 9),
            text_color=TEXT_MUTED
        )
        lbl_brand.pack(pady=(3, 0))

        # Nav Icon Buttons (Downloads, Settings, About)
        self.btn_nav_downloads = ctk.CTkButton(
            self.sidebar, text="📥", width=46, height=46, corner_radius=12,
            font=("Segoe UI Bold", 18), fg_color=ACCENT_BLUE, text_color="#FFFFFF",
            hover_color=ACCENT_BLUE_HOVER, command=lambda: self._show_tab("downloads")
        )
        self.btn_nav_downloads.pack(pady=6)

        self.btn_nav_settings = ctk.CTkButton(
            self.sidebar, text="⚙", width=46, height=46, corner_radius=12,
            font=("Segoe UI Bold", 18), fg_color="transparent", text_color=TEXT_MUTED,
            hover_color="#172648", command=lambda: self._show_tab("settings")
        )
        self.btn_nav_settings.pack(pady=6)

        self.btn_nav_about = ctk.CTkButton(
            self.sidebar, text="ℹ", width=46, height=46, corner_radius=12,
            font=("Segoe UI Bold", 18), fg_color="transparent", text_color=TEXT_MUTED,
            hover_color="#172648", command=lambda: self._show_tab("about")
        )
        self.btn_nav_about.pack(pady=6)

        # Bottom Author Watermark (Made bigger and easier to read as requested!)
        lbl_watermark = ctk.CTkLabel(
            self.sidebar, text="Made by\nMYG", font=("Segoe UI Bold", 12),
            text_color=ACCENT_CYAN_LIGHT
        )
        lbl_watermark.pack(side="bottom", pady=(4, 14))

        lbl_ver_badge = ctk.CTkLabel(
            self.sidebar, text="v1.0.0", font=("Segoe UI Bold", 10), text_color="#64748B"
        )
        lbl_ver_badge.pack(side="bottom", pady=(2, 2))

        self.lbl_disk_sidebar = ctk.CTkLabel(
            self.sidebar, text=f"{get_disk_free(self.cfg.get('path', ''))}\nfree",
            font=("Segoe UI Bold", 9), text_color=TEXT_MUTED
        )
        self.lbl_disk_sidebar.pack(side="bottom", pady=(8, 2))

    # ------------------------------------------------------------- Tab Switching ---
    def _show_tab(self, tab_name):
        if hasattr(self, "_current_tab") and self._current_tab == tab_name:
            return

        self._current_tab = tab_name

        tab_info = {
            "downloads": (self.btn_nav_downloads, self.frame_downloads),
            "settings": (self.btn_nav_settings, self.frame_settings),
            "about": (self.btn_nav_about, self.frame_about),
        }

        # 1. Update button highlights
        for name, (btn, frame) in tab_info.items():
            if name == tab_name:
                btn.configure(fg_color=ACCENT_BLUE, text_color="#FFFFFF")
            else:
                btn.configure(fg_color="transparent", text_color=TEXT_MUTED)

        # 2. Synchronous tab swap with zero flickering or staggered reflow
        for name, (btn, frame) in tab_info.items():
            if name != tab_name:
                frame.pack_forget()

        tab_info[tab_name][1].pack(fill="both", expand=True)

    # --------------------------------------------------- Spotlight Banner Slideshow ---
    def _init_spotlight_slides(self, parent):
        self._slides_meta = [
            {
                "title": "One Piece",
                "badge": "⭐ TOP RATED SPOTLIGHT",
                "badge_color": "#1D4ED8",
                "sub": "Load the full series into your download queue.",
                "file": "card_1_bg.png",
                "url": "https://anikoto.cz/watch/one-piece-odmau"
            },
            {
                "title": "Black Clover: Season 2",
                "badge": "🔥 TRENDING ON ANIKOTO",
                "badge_color": "#DC2626",
                "sub": "Stream & download all episodes in 1080p.",
                "file": "card_2_bg.png",
                "url": "https://anikoto.cz/watch/black-clover-season-2"
            },
            {
                "title": "Overgeared",
                "badge": "⚡ POPULAR NOW",
                "badge_color": "#EA580C",
                "sub": "Popular action spotlight ready to download.",
                "file": "card_3_bg.png",
                "url": "https://anikoto.cz/watch/overgeared-a487c"
            },
            {
                "title": "A Returner's Magic",
                "badge": "✨ HIGHLY RATED",
                "badge_color": "#7C3AED",
                "sub": "Season 2 episodes in English Sub & Dub.",
                "file": "card_4_bg.png",
                "url": "https://anikoto.cz/watch/a-returner-s-magic-should-be-special-season-2"
            },
            {
                "title": "Reincarnated as a Sword",
                "badge": "⚔️ ACTION SPOTLIGHT",
                "badge_color": "#0D9488",
                "sub": "Action fantasy episodes ready to queue.",
                "file": "card_5_bg.png",
                "url": "https://anikoto.cz/watch/tensei-shitara-ken-deshita-2nd-season"
            }
        ]

        self._current_slide_idx = 0
        self._slides_photos = []
        self._slide_timer_job = None
        self._card_w = 560
        self._card_h = 180

        for item in self._slides_meta:
            rel_p = os.path.join("spotlight_cards", item["file"])
            bf = get_asset_file(rel_p)
            if not bf:
                bf = get_asset_file(item["file"])
            if bf and os.path.exists(bf):
                try:
                    raw = Image.open(bf)
                    photo = ctk.CTkImage(light_image=raw, dark_image=raw, size=(self._card_w, self._card_h))
                    self._slides_photos.append(photo)
                except Exception:
                    pass

        # Left Card in top_grid - packed synchronously with card_download to prevent piece-by-piece lag
        self.card_spotlight = ctk.CTkFrame(parent, corner_radius=14, fg_color=BG_CARD,
                                           border_color=BORDER_CARD, border_width=1)
        self.card_spotlight.pack(side="left", fill="both", expand=True, padx=(0, 6))

        # Background image label
        if self._slides_photos:
            self.spot_bg_lbl = ctk.CTkLabel(self.card_spotlight, text="", image=self._slides_photos[0], corner_radius=14)
            self.spot_bg_lbl.place(x=0, y=0, relwidth=1, relheight=1)
            self.spot_bg_lbl.bind("<Button-1>", lambda e: self._on_banner_click())
            self.banner_lbl = self.spot_bg_lbl

        # Content container placed cleanly over background
        self.spot_content = ctk.CTkFrame(self.card_spotlight, fg_color="transparent")
        self.spot_content.place(x=18, y=14, relwidth=0.92, relheight=0.84)
        self.spot_content.tkraise()

        # Badge
        item0 = self._slides_meta[0]
        self.spot_badge = ctk.CTkLabel(
            self.spot_content, text=item0["badge"], fg_color=item0["badge_color"],
            text_color="white", corner_radius=6, font=("Segoe UI Bold", 10), height=20, padx=10
        )
        self.spot_badge.pack(anchor="w")
        self.spot_badge.bind("<Button-1>", lambda e: self._on_banner_click())

        # Title
        self.spot_title = ctk.CTkLabel(
            self.spot_content, text=item0["title"], font=("Segoe UI Bold", 20), text_color="white"
        )
        self.spot_title.pack(anchor="w", pady=(4, 1))
        self.spot_title.bind("<Button-1>", lambda e: self._on_banner_click())
        self.lbl_banner_tag = self.spot_title

        # Subtitle
        self.spot_sub = ctk.CTkLabel(
            self.spot_content, text=item0["sub"], font=("Segoe UI", 11), text_color="#94A3B8"
        )
        self.spot_sub.pack(anchor="w", pady=(0, 6))
        self.spot_sub.bind("<Button-1>", lambda e: self._on_banner_click())

        # Action Row: Load Series + Dots
        action_row = ctk.CTkFrame(self.spot_content, fg_color="transparent")
        action_row.pack(side="bottom", fill="x", anchor="w")

        # Action Button: Open on Anikoto
        self.btn_load_series = ctk.CTkButton(
            action_row, text="Open on Anikoto ↗", width=120, height=28, corner_radius=7,
            fg_color="white", text_color="#0B1329", hover_color="#E2E8F0",
            font=("Segoe UI Bold", 11), command=self._on_banner_click
        )
        self.btn_load_series.pack(side="left")

        # Slideshow dots next to Load series button
        self._slide_dots = []
        dots_box = ctk.CTkFrame(action_row, fg_color="transparent")
        dots_box.pack(side="left", padx=12)

        for s_idx in range(len(self._slides_meta)):
            dot = ctk.CTkLabel(
                dots_box, text="●" if s_idx == 0 else "○",
                font=("Segoe UI", 12),
                text_color=ACCENT_CYAN_LIGHT if s_idx == 0 else "#64748B",
                cursor="hand2"
            )
            dot.pack(side="left", padx=3)
            dot.bind("<Button-1>", lambda e, idx=s_idx: self._go_to_slide(idx))
            self._slide_dots.append(dot)

        # Auto-advance timer
        self._schedule_next_slide()

        # Pause on hover
        def _pause_slides(e=None):
            if hasattr(self, "_slide_timer_job") and self._slide_timer_job:
                try:
                    self.root.after_cancel(self._slide_timer_job)
                    self._slide_timer_job = None
                except Exception:
                    pass

        def _resume_slides(e=None):
            self._schedule_next_slide()

        self.card_spotlight.bind("<Enter>", _pause_slides)
        self.card_spotlight.bind("<Leave>", _resume_slides)
        if hasattr(self, "spot_bg_lbl"):
            self.spot_bg_lbl.bind("<Enter>", _pause_slides)
            self.spot_bg_lbl.bind("<Leave>", _resume_slides)

    def _schedule_next_slide(self):
        if hasattr(self, "_slide_timer_job") and self._slide_timer_job:
            try:
                self.root.after_cancel(self._slide_timer_job)
            except Exception:
                pass
        self._slide_timer_job = self.root.after(6000, self._next_slide)

    def _go_to_slide(self, idx):
        if not self._slides_meta or idx < 0 or idx >= len(self._slides_meta):
            return
        self._current_slide_idx = idx
        item = self._slides_meta[idx]

        if hasattr(self, "spot_bg_lbl") and idx < len(self._slides_photos):
            self.spot_bg_lbl.configure(image=self._slides_photos[idx])
        if hasattr(self, "spot_badge"):
            self.spot_badge.configure(text=item["badge"], fg_color=item["badge_color"])
        if hasattr(self, "spot_title"):
            self.spot_title.configure(text=item["title"])
        if hasattr(self, "spot_sub"):
            self.spot_sub.configure(text=item["sub"])

        # Update dots
        for i, dot in enumerate(self._slide_dots):
            if i == idx:
                dot.configure(text="●", text_color=ACCENT_CYAN_LIGHT)
            else:
                dot.configure(text="○", text_color="#64748B")

        self._schedule_next_slide()

    def _next_slide(self):
        if not self._slides_photos:
            return
        next_idx = (self._current_slide_idx + 1) % len(self._slides_photos)
        self._go_to_slide(next_idx)

    def _prev_slide(self):
        if not self._slides_photos:
            return
        prev_idx = (self._current_slide_idx - 1) % len(self._slides_photos)
        self._go_to_slide(prev_idx)

    def _on_banner_click(self):
        if not self._slides_meta or self._current_slide_idx >= len(self._slides_meta):
            return
        item = self._slides_meta[self._current_slide_idx]
        url = item.get("url", "")
        if url:
            self.url_var.set(url)
            self._set_status(f"Loaded '{item['title']}'! Opening on Anikoto website...", ACCENT_BLUE)
            self._log(f"Opening browser for spotlight: {item['title']} ({url})")
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass

    # ----------------------------------------------------- Tab 1: Downloads ---
    def _build_downloads_tab(self):
        # 1. Top Header Row (Title + Open folder)
        header_row = ctk.CTkFrame(self.frame_downloads, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 10))

        title_box = ctk.CTkFrame(header_row, fg_color="transparent")
        title_box.pack(side="left")

        lbl_tag = ctk.CTkLabel(title_box, text="ANIKOTO • PRO EDITION", font=("Segoe UI Bold", 9), text_color=ACCENT_CYAN_LIGHT)
        lbl_tag.pack(anchor="w")

        lbl_main_title = ctk.CTkLabel(title_box, text="Downloads", font=("Segoe UI Bold", 26), text_color=TEXT_TITLE)
        lbl_main_title.pack(anchor="w")

        btn_open_folder = ctk.CTkButton(
            header_row, text="📁  Open Downloads folder", height=32, corner_radius=8,
            font=("Segoe UI Semibold", 11), fg_color=BG_CARD, hover_color="#1A2C59",
            text_color="#93C5FD", border_color=BORDER_CARD, border_width=1,
            command=self._open_save_folder
        )
        btn_open_folder.pack(side="right", pady=4)

        # 2. Upper 2-Column Split Dashboard (Grid of Spotlight Card + New Download Card)
        self.top_grid = top_grid = ctk.CTkFrame(self.frame_downloads, fg_color="transparent", height=180)
        top_grid.pack(fill="x", pady=(0, 8))
        top_grid.pack_propagate(False)

        # 2a. Left Column: Spotlight Anime Card
        self._init_spotlight_slides(top_grid)

        # 2b. Right Column: New Download Cockpit Card
        card_download = ctk.CTkFrame(top_grid, corner_radius=14, fg_color=BG_CARD,
                                     border_color=BORDER_CARD, border_width=1)
        card_download.pack(side="right", fill="both", expand=True, padx=(6, 0))

        lbl_dl_hdr = ctk.CTkLabel(card_download, text="New download", font=("Segoe UI Bold", 13), text_color=TEXT_TITLE)
        lbl_dl_hdr.pack(anchor="w", padx=16, pady=(10, 4))

        # Row 1: URL input
        row_url = ctk.CTkFrame(card_download, fg_color="transparent")
        row_url.pack(fill="x", padx=16, pady=3)

        self.url_var = tk.StringVar(value=self.cfg.get("url", ""))
        self.url_entry = ctk.CTkEntry(
            row_url, textvariable=self.url_var, height=30, corner_radius=8,
            fg_color=BG_CARD_INNER, border_color=BORDER_CARD, text_color=TEXT_TITLE,
            placeholder_text="https://anikoto.cz/watch/..."
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.url_entry.bind("<Return>", lambda e: self._load_episodes())

        self.btn_paste = ctk.CTkButton(
            row_url, text="Paste", width=62, height=30, corner_radius=8,
            fg_color=BG_ROOT, hover_color="#1D2E56", text_color=TEXT_TITLE,
            border_color=BORDER_CARD, border_width=1, command=self._paste_clipboard
        )
        self.btn_paste.pack(side="right")

        # Row 2: Save path
        row_path = ctk.CTkFrame(card_download, fg_color="transparent")
        row_path.pack(fill="x", padx=16, pady=3)

        self.path_var = tk.StringVar(value=self.cfg.get("path", ""))
        self.path_entry = ctk.CTkEntry(
            row_path, textvariable=self.path_var, height=30, corner_radius=8,
            fg_color=BG_CARD_INNER, border_color=BORDER_CARD, text_color=TEXT_TITLE
        )
        self.path_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        btn_browse = ctk.CTkButton(
            row_path, text="Browse", width=62, height=30, corner_radius=8,
            fg_color=BG_ROOT, hover_color="#1D2E56", text_color=TEXT_TITLE,
            border_color=BORDER_CARD, border_width=1, command=self._browse_folder
        )
        btn_browse.pack(side="right")

        # Row 3: Audio pills, Quality dropdown, Fetch button
        row_opts = ctk.CTkFrame(card_download, fg_color="transparent")
        row_opts.pack(fill="x", padx=16, pady=(4, 6))

        initial_audio = (self.cfg.get("audio") or "sub").upper()
        if initial_audio not in ("SUB", "DUB", "HSUB"):
            initial_audio = "SUB"
        self.audio_var = tk.StringVar(value=initial_audio.lower())

        self.seg_audio = ctk.CTkSegmentedButton(
            row_opts, values=["SUB", "DUB", "HSUB"], corner_radius=7,
            height=30, selected_color=ACCENT_BLUE, selected_hover_color=ACCENT_BLUE_HOVER,
            unselected_color=BG_ROOT, unselected_hover_color="#1E3A8A",
            text_color="#FFFFFF", font=("Segoe UI Bold", 10), command=self._on_audio_changed
        )
        self.seg_audio.set(initial_audio)
        self.seg_audio.pack(side="left", padx=(0, 6))

        initial_quality = str(self.cfg.get("quality") or "1080")
        if not initial_quality.endswith("p"):
            initial_quality += "p"
        self.quality_var = tk.StringVar(value=initial_quality.rstrip("p"))

        self.opt_quality = ctk.CTkOptionMenu(
            row_opts, values=["1080p", "720p", "480p", "360p", "2160p", "1440p"],
            width=88, height=30, corner_radius=7, fg_color=BG_CARD_INNER,
            button_color=BG_CARD_INNER, button_hover_color="#1D356A",
            dropdown_fg_color=BG_CARD, dropdown_hover_color="#132247",
            text_color=TEXT_TITLE, font=("Segoe UI", 11), command=self._on_quality_changed
        )
        self.opt_quality.set(initial_quality)
        self.opt_quality.pack(side="left", padx=(0, 8))

        self.btn_fetch = ctk.CTkButton(
            row_opts, text="Fetch episodes", height=30, corner_radius=8,
            font=("Segoe UI Bold", 12), fg_color=ACCENT_BLUE,
            hover_color=ACCENT_BLUE_HOVER, text_color="#FFFFFF",
            command=self._load_episodes
        )
        self.btn_fetch.pack(side="right", fill="x", expand=True)

        # 3. BOTTOM DOCKED MONITOR & CONTROLS DECK (Packed side="bottom" first to guarantee 100% visibility)
        card_bottom = ctk.CTkFrame(self.frame_downloads, corner_radius=12, fg_color=BG_CARD,
                                   border_color=BORDER_CARD, border_width=1)
        card_bottom.pack(side="bottom", fill="x", pady=(4, 0))

        deck_content = ctk.CTkFrame(card_bottom, fg_color="transparent")
        deck_content.pack(fill="both", expand=True, padx=14, pady=10)

        # Top of Deck: Status & Metrics
        top_info = ctk.CTkFrame(deck_content, fg_color="transparent")
        top_info.pack(fill="x")

        self.lbl_task_name = ctk.CTkLabel(top_info, text="Ready to Download",
                                          font=("Segoe UI Semibold", 11), text_color="#93C5FD")
        self.lbl_task_name.pack(side="left")

        self.lbl_counts = ctk.CTkLabel(top_info, text="0 episodes loaded",
                                       font=("Segoe UI", 10), text_color=TEXT_MUTED)
        self.lbl_counts.pack(side="right")

        # Smooth Electric Blue Progress Bar
        self.file_progress = ctk.CTkProgressBar(deck_content, corner_radius=3, height=6,
                                                progress_color=ACCENT_BLUE, fg_color=BG_ROOT)
        self.file_progress.set(0)
        self.file_progress.pack(fill="x", pady=(6, 4))

        # Speed and ETA subline
        sub_info = ctk.CTkFrame(deck_content, fg_color="transparent")
        sub_info.pack(fill="x", pady=(0, 6))

        self.lbl_speed_eta = ctk.CTkLabel(sub_info, text="No active download",
                                          font=("Segoe UI", 10), text_color=TEXT_MUTED)
        self.lbl_speed_eta.pack(side="left")

        self.lbl_task_metrics = ctk.CTkLabel(sub_info, text="",
                                             font=("Segoe UI Semibold", 10), text_color=ACCENT_CYAN_LIGHT)
        self.lbl_task_metrics.pack(side="right")

        # Action Buttons Row
        action_row = ctk.CTkFrame(deck_content, fg_color="transparent")
        action_row.pack(fill="x")

        self.tb_start = ctk.CTkButton(
            action_row, text="▶   Start queue", corner_radius=8,
            fg_color=ACCENT_BLUE, hover_color=ACCENT_BLUE_HOVER,
            font=("Segoe UI Bold", 12), height=34, width=150, text_color="#FFFFFF",
            command=self._start_download
        )
        self.tb_start.pack(side="left", padx=(0, 6))

        self.tb_all = ctk.CTkButton(
            action_row, text="📥  Download remaining", corner_radius=8,
            fg_color=BG_ROOT, hover_color="#1D356A", border_color=BORDER_CARD, border_width=1,
            text_color=TEXT_TITLE, font=("Segoe UI", 11), height=34,
            command=self._download_all_remaining
        )
        self.tb_all.pack(side="left", padx=(0, 6))

        self.tb_stop = ctk.CTkButton(
            action_row, text="⏸  Stop / Pause", corner_radius=8,
            fg_color=ACCENT_RED_BG, hover_color="#5F162B", text_color="#FDA4AF",
            border_color=ACCENT_RED_BORDER, border_width=1,
            font=("Segoe UI Bold", 11), height=34, width=120,
            state="disabled", command=self._cancel_download
        )
        self.tb_stop.pack(side="left", padx=(0, 6))

        self.tb_delete = ctk.CTkButton(
            action_row, text="🗑  Delete file", corner_radius=8,
            fg_color=BG_ROOT, hover_color="#1D356A", border_color=BORDER_CARD, border_width=1,
            text_color=TEXT_TITLE, font=("Segoe UI", 11), height=34, width=105,
            command=self._delete_selected_episode
        )
        self.tb_delete.pack(side="left", padx=(0, 6))

        self.tb_clear = ctk.CTkButton(
            action_row, text="🧹  Clear done", corner_radius=8,
            fg_color=BG_ROOT, hover_color="#1D356A", border_color=BORDER_CARD, border_width=1,
            text_color=TEXT_TITLE, font=("Segoe UI", 11), height=34, width=100,
            command=self._clear_completed_selection
        )
        self.tb_clear.pack(side="left", padx=(0, 6))

        self.tb_log = ctk.CTkButton(
            action_row, text="📋  Log", width=65, corner_radius=8,
            fg_color=BG_ROOT, hover_color="#1D356A", border_color=BORDER_CARD, border_width=1,
            text_color=TEXT_TITLE, font=("Segoe UI", 11), height=34,
            command=self._open_activity_log
        )
        self.tb_log.pack(side="right")

        # Bottom Bar Author Watermark (Bigger and easier to read)
        lbl_bar_wm = ctk.CTkLabel(
            action_row, text="Made by : MYG", font=("Segoe UI Bold", 12),
            text_color=ACCENT_CYAN_LIGHT
        )
        lbl_bar_wm.pack(side="right", padx=(0, 16))

        # 4. CENTRAL CARD: Episodes Table & Filters (Fills space between top grid and bottom deck)
        self.card_episodes = ctk.CTkFrame(self.frame_downloads, corner_radius=14, fg_color=BG_CARD,
                                          border_color=BORDER_CARD, border_width=1)
        self.card_episodes.pack(side="top", fill="both", expand=True, pady=(0, 6))

        # Filter bar at top of card
        filter_strip = ctk.CTkFrame(self.card_episodes, fg_color="transparent")
        filter_strip.pack(fill="x", padx=14, pady=(10, 6))

        # Filter Pills
        self.pill_all = ctk.CTkButton(
            filter_strip, text="All", width=54, height=28, corner_radius=6,
            fg_color=ACCENT_BLUE, hover_color=ACCENT_BLUE_HOVER,
            font=("Segoe UI Semibold", 11), text_color="#FFFFFF",
            command=lambda: self._on_filter_changed("All")
        )
        self.pill_all.pack(side="left", padx=(0, 4))

        self.pill_queued = ctk.CTkButton(
            filter_strip, text="Queued", width=68, height=28, corner_radius=6,
            fg_color="transparent", hover_color="#172648",
            text_color=TEXT_MUTED, font=("Segoe UI Semibold", 11),
            command=lambda: self._on_filter_changed("Queued")
        )
        self.pill_queued.pack(side="left", padx=(0, 4))

        self.pill_downloading = ctk.CTkButton(
            filter_strip, text="Downloading", width=92, height=28, corner_radius=6,
            fg_color="transparent", hover_color="#172648",
            text_color=TEXT_MUTED, font=("Segoe UI Semibold", 11),
            command=lambda: self._on_filter_changed("Downloading")
        )
        self.pill_downloading.pack(side="left", padx=(0, 4))

        self.pill_completed = ctk.CTkButton(
            filter_strip, text="Completed", width=82, height=28, corner_radius=6,
            fg_color="transparent", hover_color="#172648",
            text_color=TEXT_MUTED, font=("Segoe UI Semibold", 11),
            command=lambda: self._on_filter_changed("Completed")
        )
        self.pill_completed.pack(side="left", padx=(0, 4))

        self.pill_failed = ctk.CTkButton(
            filter_strip, text="Failed", width=62, height=28, corner_radius=6,
            fg_color="transparent", hover_color="#172648",
            text_color=TEXT_MUTED, font=("Segoe UI Semibold", 11),
            command=lambda: self._on_filter_changed("Failed")
        )
        self.pill_failed.pack(side="left", padx=(0, 8))

        # Right-aligned Search & Quick Selects
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *args: self._on_search_changed())
        self.search_entry = ctk.CTkEntry(
            filter_strip, textvariable=self.search_var,
            placeholder_text="Search episodes...", width=140, height=28,
            corner_radius=6, fg_color=BG_CARD_INNER, border_color=BORDER_CARD,
            font=("Segoe UI", 11), text_color=TEXT_TITLE
        )
        self.search_entry.pack(side="right", padx=(6, 0))

        btn_range = ctk.CTkButton(
            filter_strip, text="Range", width=54, height=28, corner_radius=6,
            fg_color=BG_ROOT, hover_color="#132247", border_color=BORDER_CARD, border_width=1,
            text_color=TEXT_MUTED, font=("Segoe UI", 10), command=self._prompt_range_selector
        )
        btn_range.pack(side="right", padx=2)

        btn_none = ctk.CTkButton(
            filter_strip, text="None", width=48, height=28, corner_radius=6,
            fg_color=BG_ROOT, hover_color="#132247", border_color=BORDER_CARD, border_width=1,
            text_color=TEXT_MUTED, font=("Segoe UI", 10), command=self._select_none
        )
        btn_none.pack(side="right", padx=2)

        btn_all = ctk.CTkButton(
            filter_strip, text="Select all", width=68, height=28, corner_radius=6,
            fg_color=BG_ROOT, hover_color="#132247", border_color=BORDER_CARD, border_width=1,
            text_color=TEXT_MUTED, font=("Segoe UI", 10), command=self._select_all_visible
        )
        btn_all.pack(side="right", padx=2)

        # 3a. Anime Series Showcase Pill (Shown above table when series loaded)
        self.anime_pill = ctk.CTkFrame(self.card_episodes, corner_radius=8, fg_color="#111E3E",
                                       border_color=BORDER_ACCENT, border_width=1)

        left_pill = ctk.CTkFrame(self.anime_pill, fg_color="transparent")
        left_pill.pack(side="left", padx=10, pady=6)

        ctk.CTkLabel(left_pill, text="🎬", font=("Segoe UI", 15)).pack(side="left", padx=(0, 6))

        self.lbl_anime_name = ctk.CTkLabel(left_pill, text="Anime Series",
                                           font=("Segoe UI Bold", 12), text_color=TEXT_TITLE)
        self.lbl_anime_name.pack(side="left", padx=(0, 8))

        self.badge_ep_count = ctk.CTkLabel(left_pill, text="0 EPISODES", font=("Segoe UI Bold", 9),
                                          text_color="#93C5FD", fg_color="#1E3A8A",
                                          corner_radius=4, padx=6, pady=2)
        self.badge_ep_count.pack(side="left", padx=3)

        self.badge_audio = ctk.CTkLabel(left_pill, text="SUB", font=("Segoe UI Bold", 9),
                                        text_color="#34D399", fg_color="#064E3B",
                                        corner_radius=4, padx=6, pady=2)
        self.badge_audio.pack(side="left", padx=3)

        self.badge_quality = ctk.CTkLabel(left_pill, text="1080p", font=("Segoe UI Bold", 9),
                                          text_color="#38BDF8", fg_color="#0C4A6E",
                                          corner_radius=4, padx=6, pady=2)
        self.badge_quality.pack(side="left", padx=3)

        self.badge_selected = ctk.CTkLabel(left_pill, text="0 Selected", font=("Segoe UI Bold", 9),
                                           text_color="#CBD5E1", fg_color="#1E293B",
                                           corner_radius=4, padx=6, pady=2)
        self.badge_selected.pack(side="left", padx=3)

        right_pill = ctk.CTkFrame(self.anime_pill, fg_color="transparent")
        right_pill.pack(side="right", padx=10, pady=6)

        btn_clear_series = ctk.CTkButton(
            right_pill, text="✕ Clear Anime", width=85, height=24, corner_radius=5,
            font=("Segoe UI", 10), fg_color="#1E3A8A", hover_color="#2563EB",
            text_color="#93C5FD", command=self._clear_series
        )
        btn_clear_series.pack(side="right")

        # 3b. Empty State Frame (Cat mascot illustration on blue cushion)
        self.empty_state_frame = ctk.CTkFrame(self.card_episodes, fg_color="transparent")
        self.empty_state_frame.pack(fill="both", expand=True, pady=(8, 8))

        cat_file = get_asset_file("mascot_cat.png")
        if cat_file and os.path.exists(cat_file):
            try:
                raw_cat = Image.open(cat_file)
                self._cat_photo = ctk.CTkImage(light_image=raw_cat, dark_image=raw_cat, size=(125, 70))
                lbl_cat = ctk.CTkLabel(self.empty_state_frame, text="", image=self._cat_photo)
                lbl_cat.pack(pady=(6, 4))
            except Exception:
                pass

        lbl_empty_title = ctk.CTkLabel(self.empty_state_frame, text="No tasks yet",
                                       font=("Segoe UI Bold", 15), text_color=TEXT_TITLE)
        lbl_empty_title.pack(pady=(0, 2))

        lbl_empty_sub = ctk.CTkLabel(self.empty_state_frame,
                                     text="Paste a link and fetch episodes to fill your queue.",
                                     font=("Segoe UI", 11), text_color=TEXT_MUTED)
        lbl_empty_sub.pack(pady=(0, 4))

        # 3c. Populated State Frame (Treeview Table)
        self.table_frame = ctk.CTkFrame(self.card_episodes, fg_color=BG_CARD_INNER, corner_radius=8, border_width=0)

        columns = ("pick", "num", "title", "format", "status", "progress", "eta")
        self.tree = ttk.Treeview(self.table_frame, columns=columns, show="headings",
                                 style="Oceanic.Treeview", selectmode="browse")

        # Column headings with explicit matching anchors and header click handler
        self.tree.heading("pick", text="✔", anchor=tk.CENTER, command=self._toggle_select_all)
        self.tree.heading("num", text="#", anchor=tk.CENTER)
        self.tree.heading("title", text="  Episode Title", anchor=tk.W)
        self.tree.heading("format", text="Format", anchor=tk.CENTER)
        self.tree.heading("status", text="Status", anchor=tk.CENTER)
        self.tree.heading("progress", text="Size / Progress", anchor=tk.CENTER)
        self.tree.heading("eta", text="ETA", anchor=tk.CENTER)

        # Proportional column sizing: ONLY title stretches to fill window width!
        self.tree.column("pick", width=42, minwidth=42, anchor=tk.CENTER, stretch=False)
        self.tree.column("num", width=48, minwidth=48, anchor=tk.CENTER, stretch=False)
        self.tree.column("title", width=360, minwidth=220, anchor=tk.W, stretch=True)
        self.tree.column("format", width=115, minwidth=90, anchor=tk.CENTER, stretch=False)
        self.tree.column("status", width=140, minwidth=120, anchor=tk.CENTER, stretch=False)
        self.tree.column("progress", width=135, minwidth=110, anchor=tk.CENTER, stretch=False)
        self.tree.column("eta", width=80, minwidth=65, anchor=tk.CENTER, stretch=False)

        # Style tags: completed, downloading, queued, failed, plus ticked row highlighting!
        self.tree.tag_configure("done", foreground=ACCENT_GREEN)
        self.tree.tag_configure("error", foreground=ACCENT_RED)
        self.tree.tag_configure("active", foreground=ACCENT_AMBER)
        self.tree.tag_configure("waiting", foreground=ACCENT_CYAN_LIGHT)
        self.tree.tag_configure("ticked", background=ROW_TICKED, foreground="#FFFFFF")
        self.tree.tag_configure("stripe", background=ROW_ALT)

        # Modern CustomTkinter electric blue scrollbar
        self.vsb = ctk.CTkScrollbar(self.table_frame, command=self.tree.yview,
                                    width=10, corner_radius=5,
                                    button_color="#0284C7", button_hover_color=ACCENT_CYAN_LIGHT,
                                    fg_color=BG_CARD_INNER)
        self.tree.configure(yscrollcommand=self.vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(2, 0), pady=2)
        self.vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 2), pady=2)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<space>", self._on_space_key)
        self.tree.bind("<Delete>", lambda e: self._delete_selected_episode())

        # Right-click context menu (Deep Oceanic Navy)
        self.context_menu = tk.Menu(self.root, tearoff=0, bg=BG_CARD, fg=TEXT_TITLE,
                                    activebackground=ACCENT_BLUE, activeforeground="#ffffff",
                                    relief=tk.FLAT, bd=1)
        self.context_menu.add_command(label="▶  Download This Episode", command=self._context_download_single)
        self.context_menu.add_command(label="🎬  Play in Default Player", command=self._play_selected_file)
        self.context_menu.add_command(label="📁  Reveal in File Explorer", command=self._open_save_folder)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="🗑  Delete File from Disk", command=self._delete_selected_episode)
        self.context_menu.add_command(label="🧹  Remove from Queue", command=self._context_uncheck_selected)
        self.context_menu.add_command(label="🔄  Restart Clean from 0% (Clear Cache)", command=self._context_redownload_clean)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="☑  Select / Check", command=self._context_check_selected)
        self.context_menu.add_command(label="☐  Deselect / Uncheck", command=self._context_uncheck_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="📋  Copy Title", command=self._context_copy_title)

    # ----------------------------------------------------- Tab 2: Settings ---
    def _build_settings_tab(self):
        scroll = ctk.CTkScrollableFrame(self.frame_settings, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # Header Title
        title_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        title_frame.pack(fill="x", pady=(4, 16))

        lbl_s_title = ctk.CTkLabel(title_frame, text="⚙  Application Settings",
                                   font=("Segoe UI Bold", 20), text_color=TEXT_TITLE)
        lbl_s_title.pack(anchor="w")

        lbl_s_sub = ctk.CTkLabel(title_frame,
                                 text="Configure storage directories, stream decryption, fragment concurrency, and transcoders.",
                                 font=("Segoe UI", 11), text_color=TEXT_MUTED)
        lbl_s_sub.pack(anchor="w", pady=(2, 0))

        # Section 1: Storage & Directory Management
        card_storage = ctk.CTkFrame(scroll, corner_radius=10, fg_color=BG_CARD,
                                    border_color=BORDER_CARD, border_width=1)
        card_storage.pack(fill="x", pady=(0, 12))

        c1_head = ctk.CTkFrame(card_storage, fg_color="transparent")
        c1_head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(c1_head, text="📁  Storage & Output Directory", font=("Segoe UI Bold", 13),
                     text_color=TEXT_TITLE).pack(anchor="w")
        ctk.CTkLabel(c1_head, text="Manage where your downloaded anime seasons and temporary files are stored.",
                     font=("Segoe UI", 10), text_color=TEXT_MUTED).pack(anchor="w")

        c1_body = ctk.CTkFrame(card_storage, fg_color="transparent")
        c1_body.pack(fill="x", padx=16, pady=(0, 14))

        ctk.CTkLabel(c1_body, text="Default Download Path:", font=("Segoe UI Semibold", 11),
                     text_color=TEXT_MAIN).pack(anchor="w", pady=(4, 2))

        path_row = ctk.CTkFrame(c1_body, fg_color="transparent")
        path_row.pack(fill="x", pady=(0, 8))

        self.set_path_entry = ctk.CTkEntry(
            path_row, textvariable=self.path_var, height=32, corner_radius=6,
            fg_color=BG_CARD_INNER, border_color=BORDER_CARD, font=("Segoe UI", 11),
            text_color=TEXT_TITLE
        )
        self.set_path_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        btn_set_browse = ctk.CTkButton(
            path_row, text="Browse…", width=80, height=32, corner_radius=6,
            fg_color=ACCENT_BLUE_LIGHT, hover_color="#1D356A", font=("Segoe UI", 11),
            text_color=TEXT_MAIN, border_color=BORDER_CARD, border_width=1,
            command=self._browse_folder
        )
        btn_set_browse.pack(side="left")

        self.sw_subfolder = ctk.CTkSwitch(
            c1_body, text="Organize into dedicated anime folders (Auto-create subfolder for each anime)",
            font=("Segoe UI", 11), progress_color=ACCENT_BLUE
        )
        if self.cfg.get("create_subfolder", False):
            self.sw_subfolder.select()
        else:
            self.sw_subfolder.deselect()
        self.sw_subfolder.pack(anchor="w", pady=4)

        self.sw_cleancache = ctk.CTkSwitch(
            c1_body, text="Automatically clean temporary fragment cache (.anikoto_cache) after completion",
            font=("Segoe UI", 11), progress_color=ACCENT_BLUE
        )
        if self.cfg.get("clean_cache", True):
            self.sw_cleancache.select()
        else:
            self.sw_cleancache.deselect()
        self.sw_cleancache.pack(anchor="w", pady=4)

        # Section 2: Stream & Video Preferences
        card_stream = ctk.CTkFrame(scroll, corner_radius=10, fg_color=BG_CARD,
                                   border_color=BORDER_CARD, border_width=1)
        card_stream.pack(fill="x", pady=(0, 12))

        c2_head = ctk.CTkFrame(card_stream, fg_color="transparent")
        c2_head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(c2_head, text="🎬  Stream & Video Preferences", font=("Segoe UI Bold", 13),
                     text_color=TEXT_TITLE).pack(anchor="w")
        ctk.CTkLabel(c2_head, text="Default audio tracks, resolution targets, and container encapsulation.",
                     font=("Segoe UI", 10), text_color=TEXT_MUTED).pack(anchor="w")

        c2_body = ctk.CTkFrame(card_stream, fg_color="transparent")
        c2_body.pack(fill="x", padx=16, pady=(0, 14))

        grid_row = ctk.CTkFrame(c2_body, fg_color="transparent")
        grid_row.pack(fill="x", pady=4)

        col1 = ctk.CTkFrame(grid_row, fg_color="transparent")
        col1.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkLabel(col1, text="Preferred Default Audio:", font=("Segoe UI Semibold", 11),
                     text_color=TEXT_MAIN).pack(anchor="w", pady=(0, 4))
        self.set_audio_seg = ctk.CTkSegmentedButton(
            col1, values=["SUB", "DUB", "HSUB"], corner_radius=6, height=30,
            selected_color=ACCENT_BLUE, unselected_color="#172554", unselected_hover_color="#1E3A8A",
            text_color="#FFFFFF", font=("Segoe UI Bold", 10)
        )
        self.set_audio_seg.set((self.cfg.get("audio") or "sub").upper())
        self.set_audio_seg.pack(fill="x")

        col2 = ctk.CTkFrame(grid_row, fg_color="transparent")
        col2.pack(side="left", fill="x", expand=True, padx=(10, 10))
        ctk.CTkLabel(col2, text="Default Resolution:", font=("Segoe UI Semibold", 11),
                     text_color=TEXT_MAIN).pack(anchor="w", pady=(0, 4))
        self.set_quality_opt = ctk.CTkOptionMenu(
            col2, values=["1080p", "720p", "480p", "360p", "2160p (4K)"],
            height=30, corner_radius=6, fg_color=BG_CARD_INNER,
            button_color="#132247", button_hover_color="#1D356A",
            dropdown_fg_color=BG_CARD, dropdown_hover_color="#132247",
            text_color=TEXT_TITLE
        )
        cur_q = str(self.cfg.get("quality", "1080"))
        if not cur_q.endswith("p"):
            cur_q += "p"
        self.set_quality_opt.set(cur_q)
        self.set_quality_opt.pack(fill="x")

        col3 = ctk.CTkFrame(grid_row, fg_color="transparent")
        col3.pack(side="left", fill="x", expand=True, padx=(10, 0))
        ctk.CTkLabel(col3, text="Video Container Format:", font=("Segoe UI Semibold", 11),
                     text_color=TEXT_MAIN).pack(anchor="w", pady=(0, 4))
        self.set_format_opt = ctk.CTkOptionMenu(
            col3, values=[".mp4 (Universal H.264)", ".mkv (Matroska)", ".ts (Raw Stream)"],
            height=30, corner_radius=6, fg_color=BG_CARD_INNER,
            button_color="#132247", button_hover_color="#1D356A",
            dropdown_fg_color=BG_CARD, dropdown_hover_color="#132247",
            text_color=TEXT_TITLE
        )
        fmt_val = self.cfg.get("output_format", "mp4")
        if fmt_val == "mkv":
            self.set_format_opt.set(".mkv (Matroska)")
        elif fmt_val == "ts":
            self.set_format_opt.set(".ts (Raw Stream)")
        else:
            self.set_format_opt.set(".mp4 (Universal H.264)")
        self.set_format_opt.pack(fill="x")

        # Section 3: Performance, Anti-Skip Engine & Network
        card_perf = ctk.CTkFrame(scroll, corner_radius=10, fg_color=BG_CARD,
                                 border_color=BORDER_CARD, border_width=1)
        card_perf.pack(fill="x", pady=(0, 12))

        c3_head = ctk.CTkFrame(card_perf, fg_color="transparent")
        c3_head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(c3_head, text="⚡  Engine Acceleration & Anti-Skip Integrity",
                     font=("Segoe UI Bold", 13), text_color=TEXT_TITLE).pack(anchor="w")
        ctk.CTkLabel(c3_head, text="Parallel multi-threaded chunk downloading with zero-loss fragment integrity.",
                     font=("Segoe UI", 10), text_color=TEXT_MUTED).pack(anchor="w")

        c3_body = ctk.CTkFrame(card_perf, fg_color="transparent")
        c3_body.pack(fill="x", padx=16, pady=(0, 14))

        p_row = ctk.CTkFrame(c3_body, fg_color="transparent")
        p_row.pack(fill="x", pady=4)

        p_col1 = ctk.CTkFrame(p_row, fg_color="transparent")
        p_col1.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkLabel(p_col1, text="Concurrent Fragment Threads:", font=("Segoe UI Semibold", 11),
                     text_color=TEXT_MAIN).pack(anchor="w", pady=(0, 4))
        self.set_threads_opt = ctk.CTkOptionMenu(
            p_col1, values=["4 threads", "8 threads", "16 threads (Fast)", "24 threads (Ultra)", "32 threads"],
            height=30, corner_radius=6, fg_color=BG_CARD_INNER,
            button_color="#132247", button_hover_color="#1D356A",
            dropdown_fg_color=BG_CARD, text_color=TEXT_TITLE
        )
        cur_threads = self.cfg.get("concurrent_fragments", 16)
        self.set_threads_opt.set(f"{cur_threads} threads" if f"{cur_threads} threads" in ["4 threads", "8 threads", "32 threads"] else "16 threads (Fast)")
        self.set_threads_opt.pack(fill="x")

        p_col2 = ctk.CTkFrame(p_row, fg_color="transparent")
        p_col2.pack(side="left", fill="x", expand=True, padx=(10, 10))
        ctk.CTkLabel(p_col2, text="Connection Socket Timeout:", font=("Segoe UI Semibold", 11),
                     text_color=TEXT_MAIN).pack(anchor="w", pady=(0, 4))
        self.set_timeout_opt = ctk.CTkOptionMenu(
            p_col2, values=["10 seconds", "15 seconds", "20 seconds (Default)", "30 seconds", "60 seconds"],
            height=30, corner_radius=6, fg_color=BG_CARD_INNER,
            button_color="#132247", button_hover_color="#1D356A",
            dropdown_fg_color=BG_CARD, text_color=TEXT_TITLE
        )
        cur_to = self.cfg.get("socket_timeout", 20)
        self.set_timeout_opt.set(f"{cur_to} seconds (Default)" if cur_to == 20 else f"{cur_to} seconds")
        self.set_timeout_opt.pack(fill="x")

        p_col3 = ctk.CTkFrame(p_row, fg_color="transparent")
        p_col3.pack(side="left", fill="x", expand=True, padx=(10, 0))
        ctk.CTkLabel(p_col3, text="Max Fragment Retries:", font=("Segoe UI Semibold", 11),
                     text_color=TEXT_MAIN).pack(anchor="w", pady=(0, 4))
        self.set_retries_opt = ctk.CTkOptionMenu(
            p_col3, values=["5 retries", "10 retries", "20 retries (Default)", "50 retries"],
            height=30, corner_radius=6, fg_color=BG_CARD_INNER,
            button_color="#132247", button_hover_color="#1D356A",
            dropdown_fg_color=BG_CARD, text_color=TEXT_TITLE
        )
        cur_ret = self.cfg.get("max_retries", 20)
        self.set_retries_opt.set(f"{cur_ret} retries (Default)" if cur_ret == 20 else f"{cur_ret} retries")
        self.set_retries_opt.pack(fill="x")

        self.sw_antiskip = ctk.CTkSwitch(
            c3_body, text="Anti-Skip Protection: Guarantees 100% complete frames (Prevents video freeze & desync bugs)",
            font=("Segoe UI", 11), progress_color=ACCENT_BLUE
        )
        if self.cfg.get("anti_skip", True):
            self.sw_antiskip.select()
        else:
            self.sw_antiskip.deselect()
        self.sw_antiskip.pack(anchor="w", pady=(10, 2))

        # Section 4: FFmpeg Transcoder & Tools
        card_tools = ctk.CTkFrame(scroll, corner_radius=10, fg_color=BG_CARD,
                                  border_color=BORDER_CARD, border_width=1)
        card_tools.pack(fill="x", pady=(0, 12))

        c4_head = ctk.CTkFrame(card_tools, fg_color="transparent")
        c4_head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(c4_head, text="🔧  Transcoder & System Tools", font=("Segoe UI Bold", 13),
                     text_color=TEXT_TITLE).pack(anchor="w")

        c4_body = ctk.CTkFrame(card_tools, fg_color="transparent")
        c4_body.pack(fill="x", padx=16, pady=(0, 14))

        self.sw_ffmpeg = ctk.CTkSwitch(
            c4_body, text="Lossless FFmpeg Direct Remux (Merge audio/video/subtitles without re-encoding)",
            font=("Segoe UI", 11), progress_color=ACCENT_BLUE
        )
        if self.cfg.get("auto_remux", True):
            self.sw_ffmpeg.select()
        else:
            self.sw_ffmpeg.deselect()
        self.sw_ffmpeg.pack(anchor="w", pady=4)

        ffmpeg_status = "✓ Detected & Linked" if find_ffmpeg_dir() else "⚠ Not Found (Will auto-download on first run)"
        lbl_ffmpeg_status = ctk.CTkLabel(
            c4_body, text=f"FFmpeg Status: {ffmpeg_status}",
            font=("Segoe UI", 10), text_color=ACCENT_GREEN if find_ffmpeg_dir() else ACCENT_AMBER
        )
        lbl_ffmpeg_status.pack(anchor="w", pady=(2, 4))

        # Section 5: Notifications & Feedback
        card_notif = ctk.CTkFrame(scroll, corner_radius=10, fg_color=BG_CARD,
                                  border_color=BORDER_CARD, border_width=1)
        card_notif.pack(fill="x", pady=(0, 14))

        c5_head = ctk.CTkFrame(card_notif, fg_color="transparent")
        c5_head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(c5_head, text="🔔  Notifications & System Alerts", font=("Segoe UI Bold", 13),
                     text_color=TEXT_TITLE).pack(anchor="w")

        c5_body = ctk.CTkFrame(card_notif, fg_color="transparent")
        c5_body.pack(fill="x", padx=16, pady=(0, 14))

        self.sw_sound = ctk.CTkSwitch(
            c5_body, text="Play audio chime notification when download queue finishes",
            font=("Segoe UI", 11), progress_color=ACCENT_BLUE
        )
        if self.cfg.get("sound_notification", True):
            self.sw_sound.select()
        else:
            self.sw_sound.deselect()
        self.sw_sound.pack(anchor="w", pady=4)

        # Save and Feedback Action Bar
        action_bar = ctk.CTkFrame(scroll, fg_color="transparent")
        action_bar.pack(fill="x", pady=(0, 20))

        btn_save = ctk.CTkButton(
            action_bar, text="💾   Save & Apply Settings", font=("Segoe UI Bold", 13),
            height=42, width=220, corner_radius=8, fg_color=ACCENT_BLUE,
            hover_color=ACCENT_BLUE_HOVER, text_color="#FFFFFF", command=self._save_settings_from_tab
        )
        btn_save.pack(side="left", padx=(0, 10))

        btn_reset = ctk.CTkButton(
            action_bar, text="↺  Reset Defaults", font=("Segoe UI", 11),
            height=42, width=130, corner_radius=8, fg_color=ACCENT_BLUE_LIGHT,
            hover_color="#1D356A", border_color=BORDER_CARD, border_width=1,
            text_color=TEXT_MAIN, command=self._reset_default_settings
        )
        btn_reset.pack(side="left", padx=(0, 14))

        self.lbl_settings_status = ctk.CTkLabel(action_bar, text="", font=("Segoe UI Semibold", 11),
                                               text_color=ACCENT_GREEN)
        self.lbl_settings_status.pack(side="left")

    def _save_settings_from_tab(self):
        # Parse inputs
        path = self.path_var.get().strip()
        audio = self.set_audio_seg.get().lower()
        quality_raw = self.set_quality_opt.get().replace("p", "").split()[0]

        fmt_str = self.set_format_opt.get()
        fmt = "mp4"
        if "mkv" in fmt_str:
            fmt = "mkv"
        elif "ts" in fmt_str:
            fmt = "ts"

        threads_str = self.set_threads_opt.get().split()[0]
        threads = int(threads_str) if threads_str.isdigit() else 16

        to_str = self.set_timeout_opt.get().split()[0]
        timeout = int(to_str) if to_str.isdigit() else 20

        ret_str = self.set_retries_opt.get().split()[0]
        retries = int(ret_str) if ret_str.isdigit() else 20

        self.cfg.update({
            "path": path,
            "audio": audio,
            "quality": quality_raw,
            "output_format": fmt,
            "create_subfolder": bool(self.sw_subfolder.get()),
            "clean_cache": bool(self.sw_cleancache.get()),
            "concurrent_fragments": threads,
            "anti_skip": bool(self.sw_antiskip.get()),
            "socket_timeout": timeout,
            "max_retries": retries,
            "auto_remux": bool(self.sw_ffmpeg.get()),
            "sound_notification": bool(self.sw_sound.get()),
        })
        save_settings(self.cfg)

        # Sync Downloads tab widgets
        self.seg_audio.set(audio.upper())
        self.audio_var.set(audio)
        q_label = quality_raw + "p" if not quality_raw.endswith("p") else quality_raw
        self.opt_quality.set(q_label)
        self.quality_var.set(quality_raw)
        self.lbl_disk_sidebar.configure(text=f"💾 Free: {get_disk_free(path)}")

        self.lbl_settings_status.configure(text="✓ Settings saved and applied successfully!")
        self.root.after(3000, lambda: self.lbl_settings_status.configure(text=""))
        self._log("Settings updated and saved to settings.json.")

    def _reset_default_settings(self):
        self.sw_subfolder.deselect()
        self.sw_cleancache.select()
        self.set_audio_seg.set("SUB")
        self.set_quality_opt.set("1080p")
        self.set_format_opt.set(".mp4 (Universal H.264)")
        self.set_threads_opt.set("16 threads (Fast)")
        self.sw_antiskip.select()
        self.set_timeout_opt.set("20 seconds (Default)")
        self.set_retries_opt.set("20 retries (Default)")
        self.sw_ffmpeg.select()
        self.sw_sound.select()
        self._save_settings_from_tab()

    # -------------------------------------------------------- Tab 3: About ---
    def _build_about_tab(self):
        scroll = ctk.CTkScrollableFrame(self.frame_about, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # 1. Hero Showcase Card
        hero_card = ctk.CTkFrame(scroll, corner_radius=12, fg_color=BG_CARD,
                                 border_color=BORDER_CARD, border_width=1)
        hero_card.pack(fill="x", pady=(4, 14))

        hero_inner = ctk.CTkFrame(hero_card, fg_color="transparent")
        hero_inner.pack(fill="both", expand=True, padx=20, pady=20)

        title_row = ctk.CTkFrame(hero_inner, fg_color="transparent")
        title_row.pack(fill="x")

        lbl_h_title = ctk.CTkLabel(title_row, text="▲ ANIKOTO Download Manager Pro",
                                   font=("Segoe UI Black", 22), text_color=ACCENT_CYAN_LIGHT)
        lbl_h_title.pack(side="left")

        lbl_badge = ctk.CTkLabel(title_row, text="v1.0.0 RELEASE", font=("Segoe UI Bold", 10),
                                 text_color="#FFFFFF", fg_color=ACCENT_BLUE,
                                 corner_radius=6, padx=8, pady=3)
        lbl_badge.pack(side="left", padx=(12, 0))

        lbl_author = ctk.CTkLabel(title_row, text="Made by : MYG", font=("Segoe UI Bold", 10),
                                  text_color="#FFFFFF", fg_color="#0284C7",
                                  corner_radius=6, padx=8, pady=3)
        lbl_author.pack(side="left", padx=(8, 0))

        lbl_h_sub = ctk.CTkLabel(
            hero_inner,
            text="High-Performance Batch Anime Downloader with Zero-Loss Stream Decryption & Anti-Skip Engine",
            font=("Segoe UI", 12), text_color=TEXT_MUTED
        )
        lbl_h_sub.pack(anchor="w", pady=(4, 0))

        # 2. System Diagnostics & Engine Status Card
        card_diag = ctk.CTkFrame(scroll, corner_radius=10, fg_color=BG_CARD,
                                 border_color=BORDER_CARD, border_width=1)
        card_diag.pack(fill="x", pady=(0, 14))

        d_head = ctk.CTkFrame(card_diag, fg_color="transparent")
        d_head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(d_head, text="🖥  Engine & Diagnostics Status", font=("Segoe UI Bold", 13),
                     text_color=TEXT_TITLE).pack(anchor="w")

        d_body = ctk.CTkFrame(card_diag, fg_color="transparent")
        d_body.pack(fill="x", padx=16, pady=(0, 14))

        grid = ctk.CTkFrame(d_body, fg_color="transparent")
        grid.pack(fill="x")

        diag_items = [
            ("Python Core", f"{sys.version.split()[0]} ({sys.platform})"),
            ("GUI Framework", "CustomTkinter 6.0.0 (Fluent Oceanic Dark)"),
            ("Stream Decryption", "AES-128-CBC Hardware Acceleration"),
            ("HLS Stream Engine", "yt-dlp Core Integration"),
            ("FFmpeg Remuxer", "Active & Integrated" if find_ffmpeg_dir() else "Ready on-demand"),
            ("Operating System", "Windows 10 / 11 64-bit"),
            ("Active Storage", self.cfg.get("path", "")),
            ("Disk Free Space", get_disk_free(self.cfg.get("path", ""))),
        ]

        for i, (k, v) in enumerate(diag_items):
            row = i // 2
            col = i % 2
            cell = ctk.CTkFrame(grid, fg_color=BG_CARD_INNER, corner_radius=6)
            cell.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            grid.columnconfigure(col, weight=1)

            ctk.CTkLabel(cell, text=k, font=("Segoe UI Semibold", 10),
                         text_color=TEXT_MUTED).pack(anchor="w", padx=10, pady=(6, 1))
            ctk.CTkLabel(cell, text=v, font=("Segoe UI Bold", 11),
                         text_color=TEXT_TITLE).pack(anchor="w", padx=10, pady=(0, 6))

        # 3. Architectural Highlights Card
        card_arch = ctk.CTkFrame(scroll, corner_radius=10, fg_color=BG_CARD,
                                 border_color=BORDER_CARD, border_width=1)
        card_arch.pack(fill="x", pady=(0, 14))

        a_head = ctk.CTkFrame(card_arch, fg_color="transparent")
        a_head.pack(fill="x", padx=16, pady=(14, 8))
        ctk.CTkLabel(a_head, text="⚡  Architectural Highlights & Capabilities", font=("Segoe UI Bold", 13),
                     text_color=TEXT_TITLE).pack(anchor="w")

        a_body = ctk.CTkFrame(card_arch, fg_color="transparent")
        a_body.pack(fill="x", padx=16, pady=(0, 14))

        features = [
            ("⚡ Parallel Multi-Threaded Chunk Acceleration",
             "Downloads video fragments concurrently across multiple parallel streams for maximum bandwidth utilization."),
            ("🛡 Anti-Skip Zero Corruption Engine",
             "Validates every single TS packet before remuxing to eliminate video stutters, missing seconds, and audio desync."),
            ("🎧 Triple Audio Stream Switcher",
             "Seamlessly toggle between Japanese Subbed (SUB), English Dubbed (DUB), and Hardsubbed (HSUB)."),
            ("🎬 Lossless Fast Remuxing",
             "Merges audio and video losslessly into universal MP4 files in seconds without quality degradation."),
            ("📁 Direct Media Player Integration",
             "Double click or right click any episode to launch instantly in VLC, MPC-HC, or default Windows player."),
        ]

        for title, desc in features:
            f_box = ctk.CTkFrame(a_body, fg_color="transparent")
            f_box.pack(fill="x", pady=4)
            ctk.CTkLabel(f_box, text=title, font=("Segoe UI Semibold", 11),
                         text_color=ACCENT_CYAN_LIGHT).pack(anchor="w")
            ctk.CTkLabel(f_box, text=desc, font=("Segoe UI", 10),
                         text_color=TEXT_MUTED).pack(anchor="w")

        # 4. Legal & Disclaimer Card
        card_legal = ctk.CTkFrame(scroll, corner_radius=10, fg_color=BG_CARD,
                                  border_color=BORDER_CARD, border_width=1)
        card_legal.pack(fill="x", pady=(0, 16))

        l_head = ctk.CTkFrame(card_legal, fg_color="transparent")
        l_head.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(l_head, text="⚖  Disclaimer & Open-Source Archival", font=("Segoe UI Bold", 12),
                     text_color=TEXT_TITLE).pack(anchor="w")

        l_body = ctk.CTkFrame(card_legal, fg_color="transparent")
        l_body.pack(fill="x", padx=16, pady=(0, 12))
        ctk.CTkLabel(
            l_body,
            text="This application is designed strictly for personal offline archiving and anime study.\n"
                 "Anikoto Downloader does not host media or collect user telemetry. Made by : MYG with ❤️ for the anime community.",
            font=("Segoe UI", 10), text_color=TEXT_MUTED, justify="left"
        ).pack(anchor="w")

    # ---------------------------------------------------- Activity Log Window ---
    def _open_activity_log(self):
        if self._log_window is not None and self._log_window.winfo_exists():
            self._log_window.lift()
            self._log_window.focus()
            return

        w = ctk.CTkToplevel(self.root)
        w.title("Activity & Engine Log — Anikoto Pro")
        w.geometry("680x420")
        w.configure(fg_color=BG_ROOT)
        self._log_window = w

        # Header
        head = ctk.CTkFrame(w, fg_color=BG_CARD, height=44, corner_radius=0)
        head.pack(fill="x")

        lbl = ctk.CTkLabel(head, text="📋  Real-time Scraper & Transcoder Log",
                           font=("Segoe UI Bold", 13), text_color=TEXT_TITLE)
        lbl.pack(side="left", padx=16, pady=10)

        # Buttons in header
        btn_close = ctk.CTkButton(head, text="Close", width=70, height=28, corner_radius=6,
                                  fg_color=ACCENT_BLUE_LIGHT, hover_color="#1D356A", text_color=TEXT_MAIN,
                                  command=w.destroy)
        btn_close.pack(side="right", padx=12, pady=8)

        btn_copy = ctk.CTkButton(head, text="Copy All", width=75, height=28, corner_radius=6,
                                 fg_color=ACCENT_BLUE, hover_color=ACCENT_BLUE_HOVER, text_color="#FFFFFF",
                                 command=self._copy_log)
        btn_copy.pack(side="right", padx=4, pady=8)

        btn_clear = ctk.CTkButton(head, text="Clear", width=65, height=28, corner_radius=6,
                                  fg_color=ACCENT_BLUE_LIGHT, hover_color="#1D356A", text_color=TEXT_MAIN,
                                  command=self._clear_log)
        btn_clear.pack(side="right", padx=4, pady=8)

        # Text area
        self.log_textbox = ctk.CTkTextbox(
            w, fg_color=BG_CARD_INNER, text_color=TEXT_MAIN,
            font=("Consolas", 10), corner_radius=8, border_color=BORDER_CARD, border_width=1
        )
        self.log_textbox.pack(fill="both", expand=True, padx=12, pady=12)

        # Insert previous messages
        for msg in self._log_messages:
            self.log_textbox.insert("end", msg + "\n")
        self.log_textbox.see("end")

    def _copy_log(self):
        full_text = "\n".join(self._log_messages)
        self.root.clipboard_clear()
        self.root.clipboard_append(full_text)

    def _clear_log(self):
        self._log_messages.clear()
        if hasattr(self, "log_textbox") and self.log_textbox and self.log_textbox.winfo_exists():
            self.log_textbox.delete("1.0", "end")

    def _log(self, msg):
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {msg}"
        self._log_messages.append(entry)
        if len(self._log_messages) > 1000:
            self._log_messages = self._log_messages[-1000:]
        if hasattr(self, "log_textbox") and self.log_textbox and self.log_textbox.winfo_exists():
            try:
                self.log_textbox.insert("end", entry + "\n")
                self.log_textbox.see("end")
            except Exception:
                pass

    # -------------------------------------------------- Shortcuts & Clipboard ---
    def _setup_shortcuts(self):
        self.root.bind("<Control-o>", lambda e: self._open_save_folder())
        self.root.bind("<Control-a>", lambda e: self._select_all_visible())
        self.root.bind("<Control-d>", lambda e: self._select_none())
        self.root.bind("<Control-l>", lambda e: self._open_activity_log())

    def _paste_clipboard(self):
        try:
            clip = self.root.clipboard_get().strip()
            if clip:
                self.url_var.set(clip)
                self._load_episodes()
        except Exception:
            pass

    def _browse_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.path_var.get())
        if chosen:
            self.path_var.set(chosen)
            self.lbl_disk_sidebar.configure(text=f"💾 Free: {get_disk_free(chosen)}")
            self.cfg["path"] = chosen
            save_settings(self.cfg)
            if self._episodes:
                self._refresh_table()

    def _on_audio_changed(self, value):
        self.audio_var.set(value.lower())
        self.cfg["audio"] = value.lower()
        save_settings(self.cfg)
        if hasattr(self, "set_audio_seg"):
            self.set_audio_seg.set(value.upper())
        if self._episodes:
            self._refresh_table()

    def _on_quality_changed(self, value):
        clean_q = value.rstrip("p")
        self.quality_var.set(clean_q)
        self.cfg["quality"] = clean_q
        save_settings(self.cfg)
        if hasattr(self, "set_quality_opt"):
            self.set_quality_opt.set(clean_q + "p")
        if self._episodes:
            self._refresh_table()

    def _open_save_folder(self):
        folder = self.path_var.get().strip()
        if os.path.exists(folder):
            try:
                os.startfile(folder)
            except Exception as e:
                messagebox.showerror("Error", f"Could not open directory: {e}")
        else:
            messagebox.showwarning("Not Found", f"Directory does not exist:\n{folder}")

    def _play_selected_file(self):
        selected_num = self._get_selected_tree_item_num()
        if selected_num is None:
            messagebox.showinfo("Select Episode", "Click an episode row to play.")
            return

        ep = next((e for e in self._episodes if e['number'] == selected_num), None)
        title = ep['title'] if ep else None
        target = find_episode_file(self.path_var.get(), selected_num, title)
        if target and os.path.exists(target):
            try:
                os.startfile(target)
            except Exception as e:
                messagebox.showerror("Player Error", f"Could not launch file: {e}")
        else:
            messagebox.showinfo("Not Downloaded", f"Episode {selected_num} has not been downloaded yet.")

    def _delete_selected_episode(self):
        selected_num = self._get_selected_tree_item_num()
        if selected_num is None:
            messagebox.showinfo("Select Episode", "Click an episode row to delete.")
            return

        ep = next((e for e in self._episodes if e['number'] == selected_num), None)
        title = ep['title'] if ep else None
        target = find_episode_file(self.path_var.get(), selected_num, title)
        safe_title = sanitize_filename(title) if title else f"E{selected_num}"
        file_stem = f"E{selected_num} {safe_title}"
        cache_dir = os.path.join(self.path_var.get().strip(), ".anikoto_cache")

        if target and os.path.exists(target):
            fname = os.path.basename(target)
            if messagebox.askyesno("Delete Episode", f"Permanently delete file:\n{fname}\nfrom disk?"):
                try:
                    os.remove(target)
                    purge_episode_cache(cache_dir, file_stem)
                    if ep:
                        ep["status"] = "idle"
                        ep["size"] = 0
                    self._refresh_table()
                    self.lbl_disk_sidebar.configure(text=f"💾 Free: {get_disk_free(self.path_var.get())}")
                    self._log(f"Deleted {fname} from disk.")
                except Exception as e:
                    messagebox.showerror("Delete Error", f"Could not delete file:\n{e}")
        else:
            self._ticked.discard(selected_num)
            purge_episode_cache(cache_dir, file_stem)
            if ep:
                ep["status"] = "idle"
            self._refresh_table()
            self._log(f"Cleared state for Episode {selected_num}.")

    def _clear_completed_selection(self):
        done_set = self._get_existing_episode_nums()
        self._ticked = {num for num in self._ticked if num not in done_set}
        self._refresh_table()
        self._log("Cleared completed episodes from selection queue.")

    def _set_status(self, text, color=TEXT_MUTED):
        self.lbl_task_name.configure(text=text)

    # ---------------------------------------------------- Filter & Search ---
    def _on_filter_changed(self, value):
        self._active_filter = value

        pills = [
            ("All", self.pill_all),
            ("Queued", self.pill_queued),
            ("Downloading", self.pill_downloading),
            ("Completed", self.pill_completed),
            ("Failed", self.pill_failed),
        ]
        for name, btn in pills:
            if name == value:
                btn.configure(fg_color=ACCENT_BLUE, text_color="#FFFFFF", border_width=0)
            else:
                btn.configure(fg_color=BG_CARD, text_color=TEXT_MUTED, border_color=BORDER_CARD, border_width=1)

        self._refresh_table()

    def _on_search_changed(self):
        self._search_query = self.search_var.get().strip().lower()
        self._refresh_table()

    def _update_filter_badges(self):
        total = len(self._episodes)
        existing = self._get_existing_episode_nums()
        done_count = len(existing)
        waiting_count = len(self._ticked - existing)
        err_count = sum(1 for e in self._episodes if e.get("status") == "error")
        active_count = 1 if self._current_ep_num else 0

        # Update pill labels with dynamic counts
        self.pill_all.configure(text=f"⊞ All ({total})")
        self.pill_queued.configure(text=f"🕒 Queued ({waiting_count})")
        self.pill_downloading.configure(text=f"📥 Downloading ({active_count})")
        self.pill_completed.configure(text=f"✓ Completed ({done_count})")
        self.pill_failed.configure(text=f"⚠ Failed ({err_count})")

        self.lbl_counts.configure(text=f"{len(self._ticked)} ticked / {total} total ({done_count} on disk)")

        # Update anime pill stats if present
        if hasattr(self, "badge_selected") and self._episodes:
            self.badge_selected.configure(text=f"{len(self._ticked)} / {total} Selected")
            self.badge_audio.configure(text=self.audio_var.get().upper())
            self.badge_quality.configure(text=f"{self.quality_var.get()}p")
            self.badge_ep_count.configure(text=f"{total} EPISODES")

        # Update header checkbox icon
        if total > 0 and len(self._ticked) == total:
            self.tree.heading("pick", text="☑")
        elif len(self._ticked) > 0:
            self.tree.heading("pick", text="✔")
        else:
            self.tree.heading("pick", text="☐")

    # ------------------------------------------------------ Table Display ---
    def _get_existing_episode_nums(self):
        done = set()
        path = self.path_var.get()
        if not os.path.isdir(path):
            return done
        try:
            p = re.compile(r"^E(\d+)(?:[ ._-].*)?\.(mp4|mkv|webm)$", re.IGNORECASE)
            for name in os.listdir(path):
                m = p.match(name)
                if m:
                    fpath = os.path.join(path, name)
                    if os.path.isfile(fpath) and os.path.getsize(fpath) > 50_000:
                        done.add(int(m.group(1)))
        except OSError:
            pass
        return done

    def _toggle_select_all(self):
        if not self._episodes:
            return
        visible_nums = {int(item) for item in self.tree.get_children()}
        if visible_nums and visible_nums.issubset(self._ticked):
            self._ticked.difference_update(visible_nums)
        else:
            self._ticked.update(visible_nums)
        self._refresh_table()

    def _clear_series(self):
        self._episodes = []
        self._ticked.clear()
        self._current_ep_num = None
        self.url_var.set("")
        self._refresh_table()
        self._set_status("Ready", TEXT_MUTED)
        self._log("Series cleared from view.")

    def _refresh_table(self):
        # Empty state vs Populated table toggle
        if not self._episodes:
            if hasattr(self, "anime_pill"):
                self.anime_pill.pack_forget()
            self.table_frame.pack_forget()
            self.empty_state_frame.pack(fill="both", expand=True, pady=(8, 8))
            self._update_filter_badges()
            return

        self.empty_state_frame.pack_forget()
        if hasattr(self, "anime_pill"):
            self.anime_pill.pack(fill="x", padx=8, pady=(8, 4))
        self.table_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        for item in self.tree.get_children():
            self.tree.delete(item)

        existing = self._get_existing_episode_nums()
        fmt_str = f"{self.audio_var.get().upper()} · {self.quality_var.get()}p"

        displayed_index = 0
        for ep in self._episodes:
            num = ep["number"]
            title = ep["title"]
            is_done = num in existing
            is_active = (num == self._current_ep_num)
            is_queued = (num in self._ticked and not is_done and not is_active)
            is_error = (ep.get("status") == "error")

            # Apply Category Tab Filter
            if self._active_filter != "All":
                if self._active_filter == "Queued" and not is_queued:
                    continue
                if self._active_filter == "Downloading" and not is_active:
                    continue
                if self._active_filter == "Completed" and not is_done:
                    continue
                if self._active_filter == "Failed" and not is_error:
                    continue

            # Apply Search Filter
            if self._search_query:
                query = self._search_query
                match_num = f"ep {num}" in query or f"e{num}" in query or query == str(num)
                match_title = query in title.lower()
                if not (match_num or match_title):
                    continue

            # Build status and progress display
            is_ticked = (num in self._ticked)
            check_box = "☑" if is_ticked else "☐"

            if is_active:
                status_text = "⚡ Downloading"
                tag = "active"
                pct = ep.get("pct", 0)
                speed = ep.get("speed_str", "—")
                prog_str = f"{pct:.0f}% ({speed})"
                eta_str = ep.get("eta_str", "—")
            elif is_done:
                status_text = "✔ Completed"
                tag = "done"
                size = ep.get("size")
                if not size:
                    fpath = find_episode_file(self.path_var.get(), num, title)
                    size = os.path.getsize(fpath) if fpath else 0
                prog_str = format_bytes(size) if size else "100%"
                eta_str = "Done"
            elif is_error:
                status_text = "✖ Failed"
                tag = "error"
                prog_str = "Error"
                eta_str = "—"
            elif is_queued:
                status_text = "⏳ Queued"
                tag = "waiting"
                prog_str = "Ready"
                eta_str = "—"
            else:
                status_text = "○ Available"
                tag = "waiting"
                prog_str = "—"
                eta_str = "—"

            if is_ticked and not is_done and not is_active:
                row_tags = ("ticked",)
            elif is_active:
                row_tags = ("active",)
            elif is_done:
                row_tags = ("done",) + (("ticked",) if is_ticked else (("stripe",) if displayed_index % 2 == 0 else ()))
            elif is_error:
                row_tags = ("error",)
            else:
                row_tags = (tag,) + (("stripe",) if displayed_index % 2 == 0 else ())

            self.tree.insert("", tk.END, iid=str(num),
                             values=(check_box, f"{num:02d}", f"  {title}", fmt_str, status_text, prog_str, eta_str),
                             tags=row_tags)
            displayed_index += 1

        self._update_filter_badges()

    # ------------------------------------------------------ Click & Events ---
    def _on_tree_click(self, event):
        item = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if item:
            num = int(item)
            if col == "#1":
                self._toggle_tick(num)
                return "break"
            else:
                self.tree.selection_set(item)

    def _on_space_key(self, event):
        num = self._get_selected_tree_item_num()
        if num is not None:
            self._toggle_tick(num)
            return "break"

    def _on_tree_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        num = int(item)
        existing = self._get_existing_episode_nums()
        if num in existing:
            self._play_selected_file()
        else:
            if not self._is_downloading:
                self._ticked = {num}
                self._refresh_table()
                self._start_download()

    def _show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def _get_selected_tree_item_num(self):
        sel = self.tree.selection()
        if sel:
            try:
                return int(sel[0])
            except ValueError:
                return None
        return None

    def _toggle_tick(self, num):
        if num in self._ticked:
            self._ticked.discard(num)
        else:
            self._ticked.add(num)
        self._refresh_table()

    def _select_all_visible(self):
        for item in self.tree.get_children():
            self._ticked.add(int(item))
        self._refresh_table()

    def _select_none(self):
        self._ticked.clear()
        self._refresh_table()

    def _prompt_range_selector(self):
        w = ctk.CTkToplevel(self.root)
        w.title("Select Range")
        w.geometry("340x160")
        w.configure(fg_color=BG_CARD)
        w.transient(self.root)
        w.grab_set()

        lbl = ctk.CTkLabel(w, text="Enter episode range (e.g. 1-12, 15, 20-25):",
                           font=("Segoe UI", 11), text_color=TEXT_TITLE)
        lbl.pack(pady=(16, 8))

        entry = ctk.CTkEntry(w, width=220, height=32, corner_radius=6,
                             fg_color=BG_CARD_INNER, border_color=BORDER_CARD,
                             font=("Segoe UI", 11), text_color=TEXT_TITLE)
        entry.pack(pady=4)
        entry.focus()

        def apply_range():
            text = entry.get().strip()
            if text:
                text = text.replace('–', '-').replace('—', '-').replace('to', '-')
                parsed_nums = set()
                parts = text.split(",")
                for p in parts:
                    p = p.strip()
                    if "-" in p:
                        sub = p.split("-")
                        if len(sub) == 2 and sub[0].strip().isdigit() and sub[1].strip().isdigit():
                            start, end = int(sub[0].strip()), int(sub[1].strip())
                            parsed_nums.update(range(min(start, end), max(start, end) + 1))
                    elif p.isdigit():
                        parsed_nums.add(int(p))
                available_nums = {e["number"] for e in self._episodes}
                self._ticked.update(parsed_nums & available_nums)
                self._refresh_table()
            w.destroy()

        btn = ctk.CTkButton(w, text="Apply Range", width=120, height=32, corner_radius=6,
                            fg_color=ACCENT_BLUE, hover_color=ACCENT_BLUE_HOVER,
                            font=("Segoe UI Bold", 11), text_color="#FFFFFF", command=apply_range)
        btn.pack(pady=12)

    # Context menu commands
    def _context_download_single(self):
        num = self._get_selected_tree_item_num()
        if num is not None and not self._is_downloading:
            self._ticked = {num}
            self._refresh_table()
            self._start_download()

    def _context_check_selected(self):
        num = self._get_selected_tree_item_num()
        if num is not None:
            self._ticked.add(num)
            self._refresh_table()

    def _context_uncheck_selected(self):
        num = self._get_selected_tree_item_num()
        if num is not None:
            self._ticked.discard(num)
            self._refresh_table()

    def _context_copy_title(self):
        num = self._get_selected_tree_item_num()
        if num is not None:
            ep = next((e for e in self._episodes if e['number'] == num), None)
            if ep:
                self.root.clipboard_clear()
                self.root.clipboard_append(ep['title'])

    def _context_redownload_clean(self):
        """Restart download from 0%: purges partial cache for selected episode and re-downloads."""
        num = self._get_selected_tree_item_num()
        if num is not None and not self._is_downloading:
            ep = next((e for e in self._episodes if e['number'] == num), None)
            if ep:
                safe_title = sanitize_filename(ep['title'])
                file_stem = f"E{num} {safe_title}"
                cache_dir = os.path.join(self.path_var.get().strip(), ".anikoto_cache")
                purge_episode_cache(cache_dir, file_stem)
                self._log(f"Purged cached fragments for Episode {num} — restarting clean from 0%.")
            self._ticked = {num}
            self._refresh_table()
            self._start_download()

    def _start_fetch_spinner(self):
        self._fetch_spinner_active = True
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

        def _spin(idx=0):
            if not getattr(self, "_fetch_spinner_active", False):
                self.btn_fetch.configure(text="Fetch Episodes")
                return
            char = frames[idx % len(frames)]
            self.btn_fetch.configure(text=f"{char} Fetching...")
            self.root.after(80, lambda: _spin(idx + 1))

        _spin(0)

    # ----------------------------------------------------- Scraping & Load ---
    def _load_episodes(self):
        if self._is_downloading:
            return
        url = self.url_var.get().strip()
        if not url or not url.startswith("http"):
            messagebox.showwarning("Invalid URL", "Please paste a valid Anikoto watch page URL.")
            return

        self.btn_fetch.configure(state="disabled")
        self._start_fetch_spinner()
        self._set_status("Fetching series data...", ACCENT_BLUE)
        self.cfg["url"] = url
        save_settings(self.cfg)
        self._log(f"Fetching anime series from: {url}")

        threading.Thread(target=self._load_thread, args=(url,), daemon=True).start()

    def _load_thread(self, url):
        try:
            scraper = AnikotoScraper()
            video_id, domain = scraper.get_video_id(url)
            episodes = scraper.get_episodes(domain, video_id)
            series_info = scraper.parse_series_info(scraper.last_html)

            self.root.after(0, lambda: self._on_episodes_loaded(episodes, series_info))
        except Exception as e:
            self.root.after(0, lambda: self._set_status("Load failed", ACCENT_RED))
            self.root.after(0, lambda: self._log(f"Load error: {e}"))
            self.root.after(0, lambda: messagebox.showerror("Load Failed", f"Could not read anime watch page:\n{e}"))
        finally:
            self._fetch_spinner_active = False
            self.root.after(0, lambda: self.btn_fetch.configure(state="normal", text="Fetch Episodes"))

    def _on_episodes_loaded(self, episodes, series_info=None):
        self._episodes = episodes
        self._ticked.clear()
        title_str = (series_info.get("title") if series_info else None) or "Anime Series"
        if hasattr(self, "lbl_anime_name"):
            self.lbl_anime_name.configure(text=title_str)
        self._refresh_table()
        existing = self._get_existing_episode_nums()
        self._set_status(f"Loaded {len(episodes)} episodes", ACCENT_GREEN)
        self._log(f"Successfully loaded '{title_str}': {len(episodes)} episodes ({len(existing)} already on disk).")

    # ------------------------------------------------------- Download Core ---
    def _download_all_remaining(self):
        """Queue and download all episodes not yet completed on disk."""
        if self._is_downloading:
            return
        if not self._episodes:
            messagebox.showwarning("No Episodes", "Please fetch episodes first.")
            return
        existing = self._get_existing_episode_nums()
        remaining = [e['number'] for e in self._episodes if e['number'] not in existing]
        if not remaining:
            self._set_status("All completed", ACCENT_GREEN)
            messagebox.showinfo("All Downloaded", "All episodes in this series are already on disk!")
            return

        self._ticked = set(remaining)
        self._refresh_table()
        self._log(f"Queued all remaining {len(remaining)} episodes.")
        self._start_download()

    def _start_download(self):
        if self._is_downloading:
            return
        if not self._episodes:
            messagebox.showwarning("No Episodes", "Please fetch episodes first.")
            return
        if not self._ticked:
            messagebox.showwarning("No Selection", "Please tick at least one episode to download.")
            return
        save_path = self.path_var.get().strip()
        if not os.path.isdir(save_path):
            try:
                os.makedirs(save_path, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Invalid Directory", f"Cannot create save directory:\n{e}")
                return

        self._is_downloading = True
        self.tb_start.configure(state="disabled")
        self.tb_all.configure(state="disabled")
        self.tb_stop.configure(state="normal")
        self.btn_fetch.configure(state="disabled")

        self._queue = sorted(self._ticked)
        self._queue_pos = -1
        ep_range = list(self._queue)
        url = self.url_var.get().strip()
        audio = self.audio_var.get()
        quality = self.quality_var.get()

        self.cfg.update({
            "url": url,
            "path": save_path,
            "audio": audio,
            "quality": quality,
        })
        save_settings(self.cfg)
        self.downloader = Downloader()

        total = len(self._queue)
        self._set_status(f"Downloading queue ({total} episodes)...", ACCENT_AMBER)
        self.lbl_task_name.configure(text=f"Queue initialized ({total} items)")
        self._log(f"=== Starting batch download: {total} episode(s) ===")

        threading.Thread(target=self._download_worker,
                         args=(url, save_path, audio, "all", quality, ep_range),
                         daemon=True).start()

    def _download_worker(self, url, save_path, audio, source_filter, quality, ep_range):
        try:
            _last_progress_time = [0.0]

            def log_hook(msg):
                self.root.after(0, lambda: self._log(msg))

            def progress_hook(d):
                now = time.time()
                status = d.get("status")
                if status == "finished" or (now - _last_progress_time[0]) >= 0.08:
                    _last_progress_time[0] = now
                    info = {
                        "status": status,
                        "downloaded_bytes": d.get("downloaded_bytes", 0),
                        "total_bytes": d.get("total_bytes") or d.get("total_bytes_estimate", 0),
                        "speed": d.get("speed") or 0,
                        "eta": d.get("eta"),
                    }
                    self.root.after(0, lambda data=info: self._handle_progress(data))

            def ep_hook(num, status, meta):
                self.root.after(0, lambda: self._handle_ep_state(num, status, meta))

            self.downloader.download(
                url, save_path, audio, source_filter, quality, ep_range,
                progress_hook=progress_hook,
                log_hook=log_hook,
                ep_hook=ep_hook,
                options=self.cfg
            )

            if self.downloader and self.downloader.was_cancelled:
                self.root.after(0, lambda: self._set_status("Stopped", TEXT_MUTED))
                self.root.after(0, lambda: self._log("=== Download cancelled by user ==="))
            else:
                self.root.after(0, lambda: self._set_status("All tasks finished", ACCENT_GREEN))
                self.root.after(0, lambda: self._log("=== All queued downloads complete! ==="))
                if self.cfg.get("sound_notification", True):
                    try:
                        import winsound
                        winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    except Exception:
                        pass
        except Exception as e:
            self.root.after(0, lambda: self._set_status("Error occurred", ACCENT_RED))
            self.root.after(0, lambda: self._log(f"Critical error: {e}"))
        finally:
            self.root.after(0, self._on_download_finished)

    def _smooth_progress_set(self, target_val):
        target = max(0.0, min(1.0, float(target_val)))
        curr = self.file_progress.get()
        if target < curr or abs(target - curr) < 0.005:
            self.file_progress.set(target)
        else:
            self.file_progress.set(curr + (target - curr) * 0.45)

    def _handle_progress(self, d):
        if not self._is_downloading:
            return
        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes", 0)
            pct = min(downloaded / total * 100, 100) if total > 0 else 0
            speed = d.get("speed") or 0
            speed_str = f"{speed / 1024 / 1024:.2f} MB/s" if speed else "connecting…"
            eta = d.get("eta")
            eta_str = f"{int(eta)//60}:{int(eta)%60:02d}" if (eta is not None and eta >= 0) else "—"

            # Smoothly glide progress bar
            self._smooth_progress_set(pct / 100.0)
            self.lbl_task_name.configure(text=f"Downloading Episode {self._current_ep_num} ({pct:.1f}%)")
            self.lbl_task_metrics.configure(text=f"{format_bytes(downloaded)} / {format_bytes(total)}")
            self.lbl_speed_eta.configure(text=f"Speed: {speed_str}   •   ETA: {eta_str}")

            # Queue progress
            if self._queue:
                q_idx = max(self._queue_pos, 0)
                overall_pct = (q_idx + pct / 100) / len(self._queue) * 100
                self.lbl_counts.configure(text=f"Queue {q_idx + 1} of {len(self._queue)} ({overall_pct:.0f}% total)")

            # Update in-memory episode object
            for ep in self._episodes:
                if ep["number"] == self._current_ep_num:
                    ep["pct"] = pct
                    ep["speed_str"] = speed_str
                    ep["eta_str"] = eta_str
                    break

            # Update treeview row directly
            if self._current_ep_num and self.tree.exists(str(self._current_ep_num)):
                vals = list(self.tree.item(str(self._current_ep_num), "values"))
                if len(vals) >= 7:
                    vals[4] = f"⚡ {pct:.0f}%"
                    vals[5] = f"{format_bytes(downloaded)} ({speed_str})"
                    vals[6] = eta_str
                    self.tree.item(str(self._current_ep_num), values=vals)

        elif status == "finished":
            self.lbl_task_name.configure(text=f"Merging & Embedding Subtitles (Ep {self._current_ep_num})...")
            self.lbl_speed_eta.configure(text="FFmpeg processing...")
            self.file_progress.set(1.0)

    def _handle_ep_state(self, num, status, meta):
        if status == "start":
            self._current_ep_num = num
            if num in self._queue:
                self._queue_pos = self._queue.index(num)
            self._set_status(f"Downloading Episode {num}...", ACCENT_AMBER)
            self._refresh_table()
        elif status == "done":
            size = meta.get("size", 0)
            for ep in self._episodes:
                if ep["number"] == num:
                    ep["status"] = "done"
                    ep["size"] = size
                    break
            self._refresh_table()
            self._log(f"Episode {num} completed successfully ({format_bytes(size)}).")
        elif status == "error":
            for ep in self._episodes:
                if ep["number"] == num:
                    ep["status"] = "error"
                    break
            self._refresh_table()
        elif status == "cancelled":
            self._refresh_table()

    def _cancel_download(self):
        if self.downloader:
            self.downloader.cancel()
            self.tb_stop.configure(state="disabled")
            self._set_status("Stopping download...", ACCENT_RED)
            self.lbl_task_name.configure(text="Stopping gracefully (preserving partial download)...")
            self._log("Cancel signal sent to worker...")

    def _on_download_finished(self):
        self._is_downloading = False
        self._current_ep_num = None
        if self.cfg.get("clean_cache", True):
            cleanup_cache_if_empty(os.path.join(self.path_var.get().strip(), ".anikoto_cache"))
        self.tb_start.configure(state="normal")
        self.tb_all.configure(state="normal")
        self.tb_stop.configure(state="disabled")
        self.btn_fetch.configure(state="normal")
        self.file_progress.set(0)
        self.lbl_speed_eta.configure(text="")
        self.lbl_task_metrics.configure(text="")
        self.lbl_disk_sidebar.configure(text=f"💾 Free: {get_disk_free(self.path_var.get())}")
        self._refresh_table()

    def _on_close(self):
        self.cfg["geometry"] = self.root.geometry()
        save_settings(self.cfg)

        if self._is_downloading:
            if not messagebox.askyesno("Quit?", "A download is currently running.\nStop and exit?"):
                return
            if self.downloader:
                self.downloader.cancel()
        cleanup_cache_if_empty(os.path.join(self.path_var.get().strip(), ".anikoto_cache"))
        self.root.destroy()


# ------------------------------------------------------------- Main ---
def main():
    root = ctk.CTk()
    app = AnikotoDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
