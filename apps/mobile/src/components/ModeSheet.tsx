import { AlertTriangle, Check, ShieldCheck, Zap } from "lucide-react-native";
import { useState } from "react";
import { Modal, Pressable, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { errorMessage, type ApprovalMode } from "@/hooks";
import { APPROVAL_OPTIONS } from "@/components/chat/Composer";
import { Banner, T } from "@/components/ui";
import { alpha, radius, useTheme } from "@/theme";

/** Choose Safe approvals or Full autonomy. Either change is confirmed with the fingerprint. */
export function ModeSheet({
  visible,
  mode,
  onClose,
  onChoose
}: {
  visible: boolean;
  mode: ApprovalMode;
  onClose: () => void;
  onChoose: (mode: ApprovalMode) => Promise<void>;
}) {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const choose = async (next: ApprovalMode) => {
    if (next === mode) return onClose();
    setBusy(true);
    setError("");
    try {
      await onChoose(next);
      onClose();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose} statusBarTranslucent navigationBarTranslucent>
      <Pressable style={{ flex: 1, backgroundColor: "rgba(0,0,0,0.55)" }} onPress={onClose} accessibilityLabel="Close" />
      <View
        style={{
          backgroundColor: c.popover,
          borderTopLeftRadius: radius.xxl,
          borderTopRightRadius: radius.xxl,
          borderWidth: 1,
          borderColor: c.lineStrong,
          padding: 16,
          paddingBottom: 16 + insets.bottom,
          gap: 10
        }}
      >
        <View style={{ alignSelf: "center", width: 36, height: 4, borderRadius: 2, backgroundColor: c.lineStrong, marginBottom: 6 }} />
        <T variant="title">Final actions</T>
        <T variant="small">Whether Lumen pauses before the consequential final step of each new task. You'll confirm a change with your fingerprint.</T>
        {APPROVAL_OPTIONS.map((option) => {
          const selected = option.mode === mode;
          const risky = option.mode === "autonomous";
          const OptionIcon = risky ? Zap : ShieldCheck;
          const solid = risky ? c.attention : c.brand;
          return (
            <Pressable
              key={option.mode}
              accessibilityRole="radio"
              accessibilityState={{ checked: selected, disabled: busy }}
              disabled={busy}
              onPress={() => void choose(option.mode)}
              style={{
                flexDirection: "row",
                gap: 12,
                padding: 14,
                borderRadius: radius.lg,
                borderWidth: 1,
                borderColor: selected ? alpha(solid, 0.45) : c.line,
                backgroundColor: selected ? alpha(solid, 0.06) : "transparent"
              }}
            >
              <View
                style={{
                  width: 34,
                  height: 34,
                  borderRadius: radius.md,
                  alignItems: "center",
                  justifyContent: "center",
                  backgroundColor: alpha(solid, 0.1),
                  borderWidth: 1,
                  borderColor: alpha(solid, 0.3)
                }}
              >
                <OptionIcon size={17} color={risky ? c.attentionInk : c.brandInk} />
              </View>
              <View style={{ flex: 1, gap: 2 }}>
                <T weight="600">{option.title}</T>
                <T variant="small">{option.description}</T>
              </View>
              {selected ? <Check size={18} color={risky ? c.attentionInk : c.brandInk} /> : null}
            </Pressable>
          );
        })}
        {mode === "autonomous" ? (
          <Banner tone="attention" icon={AlertTriangle}>
            On this phone, your fingerprint unlocks Full autonomy for several hours at a time. Saving sign-ins and changing settings still ask each time.
          </Banner>
        ) : null}
        {error ? (
          <T variant="small" color={c.dangerInk}>
            {error}
          </T>
        ) : null}
      </View>
    </Modal>
  );
}
