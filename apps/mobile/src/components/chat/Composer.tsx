import { ArrowUp, ShieldCheck, Zap } from "lucide-react-native";
import { forwardRef, useState } from "react";
import { ActivityIndicator, Pressable, TextInput, View } from "react-native";
import type { ApprovalMode } from "@/hooks";
import { T } from "@/components/ui";
import { alpha, radius, useTheme } from "@/theme";

export const APPROVAL_OPTIONS: Array<{ mode: ApprovalMode; title: string; description: string }> = [
  { mode: "approval", title: "Safe approvals", description: "Works on its own, then waits for you before consequential final actions." },
  {
    mode: "autonomous",
    title: "Full autonomy",
    description: "Completes requested final actions without asking, including submits and sends. Payments still wait for you."
  }
];

export const Composer = forwardRef<
  TextInput,
  {
    value: string;
    onChangeText: (value: string) => void;
    onSend: () => void;
    sending: boolean;
    disabled?: boolean;
    placeholder?: string;
    mode: ApprovalMode;
    onModePress: () => void;
  }
>(function Composer({ value, onChangeText, onSend, sending, disabled, placeholder, mode, onModePress }, ref) {
  const { c } = useTheme();
  const [height, setHeight] = useState(44);
  const canSend = Boolean(value.trim()) && !sending && !disabled;
  const autonomous = mode === "autonomous";
  const ModeIcon = autonomous ? Zap : ShieldCheck;
  return (
    <View style={{ paddingHorizontal: 12, paddingTop: 8, paddingBottom: 10, gap: 8 }}>
      <View
        style={{
          borderRadius: radius.xxl,
          borderWidth: 1,
          borderColor: c.lineStrong,
          backgroundColor: c.surface1,
          paddingLeft: 14,
          paddingRight: 6,
          paddingVertical: 6,
          flexDirection: "row",
          alignItems: "flex-end",
          gap: 8
        }}
      >
        <TextInput
          ref={ref}
          value={value}
          onChangeText={onChangeText}
          placeholder={placeholder ?? "Describe a task, or ask a question…"}
          placeholderTextColor={c.fgSubtle}
          selectionColor={c.brand}
          multiline
          editable={!disabled}
          onContentSizeChange={(event) => setHeight(Math.min(140, Math.max(44, event.nativeEvent.contentSize.height + 12)))}
          style={{ flex: 1, color: c.fg, fontSize: 16, height, paddingTop: 10, paddingBottom: 10, textAlignVertical: "top" }}
          accessibilityLabel="Message Lumen"
        />
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Send"
          disabled={!canSend}
          onPress={onSend}
          style={({ pressed }) => ({
            width: 40,
            height: 40,
            borderRadius: 20,
            marginBottom: 2,
            alignItems: "center",
            justifyContent: "center",
            backgroundColor: canSend ? (pressed ? c.brandStrong : c.brand) : alpha(c.fg, 0.08)
          })}
        >
          {sending ? <ActivityIndicator size="small" color={c.fgMuted} /> : <ArrowUp size={20} color={canSend ? c.brandFg : c.fgDisabled} strokeWidth={2.2} />}
        </Pressable>
      </View>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Final actions: ${autonomous ? "Full autonomy" : "Safe approvals"}. Change`}
        onPress={onModePress}
        hitSlop={6}
        style={{
          alignSelf: "flex-start",
          flexDirection: "row",
          alignItems: "center",
          gap: 6,
          paddingHorizontal: 10,
          paddingVertical: 5,
          borderRadius: 999,
          backgroundColor: autonomous ? alpha(c.attention, 0.1) : alpha(c.fg, 0.06)
        }}
      >
        <ModeIcon size={13} color={autonomous ? c.attentionInk : c.fgMuted} />
        <T variant="caption" weight="600" color={autonomous ? c.attentionInk : c.fgMuted}>
          {autonomous ? "Full autonomy" : "Safe approvals"}
        </T>
      </Pressable>
    </View>
  );
});
