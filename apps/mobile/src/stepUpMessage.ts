/**
 * The exact bytes the phone's key signs for a step-up: the server's one-time challenge bound to one
 * request. Must match apps/api/app/remote_access.py:step_up_message byte for byte.
 */
export const STEP_UP_FORMAT = "lumen-step-up/v1";

export function stepUpMessage(nonce: string, method: string, target: string, bodySha256Hex: string): string {
  return [STEP_UP_FORMAT, nonce, method.toUpperCase(), target, bodySha256Hex].join("\n");
}
