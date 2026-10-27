// Android blocks cleartext HTTP by default. The only cleartext Lumen allows is the Android emulator's
// alias for the host PC (10.0.2.2), so the app can be developed against the backend's remote
// listener without Tailscale. Real phones always use the tailnet's HTTPS address; the app also
// refuses http:// pairing on a physical device (src/session.ts).
const fs = require("fs");
const path = require("path");
const { withAndroidManifest, withDangerousMod } = require("expo/config-plugins");

const NETWORK_SECURITY_CONFIG = `<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
  <base-config cleartextTrafficPermitted="false" />
  <domain-config cleartextTrafficPermitted="true">
    <domain includeSubdomains="false">10.0.2.2</domain>
  </domain-config>
</network-security-config>
`;

function withEmulatorCleartext(config) {
  config = withDangerousMod(config, [
    "android",
    async (modConfig) => {
      const xmlDir = path.join(modConfig.modRequest.platformProjectRoot, "app", "src", "main", "res", "xml");
      fs.mkdirSync(xmlDir, { recursive: true });
      fs.writeFileSync(path.join(xmlDir, "network_security_config.xml"), NETWORK_SECURITY_CONFIG);
      return modConfig;
    }
  ]);
  return withAndroidManifest(config, (modConfig) => {
    const application = modConfig.modResults.manifest.application?.[0];
    if (application) {
      application.$["android:networkSecurityConfig"] = "@xml/network_security_config";
      delete application.$["android:usesCleartextTraffic"];
    }
    return modConfig;
  });
}

module.exports = withEmulatorCleartext;
