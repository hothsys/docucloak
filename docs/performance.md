# Performance

DocuCloak uses every CPU core. Each job runs in three steps:

1. **Survey.** Every file is classified (text PDF, scanned PDF, mixed, image, password-protected) and its work estimated. This takes a fraction of a second, even for hundreds of files.
2. **Split.** Each scanned page becomes its own task and large text PDFs are searched in 25-page chunks, so no single big file holds up the job. Small files stay whole.
3. **Schedule.** Tasks run in worker processes, one per core, largest first, so the job never ends with one big file running alone. One worker serves the smallest files first, so results start appearing within a second.

On a 12-core Mac, a mixed folder of 48 files (30 text PDFs, 7 scanned or partly scanned PDFs, 10 photos and a password-protected PDF; 566 PDF pages) takes about 11 seconds, compared with about 60 seconds before. OCR is most of the work: a scanned page or photo takes about a second, a text page a few hundredths of a second.

The time-left estimate replays the remaining work on the available workers, calibrated as tasks finish to how fast this machine actually is. On the folders above it was typically within a second of the real time left; the first estimate appears within a second or two of starting.

Worker processes stop 2 minutes after the last job to give their memory back, and start again with the next job. To use fewer cores, set `WORKERS` in `app.conf`.
