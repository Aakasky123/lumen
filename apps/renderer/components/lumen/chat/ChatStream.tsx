"use client";

import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ArrowDown } from "lucide-react";
import type { ChatMessage as ChatMessageType } from "@/lib/api";
import { ChatMessage } from "@/components/lumen/chat/ChatMessage";
import { cn, parseBackendDate } from "@/lib/utils";

/** Within this of the end counts as "at the bottom" (absorbs fractional offsets on scaled displays). */
const AT_BOTTOM_PX = 8;
/** The jump-to-latest button appears once the newest content is at least this far out of view. */
const JUMP_BUTTON_PX = 120;
const SCROLL_UP_KEYS = new Set(["ArrowUp", "PageUp", "Home"]);

function isEditable(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
}

function dayKey(value: string): string {
  const date = parseBackendDate(value);
  return Number.isNaN(date.getTime()) ? "" : date.toDateString();
}

function dayLabel(value: string): string {
  const date = parseBackendDate(value);
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    ...(date.getFullYear() === today.getFullYear() ? {} : { year: "numeric" })
  });
}

export function ChatStream({
  messages,
  onRetry,
  resetKey,
  children,
  footer
}: {
  messages: ChatMessageType[];
  onRetry: (message: ChatMessageType) => void;
  /** Identifies the conversation on screen; a new value starts the view at the bottom again. */
  resetKey?: string;
  children?: ReactNode;
  /** Pinned under the scroller (the composer), so the jump-to-latest button can sit above it. */
  footer?: ReactNode;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  // Follow new content only while the reader is parked at the bottom. Any upward scroll (wheel,
  // touch, keys, scrollbar) hands the view to the reader until they scroll back to the end, jump
  // to latest, or send a message. Following reacts to the content actually changing size, never
  // to re-renders, so the 1-3 s run polls can't pull the view back down.
  const followRef = useRef(true);
  const lastTopRef = useRef(0);
  const [away, setAway] = useState(false);

  const syncAway = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setAway(!followRef.current && el.scrollHeight - el.scrollTop - el.clientHeight > JUMP_BUTTON_PX);
  }, []);

  const pinToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    lastTopRef.current = el.scrollTop;
  }, []);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const top = el.scrollTop;
    const movedUp = top < lastTopRef.current - 1;
    lastTopRef.current = top;
    const distance = el.scrollHeight - top - el.clientHeight;
    if (distance > AT_BOTTOM_PX) {
      if (movedUp) followRef.current = false;
    } else if (!movedUp) {
      // Scrolled back down to the end: follow again. (Moving up while still within the end zone
      // is a reader starting to scroll away, which the wheel/touch/key listeners already caught.)
      followRef.current = true;
    }
    syncAway();
  };

  // Intent listeners fire before the scroll they cause, so a reader scrolling up from the bottom is
  // released at once instead of losing a race with the next content-growth pin.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const release = () => {
      if (el.scrollHeight > el.clientHeight) followRef.current = false;
    };
    let touchY: number | null = null;
    const onWheel = (event: WheelEvent) => {
      // Ctrl+wheel (and touchpad pinch) zooms rather than scrolls.
      if (event.deltaY < 0 && !event.ctrlKey) release();
    };
    const onTouchStart = (event: TouchEvent) => {
      touchY = event.touches[0]?.clientY ?? null;
    };
    const onTouchMove = (event: TouchEvent) => {
      const y = event.touches[0]?.clientY;
      if (y == null) return;
      // A finger dragging down scrolls the stream up.
      if (touchY != null && y > touchY + 2) release();
      touchY = y;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (isEditable(event.target)) return;
      if (SCROLL_UP_KEYS.has(event.key) || (event.key === " " && event.shiftKey)) release();
    };
    el.addEventListener("wheel", onWheel, { passive: true });
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: true });
    el.addEventListener("keydown", onKeyDown);
    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  // Keep the newest content in view while following: when messages stream in, the run timeline
  // grows, an image loads, or the composer/window changes the viewport height.
  useEffect(() => {
    const el = scrollRef.current;
    const content = contentRef.current;
    if (!el || !content) return;
    let lastHeight = content.offsetHeight;
    const observer = new ResizeObserver(() => {
      const height = content.offsetHeight;
      const shrank = height < lastHeight;
      lastHeight = height;
      // Content that shrank under the reader and left them at the end resumes following.
      if (shrank && el.scrollHeight - el.scrollTop - el.clientHeight <= AT_BOTTOM_PX) followRef.current = true;
      if (followRef.current) pinToBottom();
      syncAway();
    });
    observer.observe(content);
    observer.observe(el);
    return () => observer.disconnect();
  }, [pinToBottom, syncAway]);

  // Sending a message (or answering a question) always brings the view down to it and the reply;
  // so does switching to another conversation. Polls that merely re-deliver messages don't.
  const userMessageCount = messages.reduce((count, message) => count + (message.role === "user" ? 1 : 0), 0);
  const userCountRef = useRef(userMessageCount);
  const resetKeyRef = useRef(resetKey);
  useLayoutEffect(() => {
    const sent = userMessageCount > userCountRef.current;
    const switched = resetKey !== resetKeyRef.current;
    userCountRef.current = userMessageCount;
    resetKeyRef.current = resetKey;
    if (!sent && !switched) return;
    followRef.current = true;
    pinToBottom();
    syncAway();
  }, [userMessageCount, resetKey, pinToBottom, syncAway]);

  const jumpToLatest = () => {
    const el = scrollRef.current;
    if (!el) return;
    followRef.current = true;
    setAway(false);
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollTo({ top: el.scrollHeight, behavior: reduceMotion ? "auto" : "smooth" });
  };

  let previousDay = "";
  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div ref={scrollRef} onScroll={onScroll} className="scroll-area min-h-0 flex-1 overflow-y-auto">
        <div ref={contentRef} className="mx-auto flex w-full max-w-[46rem] flex-col gap-5 px-6 pb-10 pt-8">
          {messages.map((message) => {
            const key = dayKey(message.created_at);
            const separator = key && key !== previousDay ? dayLabel(message.created_at) : "";
            previousDay = key || previousDay;
            return (
              <Fragment key={message.id}>
                {separator ? (
                  <div className="flex items-center gap-3 py-1" aria-hidden="true">
                    <span className="h-px flex-1 bg-line" />
                    <span className="text-2xs font-medium uppercase tracking-[0.08em] text-fg-subtle">{separator}</span>
                    <span className="h-px flex-1 bg-line" />
                  </div>
                ) : null}
                <ChatMessage message={message} onRetry={onRetry} />
              </Fragment>
            );
          })}
          {children}
        </div>
      </div>
      <div className="relative before:pointer-events-none before:absolute before:inset-x-0 before:-top-8 before:h-8 before:bg-gradient-to-t before:from-bg before:to-transparent">
        <button
          type="button"
          onClick={jumpToLatest}
          aria-label="Jump to latest"
          tabIndex={away ? 0 : -1}
          className={cn(
            "absolute -top-12 left-1/2 z-10 flex size-9 -translate-x-1/2 items-center justify-center rounded-full border border-line-strong bg-popover text-fg-muted shadow-md transition-all duration-200 hover:text-fg focus-ring",
            away ? "translate-y-0 opacity-100" : "pointer-events-none translate-y-2 opacity-0"
          )}
        >
          <ArrowDown className="size-4" />
        </button>
        {footer}
      </div>
    </div>
  );
}
