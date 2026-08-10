# HW-GUI-VLA on a physical Android device

This adapter evaluates the official UI-Voyager screenshot policy on an
already-connected physical device. It does not start an emulator or use the
AndroidEnv gRPC accessibility backend.

## Protocol

- Keep the official `qwen3vl_instruct` prompt and 0--999 coordinates.
- Use one current screenshot and textual action history (`history_len=30`).
- Use the official decoding defaults: temperature 0.7, top-p 0.8, and up to
  16,384 output tokens.
- Restart the target app before and after every task.
- Limit each task to `ceil(expected_steps * 1.25)` actions.
- Exclude `中国联通` and `咸鱼之王`; preserve rows marked `是否统计=N` as
  diagnostic tasks outside the primary metric.
- A model `terminate` action only stops the trajectory. It is not a benchmark
  success label. Success remains `null` until screenshot/trajectory review.
- No supervised user interaction is accepted during the formal run.

## Environment

Create an isolated environment without modifying the existing AndroidWorld
environment:

```bash
python -m venv --system-site-packages .venv
.venv/bin/python -m pip install openpyxl
```

Start an OpenAI-compatible vLLM server that exposes the model as
`UI-Voyager`. The official checkpoint is `MarsXL/UI-Voyager`.

If the phone's local ADB server is reverse-forwarded to this host, export the
socket before invoking the runner:

```bash
export ADB_SERVER_SOCKET=tcp:127.0.0.1:<forwarded-port>
```

## Preflight and smoke test

```bash
.venv/bin/python run_hw_gui_vla_physical.py \
  --workbook /path/to/HW-GUI-VLA.xlsx \
  --run-dir /path/to/runs/ui_voyager_smoke \
  --base-url http://127.0.0.1:8000 \
  --model UI-Voyager \
  --limit 1 \
  --dry-run
```

Remove `--dry-run` only after the ADB Keyboard package, target applications,
and model endpoint pass preflight. Use a new run directory for the actual
smoke test.

## Full run and resume

```bash
.venv/bin/python run_hw_gui_vla_physical.py \
  --workbook /path/to/HW-GUI-VLA.xlsx \
  --run-dir /path/to/runs/ui_voyager_full \
  --base-url http://127.0.0.1:8000 \
  --model UI-Voyager

.venv/bin/python run_hw_gui_vla_physical.py \
  --workbook /path/to/HW-GUI-VLA.xlsx \
  --run-dir /path/to/runs/ui_voyager_full \
  --base-url http://127.0.0.1:8000 \
  --model UI-Voyager \
  --resume
```

`manifest.json` freezes the workbook hash, repository revision, model
configuration, prompt hash, exclusions, task list, and step budgets. Each task
stores screenshots, `trajectory.jsonl`, `run_stats.json`, `result.json`, and
the original workbook row in `task.json`.
