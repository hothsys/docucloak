# Automatic PII detection

The **Also find automatically** row adds these detectors to a run. Use them with your own terms, or with none at all:

| Type | What it accepts | Default |
| --- | --- | --- |
| Email address | `name@example.com`, including `+` tags and subdomains | On |
| Credit card number | 13-19 digits, with or without spaces or hyphens. Requires a Luhn checksum and a major issuer prefix (or a nearby card label). A grouped number with a known issuer is accepted despite a failed checksum only when a card label is nearby. A number introduced as an account, invoice, reference or similar identifier is not treated as a card | On |
| US Social Security number | The `123-45-6789` (or space-separated) shape. A bare 9-digit number is only treated as an SSN when `SSN`, `Social Security` or `Taxpayer ID` appears next to it and the SSA range rules pass | On |
| US phone number | 10 digits, with optional `1`/`+1`, parentheses, dots, spaces or hyphens, and an optional extension. The area code must start with 2-9 | Off |

Digit count separates the ambiguous cases: an SSN is 9 digits and a US phone is 10, so a hyphenless `123456789` is never treated as a phone, while a hyphenless `5551234567` is checked only when phone detection is on.

Counts are occurrences, not unique values: if the same number appears on every page, it is counted each time.

Detection runs on PDF text and on the OCR text from scanned pages and images, so it works the same way as your terms. It is best-effort and US-centric. It does not find names, addresses, dates of birth or account/passport numbers; add those as terms. See [Known limitations](limitations.md).
