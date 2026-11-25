# Usage

1. Set the **Input directory** and the **Output directory**. Type a path or click **Browse** to pick a folder in DocuCloak's file browser, which opens at the folder currently in the field (or your home folder if that folder doesn't exist) and has shortcuts to Home, Desktop, Documents and Downloads. The output directory must be a different folder from the input directory; it may be inside it, and files already in it are never scanned.
2. Enter terms in the text field (comma or newline separated), and/or click **Choose File** to pick a terms file in the same format. Optionally choose what to find under **Also find automatically**.
3. Click **Start Redaction**, then check the results list: each file shows **redacted** (with its output name and any automatic PII counts), **skipped: no matches**, or **error** with the reason. At least one term or one detector is required.

If no terms are given and a `redact_terms.txt` file exists in the DocuCloak folder, its terms are used instead.
