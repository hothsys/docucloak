# Feature overview

- Recursively scans the input directory for `.pdf`, `.jpg`/`.jpeg`, `.png`, `.tif`/`.tiff` and `.bmp` files
- **PDFs:** matched page text is removed from the file, not just covered, and a black box is drawn in its place (PyMuPDF)
- **Scanned PDFs:** pages without a text layer are read with Tesseract OCR, and the image pixels under each match are blacked out. So are pages whose text can't be read back (fonts embedded without a character map, where you can't select the text in Preview) and text turned into outlines
- **Images:** text is read with Tesseract OCR and matches are covered with black boxes
- **Hidden PDF content:** every redacted PDF copy has its document properties (title, author and so on, including XMP metadata) and bookmarks removed, and is rewritten so removed content can't be recovered from the file. A term or detected value found only in those places still produces a cleaned copy
- Case-insensitive matching; a term also matches inside longer words (`secret` matches `secret123`). Multi-word terms like `Alice Smith` work in PDFs, scans and images
- Terms come from a text field, a terms file, or both. The terms file is re-read from disk on every run, so you can edit it and run again
- **Automatic PII detection (optional):** find email addresses, credit card numbers, US Social Security numbers and US phone numbers without listing each value. Email, cards and SSNs are on by default; phone detection is off because plain 10-digit numbers are ambiguous. Detected values use the same search-and-redact path as your terms, and only per-type counts are shown or logged-never the values themselves
- Copies are written as `<name>_redacted.<ext>` in the output directory, in the same subfolders as in the input directory (so same-named files in different folders never collide). The output directory is created if it doesn't exist
- Files with no matches are skipped: no copy is written. If a file no longer matches after you change the terms, its old `_redacted` copy is deleted, so the output directory always reflects the latest terms
- Live progress: which file(s) are being redacted right now, what was found (PDFs, scanned PDFs, images), how many files are done, elapsed time and an estimate of the time left, a progress bar, and each file's result in plain language. Cancel stops the current job
- Show files: click a file in the results to see it in Finder (File Explorer on Windows): the redacted copy, or the original if nothing was saved. **Show output folder** opens the output directory
- Password-protected PDFs, unreadable files and files that crash the PDF library are reported as errors without stopping the rest of the job
- Remembers the directories and terms file location between visits; the terms themselves are never saved
- Detailed log in `server.log`: UI actions, each file's outcome, and the number of terms used (never the terms themselves). It rotates at 5 MB, keeping the 3 most recent old logs (`server.log.1` to `server.log.3`)
- Only reachable from your own Mac: the server doesn't accept connections from other devices on your network
