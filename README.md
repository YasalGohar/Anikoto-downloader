# ANIKOTO Download Manager Pro
**Made by : MYG**

High-performance desktop anime batch downloader with zero-loss AES-128 stream decryption, anti-skip recovery engine, multi-fragment acceleration, and automated lossless FFmpeg remuxing.

---

## 🚀 Quick Start (Running from Source)

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.10 – 3.14)
- FFmpeg (bundled in releases or installed on PATH)

### 2. Install Dependencies
```powershell
pip install -r requirements.txt
```

### 3. Launch the Application
- **Qt6 Edition (Recommended - Ultra smooth 60fps & instant resize):**
  ```powershell
  python anikoto_qt.py
  ```
- **CustomTkinter Edition:**
  ```powershell
  python anikoto_gui.py
  ```

---

## 📦 Building Standalone .EXE

To compile into a single self-contained executable with icon and assets bundled:
```powershell
python -m PyInstaller --noconfirm "Anikoto Downloader.spec"
```
The compiled executable will be located in the `dist/` directory.

---

## ✨ Features
- **GPU-Accelerated Native GUI (Qt6)**: Fluid resizing, smooth animated audio selector (`SUB`, `DUB`, `HSUB`), cross-fading anime spotlight banners.
- **Top Rated Anime Spotlight**: Auto-rotating slideshow of trending animes with 1-click queue loading.
- **Batch Download Queue**: Concurrent fragment fetching, ETA & speed metrics, smart pause/resume, and safe cancellation without corrupting partial files.
- **Lossless Remuxing**: Automated FFmpeg merging of video streams and English subtitles into single standalone `.mp4` containers.
- **Mascot Sleep Mode**: Cute sleeping cat illustration and dynamic queue metrics.

