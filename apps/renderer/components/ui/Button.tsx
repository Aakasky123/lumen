import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  [
    "relative inline-flex select-none items-center justify-center gap-1.5 whitespace-nowrap font-medium",
    "transition-[background-color,border-color,color,box-shadow,transform,opacity] duration-150 ease-standard",
    "focus-ring active:translate-y-px disabled:pointer-events-none disabled:opacity-45",
    "[&_svg]:pointer-events-none [&_svg]:shrink-0"
  ],
  {
    variants: {
      variant: {
        primary: "bg-brand text-brand-fg shadow-sm hover:bg-brand-strong",
        secondary: "border border-line-strong bg-surface-2 text-fg shadow-xs hover:border-fg-subtle/40 hover:bg-surface-3",
        outline: "border border-line-strong bg-transparent text-fg hover:bg-fg/[0.05]",
        ghost: "text-fg-muted hover:bg-fg/[0.06] hover:text-fg",
        subtle: "bg-fg/[0.06] text-fg hover:bg-fg/[0.1]",
        danger: "bg-danger text-danger-fg shadow-sm hover:bg-danger/90",
        "danger-ghost": "text-fg-muted hover:bg-danger/10 hover:text-danger-ink",
        attention: "bg-attention text-attention-fg shadow-sm hover:bg-attention/90",
        link: "h-auto px-0 text-brand-ink underline-offset-4 hover:underline"
      },
      size: {
        xs: "h-7 rounded-sm px-2.5 text-xs [&_svg]:size-3.5",
        sm: "h-8 rounded-md px-3 text-13 [&_svg]:size-4",
        md: "h-9 rounded-md px-3.5 text-sm [&_svg]:size-4",
        lg: "h-10 rounded-lg px-4 text-sm [&_svg]:size-4",
        "icon-xs": "size-7 rounded-sm [&_svg]:size-3.5",
        "icon-sm": "size-8 rounded-md [&_svg]:size-4",
        icon: "size-9 rounded-md [&_svg]:size-[18px]"
      }
    },
    compoundVariants: [{ variant: "link", className: "h-auto px-0" }],
    defaultVariants: {
      variant: "secondary",
      size: "md"
    }
  }
);

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants> & {
    /** Shows a spinner in place of the leading icon and blocks clicks, keeping the label. */
    loading?: boolean;
  };

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, loading = false, disabled, children, type = "button", ...props }, ref) => (
    <button
      ref={ref}
      type={type}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? <Spinner className="size-3.5" /> : null}
      {children}
    </button>
  )
);
Button.displayName = "Button";

export { Button, buttonVariants };
