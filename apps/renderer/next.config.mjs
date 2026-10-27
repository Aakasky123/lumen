const backendUrl = process.env.API_BACKEND_URL ?? "http://127.0.0.1:8001";
const desktopBuild = process.env.LUMEN_DESKTOP_BUILD === "1";

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Always inlined, so builds without the fixture backend drop its desktop-bridge stand-in entirely.
  env: {
    NEXT_PUBLIC_LUMEN_FIXTURES: process.env.NEXT_PUBLIC_LUMEN_FIXTURES ?? ""
  },
  ...(desktopBuild
    ? {
        output: "export"
      }
    : {
        async rewrites() {
          return [
            {
              source: "/api/backend/:path*",
              destination: `${backendUrl}/:path*`
            }
          ];
        }
      })
};

export default nextConfig;
