import { build } from "esbuild";
await build({
  entryPoints: ["src/main.jsx"],
  bundle: true,
  minify: true,
  outdir: "../web/chat",
  entryNames: "app",
  target: ["es2020"],
  define: { "process.env.NODE_ENV": '"production"' },
  legalComments: "linked",
});
