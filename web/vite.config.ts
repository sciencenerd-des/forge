import { defineConfig } from 'vite'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: process.env.FORGE_CONTROL_URL ?? 'http://127.0.0.1:8787',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
        headers: {
          Authorization: `Bearer ${process.env.FORGE_CONTROL_TOKEN || readTokenFile()}`,
        },
      },
    },
  },
})

function readTokenFile(): string {
  const home = process.env.FORGE_HOME || join(homedir(), '.forge')
  const path = join(home, 'control-token')
  return existsSync(path) ? readFileSync(path, 'utf8').trim() : ''
}
