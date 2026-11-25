<p align="center">
  <img src="logo.svg" alt="DocuCloak logo" width="220">
</p>

<h1 align="center">DocuCloak</h1>

A web app that runs on your Mac and redacts PDFs and images. Point it at an input directory, give it a list of terms or turn on automatic PII detection, and it writes blacked-out copies of every matching file to an output directory. Your originals are never modified.

## Quick Start

Requires macOS, Python 3.10+ and [Tesseract](https://github.com/tesseract-ocr/tesseract) (`brew install tesseract`), which is needed for images and scanned PDFs. DocuCloak installs PyMuPDF, Pillow and pytesseract on first run if they are missing.

```bash
./server.sh start      # start in the background and open http://localhost:5903
./server.sh stop       # stop it
```

You can also double-click `docucloak.command` in Finder, or run `python3 app.py` in the foreground. Set the input and output directories in the page, enter terms or turn on automatic detection, and click **Start Redaction**. More on running, requirements and usage is in [Getting started](docs/getting-started.md) and [Usage](docs/usage.md).

## Screenshots

<img width="1706" height="1594" alt="image" src="https://github.com/user-attachments/assets/0556343e-ba93-4e77-b9da-378ee41ad9a1" />

## Documentation

Full documentation lives in the [`docs/`](docs/) folder:

| Page | What it covers |
| --- | --- |
| [Getting started](docs/getting-started.md) | Requirements, starting and stopping, quarantine prompts, changing the port |
| [Features](docs/features.md) | Feature overview |
| [Usage](docs/usage.md) | Directories, terms, results |
| [Automatic PII detection](docs/pii-detection.md) | Emails, cards, SSNs and US phone numbers: what each detector accepts |
| [Performance](docs/performance.md) | How jobs are split and scheduled across CPU cores |
| [Known limitations](docs/limitations.md) | What detection and redaction do not cover |
| [Tests](docs/development.md) | Running the test suite |

## License

This project is licensed under the [MIT](LICENSE.md) License. See the [LICENSE.md](LICENSE.md) file for details.
