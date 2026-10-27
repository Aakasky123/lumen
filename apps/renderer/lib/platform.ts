import { useSyncExternalStore } from "react";

/** Whether the cockpit runs on a Mac (the desktop app's own window or a browser on one). */
export function detectMac(): boolean {
  if (typeof navigator === "undefined") return false;
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  const platform = nav.userAgentData?.platform || nav.platform || nav.userAgent || "";
  return /mac/i.test(platform);
}

const noSubscription = () => () => {};

/** The OS the cockpit runs on. The static export renders the Windows words; a Mac switches after hydration
 *  (the server snapshot keeps hydration matching). */
export function useIsMac(): boolean {
  return useSyncExternalStore(noSubscription, detectMac, () => false);
}

/** The desktop the cockpit's computer-use surface is, in words ("Mac" or "Windows"). */
export function desktopName(isMac: boolean): string {
  return isMac ? "Mac" : "Windows";
}

/** A shortcut's keys as this OS labels them: Ctrl is Command on a Mac (the handlers accept both). */
export function platformKeys(keys: readonly string[], isMac: boolean): string[] {
  if (!isMac) return [...keys];
  return keys.map((key) => (key === "Ctrl" ? "⌘" : key === "Shift" ? "⇧" : key === "Alt" ? "⌥" : key));
}
