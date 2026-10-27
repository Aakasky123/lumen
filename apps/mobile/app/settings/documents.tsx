import * as DocumentPicker from "expo-document-picker";
import * as Sharing from "expo-sharing";
import { FileText, Plus } from "lucide-react-native";
import { useCallback, useState } from "react";
import { ActionSheetIOS, Alert, KeyboardAvoidingView, Modal, Platform, Pressable, ScrollView, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { formatBytes, formatRelative } from "@/lib/format";
import { api, type ApplicationArtifact, type ApplicationArtifactPreview } from "@/api";
import { parseBackendDate } from "@/dates";
import { errorMessage, usePoll } from "@/hooks";
import { Badge, Button, Field, Group, Spinner, T } from "@/components/ui";
import { alpha, radius, useTheme } from "@/theme";

const PRESET_TYPES = [
  { value: "resume", label: "Résumé" },
  { value: "passport", label: "Passport" },
  { value: "flight ticket", label: "Flight ticket" }
];

function displayName(artifact: ApplicationArtifact): string {
  const metadata = artifact.metadata_json || {};
  for (const key of ["display_name", "display_label", "name"]) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return artifact.filename;
}

function kindLabel(artifact: ApplicationArtifact): string {
  const metadata = artifact.metadata_json || {};
  for (const key of ["artifact_role", "selected_artifact_kind", "kind"]) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim().replace(/[_-]+/g, " ");
  }
  return (artifact.artifact_kind || "document").replace(/[_-]+/g, " ");
}

export default function DocumentsScreen() {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const [items, setItems] = useState<ApplicationArtifact[] | null>(null);
  const [preview, setPreview] = useState<ApplicationArtifactPreview | null>(null);
  const [uploading, setUploading] = useState(false);
  const [type, setType] = useState("resume");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => setItems(await api.artifacts()), []);
  usePoll(load, 20000);

  const add = async () => {
    const picked = await DocumentPicker.getDocumentAsync({ copyToCacheDirectory: true, multiple: false });
    if (picked.canceled || !picked.assets?.[0]) return;
    const asset = picked.assets[0];
    setUploading(true);
    try {
      const result = await api.previewArtifact({ uri: asset.uri, name: asset.name, mimeType: asset.mimeType });
      setPreview(result);
      setType(result.suggested_artifact_kind?.trim() || "resume");
      setName(result.suggested_display_name || "");
    } catch (error) {
      Alert.alert("Couldn't read that file", errorMessage(error));
    } finally {
      setUploading(false);
    }
  };

  const commit = async () => {
    if (!preview) return;
    setSaving(true);
    try {
      await api.commitArtifact({ draftId: preview.draft_id, selectedType: type.trim() || "document", displayName: name });
      setPreview(null);
      await load();
    } catch (error) {
      Alert.alert("Couldn't save", errorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const open = async (artifact: ApplicationArtifact) => {
    try {
      const file = await api.downloadArtifact(artifact);
      await Sharing.shareAsync(file.uri, { mimeType: artifact.mime_type || undefined, dialogTitle: displayName(artifact) });
    } catch (error) {
      Alert.alert("Couldn't open it", errorMessage(error));
    }
  };

  const remove = (artifact: ApplicationArtifact) =>
    Alert.alert("Delete this document?", `${displayName(artifact)} is removed from Lumen on your PC.`, [
      { text: "Keep", style: "cancel" },
      {
        text: "Delete",
        style: "destructive",
        onPress: () => void api.deleteArtifact(artifact.id).then(load).catch((error) => Alert.alert("Couldn't delete", errorMessage(error)))
      }
    ]);

  const actions = (artifact: ApplicationArtifact) => {
    if (Platform.OS === "ios") {
      ActionSheetIOS.showActionSheetWithOptions({ options: ["Open", "Delete", "Cancel"], destructiveButtonIndex: 1, cancelButtonIndex: 2 }, (index) => {
        if (index === 0) void open(artifact);
        if (index === 1) remove(artifact);
      });
      return;
    }
    Alert.alert(displayName(artifact), undefined, [
      { text: "Delete", style: "destructive", onPress: () => remove(artifact) },
      { text: "Open", onPress: () => void open(artifact) }
    ]);
  };

  return (
    <View style={{ flex: 1, backgroundColor: c.bg }}>
      <ScrollView contentContainerStyle={{ padding: 16, paddingBottom: insets.bottom + 100, gap: 14 }}>
        <T variant="small">Lumen uses these when a form asks for a file, and reads them for facts about you.</T>
        {items === null ? (
          <Spinner />
        ) : items.length ? (
          <Group>
            {items.map((artifact, index) => (
              <Pressable
                key={artifact.id}
                onPress={() => actions(artifact)}
                style={({ pressed }) => ({
                  flexDirection: "row",
                  gap: 12,
                  padding: 14,
                  alignItems: "center",
                  borderTopWidth: index ? 1 : 0,
                  borderTopColor: c.line,
                  backgroundColor: pressed ? alpha(c.fg, 0.05) : "transparent"
                })}
              >
                <FileText size={18} color={c.fgMuted} />
                <View style={{ flex: 1, gap: 2 }}>
                  <T weight="500" numberOfLines={1}>
                    {displayName(artifact)}
                  </T>
                  <T variant="caption">{[formatBytes(artifact.file_size), formatRelative(parseBackendDate(artifact.created_at))].join(" · ")}</T>
                </View>
                <Badge label={kindLabel(artifact)} />
              </Pressable>
            ))}
          </Group>
        ) : (
          <T variant="small">No documents yet.</T>
        )}
      </ScrollView>
      <View style={{ position: "absolute", left: 16, right: 16, bottom: insets.bottom + 16 }}>
        <Button label={uploading ? "Reading…" : "Add a document"} icon={Plus} variant="primary" size="lg" loading={uploading} onPress={() => void add()} />
      </View>

      <Modal visible={Boolean(preview)} transparent animationType="slide" onRequestClose={() => setPreview(null)} statusBarTranslucent navigationBarTranslucent>
        <KeyboardAvoidingView style={{ flex: 1 }} behavior="padding">
        <Pressable style={{ flex: 1, backgroundColor: "rgba(0,0,0,0.55)" }} onPress={() => setPreview(null)} />
        {preview ? (
          <View
            style={{
              backgroundColor: c.popover,
              borderTopLeftRadius: radius.xxl,
              borderTopRightRadius: radius.xxl,
              borderWidth: 1,
              borderColor: c.lineStrong,
              padding: 16,
              paddingBottom: 16 + insets.bottom,
              gap: 12,
              maxHeight: "85%"
            }}
          >
            <ScrollView contentContainerStyle={{ gap: 12 }} keyboardShouldPersistTaps="handled">
              <T variant="title">{preview.filename}</T>
              {preview.summary ? <T variant="small">{preview.summary}</T> : null}
              {preview.key_facts.slice(0, 6).map((fact) => (
                <View key={`${fact.label}-${fact.value}`} style={{ flexDirection: "row", gap: 8 }}>
                  <T variant="small" weight="600" color={c.fg}>
                    {fact.label}
                  </T>
                  <T variant="small" style={{ flex: 1 }}>
                    {fact.value}
                  </T>
                </View>
              ))}
              {preview.warnings.map((warning) => (
                <T key={warning} variant="small" color={c.attentionInk}>
                  {warning}
                </T>
              ))}
              <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
                {[...new Set([preview.suggested_artifact_kind, ...PRESET_TYPES.map((preset) => preset.value)].filter(Boolean))].map((value) => (
                  <Pressable
                    key={value}
                    onPress={() => setType(value)}
                    style={{
                      paddingHorizontal: 12,
                      paddingVertical: 7,
                      borderRadius: 999,
                      borderWidth: 1,
                      borderColor: type === value ? alpha(c.brand, 0.5) : c.line,
                      backgroundColor: type === value ? alpha(c.brand, 0.08) : "transparent"
                    }}
                  >
                    <T variant="small" color={type === value ? c.brandInk : c.fgMuted}>
                      {PRESET_TYPES.find((preset) => preset.value === value)?.label ?? value.replace(/[_-]+/g, " ")}
                    </T>
                  </Pressable>
                ))}
              </View>
              <Field label="Kind" value={type} onChangeText={setType} autoCapitalize="none" />
              <Field label="Name" value={name} onChangeText={setName} placeholder={preview.filename} />
              <Button label="Save document" variant="primary" size="lg" loading={saving} disabled={!type.trim()} onPress={() => void commit()} />
              <Button label="Cancel" variant="ghost" onPress={() => setPreview(null)} />
            </ScrollView>
          </View>
        ) : null}
        </KeyboardAvoidingView>
      </Modal>
    </View>
  );
}
