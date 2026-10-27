import { useLocalSearchParams, useNavigation, useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { View } from "react-native";
import { api } from "@/api";
import { Button, Spinner, T } from "@/components/ui";
import { useTheme } from "@/theme";

/** Where an alert lands: find the chat that owns the run and open it. */
export default function RunRedirect() {
  const { c } = useTheme();
  const router = useRouter();
  const navigation = useNavigation();
  const params = useLocalSearchParams<{ id: string }>();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    const runId = Number(params.id);
    api
      .threads()
      .then((threads) => {
        if (!active) return;
        const owner = threads.find((thread) => thread.run_id === runId);
        if (!owner) return setFailed(true);
        // An alert tapped while its chat is already open (or under the live view) goes back to that
        // screen instead of stacking a second copy of it, so one back still reaches the chat list.
        const stack = navigation.getState();
        const open = stack ? stack.routes.findLastIndex((route) => route.name === "chat/[id]" && (route.params as { id?: string } | undefined)?.id === String(owner.id)) : -1;
        if (stack && open >= 0) router.dismiss(stack.index - open);
        else router.replace({ pathname: "/chat/[id]", params: { id: String(owner.id) } });
      })
      .catch(() => active && setFailed(true));
    return () => {
      active = false;
    };
  }, [navigation, params.id, router]);

  return (
    <View style={{ flex: 1, alignItems: "center", justifyContent: "center", gap: 14, padding: 24, backgroundColor: c.bg }}>
      {failed ? (
        <>
          <T style={{ textAlign: "center" }}>Couldn't find the chat for that task.</T>
          <Button label="Go to chats" onPress={() => router.replace("/")} />
        </>
      ) : (
        <Spinner />
      )}
    </View>
  );
}
