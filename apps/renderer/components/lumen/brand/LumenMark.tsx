import { cn } from "@/lib/utils";

/**
 * The Lumen mark: four upright slabs of light, the inner pair set slightly lower (see
 * scripts/generate-icons.py). `working` sets the slabs shifting in sequence — the one motion that
 * means "Lumen is doing something" everywhere in the product.
 */

const SLABS = [
  { x: 3.6, y: 4.2, shade: false },
  { x: 8.3, y: 5.1, shade: true },
  { x: 13, y: 5.1, shade: true },
  { x: 17.7, y: 4.2, shade: false }
];

export function LumenMark({
  className,
  working = false,
  tile = false,
  title
}: {
  className?: string;
  working?: boolean;
  /** Draw the dark rounded tile behind the slabs, as on the app icon. */
  tile?: boolean;
  title?: string;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={cn("shrink-0", className)}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      {tile ? <rect x="0.5" y="0.5" width="23" height="23" rx="5.6" fill="#0d1f1a" stroke="#1c3a31" /> : null}
      {SLABS.map((slab, index) => (
        <rect
          key={slab.x}
          x={slab.x}
          y={slab.y}
          width="2.7"
          height="14.7"
          rx="0.75"
          className={working ? "lumen-slab" : undefined}
          style={{
            fill: tile ? (slab.shade ? "#3fbf99" : "#5ee6bd") : slab.shade ? "hsl(var(--brand) / 0.78)" : "hsl(var(--brand))",
            animationDelay: working ? `${index * 110}ms` : undefined
          }}
        />
      ))}
    </svg>
  );
}

export function LumenWordmark({ className }: { className?: string }) {
  return (
    <span className={cn("font-mono text-[13px] font-semibold uppercase tracking-[0.28em] text-fg", className)}>
      Lumen
    </span>
  );
}
