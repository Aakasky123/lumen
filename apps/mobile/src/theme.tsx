import { createContext, useContext, useMemo, type ReactNode } from "react";
import { Platform, useColorScheme } from "react-native";

/**
 * The cockpit's design tokens (apps/renderer/app/globals.css), converted to hex. Graphite surfaces
 * with a faint green undertone, one brand colour (the mint of the LUMEN slab mark), and colour kept
 * for meaning: brand = Lumen / working / done, attention = needs you, danger = stop / destructive.
 * `*Ink` variants are the same hue tuned for text and icons on the current surface.
 */
const dark = {
  bg: "#0b0d0c",
  sidebar: "#0f1211",
  surface1: "#141615",
  surface2: "#191d1c",
  surface3: "#202423",
  popover: "#161918",
  line: "#202423",
  lineStrong: "#2f3433",
  fg: "#eef2f0",
  fgMuted: "#9ea9a4",
  fgSubtle: "#7c8883",
  fgDisabled: "#515855",
  brand: "#5de5bc",
  brandStrong: "#75eac7",
  brandInk: "#70e6c2",
  brandFg: "#061914",
  attention: "#fab53d",
  attentionInk: "#fccb69",
  attentionFg: "#251404",
  danger: "#ee5353",
  dangerInk: "#f97b7b",
  dangerFg: "#ffffff",
  info: "#62adf8"
};

export type Palette = typeof dark;

const light: Palette = {
  bg: "#fbfcfb",
  sidebar: "#f4f6f5",
  surface1: "#ffffff",
  surface2: "#f5f7f6",
  surface3: "#ecefee",
  popover: "#ffffff",
  line: "#e0e6e3",
  lineStrong: "#c8d0cd",
  fg: "#121c19",
  fgMuted: "#535f5c",
  fgSubtle: "#636e6b",
  fgDisabled: "#a5acaa",
  brand: "#2bc597",
  brandStrong: "#20b689",
  brandInk: "#0d7759",
  brandFg: "#061914",
  attention: "#f79708",
  attentionInk: "#a24f07",
  attentionFg: "#251404",
  danger: "#df2a2a",
  dangerInk: "#b81e1e",
  dangerFg: "#ffffff",
  info: "#1674df"
};

/** A token at reduced opacity, the way the cockpit writes `bg-brand/[0.06]`. */
export function alpha(hex: string, opacity: number): string {
  const value = hex.replace("#", "");
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${opacity})`;
}

export const mono = Platform.select({ android: "monospace", default: "Courier" });

export const radius = { sm: 6, md: 8, lg: 12, xl: 16, xxl: 20 };

export const text = {
  xs: { fontSize: 12, lineHeight: 16 },
  sm: { fontSize: 13, lineHeight: 18 },
  md: { fontSize: 15, lineHeight: 22 },
  lg: { fontSize: 17, lineHeight: 24 },
  xl: { fontSize: 22, lineHeight: 28 },
  xxl: { fontSize: 28, lineHeight: 34 }
};

type Theme = { c: Palette; dark: boolean };

const ThemeContext = createContext<Theme>({ c: dark, dark: true });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const scheme = useColorScheme();
  const value = useMemo<Theme>(() => (scheme === "light" ? { c: light, dark: false } : { c: dark, dark: true }), [scheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): Theme {
  return useContext(ThemeContext);
}
