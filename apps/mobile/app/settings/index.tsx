import Constants from "expo-constants";
import { useRouter } from "expo-router";
import { Bell, ChevronRight, Cpu, FileText, KeyRound, Laptop, Lock, ShieldCheck, Unlink, UserRound, Zap } from "lucide-react-native";
import { useCallback, useState } from "react";
import { Alert, ScrollView, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { formatRelative } from "@/lib/format";
import { api, type ModelProvider, type PairedDevice } from "@/api";
import { parseBackendDate, timeOfDay } from "@/dates";
import { autonomyUnlocked, errorMessage, usePoll, useApprovalMode } from "@/hooks";
import { useSession } from "@/SessionProvider";
import { ModeSheet } from "@/components/ModeSheet";
import { Group, Row, T } from "@/components/ui";
import { useTheme } from "@/theme";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={{ gap: 8 }}>
      <T variant="eyebrow" style={{ paddingHorizontal: 4 }}>
        {title}
      </T>
      <Group>{children}</Group>
    </View>
  );
}

export default function SettingsScreen() {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const { session, unpair, enableAlerts } = useSession();
  const approval = useApprovalMode();
  const [modeOpen, setModeOpen] = useState(false);
  const [device, setDevice] = useState<PairedDevice | null>(null);
  const [model, setModel] = useState<ModelProvider | null>(null);

  const load = useCallback(async () => {
    const [me, providers] = await Promise.all([api.me(), api.modelProviders().catch(() => null)]);
    setDevice(me.device);
    if (providers) setModel(providers.active);
  }, []);
  usePoll(load, 15000);

  const chevron = <ChevronRight size={18} color={c.fgSubtle} />;
  const host = session?.baseUrl.replace(/^https?:\/\//, "") ?? "";

  const confirmUnpair = () =>
    Alert.alert("Unpair this phone?", "It stops reaching Lumen, and its fingerprint key is deleted. You can pair again any time.", [
      { text: "Keep", style: "cancel" },
      { text: "Unpair", style: "destructive", onPress: () => void unpair() }
    ]);

  const turnOnAlerts = async () => {
    const on = await enableAlerts();
    if (!on) Alert.alert("Alerts are off", "Allow notifications for Lumen in Android settings to get alerts when Lumen needs you.");
    void load();
  };

  return (
    <ScrollView style={{ flex: 1, backgroundColor: c.bg }} contentContainerStyle={{ padding: 16, paddingBottom: insets.bottom + 32, gap: 22 }}>
      <Section title="Final actions">
        <Row
          first
          icon={approval.mode === "autonomous" ? Zap : ShieldCheck}
          title={approval.mode === "autonomous" ? "Full autonomy" : "Safe approvals"}
          detail={
            approval.mode === "autonomous"
              ? "Completes requested final actions without asking, including purchases."
              : "Waits for you before consequential final actions."
          }
          right={chevron}
          onPress={() => setModeOpen(true)}
        />
        {approval.mode === "autonomous" ? (
          autonomyUnlocked(device) ? (
            <Row
              icon={Lock}
              title="Lock Full autonomy on this phone"
              detail={`Unlocked here until ${timeOfDay(device?.autonomy_until ?? "")}. Tasks you start here then ask before final steps.`}
              onPress={() =>
                void api
                  .lockAutonomy()
                  .then(load)
                  .catch((error) => Alert.alert("Couldn't lock it", errorMessage(error)))
              }
            />
          ) : (
            <Row icon={Lock} title="Locked on this phone" detail="Your fingerprint unlocks it the next time you start a task here." />
          )
        ) : null}
      </Section>

      <Section title="What Lumen knows">
        <Row first icon={UserRound} title="Profile" detail="Everything Lumen knows about you" right={chevron} onPress={() => router.push("/settings/profile")} />
        <Row icon={FileText} title="Documents" detail="Résumés and other files Lumen can use" right={chevron} onPress={() => router.push("/settings/documents")} />
        <Row icon={KeyRound} title="Sign-ins" detail="Accounts Lumen can sign in with" right={chevron} onPress={() => router.push("/settings/signins")} />
        <Row icon={Cpu} title="Model" detail={model ? `${model.display_name || model.model_name} · change it on your PC` : "Set on your PC"} />
      </Section>

      <Section title="This phone">
        <Row first icon={Laptop} title="Connected to" detail={host} />
        <Row
          icon={Bell}
          title="Alerts"
          detail={device?.has_push ? "On: Lumen tells you when it needs you or finishes" : "Off: tap to turn on"}
          onPress={device?.has_push ? undefined : () => void turnOnAlerts()}
          right={device?.has_push ? undefined : chevron}
        />
        <Row
          title={device?.name || session?.deviceName || "This phone"}
          detail={device?.created_at ? `Paired ${formatRelative(parseBackendDate(device.created_at))}` : undefined}
        />
        <Row icon={Unlink} title="Unpair this phone" destructive onPress={confirmUnpair} />
      </Section>

      <T variant="caption" style={{ textAlign: "center" }}>
        Lumen for Android {Constants.expoConfig?.version ?? ""}
      </T>
      <ModeSheet visible={modeOpen} mode={approval.mode} onClose={() => setModeOpen(false)} onChoose={approval.update} />
    </ScrollView>
  );
}
