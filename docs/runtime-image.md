# Runtime 镜像交付

Gateway 不会自动构建或拉取 Runtime 镜像。请在可访问 npm registry 的 `linux/amd64` 机器上，从仓库根目录执行：

```bash
docker buildx build --platform linux/amd64 \
  -f agent-runtime/Dockerfile \
  -t pi-saas-agent-runtime:0.82.1-dev \
  --load agent-runtime
docker save pi-saas-agent-runtime:0.82.1-dev | gzip > pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz
sha256sum pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz > pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz.sha256
```

将两个文件复制到 Gateway 宿主机后，先校验再导入：

```bash
sha256sum -c pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz.sha256
gzip -dc pi-saas-agent-runtime-0.82.1-dev-amd64.tar.gz | docker load
docker image inspect pi-saas-agent-runtime:0.82.1-dev --format '{{.Os}}/{{.Architecture}}'
```

镜像包含 Node、Pi SDK 和工具依赖；运行时将本仓库的 `agent-runtime/src` 只读挂载到 `/opt/pi-runtime/src`。因此修改 TypeScript 源码后只需调用 `POST /api/v1/runtime:recreate`，不需要重新构建镜像。只有锁文件、系统工具或基础镜像变更才需要重新制作和导入镜像。

如果 Docker 日志出现 `EACCES: permission denied, open '/opt/pi-runtime/package.json'`，说明镜像是在源文件为 `0600` 时构建的旧版本。更新 Dockerfile 后重新构建并导入镜像，再调用 `POST /api/v1/runtime:recreate`；不要通过让用户 Runtime 以 root 身份运行来绕过该问题。

已导入旧镜像、但当前机器无法联网时，可使用 `Dockerfile.permission-fix` 在本机生成一个只增加权限修复层的替代镜像：

```bash
docker build --network none \
  -f agent-runtime/Dockerfile.permission-fix \
  -t pi-saas-agent-runtime:0.82.1-dev \
  agent-runtime
```
