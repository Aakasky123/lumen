import * as Notifications from "expo-notifications";
import { Platform } from "react-native";

/**
 * Alerts come from the Lumen backend through FCM (app/push.py on the PC). They carry a generic title
 * and ids only; tapping one opens the run, and the details load over the tailnet.
 */
export const ALERT_CHANNEL_ID = "lumen-attention";

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false
  })
});

export type AlertData = { kind?: string; run_id?: string; question_id?: string };

/** Ask for notification permission and return this phone's FCM token, or null if declined. */
export async function registerForAlerts(): Promise<string | null> {
  if (Platform.OS !== "android") return null;
  await Notifications.setNotificationChannelAsync(ALERT_CHANNEL_ID, {
    name: "When Lumen needs you",
    description: "Questions, approvals, sign-ins, and finished tasks",
    importance: Notifications.AndroidImportance.HIGH,
    vibrationPattern: [0, 180, 120, 180],
    lightColor: "#5ee6bd"
  });
  const existing = await Notifications.getPermissionsAsync();
  const granted = existing.granted || (await Notifications.requestPermissionsAsync()).granted;
  if (!granted) return null;
  const token = await Notifications.getDevicePushTokenAsync();
  return typeof token.data === "string" ? token.data : null;
}

export function runIdFromAlert(response: Notifications.NotificationResponse | null | undefined): number | null {
  const data = (response?.notification.request.content.data ?? {}) as AlertData;
  const id = Number(data.run_id);
  return Number.isFinite(id) && id > 0 ? id : null;
}
