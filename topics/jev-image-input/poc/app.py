"""Local, single-worker FastAPI POC. No Jev calls occur on import or startup."""

import asyncio
import base64
import binascii
import hashlib
import importlib.metadata
import io
import json
import logging
import math
import os
import platform
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image as PILImage
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typesafe_sdk import AsyncTypeSafeClient, Image, RetryPolicy

from domain import (
    BOX_PROMPT, CRITERIA, INPUT_PRICE_PER_MILLION, MODEL, OPERATOR_CONTEXT, PRESENCE_PROMPT,
    SUBJECTS_PROMPT, decide, mock_response, summarize, video_box,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
MAX_BODY = 9 * 1024 * 1024
VERSIONS = ("fastapi", "starlette", "uvicorn", "typesafe-sdk", "httpx2", "httpcore2", "anyio", "pydantic", "Pillow", "python-dotenv")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def versions() -> dict:
    return {name: importlib.metadata.version(name) for name in VERSIONS}


def code_hashes() -> dict:
    names = ["app.py", "domain.py", "summarize_run.py", "requirements.txt", "requirements-lock.txt", "static/index.html", "static/app.js", "static/styles.css"]
    return {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in names if (HERE / name).is_file()}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DiagnosticQuestion(StrictModel):
    type: Literal["noul", "choice", "box"]
    instructions: str = Field(min_length=10, max_length=2000)
    criteria: dict[str, str] | None = None


class RunSettings(StrictModel):
    mode: Literal["mock", "live"] = "mock"
    max_frames: int = Field(default=8, ge=1, le=120)
    interval_seconds: float = Field(default=1, ge=0.25, le=60)
    max_edge: int = Field(default=960, ge=280, le=1568)
    presence_threshold: float = Field(default=0.85, ge=0, le=1)
    min_box_confidence: float | None = Field(default=None, ge=0, le=1)
    input_token_limit: int = Field(default=20_000, ge=1, le=2_000_000)
    presence_prompt: str = Field(default=PRESENCE_PROMPT, min_length=10, max_length=2000)
    subjects_prompt: str = Field(default=SUBJECTS_PROMPT, min_length=10, max_length=2000)
    box_prompt: str = Field(default=BOX_PROMPT, min_length=10, max_length=2000)
    operator_context: str | None = Field(default=None, min_length=10, max_length=1000)
    clip_name: str = Field(default="synthetic-demo", max_length=200)
    clip_size_bytes: int = Field(default=0, ge=0)
    clip_duration_seconds: float = Field(default=0, ge=0)
    source_width: int = Field(default=0, ge=0, le=100_000)
    source_height: int = Field(default=0, ge=0, le=100_000)
    diagnostic_questions: dict[str, DiagnosticQuestion] | None = Field(default=None, min_length=1, max_length=8)


class SourceRegion(StrictModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    source_width: int = Field(gt=0, le=100_000)
    source_height: int = Field(gt=0, le=100_000)


class FramePayload(StrictModel):
    index: int = Field(ge=0, le=119)
    timestamp_seconds: float = Field(ge=0)
    image_base64: str = Field(min_length=4, max_length=8 * 1024 * 1024)
    capture_ms: float = Field(default=0, ge=0, le=600_000)
    region: SourceRegion | None = None


class ClientTiming(StrictModel):
    http_roundtrip_ms: float = Field(ge=0, le=600_000)
    end_to_end_ms: float = Field(ge=0, le=600_000)


class FixedVideoMetadata(StrictModel):
    duration_seconds: float = Field(gt=0)
    width: int = Field(gt=0, le=100_000)
    height: int = Field(gt=0, le=100_000)


def decode_frame(data: str) -> tuple[bytes, str, int, int]:
    try:
        binary = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "Frame must be plain base64-encoded PNG/JPEG, without a data URL prefix.")
    if len(binary) > 6 * 1024 * 1024:
        raise HTTPException(413, "POC frame limit is 6 MiB.")
    try:
        with PILImage.open(io.BytesIO(binary)) as image:
            width, height = image.size
            fmt = image.format
            if fmt not in ("PNG", "JPEG") or getattr(image, "is_animated", False):
                raise HTTPException(422, "Use a still PNG or JPEG.")
            if not (280 <= width <= 1568 and 280 <= height <= 1568):
                raise HTTPException(422, "Both image dimensions must be between 280 and 1568 pixels.")
            if image.getexif().get(274, 1) != 1:
                raise HTTPException(422, "Rotate image pixels upright before sending; EXIF rotation is not applied.")
            image.load()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Image bytes could not be decoded.")
    return binary, "png" if fmt == "PNG" else "jpg", width, height


class SDKGateway:
    def __init__(self, key: str):
        # No hidden retries; no debug body/header logging; explicit official endpoint.
        self.client = AsyncTypeSafeClient(
            api_key=key, model=MODEL, base_url="https://api.typesafe.ai",
            retry=RetryPolicy(max_retries=0), timeout=30, compression=False,
        )

    async def analyze(self, binary: bytes, request: dict) -> tuple[dict, str | None]:
        result = await self.client.system_one(
            state=request["state"],
            questions=request["questions"],
            assets={request["state"]["frame"]: Image(binary)},
            model=MODEL,
            extra_headers={"TypeSafe-Danger-Zone-Images": "true"},
        )
        # request_id raises TypeSafeError when the optional header is absent.
        # Preserve the raw parsed JSON too: model_dump can discard future fields.
        raw = result.raw_http_response
        return raw.json(), raw.headers.get("x-typesafe-request-id")

    async def close(self):
        await self.client.aclose()


def create_app(*, data_dir: Path | None = None, gateway=None, load_env: bool = True,
               fixed_video_path: Path | None = None, fixed_settings: RunSettings | None = None) -> FastAPI:
    storage = data_dir or HERE / "runs"
    video_path = fixed_video_path or HERE / "assets" / "sample_video.mp4"
    # Deliberately server-owned: the recording UI has no filters/settings/mode selection.
    base_settings = fixed_settings or RunSettings(max_frames=120, input_token_limit=100_000,
                                                  min_box_confidence=0.80)
    preset = base_settings.model_copy(update={
        "mode": "live", "operator_context": base_settings.operator_context or OPERATOR_CONTEXT,
        "min_box_confidence": .80 if base_settings.min_box_confidence is None else base_settings.min_box_confidence,
    })

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Tests inject a gateway and load_env=False; they never read the root .env.
        settings = dotenv_values(ROOT / ".env") if load_env else {}
        app.state.key = os.environ.get("TYPESAFE_API_KEY") or settings.get("TYPESAFE_API_KEY") or ""
        app.state.gateway = gateway
        app.state.runs = {}
        app.state.api_slot = asyncio.Semaphore(1)
        logging.getLogger("typesafe_sdk").setLevel(logging.WARNING)
        try:
            yield
        finally:
            if app.state.gateway is not None and gateway is None:
                await app.state.gateway.close()

    app = FastAPI(title="Jev Frame Lab", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo whole base64 frames into validation errors.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()
        ]})

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse(status_code=403, content={"detail": "Use the page served by this local backend."})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def get_run(run_id: UUID):
        run = app.state.runs.get(str(run_id))
        if run is None:
            raise HTTPException(404, "Run is not active in this server process. Saved evidence remains on disk.")
        return run

    def save(run):
        write_json(run["path"] / "manifest.json", {
            **run["manifest"], "summary": summarize(list(run["records"].values())),
        })

    @app.get("/")
    async def index():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/static/{name}")
    async def static_file(name: Literal["app.js", "styles.css"]):
        return FileResponse(HERE / "static" / name)

    @app.get("/styles.css")
    async def stylesheet():
        return await static_file("styles.css")

    @app.get("/app.js")
    async def javascript():
        return await static_file("app.js")

    @app.get("/api/config")
    async def config():
        return {
            "model": MODEL, "key_configured": bool(app.state.key), "versions": versions(),
            "defaults": RunSettings().model_dump(), "criteria": CRITERIA,
            "live_ready": bool(app.state.key or gateway),
            "fixed_settings": preset.model_dump(),
            "video": {"available": video_path.is_file(), "name": video_path.name, "url": "/api/video",
                      "size_bytes": video_path.stat().st_size if video_path.is_file() else None},
            "input_price_per_million_usd": INPUT_PRICE_PER_MILLION,
            "pricing_checked": "2026-10-19", "retries": 0,
        }

    @app.get("/api/video")
    async def fixed_video():
        if not video_path.is_file():
            raise HTTPException(404, "Add the fixed video at poc/assets/sample_video.mp4.")
        return FileResponse(video_path)

    @app.post("/api/fixed-runs")
    async def start_fixed_run(metadata: FixedVideoMetadata):
        if not video_path.is_file():
            raise HTTPException(404, "The fixed video is missing.")
        settings = preset.model_copy(update={
            "max_frames": min(preset.max_frames, math.ceil(metadata.duration_seconds / preset.interval_seconds)),
            "clip_name": video_path.name, "clip_size_bytes": video_path.stat().st_size,
            "clip_duration_seconds": metadata.duration_seconds,
            "source_width": metadata.width, "source_height": metadata.height,
        })
        result = await start_run(settings)
        run = get_run(UUID(result["run_id"]))

        def source_hash():
            digest = hashlib.sha256()
            with video_path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()

        run["manifest"]["fixed_video"] = {"name": video_path.name, "sha256": await asyncio.to_thread(source_hash)}
        save(run)
        return result

    @app.post("/api/runs")
    async def start_run(settings: RunSettings):
        if settings.mode == "live" and not (app.state.key or gateway):
            raise HTTPException(409, "TYPESAFE_API_KEY is not configured on the backend.")
        if settings.mode == "live" and app.state.gateway is None:
            try:
                app.state.gateway = SDKGateway(app.state.key)
            except Exception:
                raise HTTPException(409, "The SDK could not initialize. Check the backend key and pinned dependencies.")
        run_id = str(uuid4())
        path = storage / run_id
        path.mkdir(parents=True)
        manifest = {
            "schema_version": 1, "run_id": run_id, "created_at": now(), "status": "active",
            "settings": settings.model_dump(), "requested_model": MODEL,
            "versions": versions(), "python": platform.python_version(), "platform": platform.platform(),
            "installed_dependencies": dict(sorted((d.metadata["Name"], d.version) for d in importlib.metadata.distributions())),
            "code_sha256": code_hashes(), "retries": 0, "compression": False,
            "api_base_url": "https://api.typesafe.ai",
            "pricing": {"input_usd_per_million": INPUT_PRICE_PER_MILLION, "checked": "2026-10-19", "output_usd": 0},
            "measurement_limits": [
                "SDK time includes serialization, upload, network, server, decoding; not pure inference.",
                "Unknown/failed-call usage is not zero; cost is for reported input usage only.",
                "No ground-truth labels or accuracy calculation are supplied by this viewer.",
                "Frames are sampled independently; boxes are not tracking between frames.",
                "Input-token limit is checked between calls and may be exceeded by one call; unknown usage stops live sampling.",
            ],
        }
        run = {"path": path, "settings": settings, "manifest": manifest, "records": {}, "lock": asyncio.Lock()}
        app.state.runs[run_id] = run
        save(run)
        return {"run_id": run_id, "manifest": manifest}

    @app.post("/api/runs/{run_id}/frames")
    async def analyze_frame(run_id: UUID, request: Request):
        started = time.perf_counter()
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "Frame request is too large.")
        try:
            frame = FramePayload.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Invalid frame metadata/base64 payload.")
        binary, fmt, width, height = decode_frame(frame.image_base64)
        digest = hashlib.sha256(binary).hexdigest()
        run = get_run(run_id)
        async with run["lock"]:
            settings = run["settings"]
            region = frame.region.model_dump() if frame.region else {
                "x": 0, "y": 0,
                "width": settings.source_width or width, "height": settings.source_height or height,
                "source_width": settings.source_width or width, "source_height": settings.source_height or height,
            }
            if (region["x"] + region["width"] > region["source_width"]
                    or region["y"] + region["height"] > region["source_height"]
                    or (settings.source_width and region["source_width"] != settings.source_width)
                    or (settings.source_height and region["source_height"] != settings.source_height)):
                raise HTTPException(422, "Image region must lie inside the original source frame.")
            prior = run["records"].get(frame.index)
            if prior:
                if (prior["image"]["sha256"] != digest or prior["timestamp_seconds"] != frame.timestamp_seconds
                        or prior["image"]["region"] != region):
                    raise HTTPException(409, "Frame index already belongs to different pixels, timestamp or source region.")
                return {"record": prior, "summary": summarize(list(run["records"].values())), "cached": True}
            if run["manifest"]["status"] != "active":
                raise HTTPException(409, "Run is closed; start another run.")
            if frame.index != len(run["records"]) or frame.index >= settings.max_frames:
                raise HTTPException(409, "Frame index must be sequential and within this run's frame cap.")
            summary = summarize(list(run["records"].values()))
            if settings.mode == "live" and (
                summary["known_input_tokens"] >= settings.input_token_limit or summary["missing_input_usage"] > 0
            ):
                raise HTTPException(409, "Run stopped at token/unknown-usage boundary. Export evidence before starting another run.")
            alias = f"<frame:{digest}>"
            semantic = {
                "model": MODEL,
                "state": {"frame": alias, "timestamp_seconds": frame.timestamp_seconds},
                "questions": {
                    "visible": {"type": "noul", "instructions": settings.presence_prompt},
                    "subjects": {"type": "choice", "instructions": settings.subjects_prompt, "criteria": CRITERIA},
                    "person": {"type": "box", "instructions": settings.box_prompt},
                },
                "assets": {alias: {"type": "image", "part": "frame-file"}},
            }
            if settings.operator_context:
                semantic["state"]["operator_context"] = settings.operator_context
            if settings.diagnostic_questions:
                semantic["questions"] = {
                    name: question.model_dump(exclude_none=True)
                    for name, question in settings.diagnostic_questions.items()
                }
            frame_name = f"{frame.index:04d}.{fmt}"
            (run["path"] / frame_name).write_bytes(binary)
            prep_ms = (time.perf_counter() - started) * 1000
            response, error, request_id, sdk_ms, queue_ms = {}, None, None, None, 0.0
            if settings.mode == "mock":
                response = mock_response(frame.index)
            else:
                queue_start = time.perf_counter()
                async with app.state.api_slot:
                    queue_ms = (time.perf_counter() - queue_start) * 1000
                    call_start = time.perf_counter()
                    try:
                        response, request_id = await app.state.gateway.analyze(binary, semantic)
                    except Exception as exc:
                        # Error strings/headers may contain secrets or entire frames. Keep safe metadata only.
                        status = getattr(exc, "status", None)
                        error = {"type": type(exc).__name__, "http_status": status if isinstance(status, int) else None}
                        request_id = getattr(exc, "request_id", None)
                    finally:
                        sdk_ms = round((time.perf_counter() - call_start) * 1000, 3)
            decision = decide(response, settings.presence_threshold, settings.min_box_confidence) if error is None else {
                "status": "api_error", "draw": False, "box": None,
                "presence_probability": None, "subjects": None, "box_confidence": None,
            }
            if settings.diagnostic_questions and error is None:
                decision = {"status": "diagnostic_only", "draw": False, "box": None,
                            "presence_probability": None, "subjects": None, "box_confidence": None}
            decision["video_box"] = video_box(decision.get("box"), region)
            record = {
                "index": frame.index, "timestamp_seconds": frame.timestamp_seconds, "recorded_at": now(),
                "mode": settings.mode, "request_id": request_id,
                "image": {"file": frame_name, "sha256": digest, "width": width, "height": height, "bytes": len(binary), "region": region},
                "request": semantic, "request_note": "Semantic SDK input; part names/framing are reconstructed, not a raw HTTP capture. Image file preserves exact bytes.",
                "response": response, "error": error, "decision": decision,
                "timing": {"capture_ms": frame.capture_ms, "preparation_ms": round(prep_ms, 3), "queue_ms": round(queue_ms, 3), "sdk_call_ms": sdk_ms,
                           "backend_ms": round((time.perf_counter() - started) * 1000, 3)},
                "client_timing": None,
            }
            run["records"][frame.index] = record
            write_json(run["path"] / f"{frame.index:04d}.json", record)
            save(run)
            return {"record": record, "summary": summarize(list(run["records"].values())), "cached": False}

    @app.post("/api/runs/{run_id}/frames/{index}/timing")
    async def client_timing(run_id: UUID, index: int, timing: ClientTiming):
        run = get_run(run_id)
        async with run["lock"]:
            record = run["records"].get(index)
            if record is None:
                raise HTTPException(404, "Frame not found.")
            # First observed measurement wins; reloading must not rewrite the original timing.
            if record["client_timing"] is None:
                record["client_timing"] = timing.model_dump()
                write_json(run["path"] / f"{index:04d}.json", record)
                save(run)
            return {"record": record, "summary": summarize(list(run["records"].values()))}

    @app.post("/api/runs/{run_id}/finish")
    async def finish(run_id: UUID):
        run = get_run(run_id)
        async with run["lock"]:
            records = list(run["records"].values())
            summary = summarize(records)
            if summary["errors"]:
                reason = "api_error"
            elif summary["missing_input_usage"]:
                reason = "unknown_usage"
            elif summary["frames"] >= run["settings"].max_frames:
                reason = "completed"
            elif summary["known_input_tokens"] >= run["settings"].input_token_limit:
                reason = "input_token_limit"
            else:
                reason = "interrupted"
            run["manifest"].update(
                status="closed", closed_at=now(), finish_reason=reason,
                last_timestamp_seconds=records[-1]["timestamp_seconds"] if records else None,
            )
            save(run)
            return {"summary": summary, "finish_reason": reason}

    @app.get("/api/runs/{run_id}/export")
    async def export(run_id: UUID):
        # Read persistent evidence too, so an export remains available after restart.
        path = storage / str(run_id)
        if not (path / "manifest.json").is_file():
            raise HTTPException(404, "Saved run not found.")
        data = {"manifest": json.loads((path / "manifest.json").read_text()),
                "frames": [json.loads(p.read_text()) for p in sorted(path.glob("[0-9][0-9][0-9][0-9].json"))]}
        return Response(json.dumps(data, indent=2, allow_nan=False), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="jev-run-{run_id}.json"'})

    return app


app = create_app()
