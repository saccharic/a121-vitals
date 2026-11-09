"""Read-only inspection of A121 HDF5 recordings."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from threading import Thread
import webbrowser

import h5py
from acconeer.exptool import a121

from .recording import summarize_record


def _open_browser(url: str) -> None:
    print(f"Open {url} in your browser. Press Ctrl+C to stop the server.", flush=True)
    if sys.platform.startswith("linux") and not any(
        os.environ.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER")
    ):
        return
    try:
        if webbrowser.open(url):
            return
    except (OSError, webbrowser.Error):
        pass
    print("Could not open the browser automatically.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a121-vitals")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser("inspect", help="Inspect an A121 H5 recording")
    inspect_parser.add_argument("path", help="Path to a local recording")
    inspect_parser.add_argument("--json", action="store_true", help="Print the full inspection summary as JSON")
    serve_parser = commands.add_parser("serve", help="Run the local raw-data viewer API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--web-dir", type=Path, help="Built frontend directory (for example frontend/dist)")
    serve_parser.add_argument("--open", action="store_true", help="Open the viewer in the default browser when ready")
    args = parser.parse_args(argv)
    if args.command == "serve":
        if args.open and args.web_dir is None:
            serve_parser.error("--open requires --web-dir")
        try:
            import uvicorn
            from .web import create_app
        except ImportError:
            parser.error('Install web dependencies with: python -m pip install ".[web]"')
        if args.web_dir is not None and not (args.web_dir / "index.html").is_file():
            parser.error("Build the frontend first, then pass its dist directory with --web-dir")
        allowed_hosts = (
            ("127.0.0.1", "localhost", "[::1]")
            if args.host in {"127.0.0.1", "localhost", "::1"}
            else None
        )
        app = create_app(web_dir=args.web_dir, allowed_hosts=allowed_hosts)
        if args.open:
            class BrowserServer(uvicorn.Server):
                async def startup(self, sockets=None):
                    await super().startup(sockets=sockets)
                    if self.started:
                        host, port = self.servers[0].sockets[0].getsockname()[:2]
                        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
                        if ":" in host:
                            host = f"[{host}]"
                        url = f"http://{host}:{port}"
                        Thread(target=_open_browser, args=(url,), daemon=True).start()

            try:
                BrowserServer(uvicorn.Config(app, host=args.host, port=args.port)).run()
            except KeyboardInterrupt:
                return 130
        else:
            uvicorn.run(app, host=args.host, port=args.port)
        return 0
    try:
        # Own the handle so even a failed upstream format check closes the file.
        with h5py.File(args.path, "r") as file:
            with a121.open_record(file) as record:
                summary = summarize_record(record)
    except (OSError, a121.RecordError, KeyError, ValueError, TypeError, IndexError, ZeroDivisionError) as exc:
        print(f"Cannot inspect recording: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        print(f"A121 recording | Exploration Tool {summary['recording_library_version']}")
        print(f"Sessions: {summary['num_sessions']} | Recorded at: {summary['recorded_at']}")
        for session in summary["sessions"]:
            for stream in session["streams"]:
                print(f"Session {session['session_index']} / group {stream['group_index']} / sensor {stream['sensor_id']}")
                shape = [stream["num_frames"], *stream["frame_shape"]]
                print(f"  IQ shape (frames, sweeps, points): {shape}")
                duration = stream["duration_s"]
                print(f"  Duration: {duration:.3f} s" if duration is not None else "  Duration: unavailable")
                print(f"  Configured frame rate: {stream['configured_frame_rate_hz']} Hz")
                low, high = stream["interval_min_s"], stream["interval_max_s"]
                if low is not None:
                    print(f"  Frame intervals: {low:.6f} .. {high:.6f} s")
                else:
                    print("  Frame intervals: unavailable (fewer than two frames)")
                for sub in stream["subsweeps"]:
                    distances = sub["distance_m"]
                    if distances:
                        print(f"  Subsweep {sub['index']}: {len(distances)} points, {distances[0]:.3f} .. {distances[-1]:.3f} m")
                print(f"  Flag counts: {stream['flag_counts']}")
                for warning in stream["warnings"]:
                    print(f"  Warning: {warning}")
        print("Inspection only; breathing and heart rate have not been estimated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
