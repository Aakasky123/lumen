type LumenDesktopStatus = {
  backendHealthy: boolean;
  modelConfigured: boolean;
  modelReady: boolean;
  modelDisplayName: string;
  modelError: string;
  extensionConnected: boolean;
  liveActionsReady: boolean;
  computerUseAvailable: boolean;
  computerUseEnabled: boolean;
};

type LumenDesktopBridge = {
  readonly isDesktop: true;
  getStatus: () => Promise<LumenDesktopStatus>;
  startRuntime: () => Promise<LumenDesktopStatus>;
  restartRuntime: () => Promise<LumenDesktopStatus>;
  setComputerUseEnabled: (enabled: boolean) => Promise<{ available: boolean; enabled: boolean }>;
  openChromeExtensions: () => Promise<boolean>;
  openDataFolder: () => Promise<{ ok: boolean; error: string }>;
};

interface Window {
  lumenDesktop?: LumenDesktopBridge;
}
