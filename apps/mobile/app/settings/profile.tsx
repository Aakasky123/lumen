import { useHeaderHeight } from "expo-router/react-navigation";
import { ChevronDown, PenLine, Plus, Sparkles, Trash2 } from "lucide-react-native";
import { useCallback, useMemo, useState } from "react";
import { Alert, KeyboardAvoidingView, Pressable, RefreshControl, ScrollView, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { describeFactSource, factIsNew, groupFacts, shortFactDate, type MemoryFact, type MemoryPayload, type MemorySection } from "@/lib/memoryFacts";
import { api } from "@/api";
import { errorMessage, usePoll } from "@/hooks";
import { Badge, Button, Field, Group, IconButton, Spinner, T } from "@/components/ui";
import { alpha, radius, useTheme } from "@/theme";

type Draft = { label: string; value: string; section: string };

function SectionPicker({ sections, value, onChange }: { sections: MemorySection[]; value: string; onChange: (key: string) => void }) {
  const { c } = useTheme();
  return (
    <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
      {sections.map((section) => {
        const selected = section.key === value;
        return (
          <Pressable
            key={section.key}
            accessibilityRole="radio"
            accessibilityState={{ checked: selected }}
            onPress={() => onChange(section.key)}
            style={{
              paddingHorizontal: 12,
              paddingVertical: 7,
              borderRadius: 999,
              borderWidth: 1,
              borderColor: selected ? alpha(c.brand, 0.5) : c.line,
              backgroundColor: selected ? alpha(c.brand, 0.08) : "transparent"
            }}
          >
            <T variant="small" color={selected ? c.brandInk : c.fgMuted}>
              {section.title}
            </T>
          </Pressable>
        );
      })}
    </View>
  );
}

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
  const { c } = useTheme();
  const [draft, setDraft] = useState(initial);
  const ready = Boolean(draft.label.trim() && draft.value.trim());
  return (
    <View style={{ gap: 12, padding: 14, borderRadius: radius.lg, borderWidth: 1, borderColor: alpha(c.brand, 0.3), backgroundColor: alpha(c.brand, 0.04) }}>
      <Field label="Name" value={draft.label} onChangeText={(label) => setDraft({ ...draft, label })} placeholder="Notice period" autoFocus={!initial.label} />
      <Field label="What Lumen should know" value={draft.value} onChangeText={(value) => setDraft({ ...draft, value })} placeholder="Two weeks" multiline />
      <SectionPicker sections={sections} value={draft.section} onChange={(section) => setDraft({ ...draft, section })} />
      <View style={{ flexDirection: "row", gap: 8, justifyContent: "flex-end" }}>
        <Button label="Cancel" variant="ghost" size="sm" disabled={busy} onPress={onCancel} />
        <Button
          label={submitLabel}
          variant="primary"
          size="sm"
          loading={busy}
          disabled={!ready}
          onPress={() => onSubmit({ label: draft.label.trim(), value: draft.value.trim(), section: draft.section })}
        />
      </View>
    </View>
  );
}

function FactRow({ fact, first, onEdit, onDelete }: { fact: MemoryFact; first: boolean; onEdit: () => void; onDelete: () => void }) {
  const { c } = useTheme();
  const meta = [describeFactSource(fact.source), shortFactDate(fact.learned_on)].filter(Boolean).join(" · ");
  return (
    <View style={{ flexDirection: "row", gap: 8, paddingLeft: 14, paddingRight: 4, paddingVertical: 12, borderTopWidth: first ? 0 : 1, borderTopColor: c.line }}>
      <View style={{ flex: 1, gap: 3 }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
          <T variant="caption" weight="500" color={c.fgMuted}>
            {fact.label}
          </T>
          {factIsNew(fact) ? <Badge label="New" tone="brand" /> : null}
        </View>
        <T selectable>{fact.value}</T>
        {meta ? <T variant="caption">{meta}</T> : null}
      </View>
      <IconButton icon={PenLine} accessibilityLabel={`Edit ${fact.label}`} size={38} onPress={onEdit} />
      <IconButton icon={Trash2} accessibilityLabel={`Delete ${fact.label}`} size={38} onPress={onDelete} />
    </View>
  );
}

/**
 * About you: everything Lumen knows about the user, the same list the cockpit shows. Lumen adds to it
 * as it learns; the user can edit, move, or delete any fact, or add their own.
 */
export default function AboutYouScreen() {
  const { c } = useTheme();
  const insets = useSafeAreaInsets();
  const headerHeight = useHeaderHeight();
  const [memory, setMemory] = useState<MemoryPayload | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sensitiveOpen, setSensitiveOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    try {
      setMemory(await api.memory());
      setLoadError("");
    } catch (error) {
      setLoadError(errorMessage(error, "Couldn't load your profile."));
    }
  }, []);
  // Lumen may learn something while this screen is open (a chat on the PC, a task answer).
  usePoll(load, 15000, editing === null && !adding);

  const groups = useMemo(() => (memory ? groupFacts(memory) : []), [memory]);
  const sections = memory?.sections ?? [];

  const run = async (operation: () => Promise<MemoryPayload>) => {
    setBusy(true);
    try {
      setMemory(await operation());
      setEditing(null);
      setAdding(false);
    } catch (error) {
      Alert.alert("Couldn't save that", errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const confirmDelete = (fact: MemoryFact) =>
    Alert.alert(`Delete ${fact.label.toLowerCase()}?`, "Lumen forgets it everywhere, including on your PC.", [
      { text: "Keep", style: "cancel" },
      { text: "Delete", style: "destructive", onPress: () => void run(() => api.deleteMemoryFact(fact.key)) }
    ]);

  if (!memory) {
    return (
      <View style={{ flex: 1, alignItems: "center", justifyContent: "center", gap: 14, padding: 24, backgroundColor: c.bg }}>
        {loadError ? (
          <>
            <T style={{ textAlign: "center" }}>{loadError}</T>
            <Button label="Try again" onPress={() => void load()} />
          </>
        ) : (
          <Spinner />
        )}
      </View>
    );
  }

  return (
    <KeyboardAvoidingView style={{ flex: 1, backgroundColor: c.bg }} behavior="padding" keyboardVerticalOffset={headerHeight}>
      <ScrollView
        contentContainerStyle={{ padding: 16, paddingBottom: insets.bottom + 32, gap: 18 }}
        keyboardShouldPersistTaps="handled"
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            tintColor={c.brand}
            colors={[c.brand]}
            progressBackgroundColor={c.surface2}
            onRefresh={async () => {
              setRefreshing(true);
              await load().catch(() => {});
              setRefreshing(false);
            }}
          />
        }
      >
        <T variant="small">Everything Lumen knows about you. It learns from your chats, answers, and documents, and fills forms only from what's here.</T>

        {adding ? (
          <FactEditor
            sections={sections}
            initial={{ label: "", value: "", section: "other" }}
            submitLabel="Add"
            busy={busy}
            onSubmit={(draft) => void run(() => api.addMemoryFact(draft))}
            onCancel={() => setAdding(false)}
          />
        ) : (
          <Button
            label="Add a fact"
            icon={Plus}
            onPress={() => {
              setEditing(null);
              setAdding(true);
            }}
          />
        )}

        {!memory.facts.length && !adding ? (
          <View style={{ padding: 20, gap: 6, borderRadius: radius.xl, borderWidth: 1, borderStyle: "dashed", borderColor: c.lineStrong }}>
            <T weight="600" style={{ textAlign: "center" }}>
              Lumen doesn't know anything about you yet
            </T>
            <T variant="small" style={{ textAlign: "center" }}>
              Tell it things in chat, like “I live in Hoboken”, answer its questions during a task, or upload your résumé. What it learns shows up here.
            </T>
          </View>
        ) : null}

        {groups.map((group) => {
          const sensitive = group.key === "sensitive";
          const collapsed = sensitive && !sensitiveOpen;
          return (
            <View key={group.key} style={{ gap: 8 }}>
              <Pressable
                disabled={!sensitive}
                onPress={() => setSensitiveOpen((value) => !value)}
                accessibilityRole={sensitive ? "button" : undefined}
                accessibilityState={sensitive ? { expanded: !collapsed } : undefined}
                style={{ flexDirection: "row", alignItems: "center", gap: 6, paddingHorizontal: 4 }}
              >
                <T variant="eyebrow">{group.title}</T>
                <T variant="caption">{group.facts.length}</T>
                {sensitive ? (
                  <>
                    <T variant="caption">· only what you chose to share</T>
                    <ChevronDown size={16} color={c.fgSubtle} style={{ marginLeft: "auto", transform: [{ rotate: collapsed ? "0deg" : "180deg" }] }} />
                  </>
                ) : null}
              </Pressable>
              {collapsed ? null : (
                <Group>
                  {group.facts.map((fact, index) =>
                    editing === fact.key ? (
                      <View key={fact.key} style={{ padding: 8, borderTopWidth: index ? 1 : 0, borderTopColor: c.line }}>
                        <FactEditor
                          sections={sections}
                          initial={{ label: fact.label, value: fact.value, section: fact.section }}
                          submitLabel="Save"
                          busy={busy}
                          onSubmit={(draft) =>
                            void run(() =>
                              api.updateMemoryFact(fact.key, {
                                ...(draft.label !== fact.label ? { label: draft.label } : {}),
                                value: draft.value,
                                section: draft.section
                              })
                            )
                          }
                          onCancel={() => setEditing(null)}
                        />
                      </View>
                    ) : (
                      <FactRow
                        key={fact.key}
                        fact={fact}
                        first={index === 0}
                        onEdit={() => {
                          setAdding(false);
                          setEditing(fact.key);
                        }}
                        onDelete={() => confirmDelete(fact)}
                      />
                    )
                  )}
                </Group>
              )}
            </View>
          );
        })}
        {memory.facts.some((fact) => factIsNew(fact)) ? (
          <View style={{ flexDirection: "row", gap: 6, alignItems: "center", paddingHorizontal: 4 }}>
            <Sparkles size={13} color={c.brandInk} />
            <T variant="caption">New: things Lumen learned in the last two days.</T>
          </View>
        ) : null}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}
