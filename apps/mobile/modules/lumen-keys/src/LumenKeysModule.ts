import { NativeModule, requireNativeModule } from "expo";

export type BiometricStatus = "ready" | "not_enrolled" | "unavailable";

declare class LumenKeysModule extends NativeModule<Record<string, never>> {
  /** Whether a strong biometric (fingerprint or face) can unlock the key. */
  biometricStatus(): BiometricStatus;
  hasKey(alias: string): boolean;
  deleteKey(alias: string): void;
  /** Creates (or replaces) the key; resolves its public half as base64 SubjectPublicKeyInfo DER. */
  createKey(alias: string): Promise<string>;
  /**
   * Shows the system biometric prompt, then signs `payload` (UTF-8) with SHA256withECDSA.
   * Rejects with code CANCELLED, KEY_INVALIDATED, NO_KEY, NO_ACTIVITY, AUTH_ERROR, or SIGN_FAILED.
   */
  sign(alias: string, payload: string, title: string, subtitle: string): Promise<string>;
}

export default requireNativeModule<LumenKeysModule>("LumenKeys");
