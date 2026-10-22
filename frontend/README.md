# 前端开发

React + TypeScript + Vite 工作台。先按[根目录 README](../README.md)启动 Python API，再在本目录执行 `npm ci` 和 `npm run dev`，访问 `http://app.preview.localhost:5173`。Vite 把 `/api` 与 `/healthz` 代理到 `127.0.0.1:8000`。

`npm test` 运行前端测试；`npm run build` 做类型检查并生成生产资源。生成应用使用单独的 `preview.localhost` 来源，前端资源的构建并不包含 Python 服务或隔离构建镜像。
