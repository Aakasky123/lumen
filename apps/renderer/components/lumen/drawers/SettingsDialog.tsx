"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import QRCode from "qrcode";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { AlertTriangle, Check, Copy, Cpu, Eye, Keyboard, KeyRound, ListChecks, Monitor, Moon, Palette, Plus, ShieldCheck, Smartphone, Sun, SunMoon, TrendingUp, X } from "lucide-react";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/Button";
import { Kbd, StatusDot } from "@/components/ui/badge";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { Field } from "@/components/ui/field";
import { Input, Select } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/components/ui/toast";
import { api, type AgentSettings, type DevicePairingCode, type ModelProvider, type ModelProviderType, type PairedDevice, type RemoteAccessStatus } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { useApprovalMode } from "@/lib/hooks/useApprovalMode";
import type { DesktopStatusState } from "@/lib/hooks/useDesktopStatus";
import { useTheme, type ThemePreference } from "@/lib/theme";
import { APPROVAL_OPTIONS } from "@/components/lumen/chat/ActionApprovalSelector";
import { cn } from "@/lib/utils";
import { desktopName, platformKeys, useIsMac } from "@/lib/platform";

export type SettingsSection = "approvals" | "models" | "appearance" | "desktop" | "devices" | "shortcuts";

const FALLBACK_TYPES: (ModelProviderType | string)[] = ["deepseek", "openai", "anthropic", "gemini", "openrouter", "custom_openai_compatible"];
const TYPE_LABELS: Record<string, string> = {
  deepseek: "DeepSeek",
  openai: "OpenAI",
  anthropic: "Anthropic",
  gemini: "Gemini",
  openrouter: "OpenRouter",
  custom_openai_compatible: "Custom (OpenAI-compatible)"
};

function SectionHeading({ title, description }: { title: string; description?: ReactNode }) {
  return (
    <div className="mb-5">
      <h2 className="text-lg font-semibold tracking-tight text-fg">{title}</h2>
      {description ? <p className="mt-1 text-13 text-fg-muted">{description}</p> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------
// Approvals

function ApprovalsSection({ open }: { open: boolean }) {
  const { mode, saving, error, update } = useApprovalMode(open);
  const [attestationConsent, setAttestationConsent] = useState(false);
  const [attestationBusy, setAttestationBusy] = useState(false);
  const [relayConsent, setRelayConsent] = useState(false);
  const [relayBusy, setRelayBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    let active = true;
    api
      .agentSettings()
      .then((r) => {
        if (!active) return;
        setAttestationConsent(Boolean(r.settings.autonomous_attestation_consent));
        setRelayConsent(Boolean(r.settings.autonomous_relay_consent));
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [open]);

  const toggle = async (key: "autonomous_attestation_consent" | "autonomous_relay_consent", next: boolean) => {
    const set = key === "autonomous_attestation_consent" ? setAttestationConsent : setRelayConsent;
    const setBusy = key === "autonomous_attestation_consent" ? setAttestationBusy : setRelayBusy;
    set(next);
    setBusy(true);
    try {
      const response = await api.updateAgentSettings({ [key]: next });
      set(Boolean(response.settings[key]));
    } catch {
      set(!next);
      toast("Couldn't save that setting", { tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <SectionHeading title="Approvals" description="Whether Lumen pauses before the consequential final step of each new task." />
      <div role="radiogroup" aria-label="Approval mode" className="space-y-2">
        {APPROVAL_OPTIONS.map((option) => {
          const selected = mode === option.mode;
          const risky = option.mode === "autonomous";
          return (
            <button
              key={option.mode}
              type="button"
              role="radio"
              aria-checked={selected}
              disabled={saving}
              onClick={() => void update(option.mode)}
              className={cn(
                "flex w-full items-start gap-3.5 rounded-xl border px-4 py-3.5 text-left transition focus-ring disabled:opacity-60",
                selected ? (risky ? "border-attention/45 bg-attention/[0.06]" : "border-brand/45 bg-brand/[0.05]") : "border-line hover:border-line-strong hover:bg-fg/[0.02]"
              )}
            >
              <span
                className={cn(
                  "mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg border",
                  risky ? "border-attention/30 bg-attention/10 text-attention-ink" : "border-brand/30 bg-brand/10 text-brand-ink"
                )}
              >
                <option.icon className="size-4" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold text-fg">{option.title}</span>
                <span className="mt-0.5 block text-13 text-fg-muted">{option.description}</span>
              </span>
              <span
                className={cn(
                  "mt-1 flex size-[18px] shrink-0 items-center justify-center rounded-full border transition",
                  selected ? (risky ? "border-attention bg-attention" : "border-brand bg-brand") : "border-line-strong"
                )}
              >
                {selected ? <Check className="size-3 text-brand-fg" strokeWidth={3} /> : null}
              </span>
            </button>
          );
        })}
      </div>
      {error ? <p className="mt-2 text-13 text-danger-ink">{error}</p> : null}

      {mode === "autonomous" ? (
        <div className="mt-6 animate-enter-up space-y-3">
          <p className="flex gap-2.5 rounded-xl border border-attention/30 bg-attention/[0.07] px-4 py-3 text-13 text-fg">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-attention-ink" />
            <span>Full autonomy applies to tasks you start while it&rsquo;s on. Lumen can make external commitments on your behalf; anything that spends money still waits for you.</span>
          </p>
          <p className="eyebrow pt-2">Also allow Lumen to</p>
          {[
            {
              key: "autonomous_attestation_consent" as const,
              checked: attestationConsent,
              busy: attestationBusy,
              title: "Confirm read-and-agree checkboxes",
              description: "Tick attestations such as “I have read the terms” for you instead of asking."
            },
            {
              key: "autonomous_relay_consent" as const,
              checked: relayConsent,
              busy: relayBusy,
              title: "Carry values between a page and the apps it opens",
              description:
                "Copy a reference code or ID an app shows into the page that opened it, back, or into another of the task's apps, without asking. Passwords, keys and tokens, one-time and recovery codes, and payment details are still left to you."
            }
          ].map((consent) => (
            <div key={consent.key} className="flex items-start justify-between gap-4 rounded-xl border border-line px-4 py-3.5">
              <div>
                <p className="text-sm font-medium text-fg">{consent.title}</p>
                <p className="mt-0.5 text-13 text-fg-muted">{consent.description}</p>
              </div>
              <Switch
                tone="attention"
                checked={consent.checked}
                disabled={consent.busy}
                onCheckedChange={(next) => void toggle(consent.key, next)}
                aria-label={consent.title}
                className="mt-0.5"
              />
            </div>
          ))}
        </div>
      ) : null}

      <p className="mt-6 text-xs text-fg-subtle">Both modes still require grounded facts, an exactly bound target, and a verified outcome.</p>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------
// Models

// The three model seats. Brain: decides every step and answers chat (setting it also makes the row the active
// provider). Checker: the quick checks, the second opinion on sensitive values, and the backup when the Brain's
// provider fails. Escalation: takes over a part of a task the Brain keeps failing.
type RoleKey = "brain" | "checker" | "escalation";
const SEAT_FIELDS: Record<RoleKey, "vision_provider_id" | "judge_provider_id" | "vision_escalation_provider_id"> = {
  brain: "vision_provider_id",
  checker: "judge_provider_id",
  escalation: "vision_escalation_provider_id"
};
const SEAT_NAMES: Record<RoleKey, string> = { brain: "Brain", checker: "Checker", escalation: "escalation model" };

// Provider types that can carry a screenshot. Every seat reads screenshots (the Checker as the Brain's backup),
// so a text-only type (DeepSeek's own API) fills none of them.
const IMAGE_TYPES = new Set(["openai", "anthropic", "gemini", "openrouter", "custom_openai_compatible"]);

// Rows serving the same model at the same endpoint are the same model (the Checker is never the Brain's model).
function modelKey(provider: ModelProvider | undefined): string | null {
  if (!provider || !provider.model_name.trim()) return null;
  return [provider.provider_type, (provider.base_url || "").trim().replace(/\/+$/, ""), provider.model_name.trim()].join("|").toLowerCase();
}

function ModelsSection({ open }: { open: boolean }) {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [supportedTypes, setSupportedTypes] = useState<(ModelProviderType | string)[]>(FALLBACK_TYPES);
  const [brainId, setBrainId] = useState<number | null>(null);
  const [checkerId, setCheckerId] = useState<number | null>(null);
  const [escalationId, setEscalationId] = useState<number | null>(null);
  // Which seat is being saved, and for which row.
  const [busy, setBusy] = useState<{ role: RoleKey; id: number | null } | null>(null);
  const [strictModels, setStrictModels] = useState(false);
  const [strictBusy, setStrictBusy] = useState(false);

  // "Add a model" form state
  const [adding, setAdding] = useState(false);
  const [providerType, setProviderType] = useState<string>("openai");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [loadingModels, setLoadingModels] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  // Another model on a service already saved here can reuse that provider's key (it stays on the server).
  const [reuseKeyFrom, setReuseKeyFrom] = useState<number | null>(null);
  const savedKeySources = providers.filter((p) => p.id != null && p.has_api_key && p.provider_type === providerType && providerType !== "custom_openai_compatible");
  const reuseSource = savedKeySources.find((p) => p.id === reuseKeyFrom) ?? null;

  const applySeats = (settings: AgentSettings) => {
    setBrainId(settings.vision_provider_id ?? null);
    setCheckerId(settings.judge_provider_id ?? null);
    setEscalationId(settings.vision_escalation_provider_id ?? null);
  };

  const refreshProviders = async () => {
    const response = await api.modelProviders();
    setProviders(response.items);
    setActiveId(response.active?.id ?? null);
    if (response.supported_provider_types?.length) setSupportedTypes(response.supported_provider_types);
    setLoaded(true);
  };

  const refreshSeats = async () => {
    const { settings } = await api.agentSettings();
    applySeats(settings);
    setStrictModels(Boolean(settings.strict_model_provider_allowlist));
  };

  useEffect(() => {
    if (!open) return;
    let active = true;
    api
      .modelProviders()
      .then((r) => {
        if (!active) return;
        setProviders(r.items);
        setActiveId(r.active?.id ?? null);
        if (r.supported_provider_types?.length) setSupportedTypes(r.supported_provider_types);
        setLoaded(true);
        if (!r.items.length) setAdding(true);
      })
      .catch(() => setLoaded(true));
    api
      .agentSettings()
      .then((r) => {
        if (!active) return;
        applySeats(r.settings);
        setStrictModels(Boolean(r.settings.strict_model_provider_allowlist));
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [open]);

  // One seat at a time. The server keeps the invariants (the Checker is never the Brain's model, Escalation is
  // neither) and clears a seat that would break one, so every seat is read back from its reply.
  const setSeat = async (role: RoleKey, id: number | null) => {
    const prev = { brain: brainId, checker: checkerId, escalation: escalationId };
    setBusy({ role, id });
    if (role === "brain") setBrainId(id);
    if (role === "checker") setCheckerId(id);
    if (role === "escalation") setEscalationId(id);
    // The row leaves any lower seat it held, as the server will clear it.
    if (id != null && role === "brain" && id === checkerId) setCheckerId(null);
    if (id != null && role !== "escalation" && id === escalationId) setEscalationId(null);
    try {
      const { settings } = await api.updateAgentSettings({ [SEAT_FIELDS[role]]: id });
      applySeats(settings);
      // Setting the Brain also makes its row the active provider.
      if (role === "brain") await refreshProviders();
    } catch (err) {
      setBrainId(prev.brain);
      setCheckerId(prev.checker);
      setEscalationId(prev.escalation);
      toast(`Couldn't change the ${SEAT_NAMES[role]}`, {
        tone: "error",
        ...(err instanceof Error ? { detail: errorDetail(err.message) } : {})
      });
    } finally {
      setBusy(null);
    }
  };

  const setStrict = async (next: boolean) => {
    const prev = strictModels;
    setStrictBusy(true);
    setStrictModels(next);
    try {
      const { settings } = await api.updateAgentSettings({ strict_model_provider_allowlist: next });
      setStrictModels(Boolean(settings.strict_model_provider_allowlist));
    } catch {
      setStrictModels(prev);
      toast("Couldn't change the model setting", { tone: "error" });
    } finally {
      setStrictBusy(false);
    }
  };

  // Provide a key (or reuse a saved one), ask the provider for its models, fill the list.
  const loadModels = async () => {
    if (!reuseSource && !apiKey.trim()) {
      setFormError("Enter an API key first.");
      return;
    }
    setLoadingModels(true);
    setFormError(null);
    setModels([]);
    setSelectedModel("");
    try {
      const { models: fetched } = await api.availableModels(
        reuseSource?.id != null
          ? { provider_type: providerType, key_from_provider_id: reuseSource.id }
          : { provider_type: providerType, api_key: apiKey.trim(), base_url: baseUrl.trim() || undefined }
      );
      if (!fetched.length) {
        setFormError("That key works, but the provider listed no models.");
        return;
      }
      setModels(fetched);
      setSelectedModel(fetched[0]);
    } catch (err) {
      setFormError(err instanceof Error ? errorDetail(err.message) : "Couldn't reach the provider.");
    } finally {
      setLoadingModels(false);
    }
  };

  const resetForm = () => {
    setApiKey("");
    setBaseUrl("");
    setModels([]);
    setSelectedModel("");
    setReuseKeyFrom(null);
    setFormError(null);
  };

  const addModel = async () => {
    if (!selectedModel) return;
    setSaving(true);
    setFormError(null);
    try {
      const created = await api.createModelProvider({
        display_name: `${TYPE_LABELS[providerType] || providerType} · ${selectedModel}`,
        provider_type: providerType,
        model_name: selectedModel,
        ...(reuseSource?.id != null ? { key_from_provider_id: reuseSource.id } : { api_key: apiKey.trim(), base_url: baseUrl.trim() || undefined }),
        temperature: 0.2,
        max_tokens: 2048,
        timeout_seconds: 30,
        enabled: true
      });
      // Only the first model becomes the active row (and the Brain, when it can read a screenshot)
      // automatically, so adding a second model never takes over the Brain. Seats are assigned on each row.
      if (created.provider.id != null && activeId == null) await api.activateModelProvider(created.provider.id);
      await refreshProviders();
      await refreshSeats();
      resetForm();
      setAdding(false);
      toast("Model added", { detail: selectedModel });
    } catch (err) {
      setFormError(err instanceof Error ? errorDetail(err.message) : "Couldn't add the model.");
    } finally {
      setSaving(false);
    }
  };

  const nameOf = (id: number | null) => providers.find((provider) => provider.id === id)?.display_name || "";
  const roles: Array<{ key: RoleKey; label: string; icon: typeof Cpu; value: string; detail: string }> = [
    { key: "brain", label: "Brain", icon: Eye, value: nameOf(brainId), detail: "Reads the screen, decides every step of a task, and answers chat." },
    {
      key: "checker",
      label: "Checker",
      icon: ListChecks,
      value: nameOf(checkerId),
      detail: "Runs the quick checks around each action, double-checks uncertain sensitive values, and takes over if the Brain fails."
    },
    { key: "escalation", label: "Escalation", icon: TrendingUp, value: nameOf(escalationId), detail: "Takes over a step the Brain keeps failing, then hands it back." }
  ];
  const brainKey = modelKey(providers.find((provider) => provider.id === brainId));

  const rolePill = (on: boolean) =>
    cn(
      "inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs font-medium transition focus-ring disabled:cursor-default [&_svg]:size-3",
      on ? "border-brand/40 bg-brand/12 text-brand-ink" : "border-line text-fg-muted hover:border-line-strong hover:text-fg"
    );

  return (
    <div>
      <SectionHeading
        title="Models"
        description="Lumen uses three models: a Brain, a Checker, and an Escalation model. Each must be a different model. Assign them on any model below."
      />

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        {roles.map((role) => (
          <div key={role.key} className="rounded-xl border border-line px-3.5 py-3">
            <p className="flex items-center gap-1.5 text-xs font-medium text-fg-subtle">
              <role.icon className="size-3.5" /> {role.label}
            </p>
            <p className={cn("mt-1 truncate text-13 font-medium", role.value ? "text-fg" : "text-fg-subtle")}>{role.value || "Not set"}</p>
            <p className="mt-0.5 text-2xs leading-4 text-fg-subtle">{role.detail}</p>
          </div>
        ))}
      </div>

      <div className="mt-2 flex items-start justify-between gap-4 rounded-xl border border-line px-4 py-3.5">
        <div>
          <p className="text-sm font-medium text-fg">Use only these models</p>
          <p className="mt-0.5 text-13 text-fg-muted">Never fall back to a key from the app&rsquo;s environment file.</p>
        </div>
        <Switch
          checked={strictModels}
          disabled={strictBusy}
          onCheckedChange={(next) => void setStrict(next)}
          aria-label="Use only these models"
          className="mt-0.5"
        />
      </div>

      <div className="mb-2 mt-7 flex items-center justify-between">
        <p className="eyebrow">Your models{providers.length ? ` · ${providers.length}` : ""}</p>
        {!adding ? (
          <Button variant="ghost" size="xs" onClick={() => setAdding(true)}>
            <Plus /> Add a model
          </Button>
        ) : null}
      </div>

      {adding ? (
        <div className="mb-3 animate-enter-up rounded-xl border border-line-strong bg-surface-1 p-4 shadow-sm">
          <div className="mb-4 flex items-center justify-between">
            <p className="text-sm font-semibold text-fg">Add a model</p>
            {providers.length ? (
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Cancel adding a model"
                onClick={() => {
                  resetForm();
                  setAdding(false);
                }}
              >
                <X />
              </Button>
            ) : null}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field id="model-service" label="Service">
              <Select
                value={providerType}
                onChange={(e) => {
                  setProviderType(e.target.value);
                  setModels([]);
                  setSelectedModel("");
                  setFormError(null);
                  setReuseKeyFrom(null);
                }}
              >
                {supportedTypes.map((t) => (
                  <option key={t} value={t}>
                    {TYPE_LABELS[t] || t}
                  </option>
                ))}
              </Select>
            </Field>
            {reuseSource ? (
              <div className="flex items-end">
                <p className="flex h-9 items-center gap-1.5 text-13 text-fg-muted">
                  <KeyRound className="size-3.5" /> Using the saved key
                </p>
              </div>
            ) : (
              <Field id="model-key" label="API key">
                <Input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  onBlur={() => {
                    if (apiKey.trim() && !models.length && !loadingModels) void loadModels();
                  }}
                  placeholder="Paste your key"
                  autoComplete="off"
                />
              </Field>
            )}
            {savedKeySources.length ? (
              <label className="col-span-2 flex cursor-pointer items-center gap-2 text-13 text-fg-muted">
                <input
                  type="checkbox"
                  checked={reuseSource != null}
                  onChange={(e) => {
                    setReuseKeyFrom(e.target.checked ? savedKeySources[0].id : null);
                    setModels([]);
                    setSelectedModel("");
                    setFormError(null);
                  }}
                  className="size-3.5 accent-[hsl(var(--brand))]"
                />
                Reuse the key saved with {savedKeySources[0].display_name}
              </label>
            ) : null}
            {providerType === "custom_openai_compatible" ? (
              <Field id="model-base-url" label="Base URL" className="col-span-2">
                <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://…/v1" />
              </Field>
            ) : null}
            {models.length ? (
              <Field id="model-name" label="Model" className="col-span-2">
                <Select value={selectedModel} onChange={(e) => setSelectedModel(e.target.value)}>
                  {models.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </Select>
              </Field>
            ) : null}
          </div>
          {formError ? <p className="mt-3 text-13 text-danger-ink">{formError}</p> : null}
          <div className="mt-4 flex items-center gap-2">
            {models.length ? (
              <>
                <Button variant="primary" size="sm" onClick={addModel} loading={saving} disabled={!selectedModel}>
                  Add model
                </Button>
                <Button variant="ghost" size="sm" onClick={loadModels} disabled={loadingModels}>
                  Reload list
                </Button>
              </>
            ) : (
              <Button variant="primary" size="sm" onClick={loadModels} loading={loadingModels} disabled={!reuseSource && !apiKey.trim()}>
                Find models
              </Button>
            )}
            <span className="text-xs text-fg-subtle">Keys are stored encrypted on this computer.</span>
          </div>
        </div>
      ) : null}

      {!loaded ? (
        <div className="space-y-2" aria-hidden="true">
          {[0, 1].map((index) => (
            <div key={index} className="h-16 animate-pulse rounded-xl bg-fg/[0.04]" />
          ))}
        </div>
      ) : providers.length ? (
        <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line">
          {providers.map((provider) => {
            const id = provider.id;
            const isBrain = id != null && id === brainId;
            const isChecker = id != null && id === checkerId;
            const isEscalation = id != null && id === escalationId;
            const canImage = IMAGE_TYPES.has(provider.provider_type);
            // The Checker double-checks the Brain, so it is never the Brain's model (on any row).
            const brainsModel = isBrain || (brainKey != null && modelKey(provider) === brainKey);
            const busyHere = (role: RoleKey) => busy?.role === role && busy.id === id;
            const healthy = provider.health_status === "healthy";
            return (
              <li key={id ?? provider.display_name} className="flex items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-2 truncate text-13 font-medium text-fg">
                    {provider.display_name}
                    {provider.health_status && provider.health_status !== "unknown" ? (
                      <span title={provider.last_error || (healthy ? "Healthy" : provider.health_status)}>
                        <StatusDot tone={healthy ? "brand" : "attention"} />
                      </span>
                    ) : null}
                  </p>
                  <p className="truncate text-xs text-fg-subtle">
                    {provider.model_name} · {TYPE_LABELS[provider.provider_type] || provider.provider_type}
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap items-center justify-end gap-1">
                  {canImage ? (
                    <>
                      <button
                        type="button"
                        disabled={isBrain || busy != null}
                        onClick={() => id != null && setSeat("brain", id)}
                        title={isBrain ? "The Brain" : "Use as the Brain: it decides every step and answers chat"}
                        className={rolePill(isBrain)}
                      >
                        {isBrain ? <Eye /> : null}
                        {busyHere("brain") ? "…" : "Brain"}
                      </button>
                      {!brainsModel ? (
                        <button
                          type="button"
                          disabled={busy != null}
                          onClick={() => id != null && setSeat("checker", isChecker ? null : id)}
                          title={
                            isChecker
                              ? "The Checker (click to clear)"
                              : "Use as the Checker: it runs the quick checks, double-checks sensitive values, and backs up the Brain"
                          }
                          className={rolePill(isChecker)}
                        >
                          {isChecker ? <ListChecks /> : null}
                          {busyHere("checker") ? "…" : "Checker"}
                        </button>
                      ) : null}
                      {!isBrain && !isChecker ? (
                        <button
                          type="button"
                          disabled={busy != null}
                          onClick={() => id != null && setSeat("escalation", isEscalation ? null : id)}
                          title={isEscalation ? "The escalation model (click to clear)" : "Use as the escalation model: it takes over a step the Brain keeps failing"}
                          className={rolePill(isEscalation)}
                        >
                          {isEscalation ? <TrendingUp /> : null}
                          {busyHere("escalation") ? "…" : "Escalate"}
                        </button>
                      ) : null}
                    </>
                  ) : (
                    <span className="px-1 text-2xs text-fg-disabled" title="This provider's API can't read images, and every seat reads the screen">
                      No vision
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      ) : !adding ? (
        <p className="rounded-xl border border-dashed border-line-strong px-4 py-6 text-center text-13 text-fg-subtle">No models yet. Add one to start.</p>
      ) : null}
    </div>
  );
}

function errorDetail(message: string): string {
  try {
    const parsed = JSON.parse(message) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail.trim();
  } catch {
    // Plain-text error.
  }
  return message;
}

// ---------------------------------------------------------------------------------------------------
// Appearance

const THEME_OPTIONS: Array<{ value: ThemePreference; label: string; icon: typeof Sun }> = [
  { value: "dark", label: "Dark", icon: Moon },
  { value: "light", label: "Light", icon: Sun },
  { value: "system", label: "Match system", icon: SunMoon }
];

function ThemePreview({ mode }: { mode: "dark" | "light" | "split" }) {
  const pane = (dark: boolean) => (
    <div className={cn("flex h-full flex-1 gap-1 p-1.5", dark ? "bg-[#0b0d0c]" : "bg-[#f7faf9]")}>
      <div className={cn("w-1/4 rounded-sm", dark ? "bg-[#121614]" : "bg-[#ecf1ef]")} />
      <div className="flex flex-1 flex-col justify-end gap-1">
        <div className={cn("ml-auto h-1.5 w-1/2 rounded-full", dark ? "bg-[#232a27]" : "bg-[#dfe6e3]")} />
        <div className={cn("h-1.5 w-3/4 rounded-full", dark ? "bg-[#1a201d]" : "bg-[#e7ece9]")} />
        <div className={cn("h-3 rounded-sm border", dark ? "border-[#2a322e] bg-[#141917]" : "border-[#d5ddd9] bg-white")} />
      </div>
    </div>
  );
  return (
    <div className="flex h-20 overflow-hidden rounded-lg border border-line">
      {mode === "split" ? (
        <>
          {pane(true)}
          {pane(false)}
        </>
      ) : (
        pane(mode === "dark")
      )}
    </div>
  );
}

function AppearanceSection() {
  const { preference, setPreference } = useTheme();
  return (
    <div>
      <SectionHeading title="Appearance" description="How the Lumen window looks. The on-screen pointer and status pill stay dark so they read over any app." />
      <div role="radiogroup" aria-label="Theme" className="grid grid-cols-3 gap-3">
        {THEME_OPTIONS.map((option) => {
          const selected = preference === option.value;
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => setPreference(option.value)}
              className={cn(
                "rounded-xl border p-2 text-left transition focus-ring",
                selected ? "border-brand/60 bg-brand/[0.05] shadow-[0_0_0_1px_hsl(var(--brand)/0.4)]" : "border-line hover:border-line-strong"
              )}
            >
              <ThemePreview mode={option.value === "system" ? "split" : option.value} />
              <span className="mt-2 flex items-center gap-1.5 px-1 text-13 font-medium text-fg">
                <option.icon className="size-3.5 text-fg-muted" />
                {option.label}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------
// Desktop

function DesktopSection({ desktop }: { desktop: DesktopStatusState }) {
  const isMac = useIsMac();
  const status = desktop.status;
  const rows = [
    { label: "Runtime", detail: "Lumen's local backend", value: status?.backendHealthy ? "Running" : "Offline", ok: Boolean(status?.backendHealthy) },
    {
      label: "Model",
      detail: status?.modelError || "The model Lumen thinks with",
      value: status?.modelReady ? status.modelDisplayName || "Ready" : status?.modelConfigured ? "Not responding" : "Not connected",
      ok: Boolean(status?.modelReady)
    },
    {
      label: "Chrome extension",
      detail: "Lumen's hands in your browser",
      value: status?.liveActionsReady ? "Connected" : status?.extensionConnected ? "Needs a reload" : "Opens when a task needs it",
      ok: Boolean(status?.liveActionsReady)
    }
  ];
  return (
    <div>
      <SectionHeading title="Desktop" description={`The pieces Lumen runs on, and whether it may use ${desktopName(isMac)} apps.`} />
      <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line">
        {rows.map((row) => (
          <li key={row.label} className="flex items-center gap-3 px-4 py-3">
            <div className="min-w-0 flex-1">
              <p className="text-13 font-medium text-fg">{row.label}</p>
              <p className="truncate text-xs text-fg-subtle">{row.detail}</p>
            </div>
            <span className="flex items-center gap-2 text-13 text-fg-muted">
              {row.value}
              <StatusDot tone={row.ok ? "brand" : "attention"} />
            </span>
          </li>
        ))}
        <li className="flex items-center gap-3 px-4 py-3">
          <div className="min-w-0 flex-1">
            <p className="text-13 font-medium text-fg">Computer use</p>
            <p className="text-xs text-fg-subtle">Let Lumen use {desktopName(isMac)} apps with the mouse and keyboard.</p>
          </div>
          <Switch
            checked={Boolean(status?.computerUseEnabled)}
            disabled={desktop.busy || !status?.computerUseAvailable}
            onCheckedChange={(next) => void desktop.setComputerUse(next)}
            aria-label={`Let Lumen use ${desktopName(isMac)} apps`}
          />
        </li>
      </ul>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" loading={desktop.busy} onClick={() => void desktop.runRuntime(status?.backendHealthy ? "restart" : "start")}>
          {status?.backendHealthy ? "Restart runtime" : "Start runtime"}
        </Button>
        <Button variant="secondary" size="sm" onClick={desktop.openChromeExtensions}>
          Open Chrome extensions
        </Button>
        <Button variant="secondary" size="sm" onClick={desktop.openDataFolder}>
          Open data folder
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------
// Devices (paired phones)

function PairingQr({ uri }: { uri: string }) {
  const [svg, setSvg] = useState("");
  useEffect(() => {
    let active = true;
    // Dark modules on white, whatever the theme: phone cameras read that most reliably.
    QRCode.toString(uri, { type: "svg", margin: 1, errorCorrectionLevel: "M", color: { dark: "#0b0f0e", light: "#ffffff" } })
      .then((markup) => {
        if (active) setSvg(markup);
      })
      .catch(() => {
        if (active) setSvg("");
      });
    return () => {
      active = false;
    };
  }, [uri]);
  return (
    <div
      role="img"
      aria-label="Pairing QR code"
      className="size-44 shrink-0 overflow-hidden rounded-xl bg-white p-2 [&_svg]:size-full"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}

function DevicesSection({ open }: { open: boolean }) {
  const [status, setStatus] = useState<RemoteAccessStatus | null>(null);
  const [devices, setDevices] = useState<PairedDevice[]>([]);
  const [pairing, setPairing] = useState<DevicePairingCode | null>(null);
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [busy, setBusy] = useState(false);
  const knownIds = useRef<Set<number>>(new Set());

  const loadDevices = useCallback(async () => {
    const response = await api.pairedDevices();
    setDevices(response.devices);
    return response.devices;
  }, []);

  useEffect(() => {
    if (!open) return;
    let active = true;
    api
      .remoteAccessStatus()
      .then((r) => {
        if (active) setStatus(r);
      })
      .catch(() => {});
    loadDevices()
      .then((list) => {
        knownIds.current = new Set(list.map((device) => device.id));
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [open, loadDevices]);

  // While a code is showing: count down, and watch for the phone that uses it.
  useEffect(() => {
    if (!pairing) return;
    const expiresAt = new Date(pairing.expires_at).getTime();
    const remaining = () => Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
    setSecondsLeft(remaining());
    const tick = window.setInterval(() => setSecondsLeft(remaining()), 1000);
    const poll = window.setInterval(() => {
      loadDevices()
        .then((list) => {
          const fresh = list.find((device) => !knownIds.current.has(device.id));
          knownIds.current = new Set(list.map((device) => device.id));
          if (fresh) {
            setPairing(null);
            toast(`Paired ${fresh.name}`, { tone: "success" });
          }
        })
        .catch(() => {});
    }, 2500);
    return () => {
      window.clearInterval(tick);
      window.clearInterval(poll);
    };
  }, [pairing, loadDevices]);

  const showCode = async () => {
    setBusy(true);
    try {
      const code = await api.createPairingCode();
      setPairing(code);
      setStatus(await api.remoteAccessStatus());
    } catch {
      toast("Couldn't create a pairing code", { tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (device: PairedDevice) => {
    try {
      await api.revokePairedDevice(device.id);
      await loadDevices();
      toast(`${device.name} can no longer reach Lumen`);
    } catch {
      toast("Couldn't remove that device", { tone: "error" });
    }
  };

  const copyCommand = async () => {
    if (!status) return;
    try {
      await navigator.clipboard.writeText(status.serve_command);
      toast("Command copied");
    } catch {
      toast("Couldn't copy", { tone: "error" });
    }
  };

  const expired = pairing !== null && secondsLeft <= 0;
  const rows = status
    ? [
        {
          label: "Phone access",
          detail: status.ready ? status.public_url || "" : status.problem || "",
          value: status.ready ? "Ready" : "Not ready",
          ok: status.ready
        },
        {
          label: "Alerts",
          detail: "Push notifications when Lumen needs you or finishes",
          value: status.push.configured ? "On" : "Not set up",
          ok: status.push.configured
        }
      ]
    : [];

  return (
    <div>
      <SectionHeading
        title="Devices"
        description="Paired phones can start tasks, follow runs, and answer Lumen over your private Tailscale network. Payments, sign-ins, and setting changes also need the phone's fingerprint check."
      />
      {rows.length ? (
        <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line">
          {rows.map((row) => (
            <li key={row.label} className="flex items-center gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <p className="text-13 font-medium text-fg">{row.label}</p>
                <p className="truncate text-xs text-fg-subtle">{row.detail}</p>
              </div>
              <span className="flex items-center gap-2 text-13 text-fg-muted">
                {row.value}
                <StatusDot tone={row.ok ? "brand" : "attention"} />
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {status && !status.ready && status.tailscale.logged_in && status.listener.running ? (
        <div className="mt-3 flex items-center gap-2 rounded-xl border border-attention/30 bg-attention/[0.07] px-4 py-3">
          <code className="min-w-0 flex-1 truncate font-mono text-xs text-fg">{status.serve_command}</code>
          <Button variant="ghost" size="xs" onClick={() => void copyCommand()}>
            <Copy />
            Copy
          </Button>
        </div>
      ) : null}

      <p className="eyebrow mb-2 mt-6">Paired phones</p>
      {devices.length ? (
        <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line">
          {devices.map((device) => (
            <li key={device.id} className="flex items-center gap-3 px-4 py-3">
              <Smartphone className="size-4 shrink-0 text-fg-subtle" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-13 font-medium text-fg">{device.name}</p>
                <p className="text-xs text-fg-subtle">
                  {device.last_seen_at ? `Seen ${formatRelative(new Date(device.last_seen_at))}` : "Not seen yet"}
                  {device.has_push ? " · Alerts on" : ""}
                </p>
              </div>
              <ConfirmButton size="xs" confirmLabel="Remove?" onConfirm={() => revoke(device)}>
                Remove
              </ConfirmButton>
            </li>
          ))}
        </ul>
      ) : (
        <p className="rounded-xl border border-dashed border-line px-4 py-3 text-13 text-fg-muted">No phones yet.</p>
      )}

      {pairing && !expired ? (
        <div className="mt-4 flex animate-enter-up items-center gap-5 rounded-xl border border-brand/35 bg-brand/[0.05] p-4">
          {pairing.pairing_uri ? <PairingQr uri={pairing.pairing_uri} /> : null}
          <div className="min-w-0 flex-1 space-y-2">
            <p className="text-sm font-semibold text-fg">Scan with the Lumen app</p>
            <p className="text-13 text-fg-muted">
              The phone must be on the same Tailscale network. This code works once and expires in{" "}
              <span className="font-medium tabular-nums text-fg">
                {Math.floor(secondsLeft / 60)}:{String(secondsLeft % 60).padStart(2, "0")}
              </span>
              .
            </p>
            {!pairing.ready && pairing.problem ? <p className="text-13 text-attention-ink">{pairing.problem}</p> : null}
            <Button variant="ghost" size="xs" onClick={() => setPairing(null)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-4 flex items-center gap-3">
          <Button variant="secondary" size="sm" loading={busy} disabled={Boolean(status && !status.ready)} onClick={() => void showCode()}>
            <Plus />
            {expired ? "Show a new code" : "Pair a phone"}
          </Button>
          {expired ? <span className="text-13 text-fg-muted">That code expired.</span> : null}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------
// Shortcuts

const SHORTCUTS: Array<{ keys: string[]; label: string }> = [
  { keys: ["Ctrl", "K"], label: "Search chats and commands" },
  { keys: ["Ctrl", "Shift", "O"], label: "New task" },
  { keys: ["Ctrl", "B"], label: "Show or hide the sidebar" },
  { keys: ["Ctrl", ","], label: "Settings" },
  { keys: ["/"], label: "Focus the message box" },
  { keys: ["Enter"], label: "Send" },
  { keys: ["Shift", "Enter"], label: "New line" }
];

function ShortcutsSection() {
  const isMac = useIsMac();
  return (
    <div>
      <SectionHeading title="Keyboard shortcuts" />
      <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line">
        {SHORTCUTS.map((shortcut) => (
          <li key={shortcut.label} className="flex items-center justify-between gap-3 px-4 py-2.5 text-13 text-fg">
            {shortcut.label}
            <span className="flex items-center gap-1">
              {platformKeys(shortcut.keys, isMac).map((key) => (
                <Kbd key={key}>{key}</Kbd>
              ))}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------

export function SettingsDialog({
  open,
  onOpenChange,
  section,
  onSectionChange,
  desktop
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
  desktop: DesktopStatusState;
}) {
  const nav: Array<{ key: SettingsSection; label: string; icon: typeof Cpu; badge?: ReactNode }> = [
    { key: "approvals", label: "Approvals", icon: ShieldCheck },
    { key: "models", label: "Models", icon: Cpu },
    { key: "appearance", label: "Appearance", icon: Palette },
    ...(desktop.available
      ? [{ key: "desktop" as const, label: "Desktop", icon: Monitor, badge: desktop.problem ? <StatusDot tone={desktop.problem.kind === "runtime" ? "danger" : "attention"} /> : null }]
      : []),
    { key: "devices", label: "Devices", icon: Smartphone },
    { key: "shortcuts", label: "Shortcuts", icon: Keyboard }
  ];
  const current = nav.some((item) => item.key === section) ? section : "approvals";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/55 backdrop-blur-[2px] data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-1/2 z-50 flex h-[min(42rem,88vh)] w-[calc(100vw-2rem)] max-w-[56rem] -translate-x-1/2 -translate-y-1/2 overflow-hidden rounded-2xl border border-line-strong bg-popover shadow-lg duration-200 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-[0.98] data-[state=open]:zoom-in-[0.98]"
        >
          <nav aria-label="Settings sections" className="flex w-52 shrink-0 flex-col gap-0.5 border-r border-line bg-sidebar p-3">
            <DialogPrimitive.Title className="px-2.5 pb-3 pt-1.5 text-base font-semibold text-fg">Settings</DialogPrimitive.Title>
            {nav.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => onSectionChange(item.key)}
                aria-current={current === item.key ? "page" : undefined}
                className={cn(
                  "flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-13 transition focus-ring",
                  current === item.key ? "bg-fg/[0.08] font-medium text-fg" : "text-fg-muted hover:bg-fg/[0.05] hover:text-fg"
                )}
              >
                <item.icon className="size-4" />
                {item.label}
                {item.badge ? <span className="ml-auto">{item.badge}</span> : null}
              </button>
            ))}
          </nav>
          <div className="scroll-area relative min-w-0 flex-1 overflow-y-auto px-8 pb-10 pt-7">
            {current === "approvals" ? <ApprovalsSection open={open} /> : null}
            {current === "models" ? <ModelsSection open={open} /> : null}
            {current === "appearance" ? <AppearanceSection /> : null}
            {current === "desktop" ? <DesktopSection desktop={desktop} /> : null}
            {current === "devices" ? <DevicesSection open={open} /> : null}
            {current === "shortcuts" ? <ShortcutsSection /> : null}
          </div>
          <DialogPrimitive.Close className="absolute right-3.5 top-3.5 flex size-8 items-center justify-center rounded-md text-fg-subtle transition hover:bg-fg/[0.06] hover:text-fg focus-ring">
            <X className="size-4" />
            <span className="sr-only">Close</span>
          </DialogPrimitive.Close>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </Dialog>
  );
}

