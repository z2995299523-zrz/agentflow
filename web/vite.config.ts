/**
 * Vite 配置：前端开发服务器（npm run dev，默认 5173）和构建产物预览（npm run preview，默认 8080）
 * 两者都把 /api 开头的请求去掉前缀后转给接口服务（默认 http://127.0.0.1:8000），浏览器只和一个地址打交道，不用配跨域。
 * 环境变量：API_TARGET 改接口地址；WEB_PORT 改预览端口。生产部署用 nginx，转发规则见 nginx.conf。
 */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const apiTarget = process.env.API_TARGET || 'http://127.0.0.1:8000'

const proxy = {
  '/api': {
    target: apiTarget,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ''),
  },
}

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
  preview: { port: Number(process.env.WEB_PORT) || 8080, proxy },
  build: { chunkSizeWarningLimit: 1600 },
})
