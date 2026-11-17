# Local research brief

Every completed brief records its result status, elapsed time, candle evidence and verified model identity in local SQLite. Read recent runs with `hcs-trader research-history --limit 25`, `GET /api/v1/research/runs?limit=25` or the read-only MCP tool `research.get_runs`. The result includes counts and mean durations by status across the full history. Run records exclude tokens, local file paths and generated commentary. A configuration error that prevents a brief from starting exits through the CLI.

The dashboard's **Local Research Runs** panel shows the same lifetime statistics and the 25 most recent entries. Select **Refresh** after a new brief completes. [Dashboard screenshot with synthetic candle data](research-runs.png)

## Verified GGUF configuration

On 2026-11-15, Windows x64 with an AMD Ryzen 7 7735HS ran the publisher's **LFM2.5-350M Q4_K_M** on the CPU using llama.cpp **b12423**, six inference threads, a 4,096-token context and the embedded Jinja template. Both release artifacts matched their published SHA-256 digests. [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases/tag/b12423) · [Pinned Liquid AI GGUF revision](https://huggingface.co/LiquidAI/LFM2.5-350M-GGUF/tree/bb7ee58b243e4cede04187e323e760b04f8a0091)

- Windows CPU archive SHA-256 `3c128703eca49ad2a70ad51bbf5e1ce36121070a33f27c275804e3f3d5567e48`
- GGUF SHA-256 `7e6f72643caafc9a68256686638c4d7916f2cec76d1df478d4c3ddcd95a6aed4`

### Model license and runtime notes

LFM refers to Liquid AI's LFM model family. The verified LFM2.5-350M GGUF is published under the **LFM Open License v1.0** (Hugging Face license identifier `lfm1.0`). This is a source-available license, not an OSI open-source license. It limits commercial use to entities below an annual revenue threshold of US$10,000,000. Anyone who redistributes the weights or derivatives must include a copy of the license and the required notices. Read the full terms before use. [License file](https://huggingface.co/LiquidAI/LFM2.5-350M-GGUF/blob/main/LICENSE)

The application never bundles or downloads model weights. The operator obtains the GGUF and accepts its license. It runs only through the existing operator-managed llama.cpp adapter described below, and the manifest `license` field records the operator's declaration.

llama.cpp b12423 is newer than the llama.cpp v1.0.0 server hardening release. Do not enable the server's own `--tools` or `--mcp-servers-config` options for this integration. The application supplies the only tool, a frozen read-only indicator snapshot, and the server needs no other tool or MCP access.

The CLI used an isolated SQLite database containing 48 ascending synthetic BTC/USD hourly candles. The direct brief completed in 984 ms and the two-request tool brief in 1,219 ms, measured over one run each after server startup. Both saved status `available`, reported deterministic RSI(14) of 100 and returned `NO_TRADE`. The tool run completed both the validated indicator call and the schema-checked response. Generated commentary was terse and sometimes echoed indicator names or values. This test establishes interoperability for the pinned configuration. Commentary quality and other configurations need separate evaluation.

To repeat against a configured local archive, start the pinned server and run both CLI modes with a matching manifest. The original test used a generated temporary token and a free loopback port.

```text
llama-server --model MODEL.gguf --host 127.0.0.1 --port 9931 --api-key-file TOKEN --alias lfm2.5-350m --jinja --ctx-size 4096 --threads 6 --n-gpu-layers 0
hcs-trader research-brief --model-manifest MODEL.json --token-file TOKEN
hcs-trader research-brief --model-manifest MODEL.json --token-file TOKEN --research-tools
hcs-trader research-history --limit 2
```

## Opt-in scheduled research

Run `hcs-trader research-watch --every-seconds 3600 --interval 60` in a terminal. It accepts the same model and tool options as a one-off brief. The first run is due immediately on a fresh database. Each completed run stores the next due time atomically with its history entry. Model unavailability backs off exponentially up to 24 hours. A successful or model-free brief returns to the requested cadence.

Press Ctrl+C to stop. Restart the command with the same options to resume from its recorded due time. Elapsed intervals are skipped during downtime. One worker owns the schedule per database, and a one-off brief cannot run while that worker is active. The scheduler reads archived candles and leaves ingestion to the existing import workflow.

## One-off briefs and model configuration

`hcs-trader research-brief --interval 60` reads stored candles and emits the calculated indicator evidence as JSON. The default works offline with the model disabled.

The evidence includes the source, candle interval, first open and last close of the contiguous candle segment. RSI uses 14 periods and needs 15 consecutive closes. A gap starts a new segment and may return RSI to its warm-up state. REST and MCP use the same calculation. The [implementation plan](ROADMAP.md#5-mcp-skills-local-model-and-runs) tracks the tool loop, run statistics and local schedule.

For optional commentary, start an operator-managed llama.cpp server on `127.0.0.1`, configure a model alias and require an API key. Start `research-brief --model-manifest MODEL.json --token-file TOKEN` and set `--llama-port` if the server uses a port other than 9931. The token file must contain one token. Keep it in a private local directory. [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

The manifest has `schema_version` set to 1, a `gguf` file path relative to the manifest or absolute, and a `metadata` object with `alias`, lowercase `sha256`, `license`, `quantization`, and `runtime_version`. Populate these from the model publisher and local runtime. The application checks the GGUF header and file digest before contacting the model service, then requires the configured alias in the server response. The operator must configure llama.cpp to load that exact file. The alias is an application-level check and does not attest to the server's loaded weights, quantization or version. License and runtime fields are operator declarations.

The adapter sends only deterministic indicator evidence and a fixed research prompt to the loopback service. It rejects redirects, mismatched aliases and invalid output, bounds response size and generation tokens, and has no cloud fallback. The returned commentary is untrusted. Every report retains `NO_TRADE` as its execution decision.

Add `--research-tools` to a configured local research brief to try a two-request tool exchange. The model first requests `get_archived_indicators` with an empty object. The application returns only the frozen indicator snapshot used for the brief. The second request asks for the same schema-checked commentary, with tool selection disabled. The two requests share a 20-second deadline. The tested contract rejects unknown tools, arguments and additional calls. Enable function calling with a compatible llama.cpp build and chat template. Compatibility with the selected GGUF must be verified against the actual runtime. [llama.cpp server tool calling](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1chatcompletions-openai-compatible-chat-completions-api)
