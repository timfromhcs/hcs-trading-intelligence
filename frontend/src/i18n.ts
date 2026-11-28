/** Minimal, explicit en/de labels. Untranslated strings stay in English. */

export type Locale = "en" | "de";

const STORAGE_KEY = "hcs.locale";

export const labels = {
  overview: { en: "Overview", de: "Übersicht" },
  markets: { en: "Markets", de: "Märkte" },
  world: { en: "World Monitor", de: "Weltbeobachtung" },
  portfolio: { en: "Portfolio", de: "Portfolio" },
  wallets: { en: "Wallets", de: "Wallets" },
  strategy: { en: "Strategy Lab", de: "Strategielabor" },
  risk: { en: "Risk Center", de: "Risikozentrum" },
  ledger: { en: "Execution Ledger", de: "Ausführungshistorie" },
  agents: { en: "Agents", de: "Agenten" },
  api: { en: "API & MCP", de: "API & MCP" },
  settings: { en: "Settings", de: "Einstellungen" },
  mode: { en: "Mode", de: "Modus" },
  offline: { en: "Offline", de: "Offline" },
  data_only: { en: "Data only", de: "Nur Daten" },
  paper: { en: "Paper (simulated)", de: "Papier (simuliert)" },
  live: { en: "LIVE (real funds)", de: "LIVE (echtes Geld)" },
  language: { en: "Language", de: "Sprache" },
  downloads: { en: "Downloads", de: "Downloads" },
  audit_json: { en: "Download audit events (JSON)", de: "Audit-Ereignisse herunterladen (JSON)" },
  limited_de: {
    en: "Only navigation and status labels are translated. Reports stay in English.",
    de: "Nur Navigation und Statuslabels sind übersetzt. Berichte bleiben auf Englisch.",
  },
} as const;

export type LabelKey = keyof typeof labels;

export function t(locale: Locale, key: LabelKey): string {
  return labels[key][locale];
}

export function readLocale(): Locale {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "de" || stored === "en") return stored;
  } catch {
    // Storage may be disabled in a hardened browser.
  }
  return "en";
}

export function saveLocale(locale: Locale): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, locale);
  } catch {
    // Ignore storage failures; the language still applies for this session.
  }
}
