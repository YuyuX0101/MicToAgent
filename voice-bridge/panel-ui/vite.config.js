import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// base "./"：产物以 file:// 被 pywebview 直接加载，资源必须相对路径
// 多页入口：index.html = 控制面板，launcher.html = 启动向导进度卡
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
        launcher: fileURLToPath(new URL("./launcher.html", import.meta.url)),
      },
    },
  },
});
