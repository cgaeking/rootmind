import { describe, expect, it } from "vitest";
import { detectProvider, maskKey } from "./llmKey";

describe("detectProvider", () => {
  it("recognizes each provider's key format", () => {
    expect(detectProvider("sk-ant-api03-abcdef")).toBe("anthropic");
    expect(detectProvider("xai-abcdef")).toBe("xai");
    expect(detectProvider("sk-proj-abcdef")).toBe("openai");
    expect(detectProvider("sk-abcdef")).toBe("openai");
  });

  it("ignores surrounding whitespace from a paste", () => {
    expect(detectProvider("  sk-ant-api03-abc\n")).toBe("anthropic");
  });

  it("says nothing for other formats, a mask or an empty field", () => {
    expect(detectProvider("")).toBeNull();
    expect(detectProvider("…abcd")).toBeNull();
    expect(detectProvider("AIzaSyabcdef")).toBeNull();
    expect(detectProvider("ant-sk-abc")).toBeNull();
  });
});

describe("maskKey", () => {
  it("matches the backend's mask", () => {
    expect(maskKey("sk-ant-api03-abcd1234")).toBe("…1234");
    expect(maskKey("short123")).toBe("…");
    expect(maskKey("")).toBe("");
  });
});
