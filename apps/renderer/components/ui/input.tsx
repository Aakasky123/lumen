import * as React from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

const fieldBase = [
  "w-full rounded-md border border-line-strong bg-surface-1 text-sm text-fg shadow-xs",
  "placeholder:text-fg-subtle",
  "transition-[border-color,box-shadow,background-color] duration-150",
  "hover:border-fg-subtle/50",
  "focus-visible:border-brand/70 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-brand/15",
  "disabled:cursor-not-allowed disabled:opacity-55",
  "aria-[invalid=true]:border-danger/70 aria-[invalid=true]:ring-danger/15"
].join(" ");

const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(({ className, type = "text", ...props }, ref) => (
  <input ref={ref} type={type} className={cn(fieldBase, "h-9 px-3", className)} {...props} />
));
Input.displayName = "Input";

const Textarea = React.forwardRef<HTMLTextAreaElement, React.ComponentProps<"textarea">>(({ className, ...props }, ref) => (
  <textarea ref={ref} className={cn(fieldBase, "scroll-area min-h-[5rem] resize-y px-3 py-2 leading-relaxed", className)} {...props} />
));
Textarea.displayName = "Textarea";

const Select = React.forwardRef<HTMLSelectElement, React.ComponentProps<"select">>(({ className, children, ...props }, ref) => (
  <div className="relative">
    <select ref={ref} className={cn(fieldBase, "h-9 appearance-none pl-3 pr-9", className)} {...props}>
      {children}
    </select>
    <ChevronDown className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-fg-subtle" />
  </div>
));
Select.displayName = "Select";

export { Input, Textarea, Select };
