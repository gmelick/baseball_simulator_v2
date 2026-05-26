import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],

  // Mirror the tsconfig `@/*` path alias for the bundler (rollup) so runtime
  // imports resolve the same way the TypeScript type-checker does.
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: true,
    // Raise the inline-asset threshold slightly for small SVG icons
    assetsInlineLimit: 4096,
  },

  server: {
    port: 5173,
    // Dev-server proxy: forward API + WS calls to the FastAPI backend so
    // the frontend dev server has no CORS issues in development.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      // SIM-389: auth endpoints — proxy /auth/* so the browser's httpOnly
      // cookie is set by the Vite dev origin (localhost:5173), not :8000.
      // Subsequent requests from the frontend include the cookie automatically.
      '/auth': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
      '/health': 'http://localhost:8000',
      '/ready': 'http://localhost:8000',
      '/docs': 'http://localhost:8000',
      '/openapi.json': 'http://localhost:8000',
    },
  },
})
