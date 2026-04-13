import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// OmicsClaw Memory Dashboard
// `/api` -> oc memory-server (8766)
// `/kg`  -> oc app-server (8765) with embedded OmicsClaw-KG routes
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8766',
        changeOrigin: true,
      },
      '/kg': {
        target: process.env.OMICSCLAW_APP_PROXY_TARGET || 'http://127.0.0.1:8765',
        changeOrigin: true,
      }
    }
  }
})
