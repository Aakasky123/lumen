"use client";

import { forwardRef, useCallback, useImperativeHandle, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { ArrowUp, LockKeyhole } from "lucide-react";
import { ActionApprovalSelector } from "@/components/lumen/chat/ActionApprovalSelector";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

export type ComposerHandle = {
  /** Replace the draft and focus it; `select` highlights that substring (e.g. a placeholder to overwrite). */
  fill: (text: string, select?: string) => void;
  focus: () => void;
};

const MAX_HEIGHT = { hero: 280, docked: 240 };

export const ChatComposer = forwardRef<
  ComposerHandle,
  {
    onSend: (text: string) => void;
    sending: boolean;
    disabled: boolean;
    /** Why the composer is locked, shown in place of the text box. */
    lockedMessage?: string;
    placeholder?: string;
    variant?: "hero" | "docked";
    /** Rendered inside the composer card, above the text box (the live run dock). */
    dock?: ReactNode;
    className?: string;
  }
>(function ChatComposer({ onSend, sending, disabled, lockedMessage, placeholder = "Message Lumen", variant = "docked", dock, className }, ref) {
  const [draft, setDraft] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const hero = variant === "hero";

  const resize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT[variant])}px`;
  }, [variant]);

  useLayoutEffect(resize, [draft, resize]);

  useImperativeHandle(
    ref,
    () => ({
      fill: (text, select) => {
        setDraft(text);
        requestAnimationFrame(() => {
          const el = textareaRef.current;
          if (!el) return;
          el.focus();
          const start = select ? text.indexOf(select) : -1;
          if (start >= 0) el.setSelectionRange(start, start + select!.length);
          else el.setSelectionRange(text.length, text.length);
          resize();
        });
      },
      focus: () => textareaRef.current?.focus()
    }),
    [resize]
  );

  const canSend = Boolean(draft.trim()) && !sending && !disabled;

  const submit = () => {
    const text = draft.trim();
    if (!text || sending || disabled) return;
    onSend(text);
    setDraft("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <div className={cn("w-full", className)}>
      <div
        className={cn(
          // No overflow-hidden here: the approval menu opens upward out of this card. The dock rounds
          // its own top corners instead (outer 18px radius minus the 1px border).
          "rounded-2xl border bg-surface-1 shadow-md transition-[border-color,box-shadow] duration-200",
          disabled ? "border-line" : "border-line-strong focus-within:border-brand/45 focus-within:shadow-[var(--shadow-md),0_0_0_4px_hsl(var(--brand)/0.08)]"
        )}
      >
        {dock ? <div className="rounded-t-[17px] border-b border-line bg-surface-2/50">{dock}</div> : null}
        {lockedMessage ? (
          <div className={cn("flex items-center gap-3 px-4 text-sm text-fg-muted", hero ? "min-h-[6.5rem]" : "min-h-[3.25rem]")}>
            <LockKeyhole className="size-4 shrink-0 text-brand-ink" />
            <span>{lockedMessage}</span>
          </div>
        ) : (
          <textarea
            ref={textareaRef}
            rows={1}
            value={draft}
            disabled={disabled}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={placeholder}
            aria-label="Message Lumen"
            className={cn(
              "scroll-area block w-full resize-none bg-transparent px-4 text-fg outline-none placeholder:text-fg-subtle disabled:cursor-not-allowed",
              hero ? "min-h-[6.5rem] pb-2 pt-4 text-base" : "min-h-[3.25rem] pb-1.5 pt-3.5 text-15"
            )}
          />
        )}
        <div className="flex items-center justify-between gap-3 px-2.5 pb-2.5 pt-1">
          <ActionApprovalSelector disabled={disabled} />
          <div className="flex items-center gap-2">
            {draft.length > 400 ? <span className="text-2xs text-fg-subtle tabular">{draft.length.toLocaleString()}</span> : null}
            <button
              type="button"
              onClick={submit}
              disabled={!canSend}
              aria-label="Send"
              className={cn(
                "flex size-8 items-center justify-center rounded-full transition-all duration-200 focus-ring",
                canSend ? "bg-brand text-brand-fg shadow-sm hover:bg-brand-strong active:scale-95" : "bg-fg/[0.07] text-fg-disabled"
              )}
            >
              {sending ? <Spinner className="size-4" /> : <ArrowUp className="size-4" strokeWidth={2.25} />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
});
