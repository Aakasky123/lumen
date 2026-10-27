import { Image } from "expo-image";
import { useRouter } from "expo-router";
import { ScanEye } from "lucide-react-native";
import { useState } from "react";
import { Pressable, View } from "react-native";
import { api, type OperatorRun } from "@/api";
import { T } from "@/components/ui";
import { alpha, radius, useTheme } from "@/theme";

/** The latest frame of a working run, exactly as the model saw it. Tap for the full view. */
export function LiveThumb({ run }: { run: OperatorRun }) {
  const { c } = useTheme();
  const router = useRouter();
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel="See what Lumen sees"
      onPress={() => router.push({ pathname: "/live/[runId]", params: { runId: String(run.id) } })}
      style={{ borderRadius: radius.lg, overflow: "hidden", borderWidth: 1, borderColor: c.line, backgroundColor: c.surface2 }}
    >
      <Image
        source={api.latestScreenshot(run.id, run.updated_at)}
        style={{ width: "100%", aspectRatio: 16 / 9 }}
        contentFit="cover"
        contentPosition="top"
        transition={250}
        cachePolicy="none"
        onError={() => setFailed(true)}
      />
      <View
        style={{
          position: "absolute",
          left: 8,
          bottom: 8,
          flexDirection: "row",
          alignItems: "center",
          gap: 6,
          paddingHorizontal: 8,
          paddingVertical: 4,
          borderRadius: 999,
          backgroundColor: alpha("#000000", 0.6)
        }}
      >
        <ScanEye size={13} color="#ffffff" />
        <T variant="caption" color="#ffffff" weight="600">
          What Lumen sees
        </T>
      </View>
    </Pressable>
  );
}
