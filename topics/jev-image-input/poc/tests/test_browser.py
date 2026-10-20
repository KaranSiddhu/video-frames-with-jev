"""Local, fixture-only frontend validation. No key, root .env, or provider traffic."""

import asyncio
import base64
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app import RunSettings, create_app
from domain import mock_response

playwright = pytest.importorskip("playwright.sync_api")


class FixtureGateway:
    """A blocked first response lets the test check the border before/after it arrives."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def analyze(self, binary, request):
        index = self.calls
        self.calls += 1
        if index == 0:
            self.entered.set()
            await asyncio.to_thread(self.release.wait, 15)
        response = mock_response(1 if index == 1 else 0)
        response["answers"]["person"]["confidence"] = .9 if index != 1 else .4
        response["model"] = "OFFLINE-FIXTURE-NOT-A-MODEL-RESULT"
        response["usage"] = {"input_tokens": 100, "output_tokens": 10}
        return response, f"offline-test-{index}"


@pytest.fixture
def local_site(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    gateway = FixtureGateway()
    video = tmp_path / "fixture.webm"
    application = create_app(
        data_dir=tmp_path / "runs", load_env=False, gateway=gateway, fixed_video_path=video,
        fixed_settings=RunSettings(max_frames=3, interval_seconds=.25, max_edge=640, min_box_confidence=.8),
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(application, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            pytest.fail("Local test server did not start")
        time.sleep(.02)
    yield {"url": f"http://127.0.0.1:{sock.getsockname()[1]}", "video": video, "gateway": gateway}
    gateway.release.set()
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def browser():
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(channel=os.environ.get("POC_BROWSER_CHANNEL", "chrome"), headless=True)
        yield browser
        browser.close()


def make_video(page):
    encoded = page.evaluate("""async () => {
      const c=document.createElement('canvas'); c.width=640; c.height=360;
      const ctx=c.getContext('2d'); const stream=c.captureStream(12);
      const recorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp8'});
      const chunks=[]; recorder.ondataavailable=e=>chunks.push(e.data);
      const done=new Promise(resolve=>recorder.onstop=resolve);
      recorder.start();
      for(let i=0;i<16;i++) {
        ctx.fillStyle=i%2 ? '#aabbcc' : '#ccddee'; ctx.fillRect(0,0,640,360);
        ctx.fillStyle='#3457db'; ctx.fillRect(30+i*8,70,120,220);
        await new Promise(resolve=>setTimeout(resolve,90));
      }
      recorder.stop(); await done; stream.getTracks().forEach(t=>t.stop());
      const blob=new Blob(chunks,{type:'video/webm'});
      return await new Promise(resolve=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.readAsDataURL(blob);});
    }""")
    return base64.b64decode(encoded)


def test_direct_file_preview_is_styled_and_requires_local_app(browser):
    page = browser.new_page()
    page.goto((Path(__file__).resolve().parents[1] / "static" / "index.html").as_uri())
    page.wait_for_function("document.getElementById('notice').textContent.includes('8880')")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(245, 247, 251)"
    assert page.locator("#startButton").is_disabled()
    assert page.locator("button").count() == 1
    page.close()


def test_missing_fixed_video_is_clear_and_no_controls_leak_into_presentation(local_site, browser):
    page = browser.new_page()
    page.goto(local_site["url"])
    page.wait_for_function("document.getElementById('connection').textContent === 'Video unavailable'")
    assert page.locator("#startButton").is_disabled()
    assert page.locator("button").count() == 1
    assert page.locator("input,select,dialog").count() == 0
    content = page.locator("body").inner_text().lower()
    assert "mock" not in content and "backend" not in content and "key configured" not in content
    assert local_site["gateway"].calls == 0
    page.close()


def test_fixed_video_start_waits_for_response_then_draws_and_saves_evidence(local_site, browser, tmp_path):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(local_site["url"])
    local_site["video"].write_bytes(make_video(page))
    page.reload()
    page.wait_for_function("!document.getElementById('startButton').disabled")
    assert page.locator("#connection").inner_text() == "Ready"
    assert page.locator("button").count() == 1
    assert page.locator("input,select,dialog").count() == 0
    assert page.evaluate("document.getElementById('video').controls") is False
    assert local_site["gateway"].calls == 0  # Loading footage does not send a frame to Jev.

    with page.expect_response(lambda r: r.url.endswith("/api/fixed-runs")) as started:
        page.locator("#startButton").click()
    run_id = started.value.json()["run_id"]
    # JSON is retained until response arrival; the fixture event observes request arrival.
    assert local_site["gateway"].entered.wait(timeout=5)
    assert page.locator("#boxOverlay").get_attribute("data-marked") == "false"
    assert page.evaluate("document.getElementById('video').paused")
    assert json.loads(page.locator("#responseJSON").inner_text()) == {}

    local_site["gateway"].release.set()
    page.wait_for_function("document.getElementById('boxOverlay').dataset.marked === 'true'")
    pixel = page.evaluate("""() => {
      const c=document.getElementById('boxOverlay');
      return Array.from(c.getContext('2d').getImageData(Math.round(.32*c.width),Math.round(.55*c.height),1,1).data);
    }""")
    assert pixel[0] > 200 and pixel[1] < 120 and pixel[3] > 0
    assert page.locator("#frameDecision").inner_text() == "Person located"
    page.wait_for_function("document.getElementById('connection').textContent === 'Complete'")
    assert page.locator("#metricFrames").inner_text() == "3 / 2"
    assert page.locator("#metricInput").inner_text() == "300"
    assert page.locator("#metricOutput").inner_text() == "30"
    assert not page.locator("#startButton").is_disabled()
    export = page.request.get(f"{local_site['url']}/api/runs/{run_id}/export").json()
    assert export["manifest"]["status"] == "closed"
    assert export["manifest"]["fixed_video"]["sha256"]
    assert [round(r["timestamp_seconds"], 2) for r in export["frames"]] == [0, .25, .5]
    assert [r["decision"]["draw"] for r in export["frames"]] == [True, False, True]
    assert all(r["client_timing"]["end_to_end_ms"] > 0 for r in export["frames"])
    assert all(r["response"]["model"].startswith("OFFLINE-FIXTURE") for r in export["frames"])

    for width, height in [(1920, 1080), (1440, 900), (1280, 720), (1024, 768)]:
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight && document.documentElement.scrollWidth <= innerWidth")
        assert page.evaluate("""() => {
          const v=document.querySelector('.video-panel').getBoundingClientRect();
          const i=document.querySelector('.json-panel').getBoundingClientRect();
          const m=document.querySelector('.metrics').getBoundingClientRect();
          return v.height>200 && i.left>=v.right && m.top>=v.bottom && m.bottom<=innerHeight;
        }""")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    page.screenshot(path=tmp_path / "offline-fixture-desktop.png", full_page=False)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    page.screenshot(path=tmp_path / "offline-fixture-mobile.png", full_page=False)
    print(f"Offline-fixture screenshots, not real Jev results: {tmp_path}")
    assert not errors
    page.close()
