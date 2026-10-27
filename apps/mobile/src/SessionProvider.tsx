import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import LumenKeys from "../modules/lumen-keys/src/LumenKeysModule";
import { api, bindSession, pairDevice } from "./api";
import { registerForAlerts } from "./push";
import { clearSession, defaultDeviceName, loadSession, saveSession, STEP_UP_KEY_ALIAS, type PairingTarget, type Session } from "./session";

type SessionState = {
  /** undefined while the secure store is being read. */
  session: Session | null | undefined;
  /** Set when the PC revoked this phone (or its token stopped working). */
  lostPairing: boolean;
  pair: (target: PairingTarget) => Promise<void>;
  unpair: () => Promise<void>;
  enableAlerts: () => Promise<boolean>;
};

const SessionContext = createContext<SessionState | null>(null);

async function forgetLocally(): Promise<void> {
  await clearSession();
  try {
    LumenKeys.deleteKey(STEP_UP_KEY_ALIAS);
  } catch {
    // The key may already be gone.
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  const [lostPairing, setLostPairing] = useState(false);

  const handleUnpaired = useCallback(() => {
    bindSession(null, null);
    setSession(null);
    setLostPairing(true);
    void forgetLocally();
  }, []);

  useEffect(() => {
    let active = true;
    void loadSession().then((stored) => {
      if (!active) return;
      bindSession(stored, stored ? handleUnpaired : null);
      setSession(stored);
    });
    return () => {
      active = false;
    };
  }, [handleUnpaired]);

  const enableAlerts = useCallback(async () => {
    try {
      const token = await registerForAlerts();
      await api.registerPushToken(token);
      return Boolean(token);
    } catch {
      return false;
    }
  }, []);

  const pair = useCallback(
    async (target: PairingTarget) => {
      const biometrics = LumenKeys.biometricStatus();
      if (biometrics === "not_enrolled") {
        throw new Error("Add a fingerprint or face unlock in Android settings first. Lumen uses it to confirm payments and sign-ins.");
      }
      if (biometrics !== "ready") {
        throw new Error("This phone has no strong fingerprint or face unlock, which Lumen needs to confirm payments and sign-ins.");
      }
      const publicKey = await LumenKeys.createKey(STEP_UP_KEY_ALIAS);
      const name = defaultDeviceName();
      const { device, token } = await pairDevice(target.baseUrl, { code: target.code, name, public_key: publicKey });
      const next: Session = { baseUrl: target.baseUrl, token, deviceId: device.id, deviceName: device.name };
      await saveSession(next);
      bindSession(next, handleUnpaired);
      setLostPairing(false);
      setSession(next);
      void enableAlerts();
    },
    [enableAlerts, handleUnpaired]
  );

  const unpair = useCallback(async () => {
    try {
      await api.unpair();
    } catch {
      // Forget locally even if the PC can't be reached; it can be removed there too.
    }
    bindSession(null, null);
    await forgetLocally();
    setLostPairing(false);
    setSession(null);
  }, []);

  const value = useMemo(() => ({ session, lostPairing, pair, unpair, enableAlerts }), [session, lostPairing, pair, unpair, enableAlerts]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession outside SessionProvider");
  return value;
}
