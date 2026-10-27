"use client";

import { useEffect, useState, type FormEvent } from "react";
import { KeyRound, LockKeyhole, Pencil, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tooltip } from "@/components/ui/tooltip";
import { toast } from "@/components/ui/toast";
import { api, type CredentialVaultFields, type CredentialVaultItem } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { parseBackendDate } from "@/lib/utils";

type Editor = { kind: "create"; draft: CredentialDraft } | { kind: "edit"; itemId: number; draft: CredentialDraft };

type CredentialDraft = {
  accountLabel: string;
  origin: string;
  username: string;
  email: string;
  password: string;
  autoUse: boolean;
};

const EMPTY_DRAFT: CredentialDraft = {
  accountLabel: "",
  origin: "",
  username: "",
  email: "",
  password: "",
  autoUse: true
};

const SLOT_LABELS: Record<string, string> = {
  username: "Username",
  email: "Email",
  password: "Password"
};

function sortedItems(items: CredentialVaultItem[]): CredentialVaultItem[] {
  return [...items].sort((left, right) => {
    const originOrder = left.origin.localeCompare(right.origin);
    return originOrder || left.account_label.localeCompare(right.account_label);
  });
}

function draftFor(item: CredentialVaultItem): CredentialDraft {
  return {
    accountLabel: item.account_label,
    origin: item.origin,
    username: "",
    email: "",
    password: "",
    autoUse: item.auto_use
  };
}

function errorMessage(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  const message = error.message.trim();
  if (!message) return fallback;
  try {
    const parsed = JSON.parse(message) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail.trim();
  } catch {
    // Non-JSON errors are already concise client-side messages.
  }
  if (message.startsWith("{")) return fallback;
  return message;
}

function normalizedOrigin(value: string): string | null {
  try {
    const parsed = new URL(value.trim());
    const loopback = parsed.hostname === "localhost" || parsed.hostname === "[::1]" || /^127(?:\.\d{1,3}){3}$/.test(parsed.hostname);
    if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && loopback)) return null;
    return parsed.origin;
  } catch {
    return null;
  }
}

function hostOf(origin: string): string {
  try {
    return new URL(origin).hostname.replace(/^www\./, "");
  } catch {
    return origin;
  }
}

function lastUsed(value: string | null): string {
  if (!value) return "Never used";
  const parsed = parseBackendDate(value);
  if (Number.isNaN(parsed.getTime())) return "";
  const relative = formatRelative(parsed);
  return `Used ${relative === "Yesterday" ? "yesterday" : relative}`;
}

function SiteAvatar({ origin }: { origin: string }) {
  const host = hostOf(origin);
  return (
    <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-line bg-surface-2 font-mono text-13 font-semibold uppercase text-fg-muted">
      {host.replace(/^(app|my|secure|account|accounts|login)\./, "").charAt(0) || "?"}
    </span>
  );
}

export function CredentialsDrawer({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [items, setItems] = useState<CredentialVaultItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<CredentialVaultItem | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setEditor(null);
      setDeleteTarget(null);
      setDeleteError(null);
      setError(null);
      return;
    }
    let active = true;
    setLoading(true);
    setError(null);
    api
      .credentialVault()
      .then(({ items: loaded }) => {
        if (active) setItems(sortedItems(loaded));
      })
      .catch((loadError) => {
        if (active) setError(errorMessage(loadError, "Couldn't load your sign-ins."));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open]);

  const setDraft = (update: Partial<CredentialDraft>) => {
    setEditor((current) => (current ? { ...current, draft: { ...current.draft, ...update } } : current));
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editor || saving) return;

    const origin = normalizedOrigin(editor.draft.origin);
    if (!origin) {
      setError("Enter the site's full address, like https://example.com. Plain http works only for localhost.");
      return;
    }
    const accountLabel = editor.draft.accountLabel.trim();
    if (!accountLabel) {
      setError("Give the account a name.");
      return;
    }

    const fields: CredentialVaultFields = {};
    if (editor.draft.username) fields.username = editor.draft.username;
    if (editor.draft.email) fields.email = editor.draft.email;
    if (editor.draft.password) fields.password = editor.draft.password;
    if (editor.kind === "create" && Object.keys(fields).length === 0) {
      setError("Enter at least a username, email, or password.");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      let saved: CredentialVaultItem;
      if (editor.kind === "create") {
        saved = await api.createCredentialVaultItem({ origin, account_label: accountLabel, fields, auto_use: editor.draft.autoUse });
        toast("Sign-in saved", { detail: `${accountLabel} on ${hostOf(origin)}` });
      } else {
        saved = await api.updateCredentialVaultItem(editor.itemId, {
          origin,
          account_label: accountLabel,
          ...(Object.keys(fields).length ? { fields } : {}),
          auto_use: editor.draft.autoUse
        });
        toast("Sign-in updated");
      }
      setItems((current) => sortedItems([...current.filter((item) => item.id !== saved.id), saved]));
      setEditor(null);
    } catch (saveError) {
      setError(errorMessage(saveError, "Couldn't save this sign-in."));
    } finally {
      setSaving(false);
    }
  };

  const revoke = async () => {
    if (!deleteTarget || deletingId !== null) return;
    setDeletingId(deleteTarget.id);
    setDeleteError(null);
    setError(null);
    try {
      await api.deleteCredentialVaultItem(deleteTarget.id);
      setItems((current) => current.filter((item) => item.id !== deleteTarget.id));
      if (editor?.kind === "edit" && editor.itemId === deleteTarget.id) setEditor(null);
      toast("Sign-in removed", { detail: `${deleteTarget.account_label} on ${hostOf(deleteTarget.origin)}` });
      setDeleteTarget(null);
      setDeleteError(null);
    } catch (deleteError) {
      setDeleteError(errorMessage(deleteError, "Couldn't remove this sign-in."));
    } finally {
      setDeletingId(null);
    }
  };

  const editing = editor?.kind === "edit";

  return (
    <>
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent className="max-w-[34rem]">
          <SheetHeader icon={<KeyRound />} title="Sign-ins" description="Encrypted accounts Lumen can use, each only on the exact site you saved it for." />
          <SheetBody className="space-y-5">
            <div className="flex items-start gap-3 rounded-xl border border-line bg-fg/[0.025] px-4 py-3">
              <LockKeyhole className="mt-0.5 size-4 shrink-0 text-brand-ink" />
              <p className="text-xs leading-relaxed text-fg-muted">
                Values are encrypted before they&rsquo;re stored and never shown again, here, in chat, or to a model. A sign-in works only on its exact site address.
              </p>
            </div>

            {error ? (
              <p role="alert" className="rounded-lg border border-danger/25 bg-danger/[0.06] px-3 py-2 text-13 text-danger-ink">
                {error}
              </p>
            ) : null}

            {editor ? (
              <form className="animate-enter-up space-y-4 rounded-xl border border-line-strong bg-surface-1 p-4 shadow-sm" onSubmit={save}>
                <div>
                  <p className="text-sm font-semibold text-fg">{editing ? "Edit sign-in" : "Add a sign-in"}</p>
                  <p className="mt-0.5 text-xs text-fg-subtle">
                    {editing ? "Saved values stay hidden. Fill in only what you want to replace." : "Add whichever values this account uses."}
                  </p>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <Field id="credential-account-label" label="Account name" hint="Not secret. Just for you.">
                    <Input value={editor.draft.accountLabel} onChange={(event) => setDraft({ accountLabel: event.target.value })} placeholder="Personal" required />
                  </Field>
                  <Field id="credential-origin" label="Site" hint="Paths are dropped when saved.">
                    <Input
                      type="url"
                      inputMode="url"
                      value={editor.draft.origin}
                      onChange={(event) => setDraft({ origin: event.target.value })}
                      placeholder="https://example.com"
                      spellCheck={false}
                      required
                    />
                  </Field>
                  <Field id="credential-username" label="Username" optional>
                    <Input
                      value={editor.draft.username}
                      onChange={(event) => setDraft({ username: event.target.value })}
                      placeholder={editing ? "Unchanged" : ""}
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </Field>
                  <Field id="credential-email" label="Email" optional>
                    <Input
                      type="email"
                      value={editor.draft.email}
                      onChange={(event) => setDraft({ email: event.target.value })}
                      placeholder={editing ? "Unchanged" : "name@example.com"}
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </Field>
                  <Field id="credential-password" label="Password" optional className="col-span-2">
                    <Input
                      type="password"
                      value={editor.draft.password}
                      onChange={(event) => setDraft({ password: event.target.value })}
                      placeholder={editing ? "Unchanged" : ""}
                      autoComplete="new-password"
                      spellCheck={false}
                    />
                  </Field>
                </div>
                <div className="flex items-start justify-between gap-4 rounded-lg border border-line px-3.5 py-3">
                  <div>
                    <p className="text-13 font-medium text-fg">Sign in automatically</p>
                    <p className="mt-0.5 text-xs text-fg-subtle">Lumen signs in without asking when the site matches exactly.</p>
                  </div>
                  <Switch checked={editor.draft.autoUse} onCheckedChange={(next) => setDraft({ autoUse: next })} aria-label="Sign in automatically" />
                </div>
                <div className="flex justify-end gap-2">
                  <Button variant="ghost" size="sm" disabled={saving} onClick={() => setEditor(null)}>
                    Cancel
                  </Button>
                  <Button type="submit" variant="primary" size="sm" loading={saving}>
                    {editing ? "Save changes" : "Save sign-in"}
                  </Button>
                </div>
              </form>
            ) : (
              <Button
                variant="secondary"
                size="md"
                className="w-full"
                onClick={() => {
                  setError(null);
                  setEditor({ kind: "create", draft: { ...EMPTY_DRAFT } });
                }}
              >
                <Plus /> Add a sign-in
              </Button>
            )}

            <section aria-labelledby="saved-credentials-heading">
              <p id="saved-credentials-heading" className="eyebrow mb-2">
                Saved{!loading && items.length ? ` · ${items.length}` : ""}
              </p>
              {loading ? (
                <div className="space-y-2" aria-hidden="true">
                  {[0, 1].map((index) => (
                    <div key={index} className="h-16 animate-pulse rounded-xl bg-fg/[0.04]" />
                  ))}
                </div>
              ) : items.length ? (
                <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line bg-surface-1">
                  {items.map((item) => (
                    <li key={item.id} className="group/cred flex items-start gap-3 px-4 py-3">
                      <SiteAvatar origin={item.origin} />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="truncate text-13 font-medium text-fg">{item.account_label}</p>
                          {item.auto_use ? <Badge tone="brand">Auto sign-in</Badge> : null}
                        </div>
                        <p className="truncate text-xs text-fg-muted">{hostOf(item.origin)}</p>
                        <p className="mt-1 text-xs text-fg-subtle">
                          {item.available_slots.length ? item.available_slots.map((slot) => SLOT_LABELS[slot] ?? slot.replace(/_/g, " ")).join(" · ") : "No values saved"}
                          {" · "}
                          {lastUsed(item.last_used_at)}
                        </p>
                      </div>
                      <div className="flex shrink-0 items-center gap-0.5 opacity-60 transition group-hover/cred:opacity-100 group-focus-within/cred:opacity-100">
                        <Tooltip label="Edit">
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label={`Edit ${item.account_label}`}
                            onClick={() => {
                              setError(null);
                              setEditor({ kind: "edit", itemId: item.id, draft: draftFor(item) });
                            }}
                          >
                            <Pencil />
                          </Button>
                        </Tooltip>
                        <Tooltip label="Remove">
                          <Button
                            variant="danger-ghost"
                            size="icon-sm"
                            aria-label={`Remove ${item.account_label}`}
                            onClick={() => {
                              setDeleteError(null);
                              setDeleteTarget(item);
                            }}
                          >
                            <Trash2 />
                          </Button>
                        </Tooltip>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="rounded-xl border border-dashed border-line-strong px-4 py-7 text-center">
                  <KeyRound className="mx-auto size-5 text-fg-subtle" />
                  <p className="mt-2 text-13 font-medium text-fg">No sign-ins yet</p>
                  <p className="mt-0.5 text-xs text-fg-subtle">Add one here, or save it when Lumen asks during a task.</p>
                </div>
              )}
            </section>
          </SheetBody>
        </SheetContent>
      </Sheet>

      <Dialog
        open={deleteTarget !== null}
        onOpenChange={(nextOpen) => {
          if (!nextOpen && deletingId === null) {
            setDeleteTarget(null);
            setDeleteError(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove this sign-in?</DialogTitle>
            <DialogDescription>
              {deleteTarget
                ? `Lumen won't be able to sign in to ${hostOf(deleteTarget.origin)} as “${deleteTarget.account_label}”. Every encrypted value in it is deleted.`
                : "Every encrypted value in it is deleted."}
            </DialogDescription>
          </DialogHeader>
          {deleteError ? (
            <p role="alert" className="text-13 text-danger-ink">
              {deleteError}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              variant="ghost"
              disabled={deletingId !== null}
              onClick={() => {
                setDeleteTarget(null);
                setDeleteError(null);
              }}
            >
              Keep it
            </Button>
            <Button variant="danger" loading={deletingId !== null} onClick={() => void revoke()}>
              Remove sign-in
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
