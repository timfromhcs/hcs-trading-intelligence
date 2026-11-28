import { describe, expect, it, vi } from "vitest";

import { detectRuntimeCapabilities } from "./capabilities";

describe("runtime capability probe", () => {
  it("reports WebGPU when the browser exposes it", () => {
    vi.stubGlobal("navigator", { gpu: {} });
    const result = detectRuntimeCapabilities(new Date("2026-11-28T00:00:00Z"));
    expect(result.webgpu).toBe(true);
    expect(result.detectedAt).toBe("2026-11-28T00:00:00.000Z");
  });

  it("reports no WebGPU when navigator is absent", () => {
    vi.stubGlobal("navigator", undefined);
    expect(detectRuntimeCapabilities().webgpu).toBe(false);
  });

  it("validates WebAssembly and degrades safely when unavailable", () => {
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("WebAssembly", { validate: () => true });
    expect(detectRuntimeCapabilities().webassembly).toBe(true);
    vi.stubGlobal("WebAssembly", {
      validate: () => {
        throw new Error("disabled");
      },
    });
    expect(detectRuntimeCapabilities().webassembly).toBe(false);
  });

  it("reports SharedArrayBuffer availability", () => {
    vi.stubGlobal("navigator", {});
    const result = detectRuntimeCapabilities();
    expect(typeof result.sharedArrayBuffer).toBe("boolean");
  });
});
