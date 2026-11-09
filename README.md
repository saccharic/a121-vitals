# a121-vitals

Replay I/Q, phase, and amplitude from Acconeer A121 Sparse IQ recordings. Compatible recordings also show breathing-rate estimates from the [Acconeer Breathing Reference Application](https://docs.acconeer.com/en/latest/applications/a121/breathing.html).

## Run locally

On macOS or Linux, install Python 3.11+ with `venv` and `pip`, and Node.js 22.12+ with `npm`. Run this from the repository root.

```sh
bash run-local.sh
```

The first run downloads dependencies. The launcher builds the viewer and opens it at [127.0.0.1:8000](http://127.0.0.1:8000). Press `Ctrl+C` to stop the server.

The viewer opens `Sample.h5` by default. Under **Open a Recording**, drop an `.h5` or `.hdf5` file (up to 32 MiB) to analyze it locally.

## Development checks

Run the [CI checks](.github/workflows/check.yml) locally with `uv` and `npm`.

```sh
uv sync --locked --all-extras
uv run --no-sync python -m unittest discover -s tests
bash -n run-local.sh
(cd frontend && npm ci && npm run format:check && npm run build)
```

## License

[MIT](LICENSE). The Acconeer Exploration Tool uses [BSD-3-Clause-Clear](https://github.com/acconeer/acconeer-python-exploration/blob/master/LICENSE.md).
