import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // In development the API runs separately on :8000. In production the
    // backend serves this build itself, so the same relative /api paths work
    // untouched — no environment-specific base URL anywhere in the app code.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // Built straight into the location the backend serves from.
    outDir: '../backend/static',
    emptyOutDir: true,
  },
})
