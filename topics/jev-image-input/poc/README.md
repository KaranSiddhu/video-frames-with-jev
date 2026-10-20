# Jev Frame Lab

A local FastAPI + `typesafe_sdk` + HTML proof of concept for **Can Jev Tell You Where to Look?** See [the topic notes](../idea.md) for the research question and drafting decision.

The target is **the visible man holding the gun**, not the gun itself. The gun label is supplied by the operator in request state; Jev's task is context-assisted person localization, not independently proving the object is a gun. This is a local blog demo: **one fixed video and one Start button**, input/output JSON on the right and usage/timing below. It is not intended for deployment.

### Recording layout

The main page fits the viewport without page scrolling: **video on the left, input and output JSON on the right, token/timing totals below**. Desktop layouts are checked at 1920×1080, 1440×900, 1280×720 and 1024×768. There are no uploads, settings, selectors, exports, expansion buttons, or native video controls in the interface.

**Start** advances the fixed video through sampled timestamps. Video is paused on each analyzed frame while Jev answers; only then is an accepted person border drawn over that same video frame. A 650 ms presentation hold lets the response be seen before advancing. This is inference-paced sampling, not continuous tracking. Long JSON scrolls inside its panel. Idle instructions and the idle status badge are hidden; active progress/errors remain visible. Filenames, paths and key status are not shown.

## Start here

Dependencies have been installed in this POC's isolated `.venv` with Karan's permission. From the **blogging workspace root**:

```sh
topics/jev-image-input/poc/.venv/bin/python -m uvicorn app:app --app-dir topics/jev-image-input/poc --host 127.0.0.1 --port 8880
```

Open **http://127.0.0.1:8880** and click **Start** to submit live Jev requests. The fixed source is `poc/assets/sample_video.mp4` (lowercase). Current asset: 10.96s, 3840×2160, SHA-256 `2d21f370d5541c5495fb5d36dba74484e539ef6773b9bfdf8a723de804a6ed5b`. Replacing the file requires a page refresh; each new run records the active source hash. No model call occurs on page load.

Open the backend URL, not `static/index.html` by double-clicking it or through an editor's separate Live Server. CSS can load in a file preview, but video and API requests require FastAPI. After backend code changes, restart the server. Hard-refresh an older tab with **Cmd+Shift+R** on macOS or **Ctrl+Shift+R** on Windows/Linux.

The server loads `TYPESAFE_API_KEY` from the workspace-root `.env`, with an existing environment variable taking precedence. It never serves `.env` or sends the key to the browser. Key status is not printed on the public-facing page; missing configuration leaves Start disabled and shows a plain availability message. Restart after changing the key. Loading the fixed video and serving the page do not make model calls. Tests never read the root `.env`.

Use one server process/worker. Do not restart/reload it during an active run: active-run locks/counters are process-local. Completed evidence remains on disk and can still be exported after restart via its saved run ID.

### Recreate the environment

Tested with **Python 3.14.7 on macOS arm64**. Exact resolved dependencies are in [requirements-lock.txt](requirements-lock.txt); [requirements.txt](requirements.txt) pins the top-level choices. From the workspace root:

```sh
python3 -m venv --without-scm-ignore-files topics/jev-image-input/poc/.venv
topics/jev-image-input/poc/.venv/bin/python -m pip install -r topics/jev-image-input/poc/requirements-lock.txt
```

`--without-scm-ignore-files` is available on the tested Python 3.14; it prevents virtualenv creation from changing ignore rules. No ignore files were changed by this setup. Keep environment directories out of any future staging/publication.

## Fixed analysis workflow

1. The page loads the server-owned video from `/api/video`, including HTTP range support for browser seeking. Start is enabled only once the video is decodable and analysis is configured.
2. Start creates a live run through `/api/fixed-runs`. Only video duration/dimensions are submitted; prompts, sampling and gates come from the server-owned preset, not frontend filters. The full source-video SHA-256 is added to the manifest before analysis.
3. The browser seeks to a sample, clears the previous border and captures/resizes JPEG pixels. The backend hashes and stores the exact image. The last completed input/output JSON pair stays visible while the next call is pending, including across another Start; both panels update together when the response arrives. Until the first response, the initial `{}` placeholders remain. The video stays paused on the new sampled frame.
4. One shared image and `operator_context` supply the known gun label. The same call asks `visible` (target visibility/localizability Noul), `subjects` (matching-holder Choice: none/one/several/unclear), and `person` (tight full-visible-body Box). Context alone cannot establish visibility in a particular frame.
5. Only after the response arrives, draw when the Choice says one person, presence is ≥0.85, the Box is valid and Box confidence is ≥0.80. Missing confidence is withheld. These are initial policy values, not a ground-truth accuracy guarantee.
6. Display the latest raw output and update aggregates, persist client timing, hold the accepted border briefly, then clear it and advance to the next sampled timestamp. No predicted border is shown before the response or carried onto another frame.
7. Close and save the run automatically on completion/error/limit. Start becomes available again for a new, separately recorded run. There is no restart/resume of an interrupted run.

The fixed-video preset in `create_app` in [app.py](app.py) uses **1s intervals, a 120-frame cap, 960px longest edge, presence threshold 0.85, required Box confidence 0.80, and 100,000 reported-input-token boundary**. Sample count is `min(cap, ceil(video duration / interval))`; the current source schedules 11 samples at 0–10s. Prompts/gating are in [domain.py](domain.py). These are experiment settings, not calibrated thresholds. Token/unknown-usage boundaries still apply.

The verified assisted run accepted boxes at **3–10s** and withheld **0–2s**, without any timestamp-based drawing rule. Acceptance depends on the returned visibility/count/Box fields and may vary on another run. The UI subtitle and input JSON disclose the supplied gun context. The full result, raw evidence and limits are in [idea.md](../idea.md#context-assisted-question-change--2026-10-19). Public recordings must not describe this as independent weapon recognition.

The backend enforces at most 120 attempts per run and the chosen smaller cap. SDK retries are disabled. Frames are idempotent by run/index/pixel hash/timestamp: an identical retry returns the saved record, while conflicting pixels are rejected. No automatic browser retries occur. A failed live call or unknown input usage stops subsequent calls. Input-token limits are checked **between calls**; one call can cross the boundary, and this is not a hard dollar-spend cap.

## Image and overlay behavior

- Browser video seeking is useful for this POC but is **not guaranteed frame-exact**. Store the actual browser timestamp, not a claim of exact decoder-frame identity.
- Each frame is resized without cropping or padding to the configured longest edge and JPEG-encoded at quality 0.9. Both dimensions must be 280–1568 inclusive; very wide/tall aspect ratios may require a different backend preset. The backend checks format, dimensions, animation, integrity and upright orientation before a call. Local limit: 6 MiB/file.
- Coordinates are normalized. On the transparent overlay canvas:

  ```text
  x = left × width
  y = top × height
  boxWidth = (right - left) × width
  boxHeight = (bottom - top) × height
  ```

- Canvas backing dimensions equal the source video's decoded dimensions. For full-frame input, fractions map directly to the video. If a request supplies `region`, the backend maps crop-relative coordinates through the original-pixel rectangle into `decision.video_box`; the UI uses that video-relative box. Raw API coordinates are preserved. Video/canvas share contain-fit geometry; only an accepted red border is drawn over the actual paused video.
- A single Box is not an exhaustive detector. Several qualifying people are withheld rather than choosing someone arbitrarily. Continuous tracking, multiple person boxes, and temporal smoothing are outside this version.

## Timing and token definitions

| Measurement | Meaning |
| --- | --- |
| `timing.capture_ms` | Browser seek, resize, JPEG encoding and base64 extraction before fetch. Image SHA-256 is calculated by the backend. Earlier run manifests pin the code version used for their timings. |
| `timing.preparation_ms` | Backend body receive, frame validation/hash, run-lock wait, and saving image bytes before API-slot wait. |
| `timing.queue_ms` | Wait for the backend's single global live-call slot. |
| `timing.sdk_call_ms` | SDK serialization/asset handling, network upload, service, response download and parsing. Includes failed attempts; null for mock. **Not pure model inference time.** |
| `timing.backend_ms` | Backend handling through decision creation, before final JSON-record/manifest persistence and HTTP-response serialization. |
| `client_timing.http_roundtrip_ms` | Browser fetch through response JSON decode; includes backend logging/serialization and local network. |
| `client_timing.end_to_end_ms` | Capture + backend round trip; excludes initial video load/run creation/source-video hashing, the later timing-persistence request, and the 650ms presentation hold. |
| `usage.input_tokens` | Provider-reported text + image input tokens combined; this API does not supply a text/image breakdown here. |
| `usage.output_tokens` | Provider-reported output count; output is documented as free, not tokenless. |

Aggregates use **linear-interpolated p50/p95** with the displayed sample count. Live SDK percentiles include failures; they are not success-only latency. Unknown usage stays `null`; the displayed known subtotal is not presented as complete billing. Errors with no reported usage may still have incurred a charge.

Cost is an **estimate for reported input usage**, using $0.042 per million input tokens, documented on 2026-10-19. It excludes unreported/billed-only usage and is not an invoice. Record measurements for the active source in [idea.md](../idea.md); do not transfer results between different source hashes. Reference labels are required for accuracy claims.

## Saved evidence and reproduction

Runs are created on demand in `poc/runs/<uuid>/`:

```text
manifest.json       # settings, prompts, versions, platform, code hashes, summary
0000.jpg            # exact submitted frame bytes (or .png for a PNG client)
0000.json           # semantic input, hash, response/error, gate, timing
0001.jpg
0001.json
...
```

- SDK-generated multipart boundaries/part names are not a raw wire capture. The recorded semantic request plus exact image bytes preserves the intended model input. Successful response JSON is kept from `raw_http_response`, rather than losing future fields through `model_dump`.
- Optional request IDs are read from `x-typesafe-request-id`. In SDK 0.7.5, accessing `.request_id` can raise when missing; missing metadata must not discard an otherwise successful answer.
- Errors store class, HTTP status when available, and request ID, not exception strings/headers that could expose credentials or entire inputs. Raw unsuccessful HTTP bodies are not captured; affected billing remains unknown.
- `manifest.fixed_video.sha256` records the full source-video hash at run creation. Preserve the original video/source URL/license alongside sampled frames before article claims; do not replace the source file while a run is active. Model outputs are not human annotations.
- On closure, `manifest.finish_reason` distinguishes completion, token limit, unknown usage, API error and interruption; `last_timestamp_seconds` identifies the last analyzed sample. A run can be closed without having completed the clip; inspect these fields, not just `status=closed`.
- The presentation UI has no download/export controls. Evidence is automatically saved to the run directory. The retained `/api/runs/<uuid>/export` endpoint returns the manifest/records without embedded images; keep the frame files too.

Recompute aggregates without a key or network (from the workspace root):

```sh
topics/jev-image-input/poc/.venv/bin/python topics/jev-image-input/poc/summarize_run.py topics/jev-image-input/poc/runs/RUN_ID
```

The same script accepts an exported JSON file and reports whether recomputed metrics match its saved summary. It also reports ungated usable-box count versus gated overlays; neither is accuracy without labels.

## Offline validation

From the workspace root:

```sh
topics/jev-image-input/poc/.venv/bin/python -m pytest topics/jev-image-input/poc/tests/test_poc.py -q -p no:cacheprovider
node --check topics/jev-image-input/poc/static/app.js
```

Backend tests never load root `.env`. They use synthetic images, injected failures, and the **real SDK with an in-memory HTTP transport** to check multipart serialization/response parsing without TypeSafe calls. Cases include absent/multiple targets, invalid boxes, nullable confidence, duplicate requests, image rejection, run caps, timing persistence, partial usage, and exports after restart.

Optional browser tests use Playwright against an isolated localhost server, no key, no external API. This setup installed [requirements-browser.txt](requirements-browser.txt) with permission and used the existing Chrome channel (no browser installation or profile modifications). To reproduce:

```sh
topics/jev-image-input/poc/.venv/bin/python -m pip install -r topics/jev-image-input/poc/requirements-browser.txt
topics/jev-image-input/poc/.venv/bin/python -m pytest topics/jev-image-input/poc/tests/test_browser.py -q -s -p no:cacheprovider
```

Browser tests use a temporary server-owned WebM generated locally and an injected offline fixture gateway, never the user's footage/key or the actual provider. They verify exactly one button and no input/select/dialog controls, video load without provider calls, no border while the first response is deliberately blocked, border pixels after release, frame/timing/source-hash evidence, negative-frame gating, professional public copy, and viewport fit. Test screenshots/JSON clearly identify offline fixture responses and must not be shared as real Jev detections. Screenshots stay in the test temporary directory. Default browser channel is `chrome`; override `POC_BROWSER_CHANNEL` only for an already installed browser.

## Blog evidence checklist

1. **Images from video:** show the sampled frame, frame ID/hash/timestamp, exact questions, resolved model, raw output, gate, and correctly aligned **person** box. Include failures and negative/look-alike examples.
2. **Usage and timing:** preserve run evidence, unknown usage, timing boundaries, sample sizes and percentile method. Fix presets before comparisons. Separate offline tests, cold/first calls and later real calls. Explain that the presentation pauses/holds frames and is not real-time tracking.
3. **Patterns and tips:** shared image state; narrow parallel questions; nullable metadata; no hidden retries; cached client lifetime; idempotent frame IDs; exact coordinate space; frame-aligned results; honest partial totals. These are implemented patterns, not measured claims of improved accuracy or speed.

The context-assisted POC has a successful recorded run. Karan should review/record the UI and evidence, then make the drafting decision. It is not general evidence of reliable weapon recognition or localization; preserve the supplied-context, sample-size and confidence-policy limits in the article.
