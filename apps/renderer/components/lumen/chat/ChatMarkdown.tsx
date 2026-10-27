"use client";

import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

const components: Components = {
  p: ({ children }) => <p className="my-2.5 first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="my-2.5 list-disc space-y-1.5 pl-5 marker:text-fg-subtle first:mt-0 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="my-2.5 list-decimal space-y-1.5 pl-5 marker:text-fg-subtle first:mt-0 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="pl-1">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-fg">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noreferrer" className="font-medium text-brand-ink underline decoration-brand-ink/35 underline-offset-[3px] transition hover:decoration-brand-ink">
      {children}
    </a>
  ),
  code: ({ className, children }) =>
    className ? (
      <code className={cn(className, "font-mono")}>{children}</code>
    ) : (
      <code className="rounded-[5px] border border-line bg-fg/[0.05] px-1.5 py-px font-mono text-[0.86em] text-fg">{children}</code>
    ),
  pre: ({ children }) => (
    <pre className="scroll-area my-3 overflow-x-auto rounded-lg border border-line bg-surface-2 p-3.5 font-mono text-13 leading-relaxed first:mt-0 last:mb-0">{children}</pre>
  ),
  h1: ({ children }) => <h3 className="mb-2 mt-5 text-base font-semibold text-fg first:mt-0">{children}</h3>,
  h2: ({ children }) => <h3 className="mb-2 mt-5 text-15 font-semibold text-fg first:mt-0">{children}</h3>,
  h3: ({ children }) => <h4 className="mb-1.5 mt-4 text-15 font-semibold text-fg first:mt-0">{children}</h4>,
  blockquote: ({ children }) => <blockquote className="my-3 border-l-2 border-brand/50 pl-3.5 text-fg-muted">{children}</blockquote>,
  hr: () => <hr className="my-5 border-line" />,
  table: ({ children }) => (
    <div className="scroll-area my-3 overflow-x-auto rounded-lg border border-line">
      <table className="w-full border-collapse text-13">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-fg/[0.04]">{children}</thead>,
  th: ({ children }) => <th className="border-b border-line px-3 py-2 text-left font-semibold text-fg">{children}</th>,
  td: ({ children }) => <td className="border-b border-line px-3 py-2 align-top [tr:last-child_&]:border-b-0">{children}</td>
};

export const ChatMarkdown = memo(function ChatMarkdown({ content, streaming = false, className }: { content: string; streaming?: boolean; className?: string }) {
  return (
    <div className={cn("text-15 text-fg/90 [overflow-wrap:anywhere]", streaming && "[&>*:last-child]:stream-caret", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
});
