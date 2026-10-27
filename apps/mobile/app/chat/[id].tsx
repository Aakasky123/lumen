import * as Haptics from "expo-haptics";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { useHeaderHeight } from "expo-router/react-navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, FlatList, KeyboardAvoidingView, View, type TextInput } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { formatCostUsd, threadCostUsd } from "@/lib/cost";
import { runIsLive } from "@/lib/format";
import { runPhase } from "@/lib/runPhase";
import { threadTitle } from "@/lib/threads";
import type { ChatMessage } from "@/api";
import { ensureAutonomyUnlocked, errorMessage, sensitiveEntryOf, useApprovalMode, useChatThread } from "@/hooks";
import { Composer } from "@/components/chat/Composer";
import { LiveThumb } from "@/components/chat/LiveThumb";
import { MessageItem } from "@/components/chat/MessageItem";
import { QuestionCard, type FinalActionFn, type SelectFn } from "@/components/chat/QuestionCard";
import { RunDock } from "@/components/chat/RunDock";
import { DirectEntryCard, VaultHandoffCard } from "@/components/chat/SecureEntry";
import { ModeSheet } from "@/components/ModeSheet";
import { Spinner, T } from "@/components/ui";
import { useTheme } from "@/theme";

export default function ChatScreen() {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const params = useLocalSearchParams<{ id: string; prefill?: string }>();
  const initialId = params.id && params.id !== "new" ? Number(params.id) : null;
  const chat = useChatThread(Number.isFinite(initialId) ? initialId : null);
  const approval = useApprovalMode();
  const [draft, setDraft] = useState(typeof params.prefill === "string" ? params.prefill : "");
  const [modeOpen, setModeOpen] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const inputRef = useRef<TextInput>(null);
  const headerHeight = useHeaderHeight();
  const router = useRouter();

  // A new chat takes its thread's id once the first message creates it, so the stack can tell this
  // screen is that chat (an alert for its task then returns here instead of opening it a second time).
  useEffect(() => {
    if (params.id === "new" && chat.threadId != null) router.setParams({ id: String(chat.threadId) });
  }, [chat.threadId, params.id, router]);

  const run = chat.run;
  const entry = sensitiveEntryOf(run);
  const working = runPhase(run) === "working";
  const firstUserMessage = chat.messages.find((message) => message.role === "user")?.content ?? "";
  const title = run ? threadTitle({ title: firstUserMessage }, run) : firstUserMessage ? threadTitle({ title: firstUserMessage }) : "New chat";
  // The chat's run is polled every 2.5 s while it works, so the spend climbs live in the header.
  const cost = formatCostUsd(threadCostUsd(chat.thread, run));
  const reversed = useMemo(() => [...chat.messages].reverse(), [chat.messages]);

  const send = async () => {
    const content = draft.trim();
    if (!content) return;
    setDraft("");
    // A phone-started task runs with Full autonomy only while the phone's fingerprint unlock is live;
    // otherwise the PC holds it to Safe approvals (remote_access.autonomy_held). One fingerprint
    // unlocks it for hours, so most messages need none.
    if (approval.mode === "autonomous" && !run) {
      try {
        if (!(await ensureAutonomyUnlocked())) {
          setDraft(content);
          return;
        }
      } catch (error) {
        setDraft(content);
        Alert.alert("Couldn't unlock Full autonomy", errorMessage(error));
        return;
      }
    }
    await chat.send(content);
  };

  const onSelect: SelectFn = (text, extra) => {
    void Haptics.selectionAsync();
    void chat.send(text, extra);
  };

  const onFinalAction: FinalActionFn = (question, contract, choice) => {
    void chat.answerFinalAction(question, contract, choice).catch((error) => {
      const message = errorMessage(error);
      if (message) Alert.alert("Lumen didn't take that step", message);
    });
  };

  const control = async (action: "pause" | "resume" | "cancel") => {
    setControlBusy(true);
    try {
      await chat.control(action);
    } catch (error) {
      Alert.alert("That didn't work", errorMessage(error));
    } finally {
      setControlBusy(false);
    }
  };

  const ListTail = (
    <View style={{ gap: 14, paddingTop: 14 }}>
      {working && run ? <LiveThumb run={run} /> : null}
      {entry && run ? (
        entry.kind === "credential_vault" ? (
          <VaultHandoffCard runId={run.id} entry={entry} targetUrl={run.target_url} onDone={() => void chat.refresh()} onCancel={() => void control("cancel")} />
        ) : (
          <DirectEntryCard entry={entry} targetUrl={run.target_url} busy={controlBusy} onCancel={() => void control("cancel")} />
        )
      ) : chat.question ? (
        <QuestionCard key={chat.question.id} question={chat.question} sending={chat.sending} onSelect={onSelect} onFinalAction={onFinalAction} onChanged={() => void chat.refresh()} />
      ) : null}
    </View>
  );

  return (
    <KeyboardAvoidingView style={{ flex: 1, backgroundColor: c.bg }} behavior="padding" keyboardVerticalOffset={headerHeight}>
      <Stack.Screen
        options={{
          title,
          headerRight: cost
            ? () => (
                <T variant="small" accessibilityLabel={`Model spend ${cost}`} style={{ fontVariant: ["tabular-nums"] }}>
                  {cost}
                </T>
              )
            : undefined
        }}
      />
      {!chat.loaded ? (
        <View style={{ flex: 1, alignItems: "center", justifyContent: "center" }}>
          <Spinner />
        </View>
      ) : (
        <FlatList<ChatMessage>
          inverted
          data={reversed}
          keyExtractor={(message) => String(message.id)}
          renderItem={({ item }) => (
            <View style={{ paddingVertical: 8 }}>
              <MessageItem message={item} onRetry={chat.retry} />
            </View>
          )}
          ListHeaderComponent={ListTail}
          ListEmptyComponent={
            <View style={{ paddingVertical: 40, gap: 6 }}>
              <T variant="title">What should Lumen do?</T>
              <T variant="small">Lumen works on your PC. You can follow along and answer it from here.</T>
            </View>
          }
          contentContainerStyle={{ paddingHorizontal: 16, paddingBottom: 16 }}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="interactive"
        />
      )}
      <View style={{ borderTopWidth: 1, borderTopColor: c.line, backgroundColor: c.bg, paddingBottom: insets.bottom }}>
        <RunDock run={run} questionPending={Boolean(chat.question)} busy={controlBusy} onControl={(action) => void control(action)} />
        <Composer
          ref={inputRef}
          value={draft}
          onChangeText={setDraft}
          onSend={() => void send()}
          sending={chat.sending}
          disabled={Boolean(entry)}
          placeholder={entry ? "Chat is paused during secure entry" : run && runIsLive(run) ? "Reply to Lumen…" : undefined}
          mode={approval.mode}
          onModePress={() => setModeOpen(true)}
        />
      </View>
      <ModeSheet visible={modeOpen} mode={approval.mode} onClose={() => setModeOpen(false)} onChoose={approval.update} />
    </KeyboardAvoidingView>
  );
}
