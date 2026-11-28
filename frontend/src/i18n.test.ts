import { describe, expect, it } from "vitest";

import { readLocale, t } from "./i18n";

describe("locale labels", () => {
  it("translates navigation and mode labels", () => {
    expect(t("de", "overview")).toBe("Übersicht");
    expect(t("de", "risk")).toBe("Risikozentrum");
    expect(t("de", "paper")).toBe("Papier (simuliert)");
    expect(t("en", "overview")).toBe("Overview");
  });

  it("defaults to English when no locale is stored", () => {
    expect(readLocale()).toBe("en");
  });
});
