import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 开发时把 REST / 升级请求转给 gradus serve，浏览器侧只需一个同源地址。
      '/ws': { target: 'ws://127.0.0.1:8765', ws: true },
    },
  },
})
