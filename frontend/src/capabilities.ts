/** Browser runtime capability probe. Purely informational; never an authority. */

export interface RuntimeCapabilities {
  webgpu: boolean;
  webassembly: boolean;
  sharedArrayBuffer: boolean;
  detectedAt: string;
}

const WASM_MODULE = new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0]);

export function detectRuntimeCapabilities(now: Date = new Date()): RuntimeCapabilities {
  const nav = typeof navigator === "undefined" ? undefined : navigator;
  const webgpu = Boolean(nav && "gpu" in nav && nav.gpu);
  let webassembly = false;
  try {
    webassembly =
      typeof WebAssembly === "object" &&
      typeof WebAssembly.validate === "function" &&
      WebAssembly.validate(WASM_MODULE);
  } catch {
    webassembly = false;
  }
  return {
    webgpu,
    webassembly,
    sharedArrayBuffer: typeof SharedArrayBuffer === "function",
    detectedAt: now.toISOString(),
  };
}
