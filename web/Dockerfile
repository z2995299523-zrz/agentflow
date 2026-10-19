# AgentFlow 界面镜像：多阶段构建
# 第一阶段用 Node 装依赖、构建出静态文件；第二阶段只把构建产物放进 nginx，镜像里没有 Node 和源码
FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --registry=https://registry.npmmirror.com
COPY . .
RUN npm run build

FROM nginx:stable-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
