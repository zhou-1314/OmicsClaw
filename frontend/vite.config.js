import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// OmicsClaw Memory Dashboard
// Backend runs on port 8766 (oc memory-server)
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8766',
        changeOrigin: true,
      }
    }
  }
})
