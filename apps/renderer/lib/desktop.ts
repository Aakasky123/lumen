"use client";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "/api/backend";

/**
 * The Electron preload exposes window.lumenDesktop. Under the fixture backend
 * (NEXT_PUBLIC_LUMEN_FIXTURES=1, see dev/fixture-backend.mjs) a stand-in reads the fixture's host
 * status so the desktop status UI can be designed in a plain browser. Production builds never set
 * the flag, so the stand-in compiles away.
 */
export function desktopBridge(): LumenDesktopBridge | undefined {
  if (typeof window === "undefined") return undefined;
  if (window.lumenDesktop) return window.lumenDesktop;
  if (process.env.NEXT_PUBLIC_LUMEN_FIXTURES === "1") return fixtureBridge;
  return undefined;
}

async function fixture<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}/__fixture/${path}`, { cache: "no-store", headers: { "Content-Type": "application/json" }, ...init });
  return (await response.json()) as T;
}

const fixtureBridge: LumenDesktopBridge = {
  isDesktop: true,
  getStatus: () => fixture<LumenDesktopStatus>("desktop-status"),
  startRuntime: () => fixture<LumenDesktopStatus>("desktop/start", { method: "POST", body: "{}" }),
  restartRuntime: () => fixture<LumenDesktopStatus>("desktop/restart", { method: "POST", body: "{}" }),
  setComputerUseEnabled: async (enabled) => {
    const status = await fixture<LumenDesktopStatus>("desktop/computer-use", { method: "POST", body: JSON.stringify({ enabled }) });
    return { available: status.computerUseAvailable, enabled: status.computerUseEnabled };
  },
  openChromeExtensions: async () => true,
  openDataFolder: async () => ({ ok: true, error: "" })
};
