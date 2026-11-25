# Getting started

## Requirements

- macOS (DocuCloak is plain Python with its own in-page file browser, so it may also run on Windows and Linux, but only macOS is tested)
- Python 3.10+
- [Tesseract](https://github.com/tesseract-ocr/tesseract), needed for images and scanned PDFs: `brew install tesseract`. A badge in the top-right corner of the app shows whether it was found; if not, a help window explains how to install it, and scanned PDFs and images are reported as **error** (never as skipped) while regular PDFs are still redacted. DocuCloak looks on your PATH and in `/opt/homebrew/bin` and `/usr/local/bin`, and checks again each time the page loads, so there's no need to restart after installing

On first run, DocuCloak installs PyMuPDF, Pillow and pytesseract if they're missing. To install the exact versions the app is tested with:

```bash
pip install -r requirements.txt
```

## Running

```bash
./server.sh start      # start in the background and open http://localhost:5903
./server.sh stop
./server.sh restart
./server.sh status
./server.sh logs       # follow server.log
```

Or double-click `docucloak.command` in Finder (press Return in its Terminal window to stop), or run `python3 app.py` in the foreground (Ctrl+C to stop).

If macOS says it "could not verify `docucloak.command` is free of malware", the folder arrived by AirDrop or a downloaded ZIP, which macOS marks as quarantined (a `git clone` doesn't). The launcher is a short, readable shell script; to allow it, run this in the DocuCloak folder:

```bash
xattr -dr com.apple.quarantine .
```

Or click **Open Anyway** in System Settings > Privacy & Security after the warning, or start DocuCloak with `./server.sh start` instead.

After you update DocuCloak's files (for example by copying in a newer version), `./server.sh start` and `docucloak.command` notice the change and restart the server. If the page says DocuCloak "is still running the old version", run `./server.sh restart` and reload the page.

To change the port, edit `PORT` in `app.conf`.

