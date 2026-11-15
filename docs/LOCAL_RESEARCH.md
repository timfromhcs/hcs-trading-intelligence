# Local research brief

`hcs-trader research-brief --interval 60` reads stored candles and emits the calculated indicator evidence as JSON. The default works offline with the model disabled.

For optional commentary, start an operator-managed llama.cpp server on `127.0.0.1`, configure a model alias and require an API key. Start the research command with `--model-alias ALIAS --token-file PATH` and set `--llama-port` if the server uses a port other than 9931. The token file must contain one token. Keep it in a private local directory. Operator configuration currently controls the loaded GGUF, its checksum and license, runtime version, context size and GPU offload. [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

The adapter sends only deterministic indicator evidence and a fixed research prompt to the loopback service. It rejects redirects, mismatched aliases and invalid output, bounds response size and generation tokens, and has no cloud fallback. The returned commentary is untrusted. Every report retains `NO_TRADE` as its execution decision.
