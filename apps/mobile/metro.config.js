// Shared, framework-free modules come straight from the cockpit (apps/renderer/lib) so the phone and
// the PC read run phases, chat titles, and question contracts the same way. Those files import only
// types, so nothing from the renderer's node_modules is ever bundled.
const path = require("path");
const { getDefaultConfig } = require("expo/metro-config");

const config = getDefaultConfig(__dirname);
config.watchFolders = [...(config.watchFolders || []), path.resolve(__dirname, "../renderer/lib")];

module.exports = config;
