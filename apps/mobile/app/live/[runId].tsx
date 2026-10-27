import { Image } from "expo-image";
import { Stack, useLocalSearchParams } from "expo-router";
import { useCallback, useState } from "react";
import { View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { PHASE_META, runPhase, runTargetLabel } from "@/lib/runPhase";
import { api, type OperatorRun } from "@/api";
import { usePoll } from "@/hooks";
import { LumenMark } from "@/components/LumenMark";
import { T } from "@/components/ui";
import { useTheme } from "@/theme";

/**
 * The run's latest frame, exactly as the model saw it (sensitive fields are masked at capture),
 * refreshed while the run moves.
 */
export default function LiveView() {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const params = useLocalSearchParams<{ runId: string }>();
  const runId = Number(params.runId);
  const [run, setRun] = useState<OperatorRun | null>(null);
  const [missing, setMissing] = useState(false);

  const load = useCallback(async () => {
    setRun(await api.run(runId));
  }, [runId]);
  const phase = runPhase(run);
  usePoll(load, phase === "working" ? 2000 : 6000, Number.isFinite(runId));

  return (
    <View style={{ flex: 1, backgroundColor: "#000000" }}>
      <Stack.Screen options={{ headerStyle: { backgroundColor: "#000000" }, headerTintColor: "#ffffff", headerTitleStyle: { color: "#ffffff" } }} />
      <View style={{ flex: 1, alignItems: "center", justifyContent: "center" }}>
        {run && !missing ? (
          <Image
            source={api.latestScreenshot(runId, run.updated_at)}
            style={{ width: "100%", height: "100%" }}
            contentFit="contain"
            transition={200}
            cachePolicy="none"
            onError={() => setMissing(true)}
            onLoad={() => setMissing(false)}
            accessibilityLabel="The screen Lumen is working on"
          />
        ) : (
          <T color="#b8c2be">{missing ? "No screenshot yet." : ""}</T>
        )}
      </View>
      {run ? (
        <View style={{ padding: 16, paddingBottom: insets.bottom + 16, gap: 4, backgroundColor: "#0b0d0c", borderTopWidth: 1, borderTopColor: c.line }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
            <LumenMark size={16} working={phase === "working"} />
            <T variant="small" weight="600" color="#eef2f0">
              {phase ? PHASE_META[phase].label : ""}
            </T>
            <T variant="caption" color="#7c8883" numberOfLines={1} style={{ flexShrink: 1 }}>
              {runTargetLabel(run.target_url)}
            </T>
          </View>
          <T variant="small" color="#9ea9a4" numberOfLines={2}>
            {run.status_message?.trim() || " "}
          </T>
        </View>
      ) : null}
    </View>
  );
}
