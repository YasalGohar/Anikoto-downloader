"""
Anikoto Downloader — Professional IDM-Style Edition (PySide6 / Qt)
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

import io

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
        os.path.join(r"d:\Anikoto Downloader", "assets", name),
        os.path.join(r"d:\Anikoto Downloader", name),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None



# =====================================================================
#                      PySide6 (Qt) USER INTERFACE
#  Qt draws the whole window as one surface and lays it out natively, so
#  maximize / minimize / restore are smooth. All the motion in the app
#  (tab fades, button hovers, sliding audio selector, switches, progress
#  bar, spotlight crossfade) is real Qt animation.
# =====================================================================
try:
    from PySide6.QtCore import (Qt, QObject, Signal, QTimer, QPropertyAnimation, QVariantAnimation,
                                QEasingCurve, QRectF, QPointF, QSize, QAbstractTableModel,
                                QModelIndex, QUrl, QPoint)
    from PySide6.QtGui import (QColor, QPainter, QPainterPath, QPixmap, QFont, QLinearGradient,
                               QPen, QIcon, QDesktopServices, QKeySequence, QShortcut,
                               QFontMetrics, QCursor)
    from PySide6.QtWidgets import (QApplication, QWidget, QMainWindow, QFrame, QLabel, QPushButton,
                                   QLineEdit, QComboBox, QVBoxLayout, QHBoxLayout, QGridLayout,
                                   QStackedWidget, QScrollArea, QTableView, QHeaderView,
                                   QAbstractItemView, QMenu, QFileDialog, QMessageBox, QDialog,
                                   QPlainTextEdit, QInputDialog, QGraphicsOpacityEffect,
                                   QSizePolicy)
except ImportError:
    print("Please install: pip install PySide6")
    raise

# --------------------------------------------- Deep Oceanic Navy Theme ---
BG_ROOT = "#080D1A"
BG_SIDEBAR = "#0B132B"
BG_CARD = "#0F1A36"
BG_CARD_INNER = "#091024"
BORDER_CARD = "#1B2A4E"
BORDER_ACCENT = "#0284C7"
ACCENT_BLUE = "#2563EB"
ACCENT_BLUE_HOVER = "#1D4ED8"
ACCENT_BLUE_LIGHT = "#132247"
ACCENT_CYAN = "#06B6D4"
ACCENT_CYAN_LIGHT = "#38BDF8"
ACCENT_GREEN = "#10B981"
ACCENT_AMBER = "#F59E0B"
ACCENT_RED = "#F43F5E"
ACCENT_RED_BG = "#451221"
ACCENT_RED_BORDER = "#881337"
TEXT_TITLE = "#FFFFFF"
TEXT_MAIN = "#E2E8F0"
TEXT_MUTED = "#94A3B8"
TEXT_FAINT = "#64748B"
ROW_ALT = "#0D1630"
ROW_SELECT = "#2563EB"
ROW_TICKED = "#172554"

UI_FONT = "Segoe UI"

APP_QSS = f"""
* {{ color: {TEXT_MAIN}; }}
QMainWindow, QDialog {{ background: {BG_ROOT}; }}
QToolTip {{ background: {BG_CARD}; color: {TEXT_TITLE}; border: 1px solid {BORDER_CARD}; padding: 4px 6px; }}
QLabel {{ background: transparent; }}
QFrame#card {{ background: {BG_CARD}; border: 1px solid {BORDER_CARD}; border-radius: 14px; }}
QFrame#inner {{ background: {BG_CARD_INNER}; border-radius: 8px; }}
QFrame#sidebar {{ background: {BG_SIDEBAR}; border-right: 1px solid {BORDER_CARD}; }}
QFrame#pill {{ background: #111E3E; border: 1px solid {BORDER_ACCENT}; border-radius: 8px; }}
QLineEdit {{ background: {BG_CARD_INNER}; border: 1px solid {BORDER_CARD}; border-radius: 8px;
            padding: 0 10px; color: {TEXT_TITLE}; selection-background-color: {ACCENT_BLUE}; }}
QLineEdit:focus {{ border: 1px solid {ACCENT_BLUE}; }}
QComboBox {{ background: {BG_CARD_INNER}; border: 1px solid {BORDER_CARD}; border-radius: 8px;
            padding: 0 10px; color: {TEXT_TITLE}; }}
QComboBox:hover {{ border: 1px solid #1D356A; }}
QComboBox:focus {{ border: 1px solid {ACCENT_BLUE}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {BG_CARD}; border: 1px solid {BORDER_CARD}; color: {TEXT_TITLE};
            selection-background-color: {ACCENT_BLUE}; outline: 0; padding: 3px; }}
QMenu {{ background: {BG_CARD}; border: 1px solid {BORDER_CARD}; padding: 5px; border-radius: 8px; }}
QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 6px; color: {TEXT_TITLE}; }}
QMenu::item:selected {{ background: {ACCENT_BLUE}; }}
QMenu::separator {{ height: 1px; background: {BORDER_CARD}; margin: 5px 8px; }}
QTableView {{ background: {BG_CARD_INNER}; border: none; border-radius: 8px; gridline-color: transparent;
            selection-background-color: {ROW_SELECT}; selection-color: #FFFFFF; outline: 0;
            font-size: 10pt; }}
QTableView::item {{ padding: 0 6px; border: none; }}
QHeaderView {{ background: #111D3B; border: none; }}
QHeaderView::section {{ background: #111D3B; color: {ACCENT_CYAN_LIGHT}; border: none; padding: 8px 6px;
            font-weight: 700; font-size: 10pt; }}
QHeaderView::section:hover {{ background: #1A2C59; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #0284C7; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT_CYAN_LIGHT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}
QScrollArea {{ background: transparent; border: none; }}
QPlainTextEdit {{ background: {BG_CARD_INNER}; border: 1px solid {BORDER_CARD}; border-radius: 8px;
            font-family: Consolas, "Cascadia Mono", monospace; font-size: 9.5pt; color: {TEXT_MAIN}; padding: 6px; }}
QMessageBox, QInputDialog {{ background: {BG_CARD}; }}
QMessageBox QLabel, QInputDialog QLabel {{ color: {TEXT_TITLE}; }}
QDialog QPushButton, QMessageBox QPushButton {{ background: {ACCENT_BLUE}; color: white; border: none;
            border-radius: 7px; padding: 7px 18px; min-width: 64px; font-weight: 600; }}
QDialog QPushButton:hover, QMessageBox QPushButton:hover {{ background: {ACCENT_BLUE_HOVER}; }}
"""


def qc(c, alpha=None):
    col = QColor(c)
    if alpha is not None:
        col.setAlphaF(alpha)
    return col


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t),
                  int(a.alpha() + (b.alpha() - a.alpha()) * t))


def font(size=10, weight=QFont.Normal):
    f = QFont(UI_FONT)
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


def make_anim(duration, start, end, on_value, curve=QEasingCurve.OutCubic):
    a = QVariantAnimation()
    a.setDuration(duration)
    a.setStartValue(start)
    a.setEndValue(end)
    a.setEasingCurve(curve)
    a.valueChanged.connect(on_value)
    return a


# --------------------------------------------------------- Animated Button ---
class Btn(QPushButton):
    """Flat button that fades smoothly between idle / hover / active colours."""
    V = {
        "primary": dict(bg=ACCENT_BLUE, hov=ACCENT_BLUE_HOVER, fg="#FFFFFF"),
        "ghost": dict(bg=BG_ROOT, hov="#1D356A", fg=TEXT_TITLE, bd=BORDER_CARD),
        "ghost2": dict(bg=BG_CARD, hov="#1A2C59", fg="#93C5FD", bd=BORDER_CARD),
        "danger": dict(bg=ACCENT_RED_BG, hov="#5F162B", fg="#FDA4AF", bd=ACCENT_RED_BORDER),
        "white": dict(bg="#FFFFFF", hov="#E2E8F0", fg="#0B1329"),
        "soft": dict(bg="#1E3A8A", hov="#2563EB", fg="#93C5FD"),
        "small": dict(bg=BG_ROOT, hov="#132247", fg=TEXT_MUTED, bd=BORDER_CARD),
        "logo": dict(bg="#111E3E", hov="#182A57", fg=ACCENT_CYAN_LIGHT, bd="#1E3A8A"),
        "tab": dict(bg=None, hov="#172648", fg=TEXT_MUTED, act=ACCENT_BLUE, actfg="#FFFFFF", hovfg=TEXT_TITLE),
    }

    def __init__(self, text="", variant="ghost", radius=8, height=34, size=10, bold=False,
                 minw=0, parent=None):
        super().__init__(text, parent)
        self.st = self.V[variant]
        self.radius = radius
        self._minw = minw
        self._h = 0.0       # hover progress
        self._a = 0.0       # active progress
        hov = qc(self.st["hov"])
        self._base = qc(self.st["bg"]) if self.st.get("bg") else QColor(hov.red(), hov.green(), hov.blue(), 0)
        self._hov = hov
        self.setFont(font(size, QFont.Bold if bold else QFont.DemiBold))
        self.setFixedHeight(height)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._ha = make_anim(150, 0.0, 1.0, self._set_h)
        self._aa = make_anim(220, 0.0, 1.0, self._set_a)

    def _set_h(self, v):
        self._h = float(v)
        self.update()

    def _set_a(self, v):
        self._a = float(v)
        self.update()

    def _go(self, anim, cur, target):
        anim.stop()
        anim.setStartValue(cur)
        anim.setEndValue(target)
        anim.start()

    def enterEvent(self, e):
        self._go(self._ha, self._h, 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._go(self._ha, self._h, 0.0)
        super().leaveEvent(e)

    def setActive(self, on, animate=True):
        target = 1.0 if on else 0.0
        if animate:
            self._go(self._aa, self._a, target)
        else:
            self._aa.stop()
            self._set_a(target)

    def sizeHint(self):
        w = QFontMetrics(self.font()).horizontalAdvance(self.text()) + 30
        return QSize(max(w, self._minw), self.height())

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        st = self.st
        col = mix(self._base, self._hov, self._h)
        if st.get("act"):
            col = mix(col, qc(st["act"]), self._a)
        if self.isDown():
            col = col.darker(115)
        p.setOpacity(1.0 if self.isEnabled() else 0.45)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if st.get("bd"):
            bc = qc(st["bd"])
            bc.setAlphaF(1.0 - self._a)
            p.setPen(QPen(bc, 1))
        else:
            p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(r, self.radius, self.radius)
        fg = qc(st["fg"])
        if st.get("hovfg"):
            fg = mix(fg, qc(st["hovfg"]), self._h)
        if st.get("actfg"):
            fg = mix(fg, qc(st["actfg"]), self._a)
        p.setPen(fg)
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignCenter, self.text())


# -------------------------------------------------------- Sliding Selector ---
class Segmented(QWidget):
    """SUB / DUB / HSUB selector with a highlight that glides between options."""
    changed = Signal(str)

    def __init__(self, values, current, height=30, seg_w=54, parent=None):
        super().__init__(parent)
        self.values = list(values)
        self._i = self.values.index(current) if current in self.values else 0
        self._pos = float(self._i)
        self._seg_w = seg_w
        self.setFixedSize(seg_w * len(self.values) + 6, height)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = make_anim(240, 0.0, 0.0, self._set_pos, QEasingCurve.OutBack)

    def _set_pos(self, v):
        self._pos = float(v)
        self.update()

    def get(self):
        return self.values[self._i]

    def set(self, value, animate=True):
        if value not in self.values:
            return
        idx = self.values.index(value)
        if idx == self._i:
            return
        self._i = idx
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(float(idx))
            self._anim.start()
        else:
            self._set_pos(float(idx))

    def mousePressEvent(self, e):
        idx = int((e.position().x() - 3) // self._seg_w)
        idx = max(0, min(len(self.values) - 1, idx))
        if idx != self._i:
            self.set(self.values[idx])
            self.changed.emit(self.values[idx])

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(qc(BORDER_CARD), 1))
        p.setBrush(qc(BG_ROOT))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        p.setPen(Qt.NoPen)
        p.setBrush(qc(ACCENT_BLUE))
        p.drawRoundedRect(QRectF(3 + self._pos * self._seg_w, 3, self._seg_w, self.height() - 6), 6, 6)
        p.setFont(font(9.5, QFont.Bold))
        for i, v in enumerate(self.values):
            near = max(0.0, 1.0 - abs(self._pos - i))
            p.setPen(mix(qc(TEXT_MUTED), qc("#FFFFFF"), near))
            p.drawText(QRectF(3 + i * self._seg_w, 0, self._seg_w, self.height()), Qt.AlignCenter, v)


# ---------------------------------------------------------------- Switch ---
class Switch(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self._on = bool(checked)
        self._t = 1.0 if checked else 0.0
        self.setFixedSize(42, 24)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = make_anim(200, 0.0, 1.0, self._set_t)

    def _set_t(self, v):
        self._t = float(v)
        self.update()

    def get(self):
        return self._on

    def set(self, on, animate=False):
        on = bool(on)
        if on == self._on:
            return
        self._on = on
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._t)
            self._anim.setEndValue(1.0 if on else 0.0)
            self._anim.start()
        else:
            self._set_t(1.0 if on else 0.0)

    def mousePressEvent(self, e):
        self.set(not self._on, animate=True)
        self.toggled.emit(self._on)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(mix(qc("#1E293B"), qc(ACCENT_BLUE), self._t))
        p.drawRoundedRect(QRectF(self.rect()), 12, 12)
        x = 3 + self._t * (self.width() - 24 + 0)
        p.setBrush(qc("#FFFFFF"))
        p.drawEllipse(QRectF(x, 3, 18, 18))


# ------------------------------------------------------------ Progress bar ---
class SmoothBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(8)
        self._v = 0.0
        self._anim = make_anim(280, 0.0, 0.0, self._set_v)

    def _set_v(self, v):
        self._v = float(v)
        self.update()

    def value(self):
        return self._v

    def setValue(self, v, animate=True):
        v = max(0.0, min(1.0, float(v)))
        self._anim.stop()
        if animate and v > self._v and abs(v - self._v) > 0.004:
            self._anim.setStartValue(self._v)
            self._anim.setEndValue(v)
            self._anim.start()
        else:
            self._set_v(v)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        r = QRectF(self.rect())
        p.setBrush(qc(BG_ROOT))
        p.drawRoundedRect(r, 4, 4)
        if self._v > 0:
            w = max(8.0, r.width() * self._v)
            g = QLinearGradient(0, 0, r.width(), 0)
            g.setColorAt(0, qc(ACCENT_BLUE))
            g.setColorAt(1, qc(ACCENT_CYAN_LIGHT))
            p.setBrush(g)
            p.drawRoundedRect(QRectF(0, 0, w, r.height()), 4, 4)


class Combo(QComboBox):
    """Combo box with a drawn chevron (QSS can't draw one without an image file)."""
    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(qc(TEXT_MUTED), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        cx, cy = self.width() - 15, self.height() / 2
        p.drawLine(QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2))
        p.drawLine(QPointF(cx, cy + 2), QPointF(cx + 4, cy - 2))


# ------------------------------------------------------------------ Helpers ---
def label(text="", size=10, color=TEXT_MAIN, bold=False, semi=False, wrap=False):
    lb = QLabel(text)
    lb.setFont(font(size, QFont.Bold if bold else (QFont.DemiBold if semi else QFont.Normal)))
    lb.setStyleSheet(f"color: {color}; background: transparent;")
    lb.setWordWrap(wrap)
    return lb


def badge(text, fg, bg, size=8.5):
    lb = QLabel(text)
    lb.setFont(font(size, QFont.Bold))
    lb.setStyleSheet(f"color:{fg}; background:{bg}; border-radius:4px; padding:3px 7px;")
    return lb


def card_frame(name="card"):
    f = QFrame()
    f.setObjectName(name)
    return f


def fixed_h(widget, h):
    widget.setFixedHeight(h)
    return widget


def fade_in(widget, ms=220, on_done=None):
    """Fade a page in, then drop the effect so it costs nothing afterwards."""
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)

    def done():
        widget.setGraphicsEffect(None)
        if on_done:
            on_done()
    anim.finished.connect(done)
    anim.start()
    widget._fade_anim = anim
    return anim


# ------------------------------------------------------- Spotlight Card ---
SLIDES = [
    {"title": "One Piece", "badge": "⭐ TOP RATED SPOTLIGHT", "badge_color": "#1D4ED8",
     "sub": "Load the full series into your download queue.", "file": "card_1_bg.png",
     "url": "https://anikoto.cz/watch/one-piece-odmau"},
    {"title": "Black Clover: Season 2", "badge": "🔥 TRENDING ON ANIKOTO", "badge_color": "#DC2626",
     "sub": "Stream & download all episodes in 1080p.", "file": "card_2_bg.png",
     "url": "https://anikoto.cz/watch/black-clover-season-2"},
    {"title": "Overgeared", "badge": "⚡ POPULAR NOW", "badge_color": "#EA580C",
     "sub": "Popular action spotlight ready to download.", "file": "card_3_bg.png",
     "url": "https://anikoto.cz/watch/overgeared-a487c"},
    {"title": "A Returner's Magic", "badge": "✨ HIGHLY RATED", "badge_color": "#7C3AED",
     "sub": "Season 2 episodes in English Sub & Dub.", "file": "card_4_bg.png",
     "url": "https://anikoto.cz/watch/a-returner-s-magic-should-be-special-season-2"},
    {"title": "Reincarnated as a Sword", "badge": "⚔️ ACTION SPOTLIGHT", "badge_color": "#0D9488",
     "sub": "Action fantasy episodes ready to queue.", "file": "card_5_bg.png",
     "url": "https://anikoto.cz/watch/tensei-shitara-ken-deshita-2nd-season"},
]


class SpotlightCard(QWidget):
    """Banner carousel painted in one pass, with a smooth crossfade between slides."""
    clicked = Signal()
    slide_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumWidth(320)
        self.slides = []
        for meta in SLIDES:
            pm = None
            path = get_asset_file(os.path.join("spotlight_cards", meta["file"])) or get_asset_file(meta["file"])
            if path:
                loaded = QPixmap(path)
                if not loaded.isNull():
                    pm = loaded
            self.slides.append(dict(meta, pm=pm))
        self._idx = 0
        self._prev = None
        self._fade = 1.0
        self._cache = {}
        self._dot_rects = []
        self._fade_anim = make_anim(480, 0.0, 1.0, self._set_fade, QEasingCurve.InOutCubic)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.next)
        self._timer.start(6000)
        self.btn = Btn("Open on Anikoto ↗", "white", radius=7, height=30, size=9.5, bold=True, minw=120, parent=self)
        self.btn.clicked.connect(self.clicked.emit)

    # ---- slide control
    def _set_fade(self, v):
        self._fade = float(v)
        self.update()

    def current(self):
        return self.slides[self._idx]

    def go_to(self, idx):
        if idx == self._idx or not (0 <= idx < len(self.slides)):
            self._timer.start(6000)
            return
        self._prev = self._idx
        self._idx = idx
        self._fade_anim.stop()
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()
        self._timer.start(6000)
        self.slide_changed.emit(idx)

    def next(self):
        self.go_to((self._idx + 1) % len(self.slides))

    def enterEvent(self, e):
        self._timer.stop()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._timer.start(6000)
        super().leaveEvent(e)

    # ---- events
    def resizeEvent(self, e):
        self._cache.clear()
        self.btn.move(20, self.height() - 16 - self.btn.height())
        super().resizeEvent(e)

    def _dot_at(self, pos):
        for i, r in enumerate(self._dot_rects):
            if r.adjusted(-5, -8, 5, 8).contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, e):
        self.setCursor(Qt.PointingHandCursor)
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        i = self._dot_at(e.position())
        if i >= 0:
            self.go_to(i)
        else:
            self.clicked.emit()

    # ---- painting
    def _cover(self, idx):
        key = (idx, self.width(), self.height())
        if key not in self._cache:
            pm = self.slides[idx]["pm"]
            if pm is None:
                self._cache[key] = None
            else:
                s = pm.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                x = (s.width() - self.width()) // 2
                y = (s.height() - self.height()) // 2
                self._cache[key] = s.copy(x, y, self.width(), self.height())
        return self._cache[key]

    def _draw_bg(self, p, idx, opacity):
        p.setOpacity(opacity)
        pm = self._cover(idx)
        if pm is not None:
            p.drawPixmap(0, 0, pm)
        else:  # graceful fallback when the artwork file is missing
            c = qc(self.slides[idx]["badge_color"])
            g = QLinearGradient(0, 0, self.width(), self.height())
            g.setColorAt(0, qc("#0B1A45"))
            g.setColorAt(1, mix(qc("#0B1A45"), c, 0.55))
            p.fillRect(self.rect(), g)
        p.setOpacity(1.0)

    def _draw_text(self, p, meta, opacity):
        if opacity <= 0.01:
            return
        p.setOpacity(opacity)
        p.setFont(font(8, QFont.Bold))
        bw = QFontMetrics(p.font()).horizontalAdvance(meta["badge"]) + 22
        p.setPen(Qt.NoPen)
        p.setBrush(qc(meta["badge_color"]))
        p.drawRoundedRect(QRectF(20, 16, bw, 22), 6, 6)
        p.setPen(qc("#FFFFFF"))
        p.drawText(QRectF(20, 16, bw, 22), Qt.AlignCenter, meta["badge"])
        p.setFont(font(19, QFont.Bold))
        p.setPen(qc("#FFFFFF"))
        p.drawText(QRectF(20, 46, self.width() - 40, 34), Qt.AlignLeft | Qt.AlignVCenter, meta["title"])
        p.setFont(font(10.5))
        p.setPen(qc("#CBD5E1"))
        p.drawText(QRectF(20, 82, self.width() - 40, 22), Qt.AlignLeft | Qt.AlignVCenter, meta["sub"])
        p.setOpacity(1.0)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        p.setClipPath(path)
        p.fillRect(self.rect(), qc(BG_CARD))
        if self._prev is not None and self._fade < 1.0:
            self._draw_bg(p, self._prev, 1.0)
            self._draw_bg(p, self._idx, self._fade)
        else:
            self._draw_bg(p, self._idx, 1.0)
        g = QLinearGradient(0, 0, self.width(), 0)
        g.setColorAt(0.0, qc("#050914", 0.88))
        g.setColorAt(0.6, qc("#050914", 0.40))
        g.setColorAt(1.0, qc("#050914", 0.05))
        p.fillRect(self.rect(), g)
        g2 = QLinearGradient(0, self.height() * 0.5, 0, self.height())
        g2.setColorAt(0.0, qc("#050914", 0.0))
        g2.setColorAt(1.0, qc("#050914", 0.75))
        p.fillRect(self.rect(), g2)
        if self._prev is not None and self._fade < 1.0:
            self._draw_text(p, self.slides[self._prev], 1.0 - self._fade)
            self._draw_text(p, self.slides[self._idx], self._fade)
        else:
            self._draw_text(p, self.slides[self._idx], 1.0)
        # dots (active one stretches into a pill)
        self._dot_rects = []
        x = 20 + self.btn.width() + 18
        cy = self.height() - 16 - self.btn.height() / 2
        p.setPen(Qt.NoPen)
        for i in range(len(self.slides)):
            active = (i == self._idx)
            w = 20.0 if active else 7.0
            r = QRectF(x, cy - 3.5, w, 7)
            p.setBrush(qc(ACCENT_CYAN_LIGHT) if active else qc("#FFFFFF", 0.35))
            p.drawRoundedRect(r, 3.5, 3.5)
            self._dot_rects.append(r)
            x += w + 7
        p.setClipping(False)
        p.setPen(QPen(qc(BORDER_CARD), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)


# ----------------------------------------------------------- Table model ---
TAG_COLORS = {"done": ACCENT_GREEN, "error": ACCENT_RED, "active": ACCENT_AMBER,
              "waiting": ACCENT_CYAN_LIGHT}


class EpisodeModel(QAbstractTableModel):
    HEADERS = ["✔", "#", "  Episode Title", "Format", "Status", "Size / Progress", "ETA"]

    def __init__(self):
        super().__init__()
        self.rows = []
        self.index_of = {}
        self.pick_header = "☐"

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 7

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        r = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.DisplayRole:
            return r["cells"][c]
        if role == Qt.ForegroundRole:
            if r["ticked_row"]:
                return qc("#FFFFFF")
            return qc(TAG_COLORS.get(r["tag"], TEXT_MAIN))
        if role == Qt.BackgroundRole:
            if r["ticked_row"]:
                return qc(ROW_TICKED)
            if r["stripe"]:
                return qc(ROW_ALT)
            return None
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignVCenter | (Qt.AlignLeft if c == 2 else Qt.AlignHCenter))
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return self.pick_header if section == 0 else self.HEADERS[section]
            if role == Qt.TextAlignmentRole:
                return int(Qt.AlignVCenter | (Qt.AlignLeft if section == 2 else Qt.AlignHCenter))
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.index_of = {r["num"]: i for i, r in enumerate(rows)}
        self.endResetModel()

    def set_pick_header(self, text):
        if text != self.pick_header:
            self.pick_header = text
            self.headerDataChanged.emit(Qt.Horizontal, 0, 0)

    def update_progress(self, num, status, prog, eta):
        i = self.index_of.get(num)
        if i is None:
            return
        cells = self.rows[i]["cells"]
        cells[4], cells[5], cells[6] = status, prog, eta
        self.dataChanged.emit(self.index(i, 4), self.index(i, 6))


class EpisodeTable(QTableView):
    space_pressed = Signal()
    delete_pressed = Signal()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space:
            self.space_pressed.emit()
            return
        if e.key() == Qt.Key_Delete:
            self.delete_pressed.emit()
            return
        super().keyPressEvent(e)


class Bridge(QObject):
    """Lets worker threads talk to the UI safely."""
    log = Signal(str)
    progress = Signal(dict)
    ep_state = Signal(int, str, dict)
    loaded = Signal(list, object)
    load_failed = Signal(str)
    load_done = Signal()
    finished = Signal()
    stopped = Signal(bool)   # True = cancelled by user


def dark_titlebar(win):
    """Dark native title bar on Windows 10/11 (the window keeps its native frame)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(win.winId())
        val = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:
        pass


# ============================================================ Main Window ===
class AnikotoDownloaderApp(QMainWindow):
    QUALITY_DL = ["1080p", "720p", "480p", "360p", "2160p", "1440p"]
    QUALITY_SET = ["1080p", "720p", "480p", "360p", "2160p (4K)"]
    FORMATS = [".mp4 (Universal H.264)", ".mkv (Matroska)", ".ts (Raw Stream)"]
    THREADS = ["4 threads", "8 threads", "16 threads (Fast)", "24 threads (Ultra)", "32 threads"]
    TIMEOUTS = ["10 seconds", "15 seconds", "20 seconds (Default)", "30 seconds", "60 seconds"]
    RETRIES = ["5 retries", "10 retries", "20 retries (Default)", "50 retries"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ANIKOTO Download Manager Pro")
        for ic in (get_asset_file("icon.ico"), os.path.join(APP_DIR, "icon.ico")):
            if ic and os.path.exists(ic):
                self.setWindowIcon(QIcon(ic))
                break

        self.cfg = load_settings()
        m = re.match(r"(\d+)x(\d+)", str(self.cfg.get("geometry", "")))
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1140, 780)
        self.resize(max(980, w), max(640, h))
        self.setMinimumSize(980, 640)
        self._start_maximized = bool(self.cfg.get("maximized", False))

        # State
        self.downloader = None
        self._is_downloading = False
        self._episodes = []
        self._ticked = set()
        self._current_ep_num = None
        self._queue = []
        self._queue_pos = -1
        self._active_filter = "All"
        self._search_query = ""
        self._current_tab = None
        self._log_messages = []
        self._log_window = None
        self._log_box = None
        self._fetch_spinner_active = False
        self._first_show = True

        self.sig = Bridge()
        self.sig.log.connect(self._log)
        self.sig.progress.connect(self._handle_progress)
        self.sig.ep_state.connect(self._handle_ep_state)
        self.sig.loaded.connect(self._on_episodes_loaded)
        self.sig.load_failed.connect(self._on_load_failed)
        self.sig.load_done.connect(self._on_load_done)
        self.sig.finished.connect(self._on_download_finished)
        self.sig.stopped.connect(self._on_worker_stopped)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        wrap = QVBoxLayout()
        wrap.setContentsMargins(16, 12, 16, 12)
        wrap.addWidget(self.stack)
        outer.addLayout(wrap, 1)

        self.page_downloads = self._build_downloads_tab()
        self.page_settings = self._build_settings_tab()
        self.page_about = self._build_about_tab()
        for pg in (self.page_downloads, self.page_settings, self.page_about):
            self.stack.addWidget(pg)

        self._setup_shortcuts()
        self._show_tab("downloads", animate=False)
        self._refresh_table()

    # ------------------------------------------------------------ helpers ---
    def get_path(self):
        return self.path_edit.text().strip()

    def _set_disk_label(self):
        self.lbl_disk.setText(f"{get_disk_free(self.get_path())}\nfree")

    def _combo(self, values, current=None, width=None):
        cb = Combo()
        cb.addItems(values)
        cb.setFixedHeight(32)
        cb.setCursor(Qt.PointingHandCursor)
        if width:
            cb.setFixedWidth(width)
        if current is not None:
            self._combo_pick(cb, current)
        return cb

    @staticmethod
    def _combo_pick(cb, number):
        """Select the option whose leading number matches (e.g. 16 -> '16 threads (Fast)')."""
        num = str(number).lower().rstrip("p")
        for i in range(cb.count()):
            head = cb.itemText(i).split()[0].lower().rstrip("p")
            if head == num:
                cb.setCurrentIndex(i)
                return

    # ------------------------------------------------------------ sidebar ---
    def _build_sidebar(self):
        sb = QFrame()
        sb.setObjectName("sidebar")
        sb.setFixedWidth(74)
        v = QVBoxLayout(sb)
        v.setContentsMargins(0, 14, 0, 14)
        v.setSpacing(6)
        v.setAlignment(Qt.AlignHCenter)

        logo = Btn("▲", "logo", radius=10, height=42, size=15, bold=True)
        logo.setFixedWidth(42)
        logo.clicked.connect(lambda: self._show_tab("downloads"))
        v.addWidget(logo, 0, Qt.AlignHCenter)
        v.addWidget(label("ANIKOTO", 7.5, TEXT_MUTED, bold=True), 0, Qt.AlignHCenter)
        v.addSpacing(12)

        self.nav = {}
        for key, icon in (("downloads", "📥"), ("settings", "⚙"), ("about", "ℹ")):
            b = Btn(icon, "tab", radius=12, height=46, size=14, bold=True)
            b.setFixedWidth(46)
            b.clicked.connect(lambda _=False, k=key: self._show_tab(k))
            self.nav[key] = b
            v.addWidget(b, 0, Qt.AlignHCenter)
        v.addStretch(1)

        self.lbl_disk = label("", 8, TEXT_MUTED, bold=True)
        self.lbl_disk.setAlignment(Qt.AlignCenter)
        v.addWidget(self.lbl_disk, 0, Qt.AlignHCenter)
        v.addWidget(label("v1.0.0", 8.5, TEXT_FAINT, bold=True), 0, Qt.AlignHCenter)
        wm = label("Made by\nMYG", 10, ACCENT_CYAN_LIGHT, bold=True)
        wm.setAlignment(Qt.AlignCenter)
        v.addSpacing(4)
        v.addWidget(wm, 0, Qt.AlignHCenter)
        return sb

    def _show_tab(self, name, animate=True):
        if self._current_tab == name:
            return
        self._current_tab = name
        pages = {"downloads": self.page_downloads, "settings": self.page_settings, "about": self.page_about}
        for k, b in self.nav.items():
            b.setActive(k == name, animate=animate)
        page = pages[name]
        self.stack.setCurrentWidget(page)
        if animate:
            fade_in(page, 220)

    # --------------------------------------------------------- downloads ---
    def _build_downloads_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        # Header
        head = QHBoxLayout()
        tb = QVBoxLayout()
        tb.setSpacing(0)
        tb.addWidget(label("ANIKOTO • PRO EDITION", 8, ACCENT_CYAN_LIGHT, bold=True))
        tb.addWidget(label("Downloads", 22, TEXT_TITLE, bold=True))
        head.addLayout(tb)
        head.addStretch(1)
        b_open = Btn("📁  Open Downloads folder", "ghost2", radius=8, height=32, size=10)
        b_open.clicked.connect(self._open_save_folder)
        head.addWidget(b_open, 0, Qt.AlignVCenter)
        v.addLayout(head)

        # Top row: spotlight + new download
        top = QHBoxLayout()
        top.setSpacing(12)
        self.spot = SpotlightCard()
        self.spot.setFixedHeight(184)
        self.spot.clicked.connect(self._on_banner_click)
        top.addWidget(self.spot, 1)
        top.addWidget(self._build_download_card(), 1)
        v.addLayout(top)

        # Episodes card
        v.addWidget(self._build_episodes_card(), 1)

        # Bottom control deck
        v.addWidget(self._build_deck())
        return page

    def _build_download_card(self):
        card = card_frame()
        card.setFixedHeight(184)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        v.addWidget(label("New download", 11.5, TEXT_TITLE, bold=True))

        r1 = QHBoxLayout()
        r1.setSpacing(6)
        self.url_edit = QLineEdit(self.cfg.get("url", ""))
        self.url_edit.setPlaceholderText("https://anikoto.cz/watch/...")
        self.url_edit.setFixedHeight(32)
        self.url_edit.returnPressed.connect(self._load_episodes)
        r1.addWidget(self.url_edit, 1)
        b_paste = Btn("Paste", "ghost", radius=8, height=32, size=10, minw=62)
        b_paste.clicked.connect(self._paste_clipboard)
        r1.addWidget(b_paste)
        v.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(6)
        self.path_edit = QLineEdit(self.cfg.get("path", ""))
        self.path_edit.setFixedHeight(32)
        self.path_edit.textChanged.connect(self._on_path_text)
        r2.addWidget(self.path_edit, 1)
        b_browse = Btn("Browse", "ghost", radius=8, height=32, size=10, minw=62)
        b_browse.clicked.connect(self._browse_folder)
        r2.addWidget(b_browse)
        v.addLayout(r2)

        r3 = QHBoxLayout()
        r3.setSpacing(8)
        audio = (self.cfg.get("audio") or "sub").upper()
        if audio not in ("SUB", "DUB", "HSUB"):
            audio = "SUB"
        self.seg_audio = Segmented(["SUB", "DUB", "HSUB"], audio, height=34, seg_w=52)
        self.seg_audio.changed.connect(self._on_audio_changed)
        r3.addWidget(self.seg_audio)
        q = str(self.cfg.get("quality") or "1080")
        q = q if q.endswith("p") else q + "p"
        self.opt_quality = self._combo(self.QUALITY_DL, width=92)
        self.opt_quality.setFixedHeight(34)
        self.opt_quality.setCurrentText(q)
        self.opt_quality.currentTextChanged.connect(self._on_quality_changed)
        r3.addWidget(self.opt_quality)
        self.btn_fetch = Btn("Fetch episodes", "primary", radius=8, height=34, size=10.5, bold=True)
        self.btn_fetch.clicked.connect(self._load_episodes)
        r3.addWidget(self.btn_fetch, 1)
        v.addLayout(r3)
        v.addStretch(1)
        return card

    def _build_episodes_card(self):
        card = card_frame()
        v = QVBoxLayout(card)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        strip = QHBoxLayout()
        strip.setContentsMargins(14, 10, 14, 6)
        strip.setSpacing(4)
        self.pills = {}
        for name in ("All", "Queued", "Downloading", "Completed", "Failed"):
            b = Btn(name, "tab", radius=6, height=28, size=10)
            b.clicked.connect(lambda _=False, n=name: self._on_filter_changed(n))
            self.pills[name] = b
            strip.addWidget(b)
        self.pills["All"].setActive(True, animate=False)
        strip.addStretch(1)
        for text, fn in (("Select all", self._select_all_visible), ("None", self._select_none),
                         ("Range", self._prompt_range_selector)):
            b = Btn(text, "small", radius=6, height=28, size=9.5)
            b.clicked.connect(fn)
            strip.addWidget(b)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search episodes...")
        self.search_edit.setFixedSize(150, 28)
        self.search_edit.textChanged.connect(self._on_search_changed)
        strip.addWidget(self.search_edit)
        v.addLayout(strip)

        # anime summary pill
        self.anime_pill = QFrame()
        self.anime_pill.setObjectName("pill")
        pl = QHBoxLayout(self.anime_pill)
        pl.setContentsMargins(12, 6, 10, 6)
        pl.setSpacing(6)
        pl.addWidget(label("🎬", 13))
        self.lbl_anime_name = label("Anime Series", 11, TEXT_TITLE, bold=True)
        pl.addWidget(self.lbl_anime_name)
        pl.addSpacing(4)
        self.badge_ep_count = badge("0 EPISODES", "#93C5FD", "#1E3A8A")
        self.badge_audio = badge("SUB", "#34D399", "#064E3B")
        self.badge_quality = badge("1080p", "#38BDF8", "#0C4A6E")
        self.badge_selected = badge("0 Selected", "#CBD5E1", "#1E293B")
        for b in (self.badge_ep_count, self.badge_audio, self.badge_quality, self.badge_selected):
            pl.addWidget(b)
        pl.addStretch(1)
        b_clear = Btn("✕ Clear Anime", "soft", radius=5, height=26, size=9.5)
        b_clear.clicked.connect(self._clear_series)
        pl.addWidget(b_clear)
        pill_wrap = QHBoxLayout()
        pill_wrap.setContentsMargins(8, 4, 8, 2)
        pill_wrap.addWidget(self.anime_pill)
        self.pill_holder = QWidget()
        self.pill_holder.setLayout(pill_wrap)
        v.addWidget(self.pill_holder)

        # empty state / table
        self.ep_stack = QStackedWidget()
        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.setAlignment(Qt.AlignCenter)
        ev.setSpacing(4)
        cat = QLabel()
        cat.setAlignment(Qt.AlignCenter)
        cat_file = get_asset_file("mascot_cat.png")
        pm = QPixmap(cat_file) if cat_file else QPixmap()
        if not pm.isNull():
            cat.setPixmap(pm.scaled(125, 70, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            cat.setText("🐱")
            cat.setFont(font(30))
        ev.addWidget(cat)
        t = label("No tasks yet", 14, TEXT_TITLE, bold=True)
        t.setAlignment(Qt.AlignCenter)
        ev.addWidget(t)
        s = label("Paste a link and fetch episodes to fill your queue.", 10, TEXT_MUTED)
        s.setAlignment(Qt.AlignCenter)
        ev.addWidget(s)
        self.ep_stack.addWidget(empty)

        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(8, 4, 8, 8)
        self.model = EpisodeModel()
        self.table = EpisodeTable()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        for col, wid in ((0, 42), (1, 48), (3, 115), (4, 140), (5, 150), (6, 80)):
            self.table.setColumnWidth(col, wid)
        hh.sectionClicked.connect(lambda i: self._toggle_select_all() if i == 0 else None)
        self.table.clicked.connect(self._on_table_click)
        self.table.doubleClicked.connect(self._on_table_double_click)
        self.table.space_pressed.connect(self._on_space_key)
        self.table.delete_pressed.connect(self._delete_selected_episode)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        hl.addWidget(self.table)
        self.ep_stack.addWidget(holder)
        v.addWidget(self.ep_stack, 1)

        self.context_menu = QMenu(self)
        for item in (("▶  Download This Episode", self._context_download_single),
                         ("🎬  Play in Default Player", self._play_selected_file),
                         ("📁  Reveal in File Explorer", self._open_save_folder), None,
                         ("🗑  Delete File from Disk", self._delete_selected_episode),
                         ("🧹  Remove from Queue", self._context_uncheck_selected),
                         ("🔄  Restart Clean from 0% (Clear Cache)", self._context_redownload_clean), None,
                         ("☑  Select / Check", self._context_check_selected),
                         ("☐  Deselect / Uncheck", self._context_uncheck_selected), None,
                         ("📋  Copy Title", self._context_copy_title)):
            if item is None:
                self.context_menu.addSeparator()
            else:
                self.context_menu.addAction(item[0], item[1])
        return card

    def _build_deck(self):
        card = card_frame()
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(6)
        top = QHBoxLayout()
        self.lbl_task_name = label("Ready to Download", 10.5, "#93C5FD", semi=True)
        self.lbl_counts = label("0 episodes loaded", 9.5, TEXT_MUTED)
        top.addWidget(self.lbl_task_name)
        top.addStretch(1)
        top.addWidget(self.lbl_counts)
        v.addLayout(top)
        self.bar = SmoothBar()
        v.addWidget(self.bar)
        sub = QHBoxLayout()
        self.lbl_speed_eta = label("No active download", 9.5, TEXT_MUTED)
        self.lbl_task_metrics = label("", 9.5, ACCENT_CYAN_LIGHT, semi=True)
        sub.addWidget(self.lbl_speed_eta)
        sub.addStretch(1)
        sub.addWidget(self.lbl_task_metrics)
        v.addLayout(sub)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.tb_start = Btn("▶   Start queue", "primary", radius=8, height=34, size=11, bold=True, minw=150)
        self.tb_start.clicked.connect(self._start_download)
        self.tb_all = Btn("📥  Download remaining", "ghost", radius=8, height=34, size=10)
        self.tb_all.clicked.connect(self._download_all_remaining)
        self.tb_stop = Btn("⏸  Stop / Pause", "danger", radius=8, height=34, size=10.5, bold=True, minw=120)
        self.tb_stop.setEnabled(False)
        self.tb_stop.clicked.connect(self._cancel_download)
        self.tb_delete = Btn("🗑  Delete file", "ghost", radius=8, height=34, size=10, minw=105)
        self.tb_delete.clicked.connect(self._delete_selected_episode)
        self.tb_clear = Btn("🧹  Clear done", "ghost", radius=8, height=34, size=10, minw=100)
        self.tb_clear.clicked.connect(self._clear_completed_selection)
        self.tb_log = Btn("📋  Log", "ghost", radius=8, height=34, size=10, minw=65)
        self.tb_log.clicked.connect(self._open_activity_log)
        for b in (self.tb_start, self.tb_all, self.tb_stop, self.tb_delete, self.tb_clear):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(label("Made by : MYG", 11, ACCENT_CYAN_LIGHT, bold=True))
        row.addSpacing(10)
        row.addWidget(self.tb_log)
        v.addLayout(row)
        return card

    # ---------------------------------------------------------- settings ---
    def _settings_card(self, title, sub=None):
        f = card_frame()
        v = QVBoxLayout(f)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(8)
        v.addWidget(label(title, 12, TEXT_TITLE, bold=True))
        if sub:
            v.addWidget(label(sub, 9.5, TEXT_MUTED))
        return f, v

    def _switch_row(self, text, checked):
        row = QHBoxLayout()
        row.setSpacing(10)
        sw = Switch(checked)
        row.addWidget(sw)
        row.addWidget(label(text, 10, TEXT_MAIN), 1)
        return row, sw

    def _field(self, caption, widget):
        box = QVBoxLayout()
        box.setSpacing(4)
        box.addWidget(label(caption, 10, TEXT_MUTED, semi=True))
        box.addWidget(widget)
        return box

    def _build_settings_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("scrollbody")
        body.setStyleSheet("#scrollbody{background:transparent;}")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 10, 10)
        v.setSpacing(12)
        v.addWidget(label("⚙  Application Settings", 20, TEXT_TITLE, bold=True))
        v.addWidget(label("Configure storage directories, stream decryption, fragment concurrency, and transcoders.",
                          10.5, TEXT_MUTED))

        c1, l1 = self._settings_card("📁  Storage & Output Directory",
                                     "Manage where your downloaded anime seasons and temporary files are stored.")
        l1.addWidget(label("Default Download Path:", 10, TEXT_MUTED, semi=True))
        pr = QHBoxLayout()
        self.set_path_edit = QLineEdit(self.cfg.get("path", ""))
        self.set_path_edit.setFixedHeight(34)
        self.set_path_edit.textChanged.connect(self._on_set_path_text)
        pr.addWidget(self.set_path_edit, 1)
        bb = Btn("Browse…", "ghost", radius=8, height=34, size=10, minw=84)
        bb.clicked.connect(self._browse_folder)
        pr.addWidget(bb)
        l1.addLayout(pr)
        r, self.sw_subfolder = self._switch_row(
            "Organize into dedicated anime folders (Auto-create subfolder for each anime)",
            self.cfg.get("create_subfolder", False))
        l1.addLayout(r)
        r, self.sw_cleancache = self._switch_row(
            "Automatically clean temporary fragment cache (.anikoto_cache) after completion",
            self.cfg.get("clean_cache", True))
        l1.addLayout(r)
        v.addWidget(c1)

        c2, l2 = self._settings_card("🎬  Stream & Video Preferences",
                                     "Default audio tracks, resolution targets, and container encapsulation.")
        g = QHBoxLayout()
        g.setSpacing(18)
        a = (self.cfg.get("audio") or "sub").upper()
        self.set_audio_seg = Segmented(["SUB", "DUB", "HSUB"], a if a in ("SUB", "DUB", "HSUB") else "SUB",
                                       height=34, seg_w=60)
        self.set_quality_opt = self._combo(self.QUALITY_SET, self.cfg.get("quality", "1080"))
        self.set_format_opt = self._combo(self.FORMATS)
        fmt = self.cfg.get("output_format", "mp4")
        for i, f_ in enumerate(self.FORMATS):
            if f_.startswith("." + fmt):
                self.set_format_opt.setCurrentIndex(i)
        g.addLayout(self._field("Preferred Default Audio:", self.set_audio_seg))
        g.addLayout(self._field("Default Resolution:", self.set_quality_opt), 1)
        g.addLayout(self._field("Video Container Format:", self.set_format_opt), 1)
        l2.addLayout(g)
        v.addWidget(c2)

        c3, l3 = self._settings_card("⚡  Engine Acceleration & Anti-Skip Integrity",
                                     "Parallel multi-threaded chunk downloading with zero-loss fragment integrity.")
        g = QHBoxLayout()
        g.setSpacing(18)
        self.set_threads_opt = self._combo(self.THREADS, self.cfg.get("concurrent_fragments", 16))
        self.set_timeout_opt = self._combo(self.TIMEOUTS, self.cfg.get("socket_timeout", 20))
        self.set_retries_opt = self._combo(self.RETRIES, self.cfg.get("max_retries", 20))
        g.addLayout(self._field("Concurrent Fragment Threads:", self.set_threads_opt), 1)
        g.addLayout(self._field("Connection Socket Timeout:", self.set_timeout_opt), 1)
        g.addLayout(self._field("Max Fragment Retries:", self.set_retries_opt), 1)
        l3.addLayout(g)
        r, self.sw_antiskip = self._switch_row(
            "Anti-Skip Protection: Guarantees 100% complete frames (Prevents video freeze & desync bugs)",
            self.cfg.get("anti_skip", True))
        l3.addLayout(r)
        v.addWidget(c3)

        c4, l4 = self._settings_card("🔧  Transcoder & System Tools")
        r, self.sw_ffmpeg = self._switch_row(
            "Lossless FFmpeg Direct Remux (Merge audio/video/subtitles without re-encoding)",
            self.cfg.get("auto_remux", True))
        l4.addLayout(r)
        found = bool(find_ffmpeg_dir())
        l4.addWidget(label("FFmpeg Status: " + ("✓ Detected & Linked" if found
                                                else "⚠ Not Found (Will auto-download on first run)"),
                           10, ACCENT_GREEN if found else ACCENT_AMBER))
        v.addWidget(c4)

        c5, l5 = self._settings_card("🔔  Notifications & System Alerts")
        r, self.sw_sound = self._switch_row("Play audio chime notification when download queue finishes",
                                            self.cfg.get("sound_notification", True))
        l5.addLayout(r)
        v.addWidget(c5)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        bs = Btn("💾   Save & Apply Settings", "primary", radius=8, height=38, size=11, bold=True, minw=210)
        bs.clicked.connect(self._save_settings_from_tab)
        br = Btn("↺  Reset Defaults", "ghost", radius=8, height=38, size=10.5, minw=140)
        br.clicked.connect(self._reset_default_settings)
        self.lbl_settings_status = label("", 10.5, ACCENT_GREEN, semi=True)
        bar.addWidget(bs)
        bar.addWidget(br)
        bar.addWidget(self.lbl_settings_status)
        bar.addStretch(1)
        v.addLayout(bar)
        v.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _on_path_text(self, text):
        if self.set_path_edit.text() != text:
            self.set_path_edit.setText(text)
        self._set_disk_label()

    def _on_set_path_text(self, text):
        if self.path_edit.text() != text:
            self.path_edit.setText(text)

    def _save_settings_from_tab(self):
        path = self.get_path()
        audio = self.set_audio_seg.get().lower()
        quality_raw = self.set_quality_opt.currentText().replace("p", "").split()[0]
        fmt_str = self.set_format_opt.currentText()
        fmt = "mkv" if "mkv" in fmt_str else ("ts" if ".ts" in fmt_str else "mp4")

        def num(combo, default):
            head = combo.currentText().split()[0]
            return int(head) if head.isdigit() else default
        self.cfg.update({
            "path": path, "audio": audio, "quality": quality_raw, "output_format": fmt,
            "create_subfolder": self.sw_subfolder.get(), "clean_cache": self.sw_cleancache.get(),
            "concurrent_fragments": num(self.set_threads_opt, 16),
            "anti_skip": self.sw_antiskip.get(),
            "socket_timeout": num(self.set_timeout_opt, 20),
            "max_retries": num(self.set_retries_opt, 20),
            "auto_remux": self.sw_ffmpeg.get(), "sound_notification": self.sw_sound.get(),
        })
        save_settings(self.cfg)
        self.seg_audio.set(audio.upper())
        self.opt_quality.setCurrentText(quality_raw + "p")
        self._set_disk_label()
        self.lbl_settings_status.setText("✓ Settings saved and applied successfully!")
        QTimer.singleShot(3000, lambda: self.lbl_settings_status.setText(""))
        self._log("Settings updated and saved to settings.json.")
        self._refresh_table()

    def _reset_default_settings(self):
        self.sw_subfolder.set(False, True)
        self.sw_cleancache.set(True, True)
        self.set_audio_seg.set("SUB")
        self.set_quality_opt.setCurrentText("1080p")
        self.set_format_opt.setCurrentIndex(0)
        self._combo_pick(self.set_threads_opt, 16)
        self.sw_antiskip.set(True, True)
        self._combo_pick(self.set_timeout_opt, 20)
        self._combo_pick(self.set_retries_opt, 20)
        self.sw_ffmpeg.set(True, True)
        self.sw_sound.set(True, True)
        self._save_settings_from_tab()

    # ------------------------------------------------------------- about ---
    def _build_about_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("scrollbody")
        body.setStyleSheet("#scrollbody{background:transparent;}")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 10, 10)
        v.setSpacing(12)

        hero = card_frame()
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(20, 18, 20, 18)
        tr = QHBoxLayout()
        tr.addWidget(label("▲ ANIKOTO Download Manager Pro", 20, ACCENT_CYAN_LIGHT, bold=True))
        vb = badge("v1.0.0 RELEASE", "#FFFFFF", ACCENT_BLUE, 9)
        tr.addWidget(vb)
        tr.addStretch(1)
        tr.addWidget(label("Made by : MYG", 10, ACCENT_CYAN_LIGHT, bold=True))
        hv.addLayout(tr)
        hv.addWidget(label("High-Performance Batch Anime Downloader with Zero-Loss Stream Decryption & Anti-Skip Engine",
                           11, TEXT_MUTED))
        v.addWidget(hero)

        cd, ld = self._settings_card("🖥  Engine & Diagnostics Status")
        grid = QGridLayout()
        grid.setSpacing(8)
        items = [
            ("Python Core", f"{sys.version.split()[0]} ({sys.platform})"),
            ("GUI Framework", "PySide6 / Qt (Oceanic Dark)"),
            ("Stream Decryption", "AES-128-CBC Hardware Acceleration"),
            ("HLS Stream Engine", "yt-dlp Core Integration"),
            ("FFmpeg Remuxer", "Active & Integrated" if find_ffmpeg_dir() else "Ready on-demand"),
            ("Operating System", "Windows 10 / 11 64-bit"),
            ("Active Storage", self.cfg.get("path", "")),
            ("Disk Free Space", get_disk_free(self.cfg.get("path", ""))),
        ]
        for i, (k, val) in enumerate(items):
            cell = card_frame("inner")
            cv = QVBoxLayout(cell)
            cv.setContentsMargins(10, 6, 10, 7)
            cv.setSpacing(1)
            cv.addWidget(label(k, 9.5, TEXT_MUTED, semi=True))
            cv.addWidget(label(val, 10.5, TEXT_TITLE, bold=True))
            grid.addWidget(cell, i // 2, i % 2)
        ld.addLayout(grid)
        v.addWidget(cd)

        ca, la = self._settings_card("⚡  Architectural Highlights & Capabilities")
        for title, desc in [
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
        ]:
            la.addWidget(label(title, 10.5, ACCENT_CYAN_LIGHT, semi=True))
            la.addWidget(label(desc, 10, TEXT_MUTED, wrap=True))
        v.addWidget(ca)

        cl, ll = self._settings_card("⚖  Disclaimer & Open-Source Archival")
        ll.addWidget(label("This application is designed strictly for personal offline archiving and anime study.\n"
                           "Anikoto Downloader does not host media or collect user telemetry. "
                           "Made by : MYG with ❤️ for the anime community.", 10, TEXT_MUTED, wrap=True))
        v.addWidget(cl)
        v.addStretch(1)
        scroll.setWidget(body)
        return scroll

    # ----------------------------------------------------------- spotlight ---
    def _on_banner_click(self):
        meta = self.spot.current()
        url = meta.get("url")
        if url:
            self.url_edit.setText(url)
            self._set_status(f"Loaded '{meta['title']}'! Opening on Anikoto website...")
            self._log(f"Opening browser for spotlight: {meta['title']} ({url})")
            try:
                QDesktopServices.openUrl(QUrl(url))
            except Exception:
                import webbrowser
                webbrowser.open(url)

    # ---------------------------------------------------------------- log ---
    def _open_activity_log(self):
        if self._log_window is not None:
            self._log_window.show()
            self._log_window.raise_()
            self._log_window.activateWindow()
            return
        w = QDialog(self)
        w.setWindowTitle("Activity & Engine Log — Anikoto Pro")
        w.resize(680, 420)
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 12, 12, 12)
        head = QHBoxLayout()
        head.addWidget(label("📋  Real-time Scraper & Transcoder Log", 12, TEXT_TITLE, bold=True))
        head.addStretch(1)
        for text, var, fn in (("Clear", "ghost", self._clear_log), ("Copy All", "primary", self._copy_log),
                              ("Close", "ghost", w.hide)):
            b = Btn(text, var, radius=6, height=28, size=9.5, minw=70)
            b.clicked.connect(fn)
            head.addWidget(b)
        v.addLayout(head)
        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText("\n".join(self._log_messages))
        box.verticalScrollBar().setValue(box.verticalScrollBar().maximum())
        v.addWidget(box, 1)
        self._log_box = box
        self._log_window = w
        w.show()

    def _copy_log(self):
        QApplication.clipboard().setText("\n".join(self._log_messages))

    def _clear_log(self):
        self._log_messages.clear()
        if self._log_box is not None:
            self._log_box.clear()

    def _log(self, msg):
        entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self._log_messages.append(entry)
        if len(self._log_messages) > 1000:
            self._log_messages = self._log_messages[-1000:]
        if self._log_box is not None:
            self._log_box.appendPlainText(entry)

    # ----------------------------------------------------------- shortcuts ---
    def _setup_shortcuts(self):
        for seq, fn in (("Ctrl+O", self._open_save_folder), ("Ctrl+A", self._select_all_visible),
                        ("Ctrl+D", self._select_none), ("Ctrl+L", self._open_activity_log)):
            QShortcut(QKeySequence(seq), self, activated=fn)

    def _paste_clipboard(self):
        clip = QApplication.clipboard().text().strip()
        if clip:
            self.url_edit.setText(clip)
            self._load_episodes()

    def _browse_folder(self):
        chosen = QFileDialog.getExistingDirectory(self, "Select download folder", self.get_path())
        if chosen:
            self.path_edit.setText(chosen)
            self.cfg["path"] = chosen
            save_settings(self.cfg)
            if self._episodes:
                self._refresh_table()

    def _on_audio_changed(self, value):
        self.cfg["audio"] = value.lower()
        save_settings(self.cfg)
        self.set_audio_seg.set(value.upper())
        if self._episodes:
            self._refresh_table()

    def _on_quality_changed(self, value):
        clean = value.rstrip("p")
        self.cfg["quality"] = clean
        save_settings(self.cfg)
        self._combo_pick(self.set_quality_opt, clean)
        if self._episodes:
            self._refresh_table()

    def audio(self):
        return self.seg_audio.get().lower()

    def quality(self):
        return self.opt_quality.currentText().rstrip("p")

    def _open_save_folder(self):
        folder = self.get_path()
        if os.path.exists(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            QMessageBox.warning(self, "Not Found", f"Directory does not exist:\n{folder}")

    def _selected_episode(self):
        sel = self.table.selectionModel().selectedRows()
        if sel:
            return self.model.rows[sel[0].row()]["num"]
        return None

    def _ep(self, num):
        return next((e for e in self._episodes if e["number"] == num), None)

    def _play_selected_file(self):
        num = self._selected_episode()
        if num is None:
            QMessageBox.information(self, "Select Episode", "Click an episode row to play.")
            return
        ep = self._ep(num)
        target = find_episode_file(self.get_path(), num, ep["title"] if ep else None)
        if target and os.path.exists(target):
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))
        else:
            QMessageBox.information(self, "Not Downloaded", f"Episode {num} has not been downloaded yet.")

    def _delete_selected_episode(self):
        num = self._selected_episode()
        if num is None:
            QMessageBox.information(self, "Select Episode", "Click an episode row to delete.")
            return
        ep = self._ep(num)
        title = ep["title"] if ep else None
        target = find_episode_file(self.get_path(), num, title)
        stem = f"E{num} {sanitize_filename(title) if title else f'E{num}'}"
        cache_dir = os.path.join(self.get_path(), ".anikoto_cache")
        if target and os.path.exists(target):
            fname = os.path.basename(target)
            if QMessageBox.question(self, "Delete Episode",
                                    f"Permanently delete file:\n{fname}\nfrom disk?") == QMessageBox.Yes:
                try:
                    os.remove(target)
                    purge_episode_cache(cache_dir, stem)
                    if ep:
                        ep["status"] = "idle"
                        ep["size"] = 0
                    self._refresh_table()
                    self._set_disk_label()
                    self._log(f"Deleted {fname} from disk.")
                except Exception as e:
                    QMessageBox.critical(self, "Delete Error", f"Could not delete file:\n{e}")
        else:
            self._ticked.discard(num)
            purge_episode_cache(cache_dir, stem)
            if ep:
                ep["status"] = "idle"
            self._refresh_table()
            self._log(f"Cleared state for Episode {num}.")

    def _clear_completed_selection(self):
        done = self._get_existing_episode_nums()
        self._ticked = {n for n in self._ticked if n not in done}
        self._refresh_table()
        self._log("Cleared completed episodes from selection queue.")

    def _set_status(self, text, color=None):
        self.lbl_task_name.setText(text)

    # --------------------------------------------------- filter & search ---
    def _on_filter_changed(self, value):
        self._active_filter = value
        for name, b in self.pills.items():
            b.setActive(name == value)
        self._refresh_table()

    def _on_search_changed(self, text):
        self._search_query = text.strip().lower()
        self._refresh_table()

    def _update_filter_badges(self, existing=None):
        total = len(self._episodes)
        existing = self._get_existing_episode_nums() if existing is None else existing
        done_count = len(existing)
        waiting = len(self._ticked - existing)
        err = sum(1 for e in self._episodes if e.get("status") == "error")
        active = 1 if self._current_ep_num else 0
        self.pills["All"].setText(f"⊞ All ({total})")
        self.pills["Queued"].setText(f"🕒 Queued ({waiting})")
        self.pills["Downloading"].setText(f"📥 Downloading ({active})")
        self.pills["Completed"].setText(f"✓ Completed ({done_count})")
        self.pills["Failed"].setText(f"⚠ Failed ({err})")
        if not self._is_downloading:
            self.lbl_counts.setText(f"{len(self._ticked)} ticked / {total} total ({done_count} on disk)")
        if self._episodes:
            self.badge_selected.setText(f"{len(self._ticked)} / {total} Selected")
            self.badge_audio.setText(self.audio().upper())
            self.badge_quality.setText(f"{self.quality()}p")
            self.badge_ep_count.setText(f"{total} EPISODES")
        if total > 0 and len(self._ticked) == total:
            self.model.set_pick_header("☑")
        elif self._ticked:
            self.model.set_pick_header("✔")
        else:
            self.model.set_pick_header("☐")

    # ----------------------------------------------------------- table ---
    def _get_existing_episode_nums(self):
        done = set()
        path = self.get_path()
        if not os.path.isdir(path):
            return done
        try:
            pat = re.compile(r"^E(\d+)(?:[ ._-].*)?\.(mp4|mkv|webm)$", re.IGNORECASE)
            for name in os.listdir(path):
                m = pat.match(name)
                if m:
                    fp = os.path.join(path, name)
                    if os.path.isfile(fp) and os.path.getsize(fp) > 50_000:
                        done.add(int(m.group(1)))
        except OSError:
            pass
        return done

    def _toggle_select_all(self):
        if not self._episodes:
            return
        visible = {r["num"] for r in self.model.rows}
        if visible and visible.issubset(self._ticked):
            self._ticked.difference_update(visible)
        else:
            self._ticked.update(visible)
        self._refresh_table()

    def _clear_series(self):
        self._episodes = []
        self._ticked.clear()
        self._current_ep_num = None
        self.url_edit.setText("")
        self._refresh_table()
        self._set_status("Ready")
        self._log("Series cleared from view.")

    def _refresh_table(self):
        if not self._episodes:
            self.pill_holder.hide()
            self.ep_stack.setCurrentIndex(0)
            self.model.set_rows([])
            self._update_filter_badges(set())
            return
        if not self.pill_holder.isVisible():
            self.pill_holder.show()
        if self.ep_stack.currentIndex() != 1:
            self.ep_stack.setCurrentIndex(1)
            fade_in(self.ep_stack.currentWidget(), 260)
        existing = self._get_existing_episode_nums()
        fmt = f"{self.audio().upper()} · {self.quality()}p"
        rows = []
        shown = 0
        for ep in self._episodes:
            num, title = ep["number"], ep["title"]
            is_done = num in existing
            is_active = (num == self._current_ep_num)
            is_queued = (num in self._ticked and not is_done and not is_active)
            is_error = ep.get("status") == "error"
            f = self._active_filter
            if f == "Queued" and not is_queued:
                continue
            if f == "Downloading" and not is_active:
                continue
            if f == "Completed" and not is_done:
                continue
            if f == "Failed" and not is_error:
                continue
            q = self._search_query
            if q:
                if not (f"ep {num}" in q or f"e{num}" in q or q == str(num) or q in title.lower()):
                    continue
            ticked = num in self._ticked
            if is_active:
                status, tag = "⚡ Downloading", "active"
                prog = f"{ep.get('pct', 0):.0f}% ({ep.get('speed_str', '—')})"
                eta = ep.get("eta_str", "—")
            elif is_done:
                status, tag = "✔ Completed", "done"
                size = ep.get("size")
                if not size:
                    fp = find_episode_file(self.get_path(), num, title)
                    size = os.path.getsize(fp) if fp else 0
                prog = format_bytes(size) if size else "100%"
                eta = "Done"
            elif is_error:
                status, tag, prog, eta = "✖ Failed", "error", "Error", "—"
            elif is_queued:
                status, tag, prog, eta = "⏳ Queued", "waiting", "Ready", "—"
            else:
                status, tag, prog, eta = "○ Available", "waiting", "—", "—"
            ticked_row = ticked and not is_active
            stripe = (shown % 2 == 0) and not ticked_row and not is_active and not is_error
            rows.append({"num": num, "tag": tag, "ticked_row": ticked_row, "stripe": stripe,
                         "cells": ["☑" if ticked else "☐", f"{num:02d}", f"  {title}", fmt, status, prog, eta]})
            shown += 1
        keep = self._selected_episode()
        scroll = self.table.verticalScrollBar().value()
        self.model.set_rows(rows)
        self.table.verticalScrollBar().setValue(scroll)
        if keep is not None and keep in self.model.index_of:
            self.table.selectRow(self.model.index_of[keep])
        self._update_filter_badges(existing)

    # ----------------------------------------------------- click & events ---
    def _on_table_click(self, idx):
        if idx.column() == 0:
            self._toggle_tick(self.model.rows[idx.row()]["num"])

    def _on_space_key(self):
        num = self._selected_episode()
        if num is not None:
            self._toggle_tick(num)

    def _on_table_double_click(self, idx):
        num = self.model.rows[idx.row()]["num"]
        if num in self._get_existing_episode_nums():
            self._play_selected_file()
        elif not self._is_downloading:
            self._ticked = {num}
            self._refresh_table()
            self._start_download()

    def _show_context_menu(self, pos):
        idx = self.table.indexAt(pos)
        if idx.isValid():
            self.table.selectRow(idx.row())
            self.context_menu.popup(self.table.viewport().mapToGlobal(pos))

    def _toggle_tick(self, num):
        if num in self._ticked:
            self._ticked.discard(num)
        else:
            self._ticked.add(num)
        self._refresh_table()

    def _select_all_visible(self):
        for r in self.model.rows:
            self._ticked.add(r["num"])
        self._refresh_table()

    def _select_none(self):
        self._ticked.clear()
        self._refresh_table()

    def _prompt_range_selector(self):
        text, ok = QInputDialog.getText(self, "Select Range", "Enter episode range (e.g. 1-12, 15, 20-25):")
        if not ok or not text.strip():
            return
        text = text.replace("–", "-").replace("—", "-").replace("to", "-")
        parsed = set()
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                sub = part.split("-")
                if len(sub) == 2 and sub[0].strip().isdigit() and sub[1].strip().isdigit():
                    a, b = int(sub[0]), int(sub[1])
                    parsed.update(range(min(a, b), max(a, b) + 1))
            elif part.isdigit():
                parsed.add(int(part))
        self._ticked.update(parsed & {e["number"] for e in self._episodes})
        self._refresh_table()

    def _context_download_single(self):
        num = self._selected_episode()
        if num is not None and not self._is_downloading:
            self._ticked = {num}
            self._refresh_table()
            self._start_download()

    def _context_check_selected(self):
        num = self._selected_episode()
        if num is not None:
            self._ticked.add(num)
            self._refresh_table()

    def _context_uncheck_selected(self):
        num = self._selected_episode()
        if num is not None:
            self._ticked.discard(num)
            self._refresh_table()

    def _context_copy_title(self):
        ep = self._ep(self._selected_episode())
        if ep:
            QApplication.clipboard().setText(ep["title"])

    def _context_redownload_clean(self):
        num = self._selected_episode()
        if num is not None and not self._is_downloading:
            ep = self._ep(num)
            if ep:
                stem = f"E{num} {sanitize_filename(ep['title'])}"
                purge_episode_cache(os.path.join(self.get_path(), ".anikoto_cache"), stem)
                self._log(f"Purged cached fragments for Episode {num} — restarting clean from 0%.")
            self._ticked = {num}
            self._refresh_table()
            self._start_download()

    # ---------------------------------------------------- scraping & load ---
    def _start_fetch_spinner(self):
        self._fetch_spinner_active = True
        self._spin_i = 0
        self._spin_timer = QTimer(self)
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

        def tick():
            if not self._fetch_spinner_active:
                self._spin_timer.stop()
                self.btn_fetch.setText("Fetch episodes")
                return
            self.btn_fetch.setText(f"{frames[self._spin_i % len(frames)]} Fetching...")
            self._spin_i += 1
        self._spin_timer.timeout.connect(tick)
        self._spin_timer.start(80)
        tick()

    def _load_episodes(self):
        if self._is_downloading:
            return
        url = self.url_edit.text().strip()
        if not url.startswith("http"):
            QMessageBox.warning(self, "Invalid URL", "Please paste a valid Anikoto watch page URL.")
            return
        self.btn_fetch.setEnabled(False)
        self._start_fetch_spinner()
        self._set_status("Fetching series data...")
        self.cfg["url"] = url
        save_settings(self.cfg)
        self._log(f"Fetching anime series from: {url}")
        threading.Thread(target=self._load_thread, args=(url,), daemon=True).start()

    def _load_thread(self, url):
        try:
            scraper = AnikotoScraper()
            video_id, domain = scraper.get_video_id(url)
            episodes = scraper.get_episodes(domain, video_id)
            info = scraper.parse_series_info(scraper.last_html)
            self.sig.loaded.emit(episodes, info)
        except Exception as e:
            self.sig.load_failed.emit(str(e))
        finally:
            self.sig.load_done.emit()

    def _on_load_failed(self, err):
        self._set_status("Load failed")
        self._log(f"Load error: {err}")
        QMessageBox.critical(self, "Load Failed", f"Could not read anime watch page:\n{err}")

    def _on_load_done(self):
        self._fetch_spinner_active = False
        self.btn_fetch.setEnabled(True)
        self.btn_fetch.setText("Fetch episodes")

    def _on_episodes_loaded(self, episodes, info=None):
        self._episodes = episodes
        self._ticked.clear()
        title = (info.get("title") if info else None) or "Anime Series"
        self.lbl_anime_name.setText(title)
        self._refresh_table()
        existing = self._get_existing_episode_nums()
        self._set_status(f"Loaded {len(episodes)} episodes")
        self._log(f"Successfully loaded '{title}': {len(episodes)} episodes ({len(existing)} already on disk).")

    # ------------------------------------------------------ download core ---
    def _download_all_remaining(self):
        if self._is_downloading:
            return
        if not self._episodes:
            QMessageBox.warning(self, "No Episodes", "Please fetch episodes first.")
            return
        existing = self._get_existing_episode_nums()
        remaining = [e["number"] for e in self._episodes if e["number"] not in existing]
        if not remaining:
            self._set_status("All completed")
            QMessageBox.information(self, "All Downloaded", "All episodes in this series are already on disk!")
            return
        self._ticked = set(remaining)
        self._refresh_table()
        self._log(f"Queued all remaining {len(remaining)} episodes.")
        self._start_download()

    def _start_download(self):
        if self._is_downloading:
            return
        if not self._episodes:
            QMessageBox.warning(self, "No Episodes", "Please fetch episodes first.")
            return
        if not self._ticked:
            QMessageBox.warning(self, "No Selection", "Please tick at least one episode to download.")
            return
        save_path = self.get_path()
        if not os.path.isdir(save_path):
            try:
                os.makedirs(save_path, exist_ok=True)
            except Exception as e:
                QMessageBox.critical(self, "Invalid Directory", f"Cannot create save directory:\n{e}")
                return
        self._is_downloading = True
        self.tb_start.setEnabled(False)
        self.tb_all.setEnabled(False)
        self.tb_stop.setEnabled(True)
        self.btn_fetch.setEnabled(False)
        self._queue = sorted(self._ticked)
        self._queue_pos = -1
        url = self.url_edit.text().strip()
        audio, quality = self.audio(), self.quality()
        self.cfg.update({"url": url, "path": save_path, "audio": audio, "quality": quality})
        save_settings(self.cfg)
        self.downloader = Downloader()
        total = len(self._queue)
        self._set_status(f"Downloading queue ({total} episodes)...")
        self.lbl_task_name.setText(f"Queue initialized ({total} items)")
        self._log(f"=== Starting batch download: {total} episode(s) ===")
        threading.Thread(target=self._download_worker,
                         args=(url, save_path, audio, "all", quality, list(self._queue)),
                         daemon=True).start()

    def _download_worker(self, url, save_path, audio, source_filter, quality, ep_range):
        try:
            last = [0.0]

            def log_hook(msg):
                self.sig.log.emit(str(msg))

            def progress_hook(d):
                now = time.time()
                status = d.get("status")
                if status == "finished" or (now - last[0]) >= 0.08:
                    last[0] = now
                    self.sig.progress.emit({
                        "status": status,
                        "downloaded_bytes": d.get("downloaded_bytes", 0),
                        "total_bytes": d.get("total_bytes") or d.get("total_bytes_estimate", 0),
                        "speed": d.get("speed") or 0,
                        "eta": d.get("eta"),
                    })

            def ep_hook(num, status, meta):
                self.sig.ep_state.emit(int(num), str(status), dict(meta or {}))

            self.downloader.download(url, save_path, audio, source_filter, quality, ep_range,
                                     progress_hook=progress_hook, log_hook=log_hook,
                                     ep_hook=ep_hook, options=self.cfg)
            cancelled = bool(self.downloader and self.downloader.was_cancelled)
            self.sig.stopped.emit(cancelled)
            if not cancelled and self.cfg.get("sound_notification", True):
                try:
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                except Exception:
                    pass
        except Exception as e:
            self.sig.log.emit(f"Critical error: {e}")
            self.sig.stopped.emit(True)
        finally:
            self.sig.finished.emit()

    def _on_worker_stopped(self, cancelled):
        if cancelled:
            self._set_status("Stopped")
            self._log("=== Download cancelled by user ===")
        else:
            self._set_status("All tasks finished")
            self._log("=== All queued downloads complete! ===")

    def _handle_progress(self, d):
        if not self._is_downloading:
            return
        status = d.get("status")
        if status == "downloading":
            done, total = d.get("downloaded_bytes", 0), d.get("total_bytes", 0)
            pct = min(done / total * 100, 100) if total > 0 else 0
            speed = d.get("speed") or 0
            speed_str = f"{speed / 1024 / 1024:.2f} MB/s" if speed else "connecting…"
            eta = d.get("eta")
            eta_str = f"{int(eta) // 60}:{int(eta) % 60:02d}" if (eta is not None and eta >= 0) else "—"
            self.bar.setValue(pct / 100.0)
            self.lbl_task_name.setText(f"Downloading Episode {self._current_ep_num} ({pct:.1f}%)")
            self.lbl_task_metrics.setText(f"{format_bytes(done)} / {format_bytes(total)}")
            self.lbl_speed_eta.setText(f"Speed: {speed_str}   •   ETA: {eta_str}")
            if self._queue:
                qi = max(self._queue_pos, 0)
                overall = (qi + pct / 100) / len(self._queue) * 100
                self.lbl_counts.setText(f"Queue {qi + 1} of {len(self._queue)} ({overall:.0f}% total)")
            ep = self._ep(self._current_ep_num)
            if ep:
                ep["pct"], ep["speed_str"], ep["eta_str"] = pct, speed_str, eta_str
            if self._current_ep_num:
                self.model.update_progress(self._current_ep_num, f"⚡ {pct:.0f}%",
                                           f"{format_bytes(done)} ({speed_str})", eta_str)
        elif status == "finished":
            self.lbl_task_name.setText(f"Merging & Embedding Subtitles (Ep {self._current_ep_num})...")
            self.lbl_speed_eta.setText("FFmpeg processing...")
            self.bar.setValue(1.0)

    def _handle_ep_state(self, num, status, meta):
        if status == "start":
            self._current_ep_num = num
            if num in self._queue:
                self._queue_pos = self._queue.index(num)
            self._set_status(f"Downloading Episode {num}...")
            self.bar.setValue(0, animate=False)
            self._refresh_table()
        elif status == "done":
            size = meta.get("size", 0)
            ep = self._ep(num)
            if ep:
                ep["status"], ep["size"] = "done", size
            self._refresh_table()
            self._log(f"Episode {num} completed successfully ({format_bytes(size)}).")
        elif status == "error":
            ep = self._ep(num)
            if ep:
                ep["status"] = "error"
            self._refresh_table()
        elif status == "cancelled":
            self._refresh_table()

    def _cancel_download(self):
        if self.downloader:
            self.downloader.cancel()
            self.tb_stop.setEnabled(False)
            self._set_status("Stopping download...")
            self.lbl_task_name.setText("Stopping gracefully (preserving partial download)...")
            self._log("Cancel signal sent to worker...")

    def _on_download_finished(self):
        self._is_downloading = False
        self._current_ep_num = None
        if self.cfg.get("clean_cache", True):
            cleanup_cache_if_empty(os.path.join(self.get_path(), ".anikoto_cache"))
        self.tb_start.setEnabled(True)
        self.tb_all.setEnabled(True)
        self.tb_stop.setEnabled(False)
        self.btn_fetch.setEnabled(True)
        self.bar.setValue(0, animate=False)
        self.lbl_speed_eta.setText("")
        self.lbl_task_metrics.setText("")
        self._set_disk_label()
        self._refresh_table()

    # --------------------------------------------------------- window ---
    def showEvent(self, e):
        super().showEvent(e)
        if self._first_show:
            self._first_show = False
            dark_titlebar(self)
            self._set_disk_label()
            if self._start_maximized:
                self.showMaximized()
            self.setWindowOpacity(0.0)
            self._open_anim = QPropertyAnimation(self, b"windowOpacity", self)
            self._open_anim.setDuration(260)
            self._open_anim.setStartValue(0.0)
            self._open_anim.setEndValue(1.0)
            self._open_anim.setEasingCurve(QEasingCurve.OutCubic)
            self._open_anim.start()

    def closeEvent(self, e):
        if self._is_downloading:
            if QMessageBox.question(self, "Quit?", "A download is currently running.\nStop and exit?") != QMessageBox.Yes:
                e.ignore()
                return
            if self.downloader:
                self.downloader.cancel()
        normal = self.normalGeometry() if self.isMaximized() else self.geometry()
        self.cfg["geometry"] = f"{normal.width()}x{normal.height()}"
        self.cfg["maximized"] = self.isMaximized()
        save_settings(self.cfg)
        cleanup_cache_if_empty(os.path.join(self.get_path(), ".anikoto_cache"))
        if self._log_window is not None:
            self._log_window.close()
        e.accept()


# ------------------------------------------------------------- Main ---
def main():
    if sys.platform == "win32":
        try:  # own taskbar identity so the window icon shows correctly
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MYG.Anikoto.DownloadManagerPro")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setFont(font(10))
    app.setStyleSheet(APP_QSS)
    win = AnikotoDownloaderApp()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
