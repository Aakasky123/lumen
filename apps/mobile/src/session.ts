import * as Device from "expo-device";
import * as SecureStore from "expo-secure-store";

/**
 * The paired-device session: which Lumen backend this phone talks to and the bearer token it was
 * issued. Kept in the Keystore-backed secure store. The step-up signing key lives separately in the
 * Android Keystore under STEP_UP_KEY_ALIAS (modules/lumen-keys) and never leaves the hardware.
 */
export type Session = {
  baseUrl: string;
  token: string;
  deviceId: number;
  deviceName: string;
};

export const STEP_UP_KEY_ALIAS = "lumen-step-up";
const SESSION_KEY = "lumen.session.v1";

export async function loadSession(): Promise<Session | null> {
  try {
    const raw = await SecureStore.getItemAsync(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Session>;
    if (!parsed.baseUrl || !parsed.token || !parsed.deviceId) return null;
    return {
      baseUrl: parsed.baseUrl,
      token: parsed.token,
      deviceId: Number(parsed.deviceId),
      deviceName: String(parsed.deviceName || "")
    };
  } catch {
    return null;
  }
}

export async function saveSession(session: Session): Promise<void> {
  await SecureStore.setItemAsync(SESSION_KEY, JSON.stringify(session));
}

export async function clearSession(): Promise<void> {
  await SecureStore.deleteItemAsync(SESSION_KEY);
}

export type PairingTarget = { baseUrl: string; code: string };

/**
 * Read the pairing code the cockpit shows (`lumen://pair?v=1&url=…&code=…`). Real phones only pair
 * over HTTPS (the tailnet address); plain HTTP is accepted only from the Android emulator to its
 * host-PC alias, for development.
 */
export function parsePairingUri(raw: string): PairingTarget | { error: string } {
  const value = String(raw || "").trim();
  const match = /^lumen:\/\/pair\?(.*)$/i.exec(value);
  if (!match) return { error: "That isn't a Lumen pairing code." };
  const params = new URLSearchParams(match[1]);
  return pairingTarget(params.get("url") || "", params.get("code") || "");
}

export function pairingTarget(url: string, code: string): PairingTarget | { error: string } {
  if (!code || code.length < 16) return { error: "The pairing code is incomplete. Show a new one on your PC." };
  const baseUrl = url.trim().replace(/\/+$/, "");
  const allowed = /^https:\/\/[a-z0-9.-]+(:\d+)?$/i.test(baseUrl) || (!Device.isDevice && /^http:\/\/10\.0\.2\.2:\d+$/.test(baseUrl));
  if (!allowed) return { error: "Lumen can only pair over your private Tailscale address (https)." };
  return { baseUrl, code };
}

export function defaultDeviceName(): string {
  const name = [Device.manufacturer, Device.modelName].filter(Boolean).join(" ").trim();
  return name || "Android phone";
}
