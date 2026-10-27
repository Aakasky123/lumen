import * as Clipboard from "expo-clipboard";
import { AlertCircle, Check, CheckCircle2, CircleSlash, Eye, OctagonPause, RotateCcw, XCircle } from "lucide-react-native";
import { memo } from "react";
import { Pressable, View } from "react-native";
import type { ChatMessage } from "@/api";
import { Markdown } from "@/components/Markdown";
import { LumenMark } from "@/components/LumenMark";
import { Button, Card, T, type Icon } from "@/components/ui";
import { alpha, radius, useTheme, type Palette } from "@/theme";

function clientStatus(message: ChatMessage): string | null {
  const status = message.payload_json?.client_status;
  return typeof status === "string" ? status : null;
}

export function Avatar({ working = false }: { working?: boolean }) {
  const { c } = useTheme();
  return (
    <View
      style={{
        width: 28,
        height: 28,
        borderRadius: radius.md,
        borderWidth: 1,
        borderColor: working ? alpha(c.brand, 0.45) : c.line,
        backgroundColor: c.surface1,
        alignItems: "center",
        justifyContent: "center",
        marginTop: 1
      }}
    >
      <LumenMark size={15} working={working} />
    </View>
  );
}

type OutcomeState = "COMPLETED" | "READY_FOR_REVIEW" | "BLOCKED" | "FAILED" | "CANCELLED" | "WAITING_FOR_USER";

function outcomeMeta(
  c: Palette,
  state: OutcomeState,
  requiredOpen: number,
  completion: string
): { label: string; icon: Icon; ink: string; ring: string } {
  // Required fields still open means the hand-off needs you, whatever the run state says.
  if (state === "READY_FOR_REVIEW" && requiredOpen) return { label: "Needs you", icon: OctagonPause, ink: c.attentionInk, ring: alpha(c.attention, 0.3) };
  // A generic task's finish the screen confirmed reads as finished; an unbacked one asks you to look.
  if (state === "READY_FOR_REVIEW" && completion === "complete") return { label: "Finished", icon: CheckCircle2, ink: c.brandInk, ring: alpha(c.brand, 0.3) };
  if (state === "READY_FOR_REVIEW" && (completion === "incomplete" || completion === "unverified"))
    return { label: "Not confirmed", icon: AlertCircle, ink: c.attentionInk, ring: alpha(c.attention, 0.3) };
  const table: Record<OutcomeState, { label: string; icon: Icon; ink: string; ring: string }> = {
    COMPLETED: { label: "Done", icon: CheckCircle2, ink: c.brandInk, ring: alpha(c.brand, 0.3) },
    READY_FOR_REVIEW: { label: "Ready for your review", icon: Eye, ink: c.fg, ring: c.lineStrong },
    BLOCKED: { label: "Stopped", icon: OctagonPause, ink: c.attentionInk, ring: alpha(c.attention, 0.3) },
    FAILED: { label: "Couldn't finish", icon: XCircle, ink: c.dangerInk, ring: alpha(c.danger, 0.3) },
    CANCELLED: { label: "Ended", icon: CircleSlash, ink: c.fgMuted, ring: c.lineStrong },
    WAITING_FOR_USER: { label: "Waiting for you", icon: OctagonPause, ink: c.attentionInk, ring: alpha(c.attention, 0.3) }
  };
  return table[state];
}

/** A run outcome the backend posted, tallied the way its own outcome message tallies receipts. */
function outcomeOf(message: ChatMessage) {
  if (message.payload_json?.source !== "in_browser_outcome") return null;
  const summary = message.payload_json?.summary as
    | { state?: unknown; receipts?: unknown; field_census?: unknown; completion_check?: { verdict?: unknown } }
    | undefined;
  const state = String(summary?.state || "").toUpperCase() as OutcomeState;
  if (!["COMPLETED", "READY_FOR_REVIEW", "BLOCKED", "FAILED", "CANCELLED", "WAITING_FOR_USER"].includes(state)) return null;
  const receipts = Array.isArray(summary?.receipts) ? (summary?.receipts as Array<{ outcome?: unknown }>) : [];
  const census = Array.isArray(summary?.field_census) ? (summary?.field_census as Array<{ status?: unknown }>) : [];
  return {
    state,
    verified: receipts.filter((receipt) => receipt?.outcome === "verified_succeeded").length,
    unconfirmed: receipts.filter((receipt) => receipt?.outcome === "verification_inconclusive").length,
    failed: receipts.filter((receipt) => receipt?.outcome === "execution_failed" || receipt?.outcome === "verification_failed").length,
    requiredOpen: census.filter((entry) => entry?.status === "required_unfilled").length,
    completion: String(summary?.completion_check?.verdict || "")
  };
}

function copy(textValue: string) {
  void Clipboard.setStringAsync(textValue);
}

export const MessageItem = memo(function MessageItem({ message, onRetry }: { message: ChatMessage; onRetry: (message: ChatMessage) => void }) {
  const { c } = useTheme();
  const status = clientStatus(message);

  if (message.role === "user") {
    return (
      <View style={{ alignItems: "flex-end", paddingLeft: 48 }}>
        <Pressable onLongPress={() => copy(message.content)} accessibilityHint="Long-press to copy">
          <View
            style={{
              backgroundColor: c.surface2,
              borderColor: c.line,
              borderWidth: 1,
              borderRadius: 20,
              borderBottomRightRadius: 6,
              paddingHorizontal: 14,
              paddingVertical: 10,
              opacity: status === "sending" ? 0.7 : 1
            }}
          >
            <T selectable>{message.content}</T>
          </View>
        </Pressable>
      </View>
    );
  }

  const outcome = outcomeOf(message);
  if (outcome && !status) {
    const meta = outcomeMeta(c, outcome.state, outcome.requiredOpen, outcome.completion);
    const OutcomeIcon = meta.icon;
    const counts = [
      outcome.verified ? { text: `${outcome.verified} verified`, color: c.brandInk } : null,
      outcome.unconfirmed ? { text: `${outcome.unconfirmed} unconfirmed`, color: c.attentionInk } : null,
      outcome.failed ? { text: `${outcome.failed} failed`, color: c.dangerInk } : null,
      outcome.requiredOpen ? { text: `${outcome.requiredOpen} required open`, color: c.attentionInk } : null
    ].filter((item): item is { text: string; color: string } => item !== null);
    return (
      <View style={{ flexDirection: "row", gap: 10 }}>
        <Avatar />
        <Card style={{ flex: 1, borderColor: meta.ring }}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 14, paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: c.line }}>
            <OutcomeIcon size={16} color={meta.ink} />
            <T variant="small" weight="600" color={meta.ink}>
              {meta.label}
            </T>
            <View style={{ marginLeft: "auto", flexDirection: "row", gap: 8, flexShrink: 1, flexWrap: "wrap", justifyContent: "flex-end" }}>
              {counts.map((count) => (
                <View key={count.text} style={{ flexDirection: "row", alignItems: "center", gap: 3 }}>
                  {count.color === c.brandInk ? <Check size={12} color={c.brandInk} /> : null}
                  <T variant="caption" color={count.color}>
                    {count.text}
                  </T>
                </View>
              ))}
            </View>
          </View>
          <Pressable onLongPress={() => copy(message.content)} style={{ padding: 14 }}>
            <Markdown content={message.content} />
          </Pressable>
        </Card>
      </View>
    );
  }

  if (status === "loading" && !message.content.trim()) {
    return (
      <View style={{ flexDirection: "row", gap: 10, alignItems: "center" }} accessibilityLabel="Lumen is thinking">
        <Avatar working />
        <T color={c.fgMuted} weight="500">
          Thinking…
        </T>
      </View>
    );
  }

  if (status === "error") {
    return (
      <View style={{ flexDirection: "row", gap: 10 }}>
        <Avatar />
        <View
          accessibilityRole="alert"
          style={{
            flex: 1,
            flexDirection: "row",
            gap: 10,
            padding: 12,
            borderRadius: radius.lg,
            borderWidth: 1,
            borderColor: alpha(c.danger, 0.3),
            backgroundColor: alpha(c.danger, 0.06)
          }}
        >
          <AlertCircle size={16} color={c.dangerInk} style={{ marginTop: 3 }} />
          <View style={{ flex: 1, gap: 6, alignItems: "flex-start" }}>
            <T variant="small" color={c.fg}>
              {message.content}
            </T>
            <Button label="Try again" icon={RotateCcw} variant="ghost" size="sm" onPress={() => onRetry(message)} />
          </View>
        </View>
      </View>
    );
  }

  return (
    <View style={{ flexDirection: "row", gap: 10 }}>
      <Avatar working={status === "loading"} />
      <Pressable style={{ flex: 1, paddingTop: 3 }} onLongPress={() => copy(message.content)}>
        <Markdown content={message.content} />
      </Pressable>
    </View>
  );
});
