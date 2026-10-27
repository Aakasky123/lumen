import { useRouter } from "expo-router";
import { Briefcase, ClipboardList, Globe, Monitor, Plus, Settings } from "lucide-react-native";
import { useCallback, useMemo, useState } from "react";
import { Alert, Pressable, RefreshControl, ScrollView, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { formatCostUsd, threadCostUsd } from "@/lib/cost";
import { formatRelative } from "@/lib/format";
import { PHASE_META, runPhase, runTargetLabel } from "@/lib/runPhase";
import { groupByRecency, threadTitle } from "@/lib/threads";
import { api, type ChatThread, type OperatorRun } from "@/api";
import { parseBackendDate } from "@/dates";
import { usePoll } from "@/hooks";
import { LumenMark } from "@/components/LumenMark";
import { Badge, IconButton, PressableCard, T, type Icon, type Tone } from "@/components/ui";
import { alpha, mono, radius, useTheme } from "@/theme";

// Suggestions fill a new chat's composer instead of sending: Lumen acts on your machine, so a task
// starts only when you have read it and pressed send. (Same list as the cockpit's home screen.)
const SUGGESTIONS: Array<{ icon: Icon; title: string; detail: string; prompt: string }> = [
  { icon: Globe, title: "Research and compare", detail: "Read reviews and prices, then summarize", prompt: "Compare the three best-reviewed standing desks under $400 and summarize the trade-offs." },
  { icon: ClipboardList, title: "Fill out a form", detail: "From your profile, stopping before submit", prompt: "Fill out the form at [paste the link] with my profile, and stop before submitting." },
  { icon: Briefcase, title: "Apply to a job", detail: "With your résumé and profile", prompt: "Apply to this job with my résumé: [paste the job link]" },
  { icon: Monitor, title: "Work in a Windows app", detail: "Files, settings, and desktop apps", prompt: "In File Explorer, sort my Downloads folder into subfolders by file type." }
];

function greeting(hour: number): string {
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

export default function Home() {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const [threads, setThreads] = useState<ChatThread[] | null>(null);
  const [runs, setRuns] = useState<OperatorRun[]>([]);
  const [offline, setOffline] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const [nextThreads, nextRuns] = await Promise.all([api.threads(), api.runs()]);
      setThreads(nextThreads);
      setRuns(nextRuns);
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, []);
  usePoll(load, 4000);

  const runById = useMemo(() => new Map(runs.map((run) => [run.id, run])), [runs]);
  const needsYou = useMemo(
    () =>
      (threads ?? []).filter((thread) => {
        const run = thread.run_id ? runById.get(thread.run_id) : null;
        return runPhase(run) === "needs_you";
      }),
    [runById, threads]
  );
  const groups = useMemo(() => groupByRecency(threads ?? []), [threads]);

  const openChat = (thread: ChatThread) => router.push({ pathname: "/chat/[id]", params: { id: String(thread.id) } });
  const newChat = (prefill?: string) => router.push({ pathname: "/chat/[id]", params: prefill ? { id: "new", prefill } : { id: "new" } });

  const confirmDelete = (thread: ChatThread) =>
    Alert.alert("Delete this chat?", "The chat is removed from Lumen on your PC too.", [
      { text: "Keep", style: "cancel" },
      {
        text: "Delete",
        style: "destructive",
        onPress: () =>
          void api
            .deleteThread(thread.id)
            .then(load)
            .catch((error: Error) => Alert.alert("Couldn't delete", error.message))
      }
    ]);

  const ChatRow = ({ thread }: { thread: ChatThread }) => {
    const run = thread.run_id ? runById.get(thread.run_id) ?? null : null;
    const phase = runPhase(run);
    const meta = phase ? PHASE_META[phase] : null;
    const target = runTargetLabel(run?.target_url);
    const updated = formatRelative(parseBackendDate(thread.updated_at));
    const cost = formatCostUsd(threadCostUsd(thread, run));
    return (
      <Pressable
        accessibilityRole="button"
        onPress={() => openChat(thread)}
        onLongPress={() => confirmDelete(thread)}
        style={({ pressed }) => ({
          flexDirection: "row",
          alignItems: "center",
          gap: 12,
          paddingHorizontal: 16,
          paddingVertical: 12,
          backgroundColor: pressed ? alpha(c.fg, 0.05) : "transparent"
        })}
      >
        <View style={{ flex: 1, minWidth: 0, gap: 2 }}>
          <T weight="500" numberOfLines={1}>
            {threadTitle(thread, run)}
          </T>
          <T variant="caption" numberOfLines={1}>
            {[target, updated, cost].filter(Boolean).join(" · ")}
          </T>
        </View>
        {meta && meta.live ? <Badge label={meta.label} tone={meta.tone as Tone} /> : null}
      </Pressable>
    );
  };

  return (
    <View style={{ flex: 1, backgroundColor: c.bg }}>
      <View style={{ paddingTop: insets.top + 6, paddingHorizontal: 12, paddingBottom: 6, flexDirection: "row", alignItems: "center", gap: 10 }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 10, paddingLeft: 6, flex: 1 }}>
          <LumenMark size={20} />
          <T style={{ fontFamily: mono, fontSize: 13, letterSpacing: 3.6, fontWeight: "700" }}>LUMEN</T>
        </View>
        <IconButton icon={Settings} accessibilityLabel="Settings" onPress={() => router.push("/settings")} />
      </View>

      <ScrollView
        contentContainerStyle={{ paddingHorizontal: 16, paddingBottom: insets.bottom + 110, gap: 22 }}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            tintColor={c.brand}
            colors={[c.brand]}
            progressBackgroundColor={c.surface2}
            onRefresh={async () => {
              setRefreshing(true);
              await load();
              setRefreshing(false);
            }}
          />
        }
      >
        <View style={{ gap: 6, paddingTop: 18 }}>
          <T color={c.fgMuted}>{greeting(new Date().getHours())}.</T>
          <T variant="display">What should I work on?</T>
        </View>

        {offline ? (
          <View style={{ padding: 12, borderRadius: radius.lg, borderWidth: 1, borderColor: alpha(c.attention, 0.3), backgroundColor: alpha(c.attention, 0.07) }}>
            <T variant="small" color={c.fg}>
              Can't reach Lumen. Check that your PC is on and Tailscale is connected on both devices.
            </T>
          </View>
        ) : null}

        {needsYou.length ? (
          <View style={{ gap: 8 }}>
            <T variant="eyebrow" color={c.attentionInk}>
              Needs you
            </T>
            {needsYou.map((thread) => {
              const run = thread.run_id ? runById.get(thread.run_id) ?? null : null;
              return (
                <PressableCard key={thread.id} onPress={() => openChat(thread)} style={{ padding: 14, gap: 4, borderColor: alpha(c.attention, 0.35) }}>
                  <T weight="600" numberOfLines={2}>
                    {threadTitle(thread, run)}
                  </T>
                  <T variant="small" numberOfLines={2}>
                    {run?.status_message?.trim() || "Waiting for your answer"}
                  </T>
                </PressableCard>
              );
            })}
          </View>
        ) : null}

        {threads && !threads.length ? (
          <View style={{ gap: 10 }}>
            {SUGGESTIONS.map((suggestion) => (
              <PressableCard key={suggestion.title} onPress={() => newChat(suggestion.prompt)} style={{ padding: 14, flexDirection: "row", gap: 12, alignItems: "center" }}>
                <suggestion.icon size={18} color={c.brandInk} />
                <View style={{ flex: 1 }}>
                  <T weight="500">{suggestion.title}</T>
                  <T variant="small">{suggestion.detail}</T>
                </View>
              </PressableCard>
            ))}
          </View>
        ) : null}

        {groups.map((group) => (
          <View key={group.label} style={{ gap: 6 }}>
            <T variant="eyebrow" style={{ paddingHorizontal: 4 }}>
              {group.label}
            </T>
            <View style={{ backgroundColor: c.surface1, borderRadius: radius.xl, borderWidth: 1, borderColor: c.line, overflow: "hidden" }}>
              {group.items.map((thread, index) => (
                <View key={thread.id} style={{ borderTopWidth: index ? 1 : 0, borderTopColor: c.line }}>
                  <ChatRow thread={thread} />
                </View>
              ))}
            </View>
          </View>
        ))}
      </ScrollView>

      <View style={{ position: "absolute", left: 16, right: 16, bottom: insets.bottom + 16 }}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="New task"
          onPress={() => newChat()}
          style={({ pressed }) => ({
            flexDirection: "row",
            alignItems: "center",
            gap: 10,
            height: 54,
            paddingHorizontal: 18,
            borderRadius: radius.xxl,
            borderWidth: 1,
            borderColor: c.lineStrong,
            backgroundColor: pressed ? c.surface2 : c.surface1,
            elevation: 6
          })}
        >
          <Plus size={18} color={c.brandInk} />
          <T color={c.fgMuted}>Describe a task, or ask a question…</T>
        </Pressable>
      </View>
    </View>
  );
}
