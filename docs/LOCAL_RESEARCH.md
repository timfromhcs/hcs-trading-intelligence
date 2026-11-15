# Local research brief

`hcs-trader research-brief --interval 60` reads stored candles and emits the calculated indicator evidence as JSON. The default works offline with the model disabled.

For optional commentary, start an operator-managed llama.cpp server on `127.0.0.1`, configure a model alias and require an API key. Start `research-brief --model-manifest MODEL.json --token-file TOKEN` and set `--llama-port` if the server uses a port other than 9931. The token file must contain one token. Keep it in a private local directory. [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

The manifest has `schema_version` set to 1, a `gguf` file path relative to the manifest or absolute, and a `metadata` object with `alias`, lowercase `sha256`, `license`, `quantization`, and `runtime_version`. Populate these from the model publisher and local runtime. The application checks the GGUF header and file digest before contacting the model service, then requires the configured alias in the server response. The operator must configure llama.cpp to load that exact file. The alias is an application-level check and does not attest to the server's loaded weights, quantization or version. License and runtime fields are operator declarations.

The adapter sends only deterministic indicator evidence and a fixed research prompt to the loopback service. It rejects redirects, mismatched aliases and invalid output, bounds response size and generation tokens, and has no cloud fallback. The returned commentary is untrusted. Every report retains `NO_TRADE` as its execution decision.
