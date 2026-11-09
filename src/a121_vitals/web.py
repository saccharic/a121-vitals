"""Local recording analysis and bounded playback API."""

import asyncio
from collections.abc import Sequence
import csv
from contextlib import asynccontextmanager, suppress
import io
from pathlib import Path
import tempfile
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .demo import write_demo
from .viewer import MAX_UPLOAD_BYTES, RecordingStore, ViewerError

Index = Annotated[int, Query(ge=0)]
Sensor = Annotated[int, Query(ge=1)]
SAMPLE_PATH = Path(__file__).with_name("samples") / "1109 data.h5"


def create_app(
    *, store: RecordingStore | None = None, web_dir: Path | None = None,
    allowed_hosts: Sequence[str] | None = None,
) -> FastAPI:
    records = store if store is not None else RecordingStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ingestion_lock = asyncio.Lock()
        app.state.analysis_progress = None

        async def cleanup():
            while True:
                await asyncio.sleep(60)
                await run_in_threadpool(records.prune)

        task = asyncio.create_task(cleanup())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            records.records.clear()

    app = FastAPI(title="a121-vitals", version="0.1.0", lifespan=lifespan,
                  description="Sparse IQ playback and Acconeer breathing-rate analysis.")
    app.state.store = records

    @app.exception_handler(ViewerError)
    async def viewer_error(request: Request, exc: ViewerError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.middleware("http")
    async def api_response_policy(request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if (
            request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}
            or (origin is not None and origin != expected_origin)
        ):
            response = JSONResponse({"detail": "Cross-origin API requests are not allowed."}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    if allowed_hosts is not None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts, www_redirect=False)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "max_upload_bytes": MAX_UPLOAD_BYTES, "playback_api": 3}

    @asynccontextmanager
    async def ingestion(job: UUID | None, *, wait: bool = False):
        lock = app.state.ingestion_lock
        if lock.locked() and not wait:
            raise ViewerError("Another recording is loading. Please try again shortly.", 503)
        async with lock:
            loop = asyncio.get_running_loop()
            stages = {"stage": "reading"}
            if job is not None:
                app.state.analysis_progress = (job, stages)
            try:
                yield lambda stage: loop.call_soon_threadsafe(stages.__setitem__, "stage", stage)
            finally:
                app.state.analysis_progress = None

    @app.get("/api/analysis-progress/{job}")
    async def analysis_progress(job: UUID):
        progress = app.state.analysis_progress
        return progress[1] if progress and progress[0] == job else {"stage": None}

    @app.post("/api/recordings", status_code=201)
    async def upload(request: Request, filename: Annotated[str, Query(max_length=200)] = "recording.h5",
                     job: UUID | None = None):
        """Send the H5 file as the raw request body (not multipart/form-data)."""
        async with ingestion(job) as report:
            with tempfile.TemporaryDirectory(prefix="a121-vitals-") as directory:
                path = Path(directory) / "upload.h5"
                size = 0
                with path.open("wb") as file:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise ViewerError("Upload exceeds the 32 MiB limit.", 413)
                        file.write(chunk)
                if not size:
                    raise ViewerError("The uploaded file is empty.")
                item = await run_in_threadpool(records.add, path, filename, progress=report)
                return item.describe()

    @app.post("/api/examples/demo", status_code=201)
    async def example(job: UUID | None = None):
        async with ingestion(job) as report:
            with tempfile.TemporaryDirectory(prefix="a121-demo-") as directory:
                path = Path(directory) / "synthetic-demo.h5"
                await run_in_threadpool(write_demo, path)
                item = await run_in_threadpool(records.add, path, path.name, "synthetic", report)
                return item.describe()

    @app.get("/api/examples/demo.h5")
    def download_example():
        with tempfile.TemporaryDirectory(prefix="a121-demo-") as directory:
            path = Path(directory) / "synthetic-demo.h5"
            write_demo(path)
            return Response(path.read_bytes(), media_type="application/octet-stream", headers={
                "Content-Disposition": 'attachment; filename="synthetic-demo.h5"',
            })

    @app.post("/api/examples/sample", status_code=201)
    async def sample(job: UUID | None = None):
        async with ingestion(job, wait=True) as report:
            item = await run_in_threadpool(records.add, SAMPLE_PATH, SAMPLE_PATH.name, "sample", report)
            return item.describe()

    @app.get("/api/recordings/{recording_id}")
    def describe(recording_id: str):
        return records.get(recording_id).describe()

    @app.delete("/api/recordings/{recording_id}", status_code=204)
    def remove(recording_id: str):
        records.remove(recording_id)
        return Response(status_code=204)

    @app.get("/api/recordings/{recording_id}/frame")
    def frame(recording_id: str, session: Index = 0, group: Index = 0, sensor: Sensor = 1,
              subsweep: Index = 0, frame_index: Index = 0, sweep_index: Index = 0):
        return records.get(recording_id).frame(session, group, sensor, subsweep, frame_index, sweep_index)

    @app.get("/api/recordings/{recording_id}/window")
    def window(recording_id: str, stop: Annotated[int, Query(ge=1)], session: Index = 0,
               group: Index = 0, sensor: Sensor = 1, subsweep: Index = 0,
               start: Index = 0, sweep_index: Index = 0,
               max_frames: Annotated[int, Query(ge=1, le=512)] = 256):
        return records.get(recording_id).window(session, group, sensor, subsweep, start, stop, sweep_index, max_frames)

    @app.get("/api/recordings/{recording_id}/timeline")
    def timeline(recording_id: str, session: Index = 0, group: Index = 0, sensor: Sensor = 1):
        return records.get(recording_id).timeline(session, group, sensor)

    @app.get("/api/recordings/{recording_id}/frame.csv")
    def frame_csv(recording_id: str, session: Index = 0, group: Index = 0, sensor: Sensor = 1,
                  subsweep: Index = 0, frame_index: Index = 0, sweep_index: Index = 0):
        item = records.get(recording_id)
        value = item.frame(session, group, sensor, subsweep, frame_index, sweep_index)
        _, _, selected, _ = item.select(session, group, sensor, subsweep)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["session", "group", "sensor", "subsweep", "frame", "sweep",
                         "point_in_subsweep", "point_in_frame", "distance_m", "tick", "tick_time_s", "i", "q"])
        for point, (distance, i, q) in enumerate(zip(value["distance_m"], value["i"], value["q"], strict=True)):
            writer.writerow([session, group, sensor, subsweep, frame_index, sweep_index, point,
                             selected.start + point, distance, value["tick"], value["tick_time_s"], int(i), int(q)])
        return Response(output.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="a121-frame-{frame_index}-sweep-{sweep_index}.csv"',
        })

    # Unknown API paths remain JSON 404s, including with a built frontend mounted.
    @app.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
    def unknown_api(path: str):
        raise ViewerError("API route does not exist.", 404)

    if web_dir is not None:
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="frontend")
    return app
