import type { Config } from "tailwindcss";
import tailwindcssAnimate from "tailwindcss-animate";

const token = (name: string) => `hsl(var(--${name}) / <alpha-value>)`;

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: token("bg"),
        sidebar: token("bg-sidebar"),
        "surface-1": token("surface-1"),
        "surface-2": token("surface-2"),
        "surface-3": token("surface-3"),
        popover: token("popover"),
        line: token("line"),
        "line-strong": token("line-strong"),
        fg: {
          DEFAULT: token("fg"),
          muted: token("fg-muted"),
          subtle: token("fg-subtle"),
          disabled: token("fg-disabled")
        },
        brand: {
          DEFAULT: token("brand"),
          strong: token("brand-strong"),
          ink: token("brand-ink"),
          fg: token("brand-fg")
        },
        attention: {
          DEFAULT: token("attention"),
          ink: token("attention-ink"),
          fg: token("attention-fg")
        },
        danger: {
          DEFAULT: token("danger"),
          ink: token("danger-ink"),
          fg: token("danger-fg")
        },
        info: token("info"),
        ring: token("ring")
      },
      opacity: {
        3: "0.03",
        4: "0.04",
        6: "0.06",
        7: "0.07",
        8: "0.08",
        12: "0.12",
        14: "0.14",
        16: "0.16",
        18: "0.18",
        22: "0.22",
        35: "0.35",
        45: "0.45",
        55: "0.55",
        65: "0.65",
        85: "0.85"
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "Consolas", "monospace"]
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
        xs: ["0.75rem", { lineHeight: "1.125rem" }],
        "13": ["0.8125rem", { lineHeight: "1.25rem" }],
        sm: ["0.875rem", { lineHeight: "1.375rem" }],
        "15": ["0.9375rem", { lineHeight: "1.65rem" }],
        base: ["1rem", { lineHeight: "1.6rem" }],
        lg: ["1.125rem", { lineHeight: "1.65rem" }],
        xl: ["1.375rem", { lineHeight: "1.85rem", letterSpacing: "-0.012em" }],
        "2xl": ["1.75rem", { lineHeight: "2.2rem", letterSpacing: "-0.02em" }],
        "3xl": ["2.125rem", { lineHeight: "2.5rem", letterSpacing: "-0.025em" }]
      },
      borderRadius: {
        xs: "6px",
        sm: "8px",
        md: "10px",
        lg: "12px",
        xl: "14px",
        "2xl": "18px",
        "3xl": "24px"
      },
      boxShadow: {
        xs: "var(--shadow-xs)",
        sm: "var(--shadow-sm)",
        md: "var(--shadow-md)",
        lg: "var(--shadow-lg)",
        brand: "var(--shadow-brand)",
        none: "none"
      },
      transitionTimingFunction: {
        out: "cubic-bezier(0.16, 1, 0.3, 1)",
        standard: "cubic-bezier(0.2, 0, 0, 1)"
      },
      keyframes: {
        "enter-up": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" }
        },
        "enter-fade": {
          from: { opacity: "0" },
          to: { opacity: "1" }
        },
        "enter-scale": {
          from: { opacity: "0", transform: "scale(0.97)" },
          to: { opacity: "1", transform: "scale(1)" }
        },
        "dock-in": {
          from: { opacity: "0", transform: "translateY(10px) scale(0.985)" },
          to: { opacity: "1", transform: "translateY(0) scale(1)" }
        }
      },
      animation: {
        "enter-up": "enter-up 320ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "enter-fade": "enter-fade 240ms ease-out both",
        "enter-scale": "enter-scale 220ms cubic-bezier(0.16, 1, 0.3, 1) both",
        "dock-in": "dock-in 380ms cubic-bezier(0.16, 1, 0.3, 1) both"
      }
    }
  },
  plugins: [tailwindcssAnimate]
};

export default config;
