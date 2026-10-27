import * as Notifications from "expo-notifications";
import { Stack, useRouter } from "expo-router";
import { StatusBar } from "expo-status-bar";
import * as SystemUI from "expo-system-ui";
import { useEffect, useRef } from "react";
import { View } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { runIdFromAlert } from "@/push";
import { SessionProvider, useSession } from "@/SessionProvider";
import { ThemeProvider, useTheme } from "@/theme";

function Root() {
  const { c, dark } = useTheme();
  const { session } = useSession();
  const router = useRouter();
  const alert = Notifications.useLastNotificationResponse();
  const handledAlert = useRef<string | null>(null);

  useEffect(() => {
    void SystemUI.setBackgroundColorAsync(c.bg);
  }, [c.bg]);

  // Tapping an alert opens the run it is about.
  useEffect(() => {
    if (!session || !alert || alert.actionIdentifier !== Notifications.DEFAULT_ACTION_IDENTIFIER) return;
    const id = alert.notification.request.identifier;
    if (handledAlert.current === id) return;
    handledAlert.current = id;
    const runId = runIdFromAlert(alert);
    if (runId) router.push({ pathname: "/run/[id]", params: { id: String(runId) } });
  }, [alert, router, session]);

  if (session === undefined) return <View style={{ flex: 1, backgroundColor: c.bg }} />;

  return (
    <>
      <StatusBar style={dark ? "light" : "dark"} />
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: c.bg },
          headerTintColor: c.fg,
          headerTitleStyle: { color: c.fg, fontSize: 17, fontWeight: "600" },
          headerShadowVisible: false,
          contentStyle: { backgroundColor: c.bg },
          animation: "slide_from_right"
        }}
      >
        <Stack.Protected guard={Boolean(session)}>
          <Stack.Screen name="index" options={{ headerShown: false }} />
          <Stack.Screen name="chat/[id]" options={{ title: "" }} />
          <Stack.Screen name="run/[id]" options={{ title: "" }} />
          <Stack.Screen name="live/[runId]" options={{ title: "What Lumen sees", presentation: "fullScreenModal", animation: "fade" }} />
          <Stack.Screen name="settings/index" options={{ title: "Settings" }} />
          <Stack.Screen name="settings/profile" options={{ title: "About you" }} />
          <Stack.Screen name="settings/documents" options={{ title: "Documents" }} />
          <Stack.Screen name="settings/signins" options={{ title: "Sign-ins" }} />
        </Stack.Protected>
        <Stack.Protected guard={!session}>
          <Stack.Screen name="pair" options={{ headerShown: false }} />
        </Stack.Protected>
      </Stack>
    </>
  );
}

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <ThemeProvider>
        <SessionProvider>
          <Root />
        </SessionProvider>
      </ThemeProvider>
    </SafeAreaProvider>
  );
}
