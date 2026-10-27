"use client";

import { useEffect, useMemo, useState } from "react";
import { ShieldCheck, ShieldOff } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/input";
import type { ClosedWorldAttestation } from "@/lib/api";
import {
  employerMemberValidationError,
  employmentAttestationNeedsSave,
  formatEmployerMembers,
  parseEmployerMembers
} from "@/lib/employmentHistoryAttestation";
import { cn, parseBackendDate } from "@/lib/utils";

function readableDate(value: string | null | undefined): string {
  if (!value) return "";
  const date = parseBackendDate(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

/**
 * A user-verified, complete list of past employers. Only an explicit confirmation makes it
 * active; Lumen may then answer "No" to prior-employer questions about companies not on it.
 */
export function EmploymentHistoryAttestationSection({
  attestation,
  loading,
  loadError,
  onSave,
  onRevoke
}: {
  attestation: ClosedWorldAttestation | null;
  loading: boolean;
  loadError: string | null;
  onSave: (members: string[]) => Promise<void>;
  onRevoke: (attestationId: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState<"save" | "revoke" | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    setDraft(formatEmployerMembers(attestation?.members || []));
    setConfirmed(false);
    setNotice(null);
  }, [attestation?.id, attestation?.updated_at]);

  const members = useMemo(() => parseEmployerMembers(draft), [draft]);
  const validationError = employerMemberValidationError(members);
  const needsSave = employmentAttestationNeedsSave(attestation, members);
  const active = attestation?.status === "active";
  const unavailable = Boolean(loadError);
  const status = loading
    ? { label: "Loading…", tone: "neutral" as const }
    : unavailable
      ? { label: "Unavailable", tone: "neutral" as const }
      : active && needsSave
        ? { label: "Edited — confirm again", tone: "attention" as const }
        : active
          ? { label: "Verified by you", tone: "brand" as const }
          : attestation?.status === "revoked"
            ? { label: "Revoked", tone: "neutral" as const }
            : { label: "Not verified", tone: "neutral" as const };

  const save = async () => {
    setBusy("save");
    setNotice(null);
    try {
      await onSave(members);
      setConfirmed(false);
      setNotice({ tone: "ok", text: "Saved. Lumen treats this list as your complete employment history." });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "Couldn't save the list." });
    } finally {
      setBusy(null);
    }
  };

  const revoke = async () => {
    if (!attestation || attestation.status !== "active") return;
    setBusy("revoke");
    setNotice(null);
    try {
      await onRevoke(attestation.id);
      setConfirmed(false);
      setNotice({ tone: "ok", text: "Revoked. Lumen no longer treats this list as complete." });
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "Couldn't revoke the list." });
    } finally {
      setBusy(null);
    }
  };

  const locked = loading || unavailable || busy !== null;

  return (
    <div className="space-y-3 rounded-xl border border-line bg-surface-1 p-4">
      <div className="flex items-start gap-3">
        {active && !needsSave ? <ShieldCheck className="mt-0.5 size-4 shrink-0 text-brand-ink" /> : <ShieldOff className="mt-0.5 size-4 shrink-0 text-fg-subtle" />}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-13 font-medium text-fg">Complete list of employers</p>
            <Badge tone={status.tone}>{status.label}</Badge>
          </div>
          <p className="mt-0.5 text-xs text-fg-subtle">
            {active && !needsSave
              ? `Confirmed ${readableDate(attestation?.updated_at)}.`
              : "One exact name per line. Leave it empty to confirm you have no past employers."}
          </p>
        </div>
      </div>

      {loadError ? (
        <p className="rounded-lg border border-danger/25 bg-danger/[0.06] px-3 py-2 text-xs text-danger-ink" role="alert">
          {loadError}
        </p>
      ) : null}

      <Textarea
        id="profile-employment-history-members"
        aria-label="Employers, one per line"
        value={draft}
        disabled={locked}
        rows={4}
        placeholder={"Acme Corp\nGlobex"}
        onChange={(event) => {
          setDraft(event.target.value);
          setConfirmed(false);
          setNotice(null);
        }}
        className="font-mono text-13"
      />
      {validationError ? <p className="text-xs text-danger-ink">{validationError}</p> : null}

      {needsSave ? (
        <label
          className={cn(
            "flex cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2.5 text-xs leading-relaxed text-fg-muted transition",
            confirmed ? "border-attention/40 bg-attention/[0.07] text-fg" : "border-line hover:border-line-strong"
          )}
        >
          <input
            type="checkbox"
            checked={confirmed}
            disabled={locked}
            onChange={(event) => setConfirmed(event.target.checked)}
            className="mt-0.5 size-3.5 shrink-0 accent-[hsl(var(--brand))]"
          />
          <span>
            This is my complete employer history. Lumen may answer <strong className="text-fg">No</strong> to questions about having worked at any company not listed.
          </span>
        </label>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        {needsSave ? (
          <Button size="sm" variant="primary" loading={busy === "save"} disabled={locked || !confirmed || Boolean(validationError)} onClick={save}>
            {active ? "Confirm changes" : "Confirm list"}
          </Button>
        ) : null}
        {active ? (
          <Button size="sm" variant="danger-ghost" loading={busy === "revoke"} disabled={locked} onClick={revoke}>
            Revoke
          </Button>
        ) : null}
      </div>
      {notice ? (
        <p className={cn("text-xs", notice.tone === "error" ? "text-danger-ink" : "text-fg-muted")} role="status">
          {notice.text}
        </p>
      ) : null}
    </div>
  );
}
