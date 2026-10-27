import { KeyRound, Plus, Trash2 } from "lucide-react-native";
import { useCallback, useState } from "react";
import { Alert, KeyboardAvoidingView, Modal, Pressable, ScrollView, Switch, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { formatRelative } from "@/lib/format";
import { runTargetLabel } from "@/lib/runPhase";
import { api, type CredentialVaultFields, type CredentialVaultItem } from "@/api";
import { parseBackendDate } from "@/dates";
import { errorMessage, usePoll } from "@/hooks";
import { Button, Field, Group, IconButton, Spinner, T } from "@/components/ui";
import { radius, useTheme } from "@/theme";

/**
 * The credential vault, write-only from here: Lumen on the PC types a saved sign-in for its exact
 * site; the phone can add, replace, or remove one (each with a fingerprint check) but never read a
 * saved value back.
 */
export default function SignInsScreen() {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const [items, setItems] = useState<CredentialVaultItem[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [origin, setOrigin] = useState("");
  const [label, setLabel] = useState("");
  const [fields, setFields] = useState<Record<keyof CredentialVaultFields, string>>({ username: "", email: "", password: "" });
  const [autoUse, setAutoUse] = useState(true);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => setItems((await api.credentialVault()).items), []);
  usePoll(load, 20000);

  const reset = () => {
    setAdding(false);
    setOrigin("");
    setLabel("");
    setFields({ username: "", email: "", password: "" });
    setAutoUse(true);
  };

  const save = async () => {
    let normalized = origin.trim();
    if (normalized && !/^https?:\/\//i.test(normalized)) normalized = `https://${normalized}`;
    try {
      normalized = new URL(normalized).origin;
    } catch {
      Alert.alert("Enter the site's address", "For example https://accounts.example.com");
      return;
    }
    const values: CredentialVaultFields = {};
    (Object.keys(fields) as Array<keyof CredentialVaultFields>).forEach((key) => {
      if (fields[key]) values[key] = fields[key];
    });
    if (!Object.keys(values).length || !label.trim()) {
      Alert.alert("Add a name and at least one value");
      return;
    }
    setSaving(true);
    try {
      await api.createVaultItem({ origin: normalized, account_label: label.trim(), fields: values, auto_use: autoUse });
      reset();
      await load();
    } catch (error) {
      const message = errorMessage(error);
      if (message) Alert.alert("Couldn't save", message);
    } finally {
      setSaving(false);
    }
  };

  const remove = (item: CredentialVaultItem) =>
    Alert.alert("Remove this sign-in?", `${item.account_label} for ${runTargetLabel(item.origin)}`, [
      { text: "Keep", style: "cancel" },
      {
        text: "Remove",
        style: "destructive",
        onPress: () =>
          void api
            .deleteVaultItem(item.id)
            .then(load)
            .catch((error) => {
              const message = errorMessage(error);
              if (message) Alert.alert("Couldn't remove", message);
            })
      }
    ]);

  const toggleAutoUse = (item: CredentialVaultItem, next: boolean) =>
    void api
      .updateVaultItem(item.id, { auto_use: next })
      .then(load)
      .catch((error) => {
        const message = errorMessage(error);
        if (message) Alert.alert("Couldn't change that", message);
      });

  return (
    <View style={{ flex: 1, backgroundColor: c.bg }}>
      <ScrollView contentContainerStyle={{ padding: 16, paddingBottom: insets.bottom + 100, gap: 14 }}>
        <T variant="small">Saved sign-ins stay encrypted on your PC. Lumen types one only on its exact site, and nothing here can show a saved password.</T>
        {items === null ? (
          <Spinner />
        ) : items.length ? (
          <Group>
            {items.map((item, index) => (
              <View key={item.id} style={{ flexDirection: "row", alignItems: "center", gap: 12, padding: 14, borderTopWidth: index ? 1 : 0, borderTopColor: c.line }}>
                <KeyRound size={18} color={c.fgMuted} />
                <View style={{ flex: 1, gap: 2 }}>
                  <T weight="500" numberOfLines={1}>
                    {item.account_label}
                  </T>
                  <T variant="caption" numberOfLines={1}>
                    {[runTargetLabel(item.origin), item.available_slots.join(", "), item.last_used_at ? `used ${formatRelative(parseBackendDate(item.last_used_at))}` : ""]
                      .filter(Boolean)
                      .join(" · ")}
                  </T>
                </View>
                <Switch
                  value={item.auto_use}
                  onValueChange={(next) => toggleAutoUse(item, next)}
                  trackColor={{ true: c.brand, false: c.lineStrong }}
                  thumbColor="#ffffff"
                  accessibilityLabel={`Let Lumen use ${item.account_label} without asking`}
                />
                <IconButton icon={Trash2} accessibilityLabel={`Remove ${item.account_label}`} size={36} onPress={() => remove(item)} />
              </View>
            ))}
          </Group>
        ) : (
          <T variant="small">No sign-ins yet. Lumen asks for one when a site needs it.</T>
        )}
        <T variant="caption">The switch lets Lumen use an account without asking first.</T>
      </ScrollView>
      <View style={{ position: "absolute", left: 16, right: 16, bottom: insets.bottom + 16 }}>
        <Button label="Add a sign-in" icon={Plus} variant="primary" size="lg" onPress={() => setAdding(true)} />
      </View>

      <Modal visible={adding} transparent animationType="slide" onRequestClose={reset} statusBarTranslucent navigationBarTranslucent>
        <KeyboardAvoidingView style={{ flex: 1 }} behavior="padding">
        <Pressable style={{ flex: 1, backgroundColor: "rgba(0,0,0,0.55)" }} onPress={reset} />
        <View
          style={{
            backgroundColor: c.popover,
            borderTopLeftRadius: radius.xxl,
            borderTopRightRadius: radius.xxl,
            borderWidth: 1,
            borderColor: c.lineStrong,
            padding: 16,
            paddingBottom: 16 + insets.bottom,
            maxHeight: "90%"
          }}
        >
          <ScrollView contentContainerStyle={{ gap: 12 }} keyboardShouldPersistTaps="handled">
            <T variant="title">Add a sign-in</T>
            <Field label="Site" value={origin} onChangeText={setOrigin} placeholder="accounts.example.com" autoCapitalize="none" keyboardType="url" autoCorrect={false} />
            <Field label="Name" value={label} onChangeText={setLabel} placeholder="Personal" />
            <Field label="Username" value={fields.username} onChangeText={(username) => setFields((prev) => ({ ...prev, username }))} autoCapitalize="none" autoCorrect={false} autoComplete="off" />
            <Field label="Email" value={fields.email} onChangeText={(email) => setFields((prev) => ({ ...prev, email }))} autoCapitalize="none" keyboardType="email-address" autoComplete="off" />
            <Field label="Password" value={fields.password} onChangeText={(password) => setFields((prev) => ({ ...prev, password }))} secureTextEntry autoComplete="off" />
            <View style={{ flexDirection: "row", alignItems: "center", gap: 12 }}>
              <T style={{ flex: 1 }}>Use without asking</T>
              <Switch value={autoUse} onValueChange={setAutoUse} trackColor={{ true: c.brand, false: c.lineStrong }} thumbColor="#ffffff" />
            </View>
            <Button label="Save with fingerprint" variant="primary" size="lg" loading={saving} onPress={() => void save()} />
            <Button label="Cancel" variant="ghost" onPress={reset} />
          </ScrollView>
        </View>
        </KeyboardAvoidingView>
      </Modal>
    </View>
  );
}
