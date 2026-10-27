"use client";

import * as React from "react";
import * as SheetPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

const Sheet = SheetPrimitive.Root;

const SheetOverlay = React.forwardRef<
  React.ElementRef<typeof SheetPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof SheetPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <SheetPrimitive.Overlay
    ref={ref}
    className={cn(
      "fixed inset-0 z-50 bg-black/45 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
      className
    )}
    {...props}
  />
));
SheetOverlay.displayName = SheetPrimitive.Overlay.displayName;

/**
 * A right-hand panel that slides over the conversation. It floats inset from the window edge so
 * the chat stays legible behind it.
 */
const SheetContent = React.forwardRef<
  React.ElementRef<typeof SheetPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SheetPrimitive.Content>
>(({ className, children, ...props }, ref) => (
  <SheetPrimitive.Portal>
    <SheetOverlay />
    <SheetPrimitive.Content
      ref={ref}
      className={cn(
        "fixed inset-y-2 right-2 z-50 flex w-[calc(100vw-1rem)] max-w-[30rem] flex-col overflow-hidden rounded-2xl border border-line-strong bg-popover shadow-lg",
        "data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:duration-200 data-[state=open]:duration-300",
        "data-[state=closed]:slide-out-to-right-8 data-[state=open]:slide-in-from-right-8 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
        className
      )}
      {...props}
    >
      {children}
      <SheetPrimitive.Close className="absolute right-3.5 top-3.5 flex size-8 items-center justify-center rounded-md text-fg-subtle transition hover:bg-fg/[0.06] hover:text-fg focus-ring">
        <X className="size-4" />
        <span className="sr-only">Close</span>
      </SheetPrimitive.Close>
    </SheetPrimitive.Content>
  </SheetPrimitive.Portal>
));
SheetContent.displayName = SheetPrimitive.Content.displayName;

/** Panel header: icon, title, one-line description. */
function SheetHeader({ icon, title, description, actions }: { icon?: React.ReactNode; title: string; description?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="flex shrink-0 items-start gap-3 border-b border-line px-5 pb-4 pt-4 pr-14">
      {icon ? (
        <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-lg border border-line bg-surface-2 text-fg-muted [&_svg]:size-[18px]">{icon}</span>
      ) : null}
      <div className="min-w-0 flex-1">
        <SheetPrimitive.Title className="text-base font-semibold tracking-tight text-fg">{title}</SheetPrimitive.Title>
        {description ? <SheetPrimitive.Description className="mt-0.5 text-13 text-fg-muted">{description}</SheetPrimitive.Description> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
    </div>
  );
}

function SheetBody({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("scroll-area min-h-0 flex-1 overflow-y-auto px-5 py-5", className)}>{children}</div>;
}

function SheetFooter({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("flex shrink-0 items-center justify-between gap-3 border-t border-line bg-surface-1/60 px-5 py-3", className)}>{children}</div>;
}

export { Sheet, SheetContent, SheetHeader, SheetBody, SheetFooter };
