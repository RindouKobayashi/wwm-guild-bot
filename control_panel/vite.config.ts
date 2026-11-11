import { defineConfig } from 'vite';
export default defineConfig({ publicDir: false, build: { outDir: 'public', emptyOutDir: true,rollupOptions:{input:{main:'index.html',terminal:'terminal.html'}} }, server: { port: 5174 } });
