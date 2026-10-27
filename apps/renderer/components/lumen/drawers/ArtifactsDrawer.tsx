"use client";

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, BadgeCheck, Download, FileImage, FileText, FileUp, Plane, RefreshCw, Trash2 } from "lucide-react";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/sheet";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/badge";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { Field } from "@/components/ui/field";
import { Input, Select } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip } from "@/components/ui/tooltip";
import { toast } from "@/components/ui/toast";
import { api, type ApplicationArtifact, type ApplicationArtifactPreview } from "@/lib/api";
import { fileMatchesAccept, formatBytes, formatRelative } from "@/lib/format";
import { cn, parseBackendDate } from "@/lib/utils";

// Values are what the backend stores as the document type.
const PRESET_TYPES: Array<{ value: string; label: string }> = [
  { value: "resume", label: "Résumé" },
  { value: "passport", label: "Passport" },
  { value: "flight ticket", label: "Flight ticket" }
];

const ACCEPT = ".pdf,.doc,.docx,.txt,.rtf,.png,.jpg,.jpeg,.webp,.heic,.csv,.json,.md";

function titleCase(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function metadataText(artifact: ApplicationArtifact, keys: string[]): string {
  for (const key of keys) {
    const value = artifact.metadata_json?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function artifactName(artifact: ApplicationArtifact): string {
  return metadataText(artifact, ["display_name", "display_label", "name"]) || artifact.filename || `Document ${artifact.id}`;
}

function artifactRole(artifact: ApplicationArtifact): string {
  return metadataText(artifact, ["artifact_role", "selected_artifact_kind", "kind"]) || artifact.artifact_kind || "document";
}

function roleLabel(role: string): string {
  return PRESET_TYPES.find((type) => type.value === role.toLowerCase())?.label || titleCase(role);
}

function iconFor(artifact: ApplicationArtifact) {
  const role = artifactRole(artifact).toLowerCase();
  if (role.includes("passport") || role.includes("license") || role.includes("id")) return BadgeCheck;
  if (role.includes("flight") || role.includes("ticket") || role.includes("boarding")) return Plane;
  if (artifact.mime_type?.startsWith("image/")) return FileImage;
  return FileText;
}

function uploadErrorText(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.detail === "string") return parsed.detail;
  } catch {
    // Not a JSON API error.
  }
  return raw || "Upload failed";
}

function selectedArtifactType(preview: ApplicationArtifactPreview | null, selectedType: string, customType: string): string {
  if (selectedType === "suggested") return preview?.suggested_artifact_kind?.trim() || "";
  if (selectedType === "custom") return customType.trim();
  return selectedType.trim();
}

export function ArtifactsDrawer({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [artifacts, setArtifacts] = useState<ApplicationArtifact[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [preview, setPreview] = useState<ApplicationArtifactPreview | null>(null);
  const [selectedType, setSelectedType] = useState("resume");
  const [customType, setCustomType] = useState("");
  const [displayNameInput, setDisplayNameInput] = useState("");
  const [phase, setPhase] = useState<"idle" | "reading" | "saving">("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    setBusy(true);
    setError(null);
    try {
      setArtifacts(await api.applicationArtifacts());
      setLoaded(true);
    } catch {
      setError("Couldn't load your documents.");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (open) void load();
  }, [open]);

  const resetPreview = () => {
    setPreview(null);
    setSelectedType("resume");
    setCustomType("");
    setDisplayNameInput("");
  };

  const previewArtifact = async (file: File) => {
    if (!fileMatchesAccept(file, ACCEPT)) {
      setError(`Lumen can't read ${file.name}. Use a PDF, Word, image, or text file.`);
      return;
    }
    setPhase("reading");
    setError(null);
    try {
      const result = await api.previewApplicationArtifact(file);
      setPreview(result);
      setSelectedType(result.suggested_artifact_kind?.trim() ? "suggested" : "resume");
      setCustomType("");
      setDisplayNameInput(result.suggested_display_name || titleCase(result.filename.replace(/\.[^.]+$/, "")));
    } catch (err) {
      resetPreview();
      setError(uploadErrorText(err));
    } finally {
      setPhase("idle");
    }
  };

  const commitArtifact = async () => {
    if (!preview) return;
    const finalType = selectedArtifactType(preview, selectedType, customType);
    if (!finalType) {
      setError("Choose what kind of document this is.");
      return;
    }
    setPhase("saving");
    setError(null);
    try {
      const artifact = await api.commitApplicationArtifact({ draftId: preview.draft_id, selectedType: finalType, displayName: displayNameInput });
      setArtifacts((prev) => [artifact, ...prev.filter((item) => item.id !== artifact.id)]);
      resetPreview();
      toast(`${artifactName(artifact)} saved`);
    } catch (err) {
      setError(uploadErrorText(err));
    } finally {
      setPhase("idle");
    }
  };

  const deleteArtifact = async (artifact: ApplicationArtifact) => {
    try {
      await api.deleteApplicationArtifact(artifact.id);
      setArtifacts((prev) => prev.filter((item) => item.id !== artifact.id));
      toast(`${artifactName(artifact)} deleted`);
    } catch {
      toast("Couldn't delete that document", { tone: "error" });
    }
  };

  const confidence = preview ? Math.round((preview.confidence || 0) * 100) : 0;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="max-w-[34rem]">
        <SheetHeader
          icon={<FileText />}
          title="Documents"
          description="Files Lumen can use when a task calls for one, like a résumé or a passport scan."
          actions={
            <Tooltip label="Refresh" side="bottom">
              <Button variant="ghost" size="icon-sm" aria-label="Refresh documents" disabled={busy} onClick={() => void load()}>
                <RefreshCw className={cn(busy && "animate-spin")} />
              </Button>
            </Tooltip>
          }
        />
        <SheetBody className="space-y-6">
          <input
            ref={fileRef}
            type="file"
            accept={ACCEPT}
            className="sr-only"
            tabIndex={-1}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void previewArtifact(file);
              event.target.value = "";
            }}
          />

          {preview ? (
            <section className="animate-enter-up overflow-hidden rounded-xl border border-brand/30 bg-surface-1 shadow-sm" aria-label="Review before saving">
              <div className="flex items-start gap-3 border-b border-line px-4 py-3">
                <FileText className="mt-0.5 size-4 shrink-0 text-brand-ink" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-13 font-medium text-fg">{preview.filename}</p>
                  <p className="text-xs text-fg-subtle">
                    {formatBytes(preview.file_size)} · {preview.extraction_status === "extracted" ? "Text read" : titleCase(preview.extraction_status)}
                  </p>
                </div>
                <Badge tone="brand">Review</Badge>
              </div>
              <div className="space-y-4 px-4 py-4">
                {preview.summary ? <p className="text-13 text-fg-muted">{preview.summary}</p> : null}
                {preview.key_facts?.length ? (
                  <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 rounded-lg bg-fg/[0.03] px-3.5 py-3 text-13">
                    {preview.key_facts.slice(0, 5).map((fact, index) => (
                      <div key={`${fact.label}-${index}`} className="contents">
                        <dt className="text-fg-subtle">{fact.label}</dt>
                        <dd className="min-w-0 truncate text-fg">{fact.value}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
                {preview.warnings?.length ? (
                  <p className="flex gap-2 text-xs text-attention-ink">
                    <AlertTriangle className="mt-px size-3.5 shrink-0" />
                    {preview.warnings.join(" ")}
                  </p>
                ) : null}
                <div className="grid grid-cols-2 gap-3">
                  <Field id="document-name" label="Name" className="col-span-2">
                    <Input value={displayNameInput} onChange={(event) => setDisplayNameInput(event.target.value)} />
                  </Field>
                  <Field id="document-type" label="Type" className={selectedType === "custom" ? "" : "col-span-2"}>
                    <Select value={selectedType} onChange={(event) => setSelectedType(event.target.value)}>
                      {preview.suggested_artifact_kind ? (
                        <option value="suggested">
                          {roleLabel(preview.suggested_artifact_kind)} (detected, {confidence}%)
                        </option>
                      ) : null}
                      {PRESET_TYPES.map((type) => (
                        <option key={type.value} value={type.value}>
                          {type.label}
                        </option>
                      ))}
                      <option value="custom">Something else…</option>
                    </Select>
                  </Field>
                  {selectedType === "custom" ? (
                    <Field id="document-custom-type" label="Describe it">
                      <Input value={customType} onChange={(event) => setCustomType(event.target.value)} placeholder="e.g. reference letter" autoFocus />
                    </Field>
                  ) : null}
                </div>
              </div>
              <div className="flex items-center justify-end gap-2 border-t border-line bg-fg/[0.02] px-4 py-3">
                <Button variant="ghost" size="sm" disabled={phase !== "idle"} onClick={resetPreview}>
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  loading={phase === "saving"}
                  disabled={!selectedArtifactType(preview, selectedType, customType)}
                  onClick={() => void commitArtifact()}
                >
                  Save document
                </Button>
              </div>
            </section>
          ) : (
            <button
              type="button"
              disabled={phase !== "idle"}
              onClick={() => fileRef.current?.click()}
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDragging(false);
                const file = event.dataTransfer.files?.[0];
                if (file && phase === "idle") void previewArtifact(file);
              }}
              className={cn(
                "flex w-full flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-4 py-7 text-center transition focus-ring",
                dragging ? "border-brand bg-brand/[0.06]" : "border-line-strong hover:border-fg-subtle hover:bg-fg/[0.02]"
              )}
            >
              {phase === "reading" ? <Spinner className="size-5 text-fg-muted" /> : <FileUp className="size-5 text-fg-muted" />}
              <span className="text-sm font-medium text-fg">{phase === "reading" ? "Reading the document…" : "Add a document"}</span>
              <span className="text-xs text-fg-subtle">{phase === "reading" ? "Lumen checks what it is before you save it." : "Drop a file here or click to choose. PDF, Word, images, and text."}</span>
            </button>
          )}

          {error ? (
            <p role="alert" className="flex gap-2 rounded-lg border border-danger/25 bg-danger/[0.06] px-3 py-2 text-13 text-danger-ink">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              {error}
            </p>
          ) : null}

          <section aria-label="Your documents">
            <p className="eyebrow mb-2">Your documents{artifacts.length ? ` · ${artifacts.length}` : ""}</p>
            {!loaded ? (
              <div className="space-y-2" aria-hidden="true">
                {[0, 1].map((index) => (
                  <div key={index} className="h-16 animate-pulse rounded-xl bg-fg/[0.04]" />
                ))}
              </div>
            ) : artifacts.length ? (
              <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line bg-surface-1">
                {artifacts.map((artifact) => {
                  const Icon = iconFor(artifact);
                  const name = artifactName(artifact);
                  const updated = parseBackendDate(artifact.updated_at);
                  return (
                    <li key={artifact.id} className="group/doc flex items-start gap-3 px-4 py-3">
                      <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-line bg-surface-2 text-fg-muted">
                        <Icon className="size-4" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-13 font-medium text-fg">{name}</p>
                        <p className="mt-0.5 truncate text-xs text-fg-subtle">
                          {roleLabel(artifactRole(artifact))} · {formatBytes(artifact.file_size)}
                          {Number.isNaN(updated.getTime()) ? "" : ` · ${formatRelative(updated)}`}
                          {artifact.filename && artifact.filename !== name ? ` · ${artifact.filename}` : ""}
                        </p>
                        {artifact.text_preview ? <p className="mt-1.5 line-clamp-2 text-xs text-fg-muted">{artifact.text_preview}</p> : null}
                      </div>
                      <div className="flex shrink-0 items-center gap-0.5 opacity-60 transition group-hover/doc:opacity-100 group-focus-within/doc:opacity-100">
                        {artifact.storage_path ? (
                          <Tooltip label="Download">
                            <Button
                              variant="ghost"
                              size="icon-sm"
                              aria-label={`Download ${name}`}
                              onClick={() => window.open(api.applicationArtifactDownloadUrl(artifact.id), "_blank", "noopener,noreferrer")}
                            >
                              <Download />
                            </Button>
                          </Tooltip>
                        ) : null}
                        <ConfirmButton size="icon-sm" icon={<Trash2 />} ariaLabel={`Delete ${name}`} onConfirm={() => deleteArtifact(artifact)} />
                      </div>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="rounded-xl border border-dashed border-line-strong px-4 py-6 text-center text-13 text-fg-subtle">
                Nothing here yet. Add your résumé so Lumen can attach it to applications.
              </p>
            )}
          </section>
        </SheetBody>
      </SheetContent>
    </Sheet>
  );
}
