import * as Haptics from "expo-haptics";
import { forwardRef, type ComponentType, type ReactNode } from "react";
import {
  ActivityIndicator,
  Pressable,
  Text as RNText,
  TextInput,
  View,
  type PressableProps,
  type StyleProp,
  type TextInputProps,
  type TextProps,
  type TextStyle,
  type ViewStyle
} from "react-native";
import type { LucideProps } from "lucide-react-native";
import { alpha, mono, radius, text, useTheme, type Palette } from "@/theme";

export type Icon = ComponentType<LucideProps>;
export type Tone = "brand" | "attention" | "danger" | "neutral";

export function toneColors(c: Palette, tone: Tone): { ink: string; solid: string; fg: string } {
  if (tone === "brand") return { ink: c.brandInk, solid: c.brand, fg: c.brandFg };
  if (tone === "attention") return { ink: c.attentionInk, solid: c.attention, fg: c.attentionFg };
  if (tone === "danger") return { ink: c.dangerInk, solid: c.danger, fg: c.dangerFg };
  return { ink: c.fgMuted, solid: c.fgSubtle, fg: c.bg };
}

// -------------------------------------------------------------------------------------------------
// Text

type Variant = "body" | "small" | "caption" | "title" | "heading" | "display" | "eyebrow" | "mono";

export function T({
  variant = "body",
  color,
  weight,
  style,
  ...props
}: TextProps & { variant?: Variant; color?: string; weight?: "400" | "500" | "600" | "700" }) {
  const { c } = useTheme();
  const base: TextStyle =
    variant === "small"
      ? { ...text.sm, color: c.fgMuted }
      : variant === "caption"
        ? { ...text.xs, color: c.fgSubtle }
        : variant === "title"
          ? { ...text.lg, color: c.fg, fontWeight: "600" }
          : variant === "heading"
            ? { ...text.xl, color: c.fg, fontWeight: "600", letterSpacing: -0.3 }
            : variant === "display"
              ? { ...text.xxl, color: c.fg, fontWeight: "600", letterSpacing: -0.6 }
              : variant === "eyebrow"
                ? { fontSize: 11, lineHeight: 14, color: c.fgSubtle, fontWeight: "600", letterSpacing: 1.1, textTransform: "uppercase" }
                : variant === "mono"
                  ? { ...text.sm, color: c.fg, fontFamily: mono }
                  : { ...text.md, color: c.fg };
  return <RNText {...props} style={[base, color ? { color } : null, weight ? { fontWeight: weight } : null, style]} />;
}

// -------------------------------------------------------------------------------------------------
// Buttons

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "danger-ghost" | "attention";

export function Button({
  label,
  onPress,
  variant = "secondary",
  size = "md",
  icon: IconComponent,
  loading = false,
  disabled = false,
  haptic = true,
  style,
  accessibilityLabel
}: {
  label: string;
  onPress?: () => void;
  variant?: ButtonVariant;
  size?: "sm" | "md" | "lg";
  icon?: Icon;
  loading?: boolean;
  disabled?: boolean;
  haptic?: boolean;
  style?: StyleProp<ViewStyle>;
  accessibilityLabel?: string;
}) {
  const { c } = useTheme();
  const palette: Record<ButtonVariant, { bg: string; fg: string; border?: string; pressed: string }> = {
    primary: { bg: c.brand, fg: c.brandFg, pressed: c.brandStrong },
    attention: { bg: c.attention, fg: c.attentionFg, pressed: alpha(c.attention, 0.88) },
    danger: { bg: c.danger, fg: c.dangerFg, pressed: alpha(c.danger, 0.88) },
    secondary: { bg: c.surface2, fg: c.fg, border: c.lineStrong, pressed: c.surface3 },
    ghost: { bg: "transparent", fg: c.fgMuted, pressed: alpha(c.fg, 0.06) },
    "danger-ghost": { bg: "transparent", fg: c.dangerInk, pressed: alpha(c.danger, 0.1) }
  };
  const tone = palette[variant];
  const height = size === "sm" ? 36 : size === "lg" ? 52 : 44;
  const inactive = disabled || loading;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? label}
      accessibilityState={{ disabled: inactive, busy: loading }}
      disabled={inactive}
      onPress={() => {
        if (haptic) void Haptics.selectionAsync();
        onPress?.();
      }}
      style={({ pressed }) => [
        {
          height,
          paddingHorizontal: size === "sm" ? 12 : 16,
          borderRadius: size === "lg" ? radius.lg : radius.md,
          backgroundColor: pressed ? tone.pressed : tone.bg,
          borderWidth: tone.border ? 1 : 0,
          borderColor: tone.border,
          flexDirection: "row",
          alignItems: "center",
          justifyContent: "center",
          gap: 8,
          opacity: disabled ? 0.5 : 1
        },
        style
      ]}
    >
      {loading ? <ActivityIndicator size="small" color={tone.fg} /> : IconComponent ? <IconComponent size={size === "sm" ? 16 : 18} color={tone.fg} strokeWidth={1.9} /> : null}
      <RNText style={{ color: tone.fg, fontSize: size === "sm" ? 13 : 15, fontWeight: "600" }} numberOfLines={1}>
        {label}
      </RNText>
    </Pressable>
  );
}

export function IconButton({
  icon: IconComponent,
  onPress,
  accessibilityLabel,
  color,
  size = 40,
  disabled
}: {
  icon: Icon;
  onPress: () => void;
  accessibilityLabel: string;
  color?: string;
  size?: number;
  disabled?: boolean;
}) {
  const { c } = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      onPress={onPress}
      disabled={disabled}
      hitSlop={6}
      style={({ pressed }) => ({
        width: size,
        height: size,
        borderRadius: radius.md,
        alignItems: "center",
        justifyContent: "center",
        backgroundColor: pressed ? alpha(c.fg, 0.08) : "transparent",
        opacity: disabled ? 0.4 : 1
      })}
    >
      <IconComponent size={20} color={color ?? c.fgMuted} strokeWidth={1.8} />
    </Pressable>
  );
}

/** A full-width row you tap: settings lists, option lists. */
export function Row({
  title,
  detail,
  icon: IconComponent,
  right,
  onPress,
  destructive,
  first,
  style
}: {
  title: string;
  detail?: string;
  icon?: Icon;
  right?: ReactNode;
  onPress?: () => void;
  destructive?: boolean;
  first?: boolean;
  style?: StyleProp<ViewStyle>;
}) {
  const { c } = useTheme();
  const content = (pressed: boolean) => (
    <View
      style={[
        {
          flexDirection: "row",
          alignItems: "center",
          gap: 12,
          minHeight: 56,
          paddingHorizontal: 16,
          paddingVertical: 12,
          backgroundColor: pressed ? alpha(c.fg, 0.05) : "transparent",
          borderTopWidth: first ? 0 : 1,
          borderTopColor: c.line
        },
        style
      ]}
    >
      {IconComponent ? <IconComponent size={18} color={destructive ? c.dangerInk : c.fgSubtle} strokeWidth={1.8} /> : null}
      <View style={{ flex: 1, minWidth: 0 }}>
        <T weight="500" color={destructive ? c.dangerInk : c.fg} numberOfLines={2}>
          {title}
        </T>
        {detail ? (
          <T variant="small" numberOfLines={2}>
            {detail}
          </T>
        ) : null}
      </View>
      {right}
    </View>
  );
  if (!onPress) return content(false);
  return (
    <Pressable accessibilityRole="button" onPress={onPress}>
      {({ pressed }) => content(pressed)}
    </Pressable>
  );
}

// -------------------------------------------------------------------------------------------------
// Surfaces

export function Card({ children, tone, style }: { children: ReactNode; tone?: "brand" | "attention" | "danger"; style?: StyleProp<ViewStyle> }) {
  const { c } = useTheme();
  const border = tone === "brand" ? alpha(c.brand, 0.3) : tone === "attention" ? alpha(c.attention, 0.3) : tone === "danger" ? alpha(c.danger, 0.3) : c.line;
  return (
    <View style={[{ backgroundColor: c.surface1, borderRadius: radius.xl, borderWidth: 1, borderColor: border, overflow: "hidden" }, style]}>{children}</View>
  );
}

/** A card with a tinted header strip, the cockpit's question-card shape. */
export function CardShell({
  eyebrow,
  icon: IconComponent,
  tone = "attention",
  badge,
  children
}: {
  eyebrow: string;
  icon: Icon;
  tone?: "attention" | "brand";
  badge?: ReactNode;
  children: ReactNode;
}) {
  const { c } = useTheme();
  const ink = tone === "attention" ? c.attentionInk : c.brandInk;
  const solid = tone === "attention" ? c.attention : c.brand;
  return (
    <Card tone={tone}>
      <View
        style={{
          flexDirection: "row",
          alignItems: "center",
          gap: 8,
          paddingHorizontal: 16,
          paddingVertical: 9,
          backgroundColor: alpha(solid, 0.07),
          borderBottomWidth: 1,
          borderBottomColor: alpha(solid, 0.15)
        }}
      >
        <IconComponent size={16} color={ink} strokeWidth={1.9} />
        <T variant="small" weight="600" color={ink}>
          {eyebrow}
        </T>
        {badge ? <View style={{ marginLeft: "auto" }}>{badge}</View> : null}
      </View>
      <View style={{ padding: 16, paddingTop: 14, gap: 12 }}>{children}</View>
    </Card>
  );
}

export function Group({ children, style }: { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  const { c } = useTheme();
  return <View style={[{ backgroundColor: c.surface1, borderRadius: radius.xl, borderWidth: 1, borderColor: c.line, overflow: "hidden" }, style]}>{children}</View>;
}

export function Badge({ label, tone = "neutral" }: { label: string; tone?: Tone }) {
  const { c } = useTheme();
  const colors = toneColors(c, tone);
  return (
    <View
      style={{
        paddingHorizontal: 8,
        paddingVertical: 2,
        borderRadius: 999,
        backgroundColor: tone === "neutral" ? alpha(c.fg, 0.07) : alpha(colors.solid, 0.13)
      }}
    >
      <RNText style={{ fontSize: 11, fontWeight: "600", color: tone === "neutral" ? c.fgMuted : colors.ink }}>{label}</RNText>
    </View>
  );
}

export function StatusDot({ tone = "brand", size = 8 }: { tone?: Tone; size?: number }) {
  const { c } = useTheme();
  return <View style={{ width: size, height: size, borderRadius: size / 2, backgroundColor: toneColors(c, tone).solid }} />;
}

export function Banner({ tone, icon: IconComponent, children }: { tone: "attention" | "danger" | "brand"; icon?: Icon; children: ReactNode }) {
  const { c } = useTheme();
  const colors = toneColors(c, tone);
  return (
    <View
      style={{
        flexDirection: "row",
        gap: 10,
        padding: 12,
        borderRadius: radius.lg,
        borderWidth: 1,
        borderColor: alpha(colors.solid, 0.28),
        backgroundColor: alpha(colors.solid, 0.07)
      }}
    >
      {IconComponent ? <IconComponent size={16} color={colors.ink} style={{ marginTop: 2 }} /> : null}
      <View style={{ flex: 1 }}>{typeof children === "string" ? <T variant="small" color={c.fg}>{children}</T> : children}</View>
    </View>
  );
}

// -------------------------------------------------------------------------------------------------
// Inputs

export const Field = forwardRef<TextInput, TextInputProps & { label?: string; hint?: string; multiline?: boolean }>(function Field(
  { label, hint, style, multiline, ...props },
  ref
) {
  const { c } = useTheme();
  return (
    <View style={{ gap: 6 }}>
      {label ? (
        <T variant="small" weight="500" color={c.fg}>
          {label}
        </T>
      ) : null}
      <TextInput
        ref={ref}
        placeholderTextColor={c.fgSubtle}
        selectionColor={c.brand}
        multiline={multiline}
        {...props}
        style={[
          {
            minHeight: multiline ? 96 : 46,
            paddingHorizontal: 14,
            paddingVertical: multiline ? 12 : 10,
            borderRadius: radius.lg,
            borderWidth: 1,
            borderColor: c.lineStrong,
            backgroundColor: c.surface2,
            color: c.fg,
            fontSize: 15,
            textAlignVertical: multiline ? "top" : "center"
          },
          style
        ]}
      />
      {hint ? <T variant="caption">{hint}</T> : null}
    </View>
  );
});

export function Spinner({ color }: { color?: string }) {
  const { c } = useTheme();
  return <ActivityIndicator color={color ?? c.fgMuted} />;
}

export function PressableCard({ children, onPress, style, ...props }: PressableProps & { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  const { c } = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      {...props}
      style={({ pressed }) => [
        { backgroundColor: pressed ? c.surface2 : c.surface1, borderRadius: radius.xl, borderWidth: 1, borderColor: c.line },
        style
      ]}
    >
      {children}
    </Pressable>
  );
}
