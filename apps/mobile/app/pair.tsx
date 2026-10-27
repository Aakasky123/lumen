import { CameraView, useCameraPermissions } from "expo-camera";
import * as Haptics from "expo-haptics";
import { useLocalSearchParams } from "expo-router";
import { Camera, Fingerprint, Laptop, Link2 } from "lucide-react-native";
import { useEffect, useRef, useState } from "react";
import { ScrollView, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { errorMessage } from "@/hooks";
import { pairingTarget, parsePairingUri, type PairingTarget } from "@/session";
import { useSession } from "@/SessionProvider";
import { LumenMark } from "@/components/LumenMark";
import { Banner, Button, Card, T } from "@/components/ui";
import { alpha, radius, useTheme } from "@/theme";

/**
 * Pair this phone with Lumen on the PC: scan the one-time code from Settings → Devices (or open its
 * lumen://pair link), confirm the address, and the phone registers its fingerprint-locked key.
 */
export default function PairScreen() {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const params = useLocalSearchParams<{ url?: string; code?: string }>();
  const { pair, lostPairing } = useSession();
  const [permission, requestPermission] = useCameraPermissions();
  const [target, setTarget] = useState<PairingTarget | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const lastScan = useRef("");

  // A lumen://pair link opens this screen with the code already in hand.
  useEffect(() => {
    if (!params.url || !params.code) return;
    const parsed = pairingTarget(String(params.url), String(params.code));
    if ("error" in parsed) setError(parsed.error);
    else setTarget(parsed);
  }, [params.code, params.url]);

  const onScanned = (data: string) => {
    if (target || busy || data === lastScan.current) return;
    lastScan.current = data;
    const parsed = parsePairingUri(data);
    if ("error" in parsed) {
      setError(parsed.error);
      return;
    }
    void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    setError("");
    setTarget(parsed);
  };

  const confirm = async () => {
    if (!target) return;
    setBusy(true);
    setError("");
    try {
      await pair(target);
    } catch (failure) {
      setError(errorMessage(failure, "Pairing didn't work. Show a new code on your PC and try again."));
      lastScan.current = "";
    } finally {
      setBusy(false);
    }
  };

  const host = target ? target.baseUrl.replace(/^https?:\/\//, "") : "";

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: 20, paddingTop: insets.top + 28, paddingBottom: insets.bottom + 28, gap: 20 }}
    >
      <View style={{ gap: 14 }}>
        <LumenMark size={44} tile />
        <T variant="display">Pair with Lumen</T>
        <T color={c.fgMuted}>Start tasks, follow them, and answer Lumen from your phone. Lumen keeps working on your PC.</T>
      </View>

      {lostPairing ? <Banner tone="attention">This phone was removed from Lumen on your PC. Pair it again to keep going.</Banner> : null}

      {target ? (
        <Card tone="brand" style={{ padding: 16, gap: 14 }}>
          <View style={{ flexDirection: "row", gap: 12, alignItems: "center" }}>
            <Link2 size={18} color={c.brandInk} />
            <View style={{ flex: 1 }}>
              <T variant="eyebrow">Your PC</T>
              <T weight="600" numberOfLines={1}>
                {host}
              </T>
            </View>
          </View>
          <View style={{ flexDirection: "row", gap: 12 }}>
            <Fingerprint size={18} color={c.fgMuted} />
            <T variant="small" style={{ flex: 1 }}>
              This phone makes a key that only works with your fingerprint. Lumen asks for it before payments, saving sign-ins, and setting changes.
            </T>
          </View>
          <Button label="Pair this phone" variant="primary" size="lg" loading={busy} onPress={() => void confirm()} />
          <Button
            label="Scan a different code"
            variant="ghost"
            disabled={busy}
            onPress={() => {
              setTarget(null);
              lastScan.current = "";
            }}
          />
        </Card>
      ) : (
        <>
          <View style={{ gap: 10 }}>
            {[
              { icon: Laptop, text: "On your PC, open Lumen → Settings → Devices → Pair a phone." },
              { icon: Camera, text: "Point this phone at the code." }
            ].map((step, index) => (
              <View key={step.text} style={{ flexDirection: "row", gap: 12, alignItems: "center" }}>
                <View
                  style={{
                    width: 26,
                    height: 26,
                    borderRadius: 13,
                    borderWidth: 1,
                    borderColor: c.lineStrong,
                    alignItems: "center",
                    justifyContent: "center"
                  }}
                >
                  <T variant="caption" weight="600">
                    {index + 1}
                  </T>
                </View>
                <T style={{ flex: 1 }}>{step.text}</T>
              </View>
            ))}
          </View>
          <View
            style={{
              aspectRatio: 1,
              borderRadius: radius.xxl,
              overflow: "hidden",
              borderWidth: 1,
              borderColor: c.lineStrong,
              backgroundColor: c.surface1,
              alignItems: "center",
              justifyContent: "center"
            }}
          >
            {permission?.granted ? (
              <>
                <CameraView
                  style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0 }}
                  facing="back"
                  barcodeScannerSettings={{ barcodeTypes: ["qr"] }}
                  onBarcodeScanned={({ data }) => onScanned(data)}
                />
                <View
                  pointerEvents="none"
                  style={{ width: "62%", aspectRatio: 1, borderRadius: radius.xl, borderWidth: 2, borderColor: alpha(c.brand, 0.85) }}
                />
              </>
            ) : (
              <View style={{ alignItems: "center", gap: 12, padding: 24 }}>
                <Camera size={28} color={c.fgMuted} />
                <T variant="small" style={{ textAlign: "center" }}>
                  Lumen needs the camera to read the pairing code.
                </T>
                <Button label="Allow camera" variant="primary" onPress={() => void requestPermission()} />
              </View>
            )}
          </View>
          <T variant="caption" style={{ textAlign: "center" }}>
            Your phone must be signed in to the same Tailscale network as your PC.
          </T>
        </>
      )}

      {error ? <Banner tone="danger">{error}</Banner> : null}
    </ScrollView>
  );
}
