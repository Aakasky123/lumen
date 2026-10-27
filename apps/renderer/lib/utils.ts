import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// tailwind-merge only knows Tailwind's stock scale; unknown `text-*` values read as colors and would
// silently drop our custom sizes whenever a text color is also present. Register them.
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      text: ["2xs", "13", "15"]
    }
  }
});

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Backend timestamps are naive UTC ("2026-07-11 05:50:14.123456" / ISO without offset). Parsing them
// bare makes Date treat them as LOCAL wall-clock, shifting displayed times by the UTC offset. Strings
// without an explicit zone get one; epoch numbers and Z/offset strings pass through untouched.
export function parseBackendDate(value: string | number | Date): Date {
  if (typeof value === "string") {
    const s = value.trim();
    if (s && !/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(s)) return new Date(s.replace(" ", "T") + "Z");
  }
  return new Date(value);
}
