# Local model policy

The application works with no model installed. Inference is optional, local
only and never an execution authority. A model cannot place an order, change a
risk limit, promote a strategy or call the signer.

## Verified runtime evidence

The recorded Windows x64 CPU run used **llama.cpp build `b12423`** with
`LFM2.5-350M Q4_K_M` (Liquid AI, Apache-2.0) and the loopback
OpenAI-compatible server. Both the direct and tool-enabled research briefs
completed and stored successful run records. This is a historical observation,
not a promise about other builds, GPUs or models. Re-verify the build tag and
model checksum when reproducing. Source:
[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md). See also [local research evidence](LOCAL_RESEARCH.md).

## Catalog and admission

`src/hcs_trader/model_catalog.py` lists advisory candidates of at most 9B
parameters under permissive licenses (Apache-2.0 or MIT). A candidate is not
usable until the operator supplies a local `.gguf` file and a manifest with:

- the exact alias from the catalog,
- the SPDX license identifier,
- quantization and runtime version,
- the full 64-character SHA-256 of the local file.

`hcs_trader.model_manifest.verify_model_manifest` checks the GGUF header and
the checksum with a constant-time comparison. `model_catalog.admit` checks the
license allowlist and alias. A mismatch fails closed.

Downloads are operator-initiated outside the application. The app never
downloads weights at startup and never bundles multi-gigabyte files in the
executable. Keep model weights in the OS application-data directory, never in
a temporary one-file extraction directory.

## Unverified names

**OpenJEV** and **System One JEV** could not be matched to an established,
verifiable public model or project. They remain unverified aliases in the
catalog and must not be described as trained, published or benchmarked models.

## Output boundary

Only constrained JSON crosses from a model into the application. Pydantic
validates it with `extra="forbid"`; unknown citations, malformed output and
oversized responses become `NO_TRADE`. RSI and every other indicator are
computed deterministically from candles and passed to the model as structured
evidence. The model is not the source of those values.

`hcs_trader.agents.eval` provides offline release-blocking tests for schema
validity, citation faithfulness and injection resistance. Run
`hcs-trader eval-model` before promoting any model or prompt change; the command
prints a JSON report and exits non-zero when a release gate fails. A failing
injection case always blocks release.
