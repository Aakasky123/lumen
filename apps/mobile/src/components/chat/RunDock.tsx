import { useRouter } from "expo-router";
import { Globe, Monitor, Pause, Play, ScanEye, Square } from "lucide-react-native";
import { useEffect, useState } from "react";
import { Alert, View } from "react-native";
import { formatElapsed, runIsLive, runStatus } from "@/lib/format";
import { PHASE_META, runPhase, runTargetLabel } from "@/lib/runPhase";
import type { OperatorRun } from "@/api";
import { parseBackendDate } from "@/dates";
import { LumenMark } from "@/components/LumenMark";
import { Button, IconButton, StatusDot, T } from "@/components/ui";
import { alpha, radius, useTheme } from "@/theme";

const DEFAULT_LINE: Record<string, string> = {
  working: "Working on it",
  paused: "Paused. Resume when you're ready.",
  needs_you: "Waiting for you"
};

function useElapsed(since: string | undefined, ticking: boolean): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [ticking]);
  if (!since) return null;
  const start = parseBackendDate(since).getTime();
  return Number.isFinite(start) ? Math.max(0, (now - start) / 1000) : null;
}

/**
 * The live run, docked above the composer: what Lumen is doing right now (the run's own status
 * line), where, for how long, and the controls. Mirrors the cockpit's RunDock.
 */
export function RunDock({
  run,
  questionPending,
  busy,
  onControl
}: {
  run: OperatorRun | null;
  questionPending: boolean;
  busy: boolean;
  onControl: (action: "pause" | "resume" | "cancel") => void;
}) {
  const { c } = useTheme();
  const router = useRouter();
  const phase = runPhase(run);
  const working = phase === "working";
  const elapsed = useElapsed(run?.created_at, working);
  if (!run || !phase || !runIsLive(run)) return null;

  const status = runStatus(run);
  const meta = PHASE_META[phase];
  const target = runTargetLabel(run.target_url);
  const desktop = run.target_url?.startsWith("desktop://");
  const line = phase === "needs_you" && questionPending ? "Answer above to keep going" : run.status_message?.trim() || DEFAULT_LINE[phase] || "";
  const ink = meta.tone === "attention" ? c.attentionInk : meta.tone === "brand" ? c.brandInk : c.fg;

  const confirmStop = () =>
    Alert.alert("Stop this task?", "Lumen stops and leaves the page as it is.", [
      { text: "Keep going", style: "cancel" },
      { text: "Stop", style: "destructive", onPress: () => onControl("cancel") }
    ]);

  return (
    <View style={{ borderBottomWidth: 1, borderBottomColor: c.line, paddingHorizontal: 12, paddingVertical: 8, flexDirection: "row", alignItems: "center", gap: 10 }}>
      <View
        style={{
          width: 32,
          height: 32,
          borderRadius: radius.md,
          borderWidth: 1,
          borderColor: c.line,
          backgroundColor: c.surface2,
          alignItems: "center",
          justifyContent: "center"
        }}
      >
        {working ? <LumenMark size={17} working /> : phase === "paused" ? <Pause size={15} color={c.fgMuted} /> : <StatusDot tone="attention" />}
      </View>
      <View style={{ flex: 1, minWidth: 0 }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
          <T variant="small" weight="600" color={ink} accessibilityLiveRegion="polite">
            {meta.label}
          </T>
          {target ? (
            <View style={{ flexDirection: "row", alignItems: "center", gap: 3, flexShrink: 1 }}>
              {desktop ? <Monitor size={11} color={c.fgSubtle} /> : <Globe size={11} color={c.fgSubtle} />}
              <T variant="caption" numberOfLines={1} style={{ flexShrink: 1 }}>
                {target}
              </T>
            </View>
          ) : null}
          {working && elapsed != null ? <T variant="caption">· {formatElapsed(elapsed)}</T> : null}
        </View>
        <T variant="small" numberOfLines={1} color={working ? alpha(c.fg, 0.8) : c.fgMuted}>
          {line}
        </T>
      </View>
      <IconButton icon={ScanEye} accessibilityLabel="See what Lumen sees" onPress={() => router.push({ pathname: "/live/[runId]", params: { runId: String(run.id) } })} />
      {status.canResume ? <Button label="Resume" icon={Play} variant="primary" size="sm" loading={busy} onPress={() => onControl("resume")} /> : null}
      {status.canPause ? <IconButton icon={Pause} accessibilityLabel="Pause the task" disabled={busy} onPress={() => onControl("pause")} /> : null}
      {status.canEnd ? <IconButton icon={Square} accessibilityLabel="Stop the task" color={c.dangerInk} disabled={busy} onPress={confirmStop} /> : null}
    </View>
  );
}
