"use client";

import { useEffect, useState, type FormEvent } from "react";
import { KeyRound, Plus, UserRound } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { api, type CredentialVaultFields, type CredentialVaultItem } from "@/lib/api";
import type { SensitiveBrowserTakeover } from "@/lib/hooks/useChat";
import { runTargetLabel } from "@/lib/runPhase";
import { cn } from "@/lib/utils";

type CredentialSlot = keyof CredentialVaultFields;
type CredentialDestination = { kind: "create" } | { kind: "update"; itemId: number } | null;

const CREDENTIAL_SLOT_LABELS: Record<CredentialSlot, string> = {
  username: "Username",
  email: "Email",
  password: "Password"
};

function credentialSlot(value: string): CredentialSlot | null {
  return value === "username" || value === "email" || value === "password" ? value : null;
}

function savedSlotSummary(item: CredentialVaultItem): string {
  if (!item.available_slots.length) return "nothing saved yet";
  return item.available_slots
    .map((slot) => {
      const normalized = credentialSlot(slot);
      return normalized ? CREDENTIAL_SLOT_LABELS[normalized].toLowerCase() : slot;
    })
    .join(", ");
}

function credentialVaultErrorMessage(error: unknown): string {
  const fallback = "Couldn't save these. Check the values and try again.";
  if (!(error instanceof Error)) return fallback;
  const message = error.message.trim();
  if (!message) return fallback;
  try {
    const parsed = JSON.parse(message) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail.trim();
  } catch {
    // Credential request failures stay generic unless the API returned its sanitized detail.
  }
  return fallback;
}

export function CredentialVaultHandoffCard({
  takeover,
  busy,
  onContinue,
  onCancel
}: {
  takeover: SensitiveBrowserTakeover;
  busy: boolean;
  onContinue: () => Promise<boolean>;
  onCancel: () => Promise<void>;
}) {
  const requestedSlot = credentialSlot(takeover.credentialSlot);
  const [vaultItems, setVaultItems] = useState<CredentialVaultItem[]>([]);
  const [destination, setDestination] = useState<CredentialDestination>(null);
  const [loadingAccounts, setLoadingAccounts] = useState(false);
  const [accountsLoadError, setAccountsLoadError] = useState<string | null>(null);
  const [accountsReloadToken, setAccountsReloadToken] = useState(0);
  const [accountLabel, setAccountLabel] = useState("");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [saved, setSaved] = useState(false);
  const [savedMessage, setSavedMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bindingMissing = !takeover.handoffId || !takeover.credentialOrigin || !requestedSlot;
  const selectedExisting = destination?.kind === "update" ? vaultItems.find((item) => item.id === destination.itemId) ?? null : null;

  useEffect(() => {
    setVaultItems([]);
    setDestination(null);
    setLoadingAccounts(false);
    setAccountsLoadError(null);
    setAccountLabel("");
    setUsername("");
    setEmail("");
    setPassword("");
    setSaved(false);
    setSavedMessage("");
    setSubmitting(false);
    setError(null);
  }, [takeover.handoffId, takeover.credentialOrigin, takeover.credentialSlot]);

  useEffect(() => {
    let active = true;
    if (!takeover.credentialOrigin) {
      setLoadingAccounts(false);
      return () => {
        active = false;
      };
    }
    setLoadingAccounts(true);
    setAccountsLoadError(null);
    api
      .credentialVault(takeover.credentialOrigin)
      .then(({ items }) => {
        if (!active) return;
        setVaultItems(items);
        setDestination((current) => {
          if (current?.kind === "update") return items.some((item) => item.id === current.itemId) ? current : null;
          // With nothing saved for this site, a new account is the only choice; updating one
          // always stays an explicit pick.
          return current ?? (items.length ? null : { kind: "create" });
        });
      })
      .catch(() => {
        if (active) setAccountsLoadError("Couldn't load the accounts saved for this site.");
      })
      .finally(() => {
        if (active) setLoadingAccounts(false);
      });
    return () => {
      active = false;
    };
  }, [accountsReloadToken, takeover.credentialOrigin, takeover.handoffId]);

  const markChanged = () => {
    setSaved(false);
    setSavedMessage("");
    setError(null);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || submitting) return;
    if (!takeover.handoffId || !takeover.credentialOrigin || !requestedSlot) {
      setError("This request lost its secure link to the site or field. Cancel the task and try again.");
      return;
    }
    if (!destination) {
      setError("Choose a new account or one you've saved.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      if (!saved) {
        const fields: CredentialVaultFields = {};
        if (username) fields.username = username;
        if (email) fields.email = email;
        if (password) fields.password = password;
        if (!fields[requestedSlot]) {
          setError(`Enter the ${CREDENTIAL_SLOT_LABELS[requestedSlot].toLowerCase()} Lumen asked for.`);
          return;
        }
        if (destination.kind === "create") {
          if (!accountLabel.trim()) {
            setError("Give the account a name.");
            return;
          }
          await api.createCredentialVaultItem({
            origin: takeover.credentialOrigin,
            account_label: accountLabel.trim(),
            fields,
            auto_use: true
          });
          setSavedMessage("Saved to your vault.");
        } else {
          if (!selectedExisting) {
            setError("That saved account is gone. Refresh and choose again.");
            return;
          }
          await api.updateCredentialVaultItem(selectedExisting.id, {
            fields,
            auto_use: true
          });
          setSavedMessage(`Updated ${selectedExisting.account_label}.`);
        }
        setUsername("");
        setEmail("");
        setPassword("");
        setSaved(true);
      }
      const continued = await onContinue();
      if (!continued) setError("Saved, but Lumen couldn't continue yet. Press Continue to try again.");
    } catch (saveError) {
      setError(credentialVaultErrorMessage(saveError));
    } finally {
      setSubmitting(false);
    }
  };

  const disabled = busy || submitting;
  const accountChoiceUnavailable = loadingAccounts || accountsLoadError !== null;
  const site = runTargetLabel(takeover.credentialOrigin || takeover.targetUrl);
  const slotWord = requestedSlot ? CREDENTIAL_SLOT_LABELS[requestedSlot].toLowerCase() : "sign-in details";
  const slotHint = (slot: CredentialSlot) => (requestedSlot === slot ? undefined : "Only if this account uses it");

  const choiceClass = (selected: boolean) =>
    cn(
      "flex cursor-pointer items-start gap-3 rounded-xl border px-3.5 py-3 text-sm transition focus-within:ring-2 focus-within:ring-ring/50",
      selected ? "border-brand/50 bg-brand/[0.06]" : "border-line hover:border-line-strong hover:bg-fg/[0.02]"
    );

  return (
    <section className="animate-enter-up overflow-hidden rounded-2xl border border-brand/30 bg-surface-1 shadow-md" aria-label="Save a sign-in">
      <header className="flex items-center gap-2 border-b border-brand/15 bg-brand/[0.06] px-4 py-2 text-brand-ink">
        <KeyRound className="size-4" />
        <span className="text-13 font-semibold">Save a sign-in</span>
        <span className="ml-auto text-2xs font-medium text-fg-subtle">Chat is paused</span>
      </header>
      <form className="space-y-5 px-5 pb-5 pt-4" onSubmit={submit}>
        <div>
          <h3 className="text-base font-semibold text-fg">
            Lumen needs the {slotWord} for {site || "this site"}
          </h3>
          <p className="mt-1 text-sm text-fg-muted">
            What you enter here goes straight to your encrypted vault, never into chat or a model. Lumen can use it only on{" "}
            <span className="break-all font-medium text-fg">{takeover.credentialOrigin || takeover.targetUrl}</span>.
          </p>
        </div>

        <fieldset className="space-y-2" disabled={disabled || saved || loadingAccounts}>
          <legend className="mb-2 text-13 font-medium text-fg">Save it to</legend>
          {loadingAccounts ? <p className="text-13 text-fg-muted">Looking for accounts saved on this site…</p> : null}
          {accountsLoadError ? (
            <div className="flex flex-wrap items-center gap-3 rounded-xl border border-danger/25 bg-danger/[0.06] px-3.5 py-2.5">
              <p role="alert" className="text-13 text-danger-ink">
                {accountsLoadError}
              </p>
              <Button type="button" size="xs" variant="secondary" disabled={disabled} onClick={() => setAccountsReloadToken((current) => current + 1)}>
                Try again
              </Button>
            </div>
          ) : null}
          {!loadingAccounts && !accountsLoadError ? (
            <div className="grid gap-2 sm:grid-cols-2">
              <label className={choiceClass(destination?.kind === "create")}>
                <input
                  type="radio"
                  name={`credential-destination-${takeover.handoffId}`}
                  value="create"
                  checked={destination?.kind === "create"}
                  onChange={() => {
                    setDestination({ kind: "create" });
                    markChanged();
                  }}
                  className="sr-only"
                />
                <Plus className="mt-0.5 size-4 shrink-0 text-fg-muted" />
                <span>
                  <span className="block font-medium text-fg">A new account</span>
                  <span className="mt-0.5 block text-xs text-fg-muted">You&rsquo;ll name it next</span>
                </span>
              </label>
              {vaultItems.map((item) => (
                <label key={item.id} className={choiceClass(destination?.kind === "update" && destination.itemId === item.id)}>
                  <input
                    type="radio"
                    name={`credential-destination-${takeover.handoffId}`}
                    value={`update-${item.id}`}
                    checked={destination?.kind === "update" && destination.itemId === item.id}
                    onChange={() => {
                      setDestination({ kind: "update", itemId: item.id });
                      markChanged();
                    }}
                    className="sr-only"
                  />
                  <UserRound className="mt-0.5 size-4 shrink-0 text-fg-muted" />
                  <span className="min-w-0">
                    <span className="block truncate font-medium text-fg">Update {item.account_label}</span>
                    <span className="mt-0.5 block text-xs text-fg-muted">Has {savedSlotSummary(item)}</span>
                  </span>
                </label>
              ))}
            </div>
          ) : null}
        </fieldset>

        {destination?.kind === "create" ? (
          <Field id="handoff-account-label" label="Name this account" hint="Something you'll recognize later, like Personal or Work.">
            <Input
              value={accountLabel}
              onChange={(event) => {
                setAccountLabel(event.target.value);
                markChanged();
              }}
              placeholder="Personal"
              disabled={disabled || saved}
              required
            />
          </Field>
        ) : selectedExisting ? (
          <p className="text-13 text-fg-muted">
            Only <span className="font-medium text-fg">{selectedExisting.account_label}</span> changes. Fields you leave blank keep their saved values.
          </p>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="handoff-username" label="Username" optional={requestedSlot !== "username"} hint={slotHint("username")}>
            <Input
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
                markChanged();
              }}
              autoComplete="off"
              spellCheck={false}
              disabled={disabled || saved}
              required={requestedSlot === "username" && !saved}
            />
          </Field>
          <Field id="handoff-email" label="Email" optional={requestedSlot !== "email"} hint={slotHint("email")}>
            <Input
              type="email"
              value={email}
              onChange={(event) => {
                setEmail(event.target.value);
                markChanged();
              }}
              autoComplete="off"
              spellCheck={false}
              disabled={disabled || saved}
              required={requestedSlot === "email" && !saved}
            />
          </Field>
        </div>
        <Field id="handoff-password" label="Password" optional={requestedSlot !== "password"} hint={slotHint("password")}>
          <Input
            type="password"
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
              markChanged();
            }}
            autoComplete="new-password"
            spellCheck={false}
            disabled={disabled || saved}
            required={requestedSlot === "password" && !saved}
          />
        </Field>

        {bindingMissing ? (
          <p role="alert" className="text-13 text-danger-ink">
            This request lost its secure link to the site or field. Cancel the task and try again.
          </p>
        ) : null}
        {saved ? (
          <p role="status" className="text-13 text-brand-ink">
            {savedMessage} Press Continue to retry without entering them again.
          </p>
        ) : null}
        {error ? (
          <div className="flex flex-wrap items-center gap-3">
            <p role="alert" className="text-13 text-danger-ink">
              {error}
            </p>
            {!saved ? (
              <Button
                type="button"
                size="xs"
                variant="secondary"
                disabled={disabled}
                onClick={() => {
                  setError(null);
                  setAccountsReloadToken((current) => current + 1);
                }}
              >
                Refresh saved accounts
              </Button>
            ) : null}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          <Button
            type="submit"
            variant="primary"
            size="md"
            loading={submitting || busy}
            disabled={disabled || bindingMissing || accountChoiceUnavailable || !destination}
          >
            {submitting || busy ? "Working…" : saved ? "Continue" : destination?.kind === "update" ? "Update & continue" : "Save & continue"}
          </Button>
          <Button type="button" variant="danger-ghost" size="md" disabled={disabled} onClick={() => void onCancel()}>
            Cancel task
          </Button>
        </div>
      </form>
    </section>
  );
}
