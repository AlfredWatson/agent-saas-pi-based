# Pi SaaS Frontend

React/Vite 单页工作台，默认以相对 `/api/v1` 调用 Gateway。访问令牌只保存在
浏览器 `sessionStorage`；前端不会保存 Provider 或 RAG 模型密钥。

## 本地开发

```bash
cd frontend
npm ci
npm run dev
```

Vite 将 `/api/*` 代理到 `http://127.0.0.1:8000`。如 Gateway 使用其他地址，设置
非敏感的 `VITE_GATEWAY_ORIGIN`；生产构建仍应保留 `VITE_API_BASE=/api/v1`。

```bash
VITE_GATEWAY_ORIGIN=http://127.0.0.1:21995 npm run dev
npm run lint
npm run typecheck
npm test
npm run build
```

## 生产镜像

```bash
docker build -t pi-saas-frontend:dev frontend
docker run --rm -p 8080:8080 \
  -e GATEWAY_UPSTREAM=http://gateway:8000 \
  pi-saas-frontend:dev
```

Nginx 托管 SPA 并把 `/api/` 同域反代到 Gateway；深链接回退到 `index.html`。不要向
`VITE_*`、镜像环境或 Nginx 配置写入 JWT、Provider Key、RAG 模型 Key 或 Runtime 密钥。
