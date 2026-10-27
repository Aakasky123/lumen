import { KeyRound, LockKeyhole, Monitor } from "lucide-react-native";
import { useEffect, useState } from "react";
import { Pressable, View } from "react-native";
import { runTargetLabel } from "@/lib/runPhase";
import { api, type CredentialVaultFields, type CredentialVaultItem } from "@/api";
import { errorMessage, type SensitiveEntry } from "@/hooks";
import { Banner, Button, CardShell, Field, Spinner, T } from "@/components/ui";
import { alpha, radius, useTheme } from "@/theme";

/**
 * A run that needs a password or one-time code typed directly hands entry to the user on the PC;
 * the value never passes through chat, a model, or this phone. The phone can only point you there
 * (or stop the task).
 */
export function DirectEntryCard({ entry, targetUrl, onCancel, busy }: { entry: SensitiveEntry; targetUrl: string; onCancel: () => void; busy: boolean }) {
  const site = runTargetLabel(targetUrl) || "the site";
  const desktop = entry.entrySurface === "desktop";
  return (
    <CardShell eyebrow="Secure entry" icon={LockKeyhole} tone="brand">
      <T variant="title">Finish this at your PC</T>
      <T variant="small">
        Passwords and one-time codes never go through chat, a model, or your phone. Lumen is waiting for you to type this one{" "}
        {desktop ? "into the app window on your PC" : `into Chrome on your PC, on ${site}`}, then press Continue there.
      </T>
      <Banner tone="brand" icon={Monitor}>
        Lumen on your PC shows exactly where to type it.
      </Banner>
      <Button label="Stop the task" variant="danger-ghost" disabled={busy} onPress={onCancel} style={{ alignSelf: "flex-start" }} />
    </CardShell>
  );
}

type Slot = keyof CredentialVaultFields;
const SLOT_LABELS: Record<Slot, string> = { username: "Username", email: "Email", password: "Password" };

function slotOf(value: string): Slot | null {
  return value === "username" || value === "email" || value === "password" ? value : null;
}

/**
 * A sign-in Lumen needs from the credential vault: save it here (fingerprint-confirmed, encrypted on
 * the PC) and Lumen continues. Mirrors the cockpit's CredentialVaultHandoffCard.
 */
export function VaultHandoffCard({
  runId,
  entry,
  targetUrl,
  onDone,
  onCancel
}: {
  runId: number;
  entry: SensitiveEntry;
  targetUrl: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { c } = useTheme();
  const requested = slotOf(entry.credentialSlot);
  const [items, setItems] = useState<CredentialVaultItem[] | null>(null);
  const [destination, setDestination] = useState<{ kind: "create" } | { kind: "update"; id: number } | null>(null);
  const [label, setLabel] = useState("");
  const [values, setValues] = useState<Record<Slot, string>>({ username: "", email: "", password: "" });
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bindingMissing = !entry.handoffId || !entry.credentialOrigin || !requested;
  const site = runTargetLabel(entry.credentialOrigin || targetUrl);

  useEffect(() => {
    let active = true;
    setItems(null);
    if (!entry.credentialOrigin) return;
    api
      .credentialVault(entry.credentialOrigin)
      .then(({ items: found }) => {
        if (!active) return;
        setItems(found);
        // With nothing saved for this site, a new account is the only choice.
        setDestination((current) => current ?? (found.length ? null : { kind: "create" }));
      })
      .catch(() => active && setError("Couldn't load the accounts saved for this site."));
    return () => {
      active = false;
    };
  }, [entry.credentialOrigin, entry.handoffId]);

  const submit = async () => {
    if (bindingMissing || !requested) return;
    if (!destination) {
      setError("Choose a new account or one you've saved.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (!saved) {
        const fields: CredentialVaultFields = {};
        (Object.keys(values) as Slot[]).forEach((slot) => {
          if (values[slot]) fields[slot] = values[slot];
        });
        if (!fields[requested]) {
          setError(`Enter the ${SLOT_LABELS[requested].toLowerCase()} Lumen asked for.`);
          return;
        }
        if (destination.kind === "create") {
          if (!label.trim()) {
            setError("Give the account a name.");
            return;
          }
          await api.createVaultItem({ origin: entry.credentialOrigin, account_label: label.trim(), fields, auto_use: true });
        } else {
          await api.updateVaultItem(destination.id, { fields, auto_use: true });
        }
        setValues({ username: "", email: "", password: "" });
        setSaved(true);
      }
      await api.sensitiveContinue(runId, entry.handoffId);
      onDone();
    } catch (failure) {
      const message = errorMessage(failure, "Couldn't save these. Check the values and try again.");
      if (message) setError(saved ? "Saved, but Lumen couldn't continue yet. Try again." : message);
    } finally {
      setBusy(false);
    }
  };

  const choice = (selected: boolean) => ({
    flexDirection: "row" as const,
    alignItems: "center" as const,
    gap: 10,
    padding: 12,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: selected ? alpha(c.brand, 0.5) : c.line,
    backgroundColor: selected ? alpha(c.brand, 0.06) : "transparent"
  });

  return (
    <CardShell eyebrow="Sign-in needed" icon={KeyRound} tone="brand">
      <T variant="title">Save a sign-in for {site}</T>
      <T variant="small">
        Lumen needs the {requested ? SLOT_LABELS[requested].toLowerCase() : "sign-in details"} for this site. It's stored encrypted on your PC and typed by Lumen;
        it never goes through chat or a model. You'll confirm with your fingerprint.
      </T>
      {items === null && !error ? <Spinner /> : null}
      {items && items.length ? (
        <View style={{ gap: 8 }}>
          <Pressable onPress={() => setDestination({ kind: "create" })} style={choice(destination?.kind === "create")}>
            <T weight="500">A new account</T>
          </Pressable>
          {items.map((item) => (
            <Pressable key={item.id} onPress={() => setDestination({ kind: "update", id: item.id })} style={choice(destination?.kind === "update" && destination.id === item.id)}>
              <T weight="500">Update {item.account_label}</T>
            </Pressable>
          ))}
        </View>
      ) : null}
      {destination?.kind === "create" ? (
        <Field label="Name this account" value={label} onChangeText={setLabel} placeholder="Personal" editable={!saved && !busy} />
      ) : null}
      {(["username", "email", "password"] as Slot[]).map((slot) => (
        <Field
          key={slot}
          label={SLOT_LABELS[slot] + (requested === slot ? "" : " (if this account uses it)")}
          value={values[slot]}
          onChangeText={(next) => setValues((prev) => ({ ...prev, [slot]: next }))}
          secureTextEntry={slot === "password"}
          autoCapitalize="none"
          autoCorrect={false}
          autoComplete="off"
          keyboardType={slot === "email" ? "email-address" : "default"}
          editable={!saved && !busy}
        />
      ))}
      {bindingMissing ? <Banner tone="danger">This request lost its secure link to the site or field. Stop the task and try again.</Banner> : null}
      {error ? <T variant="small" color={c.dangerInk}>{error}</T> : null}
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
        <Button
          label={saved ? "Continue" : destination?.kind === "update" ? "Update & continue" : "Save & continue"}
          variant="primary"
          loading={busy}
          disabled={bindingMissing}
          onPress={() => void submit()}
        />
        <Button label="Stop the task" variant="danger-ghost" disabled={busy} onPress={onCancel} />
      </View>
    </CardShell>
  );
}
