# Video frames with Jev

A local Python/FastAPI + HTML proof of concept that sends video frames to Jev and draws confidence-gated person boxes from its responses.

The current demo supplies the context that a man is holding a firearm. Jev is asked to locate the visible person. This is **context-assisted localization**, not a claim of independent firearm recognition or production surveillance performance.

## Project layout

```text
topics/jev-image-input/
├── idea.md                 # Public method and scope notes
└── poc/
    ├── app.py              # FastAPI and TypeSafe SDK integration
    ├── domain.py           # Questions, gates and measurement helpers
    ├── static/             # Single-page HTML/CSS/JavaScript UI
    ├── tests/              # Offline backend/browser checks
    ├── assets/             # Add your local video here
    └── requirements*.txt   # Pinned dependencies
```

The original folder layout is preserved so the POC's environment-file discovery and relative paths work without modifying its application code.

## Requirements

- Python 3.14 was used for the recorded development environment.
- A TypeSafe API key and access to `jev-1.14.0`.
- A browser-supported video, such as MP4/H.264.

## Run locally

From this repository's root:

```sh
python3 -m venv --without-scm-ignore-files .venv
.venv/bin/python -m pip install -r topics/jev-image-input/poc/requirements-lock.txt
cp .env.example .env
```

Set `TYPESAFE_API_KEY` in `.env`. The key stays on the backend. The virtualenv option above is supported by the tested Python 3.14 and prevents creation of extra ignore files.

Place your permitted video at:

```text
topics/jev-image-input/poc/assets/sample_video.mp4
```

The sample media is local-only and is not distributed in this repository.

Start the server:

```sh
.venv/bin/python -m uvicorn app:app --app-dir topics/jev-image-input/poc --host 127.0.0.1 --port 8880
```

Open **http://127.0.0.1:8880** and press **Start**. Page/video loading makes no model calls; Start submits live requests using API credit.

## How it works

1. Decode the fixed video in the browser and sample JPEG frames.
2. Send one shared image plus explicit operator context to Jev.
3. Ask visibility, unique-holder and person-Box questions.
4. Require visibility ≥0.85, a unique match, valid edges and Box confidence ≥0.80.
5. Draw the returned box on the same paused frame, then advance.

The page shows input/output JSON, reported token usage and request timings. It is a sampled, inference-paced demonstration, not continuous tracking. Thresholds are initial application settings, not calibrated guarantees.

## Evidence and tests

Run records are created locally under `topics/jev-image-input/poc/runs/`. They include submitted frames, settings, source/code hashes, responses, decisions, usage and timings. Media, credentials, environments and run outputs are ignored by Git.

Run the offline backend tests without a real API key or model requests:

```sh
.venv/bin/python -m pytest topics/jev-image-input/poc/tests/test_poc.py -q -p no:cacheprovider
```

Optional browser checks use Playwright and an existing Chrome installation:

```sh
.venv/bin/python -m pip install -r topics/jev-image-input/poc/requirements-browser.txt
.venv/bin/python -m pytest topics/jev-image-input/poc/tests/test_browser.py -q -p no:cacheprovider
```

Recompute a saved run's measurements without resubmitting images:

```sh
.venv/bin/python topics/jev-image-input/poc/summarize_run.py topics/jev-image-input/poc/runs/RUN_ID
```

See the [POC guide](topics/jev-image-input/poc/README.md) and [method notes](topics/jev-image-input/idea.md) for measurement definitions and limitations.

## Documentation

- https://typesafe.ai/blog/jev-1-14
- https://docs.typesafe.ai/images
- https://docs.typesafe.ai/primitives/box
- https://docs.typesafe.ai/models

AI assistance was used in POC development and documentation. Any reported measurements should be traced to actual saved responses, with fixture outputs kept distinct from real model observations.
