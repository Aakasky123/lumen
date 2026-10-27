import type { ExpoConfig } from "expo/config";

// The Lumen Android remote. It drives the Lumen backend on the user's PC over their Tailscale network;
// nothing runs on the phone but the UI, the paired-device token, and the step-up key.
const config: ExpoConfig = {
  name: "Lumen",
  slug: "lumen-remote",
  scheme: "lumen",
  version: "0.1.2",
  orientation: "portrait",
  icon: "./assets/icon.png",
  userInterfaceStyle: "automatic",
  backgroundColor: "#0b0d0c",
  android: {
    package: "ai.lumen.remote",
    versionCode: 3,
    googleServicesFile: "./google-services.json",
    adaptiveIcon: {
      backgroundColor: "#0d1f1a",
      foregroundImage: "./assets/android-icon-foreground.png",
      monochromeImage: "./assets/android-icon-monochrome.png"
    },
    // The device token lives in the Keystore-backed secure store; never back it up off the phone.
    allowBackup: false,
    predictiveBackGestureEnabled: false,
    softwareKeyboardLayoutMode: "resize",
    blockedPermissions: ["android.permission.RECORD_AUDIO"]
  },
  plugins: [
    "expo-router",
    "expo-secure-store",
    "expo-font",
    "expo-image",
    ["expo-splash-screen", { image: "./assets/splash-icon.png", imageWidth: 120, backgroundColor: "#0b0d0c", dark: { backgroundColor: "#0b0d0c" } }],
    ["expo-camera", { cameraPermission: "Lumen uses the camera to scan the pairing code on your PC.", recordAudioAndroid: false }],
    ["expo-notifications", { icon: "./assets/notification-icon.png", color: "#5ee6bd" }],
    "./plugins/withEmulatorCleartext"
  ],
  experiments: {
    typedRoutes: true
  }
};

export default config;
