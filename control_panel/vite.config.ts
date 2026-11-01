import { defineConfig } from 'vite';
export default defineConfig({ publicDir: false, build: { outDir: 'public', emptyOutDir: true }, server: { port: 5174 } });
