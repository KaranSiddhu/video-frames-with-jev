import base64
import io
import json
import hashlib

import httpx2
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from app import RunSettings, SDKGateway, create_app
from domain import decide, mock_response, summarize, video_box


def frame(index=0, size=(640, 360)):
    stream = io.BytesIO()
    Image.new("RGB", size, "white").save(stream, format="PNG")
    return {"index": index, "timestamp_seconds": index * 1.0,
            "image_base64": base64.b64encode(stream.getvalue()).decode(), "capture_ms": 12.5}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with TestClient(create_app(data_dir=tmp_path, load_env=False)) as client:
        yield client


def start(client, **settings):
    response = client.post("/api/runs", json=settings)
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def test_missing_target_and_multiple_people_never_draw_an_always_present_box():
    for index, expected in [(1, "not_present"), (2, "unclear"), (3, "multiple_people")]:
        decision = decide(mock_response(index), .85, None)
        assert decision["status"] == expected
        assert not decision["draw"]
        assert decision["box"] is not None  # Still inspect the ungated answer.


def test_nullable_confidence_is_distinct_from_zero_or_one():
    response = mock_response(0)
    assert decide(response, .85, None)["draw"]
    assert decide(response, .85, .5)["status"] == "missing_box_confidence"
    response["answers"]["person"]["confidence"] = 0
    assert decide(response, .85, .5)["status"] == "low_box_confidence"


def test_crop_edges_map_into_original_video_coordinates_not_crop_coordinates():
    mapped = video_box({"left": .25, "right": .75, "top": .1, "bottom": .9},
                       {"x": 400, "y": 100, "width": 400, "height": 600, "source_width": 1280, "source_height": 720})
    assert mapped == pytest.approx({"left": .390625, "right": .546875, "top": 2 / 9, "bottom": 8 / 9})


def test_fixed_video_requires_strong_box_confidence_as_well_as_target_presence(client):
    assert client.get("/api/config").json()["fixed_settings"]["min_box_confidence"] == .8
    response = mock_response(0)
    for confidence, expected in [(None, False), (.79, False), (.80, True), (.95, True)]:
        response["answers"]["person"]["confidence"] = confidence
        assert decide(response, .85, .8)["draw"] is expected
    response["answers"]["visible"]["noul"] = .4
    assert not decide(response, .85, .8)["draw"]
    response["answers"]["visible"]["noul"] = .99
    response["answers"]["subjects"]["choice"] = "none"
    assert not decide(response, .85, .8)["draw"]


def test_region_metadata_is_validated_persisted_and_part_of_idempotency(client):
    run_id = start(client, source_width=1280, source_height=720)
    region = {"x": 400, "y": 100, "width": 400, "height": 600, "source_width": 1280, "source_height": 720}
    url = f"/api/runs/{run_id}/frames"
    invalid = client.post(url, json={**frame(), "region": {**region, "x": 1200}})
    assert invalid.status_code == 422
    result = client.post(url, json={**frame(), "region": region})
    assert result.status_code == 200
    record = result.json()["record"]
    assert record["image"]["region"] == region
    assert record["decision"]["draw"]
    assert record["decision"]["video_box"] != record["response"]["answers"]["person"]["box"]
    assert client.post(url, json={**frame(), "region": {**region, "x": 410}}).status_code == 409


def test_exploratory_question_overrides_are_logged_but_never_mark_people(client):
    questions = {"visible": {"type": "noul", "instructions": "Is any person visible in this frame?"}}
    run_id = start(client, diagnostic_questions=questions)
    result = client.post(f"/api/runs/{run_id}/frames", json=frame()).json()["record"]
    assert result["request"]["questions"] == questions
    assert result["response"]["answers"]["visible"]["noul"] == .96
    assert result["decision"]["status"] == "diagnostic_only" and not result["decision"]["draw"]


def test_operator_label_is_explicit_shared_context_not_a_timestamp_drawing_rule(client):
    preset = client.get("/api/config").json()["fixed_settings"]
    assert "gun" in preset["operator_context"]
    assert "every frame" in preset["operator_context"]
    assert "3–10" not in preset["operator_context"] and "03" not in preset["operator_context"]
    run_id = start(client, operator_context=preset["operator_context"])
    record = client.post(f"/api/runs/{run_id}/frames", json=frame()).json()["record"]
    assert record["request"]["state"]["operator_context"] == preset["operator_context"]
    assert "operator_context" in record["request"]["questions"]["person"]["instructions"]


@pytest.mark.parametrize("edges", [
    {"top": 0, "bottom": 1, "left": .8, "right": .2},
    {"top": 0, "bottom": 1, "left": -.1, "right": .5},
    {"top": float("nan"), "bottom": 1, "left": 0, "right": 1},
    {"top": 0, "bottom": 1, "left": 0},
])
def test_invalid_box_with_high_presence_is_not_drawn(edges):
    response = mock_response(0)
    response["answers"]["person"]["box"] = edges
    assert decide(response, .85, None)["status"] == "invalid_box"


def test_mock_run_persists_pixels_prompts_hashes_and_no_fake_usage(client, tmp_path):
    run_id = start(client, max_frames=4)
    for i in range(4):
        result = client.post(f"/api/runs/{run_id}/frames", json=frame(i))
        assert result.status_code == 200
    exported = client.get(f"/api/runs/{run_id}/export").json()
    summary = exported["manifest"]["summary"]
    assert summary["frames"] == 4 and summary["marked"] == 1
    assert summary["live_attempts"] == 0 and summary["sdk_p50_ms"] is None
    assert summary["known_input_tokens"] == 0  # Known subtotal; no model usage invented.
    assert all(r["response"]["usage"]["input_tokens"] is None for r in exported["frames"])
    assert exported["manifest"]["code_sha256"]["app.py"]
    assert (tmp_path / run_id / "0000.png").is_file()
    assert exported["frames"][0]["request"]["questions"]["person"]["type"] == "box"
    assert "Authorization" not in json.dumps(exported)


def test_duplicate_frame_returns_saved_result_and_conflicts_are_rejected(client):
    run_id = start(client, max_frames=1)
    url = f"/api/runs/{run_id}/frames"
    first = client.post(url, json=frame()).json()
    second = client.post(url, json=frame()).json()
    assert second["cached"] and second["record"] == first["record"]
    assert client.post(url, json={**frame(), "timestamp_seconds": 2}).status_code == 409
    assert client.post(url, json=frame(1)).status_code == 409


@pytest.mark.parametrize("payload", [
    {**frame(), "image_base64": "not-base64"}, frame(size=(200, 200)),
    frame(size=(1600, 900)), {**frame(), "timestamp_seconds": -1},
])
def test_bad_frames_are_rejected_before_any_model_call(client, payload):
    run_id = start(client)
    response = client.post(f"/api/runs/{run_id}/frames", json=payload)
    assert response.status_code == 422
    assert client.get(f"/api/runs/{run_id}/export").json()["frames"] == []


def test_client_timing_is_first_write_and_run_closure_is_enforced(client):
    run_id = start(client)
    client.post(f"/api/runs/{run_id}/frames", json=frame())
    timing_url = f"/api/runs/{run_id}/frames/0/timing"
    client.post(timing_url, json={"http_roundtrip_ms": 20, "end_to_end_ms": 33})
    response = client.post(timing_url, json={"http_roundtrip_ms": 999, "end_to_end_ms": 999}).json()
    assert response["record"]["client_timing"]["end_to_end_ms"] == 33
    assert response["summary"]["client_p50_ms"] == 33
    client.post(f"/api/runs/{run_id}/finish")
    assert client.post(f"/api/runs/{run_id}/frames", json=frame(1)).status_code == 409


def test_export_survives_server_restart(client, tmp_path):
    run_id = start(client)
    client.post(f"/api/runs/{run_id}/frames", json=frame())
    with TestClient(create_app(data_dir=tmp_path, load_env=False)) as restarted:
        assert restarted.get(f"/api/runs/{run_id}/export").json()["frames"][0]["index"] == 0


def test_page_and_assets_without_api_key_or_network(client):
    assert "Jev Frame Lab" in client.get("/").text
    assert client.get("/styles.css").headers["content-type"].startswith("text/css")
    assert client.get("/app.js").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/config").json()["key_configured"] is False
    assert client.post("/api/runs", json={"mode": "live"}).status_code == 409
    assert client.post("/api/runs", json={}, headers={"Origin": "https://unrelated.example"}).status_code == 403


def test_real_sdk_serialization_and_parsing_via_offline_transport(tmp_path):
    requests = []

    async def handle(request):
        requests.append(request)
        assert request.url.path == "/v1/systemone"
        assert request.headers["TypeSafe-Danger-Zone-Images"] == "true"
        assert request.headers["content-type"].startswith("multipart/form-data")
        body = await request.aread()
        assert b"jev-1.14.0" in body and b"\x89PNG" in body and b'"person"' in body
        response = mock_response(0)
        response["model"] = "jev-1.14.0"
        # Real Choice schema expects probabilities and a numeric confidence.
        response["answers"]["subjects"].update(confidence=.96, probabilities={"none": .01, "one": .97, "several": .01, "unclear": .01})
        response["usage"] = {"input_tokens": 321, "output_tokens": 19}
        return httpx2.Response(200, json=response, headers={"x-request-id": "offline-fixture"})

    gateway = object.__new__(SDKGateway)
    gateway.client = AsyncTypeSafeClient(api_key="offline-test-key", transport=httpx2.MockTransport(handle), compression=False, retry=RetryPolicy(max_retries=0))
    with TestClient(create_app(data_dir=tmp_path, gateway=gateway, load_env=False)) as client:
        run_id = start(client, mode="live", input_token_limit=300)
        response = client.post(f"/api/runs/{run_id}/frames", json=frame())
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["record"]["error"] is None, result["record"]["error"]
        assert result["record"]["request_id"] is None  # Optional header must not discard a good response.
        assert result["record"]["decision"]["draw"], result
        assert result["summary"]["known_input_tokens"] == 321
        assert result["summary"]["known_output_tokens"] == 19
        assert result["record"]["timing"]["sdk_call_ms"] is not None
        assert client.post(f"/api/runs/{run_id}/frames", json=frame(1)).status_code == 409
        client.post(f"/api/runs/{run_id}/frames", json=frame())
        assert len(requests) == 1  # Cached result; no second SDK attempt.
        assert client.post(f"/api/runs/{run_id}/finish").json()["finish_reason"] == "input_token_limit"
    import asyncio
    asyncio.run(gateway.close())


def test_failed_calls_preserve_unknown_usage_and_stop_further_calls(tmp_path):
    class FailingGateway:
        calls = 0

        async def analyze(self, binary, request):
            self.calls += 1
            raise RuntimeError("sensitive text should not appear in the record")

    gateway = FailingGateway()
    with TestClient(create_app(data_dir=tmp_path, gateway=gateway, load_env=False)) as client:
        run_id = start(client, mode="live")
        response = client.post(f"/api/runs/{run_id}/frames", json=frame()).json()
        assert response["record"]["error"]["type"] == "RuntimeError"
        assert "sensitive text" not in json.dumps(response)
        assert response["summary"]["missing_input_usage"] == 1
        assert response["summary"]["sdk_timing_samples"] == 1
        assert client.post(f"/api/runs/{run_id}/frames", json=frame(1)).status_code == 409
        assert gateway.calls == 1


def test_partial_usage_totals_are_not_presented_as_complete_measurements():
    records = []
    for i, value in enumerate([100, None, 300]):
        records.append({"mode": "live", "response": {"usage": {"input_tokens": value, "output_tokens": None}},
                        "error": None, "decision": {"draw": False}, "timing": {"sdk_call_ms": [100, 200, 1000][i]}})
    summary = summarize(records)
    assert summary["known_input_tokens"] == 400
    assert summary["missing_input_usage"] == 1 and summary["missing_output_usage"] == 3
    assert summary["sdk_p50_ms"] == 200 and summary["sdk_p95_ms"] == 920


def test_fixed_video_route_and_server_owned_run_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    video = tmp_path / "fixed.mp4"
    video.write_bytes(b"offline-video-route-fixture")
    with TestClient(create_app(data_dir=tmp_path / "runs", gateway=object(), load_env=False,
                               fixed_video_path=video, fixed_settings=RunSettings(max_frames=3))) as client:
        config = client.get("/api/config").json()
        assert config["video"]["available"] and config["live_ready"]
        assert config["fixed_settings"]["mode"] == "live"
        partial = client.get("/api/video", headers={"Range": "bytes=0-6"})
        assert partial.status_code == 206 and partial.content == b"offline"
        metadata = {"duration_seconds": 1.5, "width": 640, "height": 360}
        assert client.post("/api/fixed-runs", json={**metadata, "max_frames": 999}).status_code == 422
        started = client.post("/api/fixed-runs", json=metadata)
        assert started.status_code == 200, started.text
        manifest = started.json()["manifest"]
        assert manifest["settings"]["mode"] == "live" and manifest["settings"]["max_frames"] == 2
        assert manifest["fixed_video"]["sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()


def test_default_fixed_clip_can_finish_past_old_twenty_thousand_token_boundary(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    video = tmp_path / "fixed.mp4"
    video.write_bytes(b"offline-fixed-source")

    class Gateway:
        async def analyze(self, binary, request):
            response = mock_response(1)
            response["usage"] = {"input_tokens": 1964, "output_tokens": 80}
            return response, "offline-regression"

    with TestClient(create_app(data_dir=tmp_path / "runs", gateway=Gateway(), load_env=False, fixed_video_path=video)) as client:
        response = client.post("/api/fixed-runs", json={"duration_seconds": 30.0, "width": 640, "height": 360})
        run = response.json()
        assert run["manifest"]["settings"]["input_token_limit"] == 100_000
        assert run["manifest"]["settings"]["max_frames"] == 30
        run_id = run["run_id"]
        for index in range(30):
            result = client.post(f"/api/runs/{run_id}/frames", json=frame(index))
            assert result.status_code == 200, result.text
        finished = client.post(f"/api/runs/{run_id}/finish").json()
        assert finished["finish_reason"] == "completed"
        assert finished["summary"]["known_input_tokens"] == 58_920
        saved = client.get(f"/api/runs/{run_id}/export").json()["manifest"]
        assert saved["last_timestamp_seconds"] == 29
