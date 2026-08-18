import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // `next dev` and `next build` write incompatible artifacts under the same
  // name. A production build landing in a running dev server's directory
  // leaves it resolving module ids against manifests that were overwritten,
  // which surfaces as `__webpack_modules__[moduleId] is not a function` — a
  // runtime error that looks like a code fault and is not one. `make check-web`
  // therefore points the gate somewhere else; the default keeps `next dev`
  // exactly where every Next tutorial expects it.
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
};

export default config;
