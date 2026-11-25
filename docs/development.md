# Tests

```bash
pip install pytest playwright   # playwright is optional: it runs the UI tests in your installed Google Chrome
python3 -m pytest
python3 -m pytest -m 'not browser'   # skip the Chrome UI tests
```

Tests build their own sample files in temporary directories and never touch your documents. Without Chrome the UI tests are skipped, and without Tesseract the real-OCR test is skipped.
