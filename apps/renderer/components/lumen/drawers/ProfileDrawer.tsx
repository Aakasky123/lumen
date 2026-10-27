"use client";

import { useEffect, useMemo, useState } from "react";
import { ChevronDown, PenLine, Plus, Search, Sparkles, Trash2, UserRound } from "lucide-react";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/sheet";
import { Input, Select } from "@/components/ui/input";
import { Field } from "@/components/ui/field";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/badge";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { toast } from "@/components/ui/toast";
import { EmploymentHistoryAttestationSection } from "@/components/lumen/drawers/EmploymentHistoryAttestationSection";
import { api, type ClosedWorldAttestation } from "@/lib/api";
import { employmentHistoryAttestationContract, selectEmploymentHistoryAttestation } from "@/lib/employmentHistoryAttestation";
import { describeFactSource, factIsNew, groupFacts, shortFactDate, type MemoryFact, type MemoryPayload, type MemorySection } from "@/lib/memoryFacts";
import { cn } from "@/lib/utils";

function errorText(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  try {
    const parsed = JSON.parse(error.message) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail;
  } catch {
    // Not a JSON error body.
  }
  return fallback;
}

type Draft = { label: string; value: string; section: string };

function FactEditor({
  sections,
  initial,
  submitLabel,
  busy,
  onSubmit,
  onCancel
}: {
  sections: MemorySection[];
  initial: Draft;
  submitLabel: string;
  busy: boolean;
  onSubmit: (draft: Draft) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const ready = draft.label.trim() && draft.value.trim();
  return (
    <form
      className="animate-enter-fade space-y-3 rounded-xl border border-brand/30 bg-brand/[0.04] p-3.5"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) onSubmit({ label: draft.label.trim(), value: draft.value.trim(), section: draft.section });
      }}
    >
      <div className="grid grid-cols-[1fr_9rem] gap-2.5">
        <Field id="fact-label" label="Name">
          <Input value={draft.label} onChange={(event) => setDraft({ ...draft, label: event.target.value })} placeholder="Notice period" autoFocus />
        </Field>
        <Field id="fact-section" label="Section">
          <Select value={draft.section} onChange={(event) => setDraft({ ...draft, section: event.target.value })}>
            {sections.map((section) => (
              <option key={section.key} value={section.key}>
                {section.title}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <Field id="fact-value" label="What Lumen should know">
        <Input value={draft.value} onChange={(event) => setDraft({ ...draft, value: event.target.value })} placeholder="Two weeks" />
      </Field>
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" size="sm" loading={busy} disabled={!ready}>
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}

function FactRow({
  fact,
  editing,
  busy,
  sections,
  onEdit,
  onSave,
  onCancel,
  onDelete
}: {
  fact: MemoryFact;
  editing: boolean;
  busy: boolean;
  sections: MemorySection[];
  onEdit: () => void;
  onSave: (draft: Draft) => void;
  onCancel: () => void;
  onDelete: () => Promise<void>;
}) {
  if (editing) {
    return (
      <li className="p-2">
        <FactEditor sections={sections} initial={{ label: fact.label, value: fact.value, section: fact.section }} submitLabel="Save" busy={busy} onSubmit={onSave} onCancel={onCancel} />
      </li>
    );
  }
  const source = describeFactSource(fact.source);
  const date = shortFactDate(fact.learned_on);
  return (
    <li className="group flex items-start gap-3 px-3.5 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="text-xs font-medium text-fg-muted">{fact.label}</p>
          {factIsNew(fact) ? (
            <Badge tone="brand">
              <Sparkles /> New
            </Badge>
          ) : null}
        </div>
        <p className="mt-0.5 whitespace-pre-wrap text-sm text-fg [overflow-wrap:anywhere]">{fact.value}</p>
        {source || date ? <p className="mt-1 text-2xs text-fg-subtle">{[source, date].filter(Boolean).join(" · ")}</p> : null}
      </div>
      <div className="flex shrink-0 items-center gap-0.5 opacity-60 transition group-hover:opacity-100 group-focus-within:opacity-100">
        <Button variant="ghost" size="icon-sm" aria-label={`Edit ${fact.label}`} disabled={busy} onClick={onEdit}>
          <PenLine />
        </Button>
        <ConfirmButton size="icon-sm" icon={<Trash2 />} ariaLabel={`Delete ${fact.label}`} disabled={busy} onConfirm={onDelete} />
      </div>
    </li>
  );
}

/**
 * About you: everything Lumen knows about the user, in one list they can read and fix. Lumen adds to
 * it as it learns (from chat, answers during tasks, and documents); the user can edit, move, or
 * delete any fact, or add their own.
 */
export function ProfileDrawer({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [memory, setMemory] = useState<MemoryPayload | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sensitiveOpen, setSensitiveOpen] = useState(false);
  const [employmentAttestation, setEmploymentAttestation] = useState<ClosedWorldAttestation | null>(null);
  const [attestationLoading, setAttestationLoading] = useState(false);
  const [attestationLoadError, setAttestationLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoadError(null);
    setEditing(null);
    setAdding(false);
    setAttestationLoading(true);
    setAttestationLoadError(null);
    api
      .memory()
      .then((payload) => {
        if (active) setMemory(payload);
      })
      .catch(() => {
        if (active) setLoadError("Couldn't load your profile. Close this panel and open it again to retry.");
      });
    api
      .closedWorldAttestations()
      .then(({ items }) => {
        if (active) setEmploymentAttestation(selectEmploymentHistoryAttestation(items));
      })
      .catch(() => {
        if (active) setAttestationLoadError("Couldn't load the verification status. Close and reopen this panel to retry.");
      })
      .finally(() => {
        if (active) setAttestationLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open]);

  const groups = useMemo(() => {
    if (!memory) return [];
    const needle = query.trim().toLowerCase();
    const facts = needle ? memory.facts.filter((fact) => `${fact.label} ${fact.value}`.toLowerCase().includes(needle)) : memory.facts;
    return groupFacts({ ...memory, facts });
  }, [memory, query]);

  const run = async (operation: () => Promise<MemoryPayload>, done: string) => {
    setBusy(true);
    try {
      setMemory(await operation());
      setEditing(null);
      setAdding(false);
      toast(done);
    } catch (error) {
      toast("Couldn't save that", { tone: "error", detail: errorText(error, "Try again.") });
    } finally {
      setBusy(false);
    }
  };

  const sections = memory?.sections ?? [];
  const factCount = memory?.facts.length ?? 0;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="max-w-[34rem]">
        <SheetHeader
          icon={<UserRound />}
          title="About you"
          description="Everything Lumen knows about you. It learns from your chats, answers, and documents, and fills forms only from what's here."
        />
        <SheetBody className="space-y-7">
          {loadError ? <p className="rounded-lg border border-danger/25 bg-danger/[0.06] px-3 py-2 text-13 text-danger-ink">{loadError}</p> : null}

          <div className="flex items-center gap-2">
            {factCount > 6 ? (
              <div className="relative flex-1">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-fg-subtle" />
                <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search what Lumen knows" className="h-8 pl-8 text-13" aria-label="Search what Lumen knows" />
              </div>
            ) : (
              <span className="flex-1" />
            )}
            {!adding ? (
              <Button variant="secondary" size="sm" disabled={!memory} onClick={() => setAdding(true)}>
                <Plus /> Add a fact
              </Button>
            ) : null}
          </div>

          {adding ? (
            <FactEditor
              sections={sections}
              initial={{ label: "", value: "", section: "other" }}
              submitLabel="Add"
              busy={busy}
              onSubmit={(draft) => run(() => api.addMemoryFact(draft), "Added to your profile")}
              onCancel={() => setAdding(false)}
            />
          ) : null}

          {memory && !factCount && !adding ? (
            <div className="rounded-xl border border-dashed border-line-strong px-5 py-6 text-center">
              <p className="text-sm font-medium text-fg">Lumen doesn't know anything about you yet</p>
              <p className="mx-auto mt-1 max-w-sm text-13 text-fg-muted">
                Tell it things in chat, like “I live in Hoboken”, answer its questions during a task, or upload your résumé. What it learns shows up here.
              </p>
            </div>
          ) : null}
          {memory && factCount > 0 && !groups.length ? <p className="text-13 text-fg-subtle">Nothing matches “{query}”.</p> : null}

          {groups.map((group) => {
            const sensitive = group.key === "sensitive";
            const collapsed = sensitive && !sensitiveOpen && !query.trim();
            return (
              <section key={group.key}>
                <button
                  type="button"
                  disabled={!sensitive}
                  onClick={() => setSensitiveOpen((value) => !value)}
                  aria-expanded={sensitive ? !collapsed : undefined}
                  className={cn("mb-2 flex w-full items-center gap-2 text-left", sensitive ? "cursor-pointer" : "cursor-default")}
                >
                  <h2 className="text-13 font-semibold text-fg">{group.title}</h2>
                  <span className="text-xs text-fg-subtle">{group.facts.length}</span>
                  {sensitive ? (
                    <>
                      <span className="text-xs text-fg-subtle">· only what you chose to share</span>
                      <ChevronDown className={cn("ml-auto size-4 text-fg-subtle transition-transform", !collapsed && "rotate-180")} />
                    </>
                  ) : null}
                </button>
                {collapsed ? null : (
                  <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line">
                    {group.facts.map((fact) => (
                      <FactRow
                        key={fact.key}
                        fact={fact}
                        sections={sections}
                        editing={editing === fact.key}
                        busy={busy}
                        onEdit={() => {
                          setAdding(false);
                          setEditing(fact.key);
                        }}
                        onCancel={() => setEditing(null)}
                        onSave={(draft) =>
                          run(
                            () =>
                              api.updateMemoryFact(fact.key, {
                                ...(draft.label !== fact.label ? { label: draft.label } : {}),
                                value: draft.value,
                                section: draft.section
                              }),
                            "Saved"
                          )
                        }
                        onDelete={() => run(() => api.deleteMemoryFact(fact.key), `Removed ${fact.label.toLowerCase()}`)}
                      />
                    ))}
                  </ul>
                )}
              </section>
            );
          })}

          <section>
            <div className="mb-3">
              <h2 className="text-13 font-semibold text-fg">Employment history</h2>
              <p className="mt-0.5 text-xs text-fg-subtle">Lumen never assumes your résumé lists every employer.</p>
            </div>
            <EmploymentHistoryAttestationSection
              attestation={employmentAttestation}
              loading={attestationLoading}
              loadError={attestationLoadError}
              onSave={async (members) => {
                const { attestation } = await api.createClosedWorldAttestation({
                  scope: employmentHistoryAttestationContract.scope,
                  set_key: employmentHistoryAttestationContract.setKey,
                  members
                });
                setEmploymentAttestation(attestation);
              }}
              onRevoke={async (attestationId) => {
                const { attestation } = await api.revokeClosedWorldAttestation(attestationId);
                setEmploymentAttestation(attestation);
              }}
            />
          </section>
        </SheetBody>
      </SheetContent>
    </Sheet>
  );
}
