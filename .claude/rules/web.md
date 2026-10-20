---
paths:
  - "web/**"
---

# 前端（web/）

- React 19 + TypeScript 5.9 + Vite 8 + Ant Design 6；血缘图 React Flow 12，SQL 高亮 highlight.js，路由 react-router 7；Node ≥ 20.19
- 版本写死在 `web/package.json`。新加或升级依赖，选发布满两周的版本；国内装依赖加 `--registry=https://registry.npmmirror.com`
- 改完跑 `npm run build`（类型检查加构建），只查类型用 `npm run typecheck`
- `src/state/chat.tsx`：会话状态放在路由外层，切到元数据页再回来，正在跑的一轮还在
- `src/api/client.ts`：用 fetch 读 SSE（EventSource 只能发 GET）
- `src/utils/lineageLayout.ts`：血缘图排版（按跳数分列、重心法排序），不依赖画图组件

## 踩坑

- antd 的 `<App>` 会多包一层 `div.ant-app`：整页 `height: 100%` 要连它一起设，flex 子项还要 `min-height: 0`，否则整页跟着内容滚动
- SSE 走代理：`nginx.conf` 要 `proxy_buffering off`、读超时放长；接口回 `X-Accel-Buffering: no`
- DuckDB 的 DECIMAL 到前端是字符串：按字符串加千分位，不转浮点
- 内置浏览器的窗口在后台时页面不渲染：React Flow 量不了节点尺寸、不画连线，弹窗关不掉；截图验证要让窗口在前台
