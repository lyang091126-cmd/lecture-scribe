import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backend = process.env.VITE_BACKEND_URL || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      // SSE 也走这条代理：关闭缓冲即可流式转发
      '/api': { target: backend, changeOrigin: true, ws: false },
    },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 1200 },
})
