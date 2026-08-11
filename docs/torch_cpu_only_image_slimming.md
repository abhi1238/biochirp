# Image slimming: switch torch to CPU-only

## Audit finding (2026-05-18)

Three BioChirp images weigh ~5.8 GB each:
- `biochirp_interpreter_tool:latest` (5.84 GB)
- `biochirp_semantic_tool:latest` (5.8 GB)
- `opentargets_service:latest` (5.84 GB)

`docker history` shows **98% of the size lives in one `pip install -r requirements.txt` layer** (5.71 GB). `pip list` inside the image reveals the bulk is GPU torch:

```
torch==2.5.1
nvidia-cublas-cu12   12.4.5.8
nvidia-cudnn-cu12    9.1.0.70
nvidia-cusolver-cu12 11.6.1.9
... (10+ nvidia-cuda-* packages, ~4 GB combined)
```

The intent in `requirements.txt` was **CPU-only**:

```python
# 2026-05-17: GLiNER-bio (urchade/gliner_large_bio-v0.1) preprocessing
# ...
# The CPU-only torch wheel ships by default on Linux; the model (~440 MB)
# is cached under HF_HOME on first use.
```

**That comment is outdated.** Since torch 2.5, the default PyPI wheel on
Linux is the **GPU variant** bundling CUDA 12.4. The CPU-only wheel must be
requested explicitly.

## Fix per affected `requirements.txt`

Replace:

```
torch==2.5.1
```

with:

```
--extra-index-url https://download.pytorch.org/whl/cpu
torch==2.5.1+cpu
```

The `--extra-index-url` line goes at the **top** of `requirements.txt`. The
`+cpu` build is the same torch API, just without the CUDA runtime.

Affected files (verified `torch` mention):
- `app/tools/interpreter_agent/requirements.txt`
- `app/tools/semantic_filter/requirements.txt` (verify before editing)
- Any other service that lists `torch` (grep first)

```bash
grep -l "^torch==" app/tools/*/requirements.txt
```

## Rebuild + redeploy

```bash
# Per affected service (example: interpreter_tool)
docker compose build biochirp_interpreter_tool
docker compose up -d biochirp_interpreter_tool
docker compose logs -f biochirp_interpreter_tool   # confirm GLiNER inits OK
```

Verify the inference path still works end-to-end by hitting any chat service —
the interpreter is called on every WS message.

## Expected savings

- Per-image: ~5.8 GB → ~1.4 GB (saves ~4.4 GB per image)
- Across 3 images: **~13 GB freed**, plus faster `docker pull` / cold start

## Verification

```bash
# After rebuild, pip list inside the new image should NOT include nvidia-* packages.
docker run --rm --entrypoint sh biochirp_interpreter_tool:latest -c 'pip list 2>/dev/null | grep -i nvidia | wc -l'
# Expected output: 0
```

## Risks to watch for

- **Inference latency**: GLiNER on CPU is ~5-10× slower than on GPU for the
  same span. The current setup was already CPU-bound (since the GPU wheel
  was installed but no `cuda:0` device is being used by the model loader),
  so behavior should not change. Verify by timing one `/interpreter` call
  before and after.
- **Hidden GPU users**: if any service quietly uses `torch.cuda.is_available()`
  to decide a path, that branch will no longer be taken. Grep for it first:
  ```bash
  grep -rn "torch.cuda\|cuda:0\|to(.cuda" app/tools/ 2>/dev/null
  ```
- **opentargets_service** is not in the BioChirp main compose — it's a
  separate stack. Audit its requirements before applying the same fix.
