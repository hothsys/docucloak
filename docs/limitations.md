# Known limitations

Check redacted output before sharing it.

- **Automatic detection is best-effort.** It is tuned for high precision, but no detector is perfect and it only knows the formats described above. Review the redacted copy.
- **Account, invoice and reference numbers are not cards.** A number introduced by one of those labels is left for your terms even when it is 13-19 digits, unless a card label such as `Card` or `Visa` is next to it.
- **A few unlabelled account numbers can still look like cards.** If a card-shaped number has no account label and happens to pass the card checksum, it can be detected. Check the counts if your documents contain many long account numbers.
- **Phone detection is noisy.** Any 10-digit number (an order, account or ticket number) can look like a US phone. Leave it off unless your documents contain phone numbers.
- **Bare 9-digit SSNs need a label.** Without `SSN`, `Social Security` or `Taxpayer ID` nearby, a 9-digit number is left alone because ZIP+4s, EINs and invoice numbers share its shape. Separated SSNs are redacted by shape.
- **Only US phone numbers are recognized.** International numbers and unusual extension formats are not detected.
- **Detection does not find names or addresses.** Those still need to be entered as terms.
- **OCR can miss text.** Scanned pages and images are only as good as Tesseract's reading of them: faint, skewed, low-resolution or handwritten text may not be recognized, and only English is installed by default.
- **Images inside text pages are not OCR'd.** OCR runs on PDF pages with no text layer; on a page that has text, an embedded image (such as a scanned signature or stamp) is not searched.
- **Other hidden PDF content is not searched.** Comments and other annotations, form field values and file attachments are not checked for terms.
- **Password-protected PDFs are not redacted.** They are reported as errors; remove the password first.
