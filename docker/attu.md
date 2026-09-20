Mac 的 `127.0.0.1:19530` 已被占用，通常是已有 SSH 隧道、Milvus 容器，或其他本地进程。先确认占用者：

```bash
lsof -nP -iTCP:19530 -sTCP:LISTEN
```

不必停掉它；直接换一个 Mac 本地端口即可，例如 `19531`：

```bash
autossh -M 0 \
  -N \
  -o "ServerAliveInterval 30" \
  -o "ServerAliveCountMax 3" \
  -o "ExitOnForwardFailure yes" \
  -L 19531:127.0.0.1:19530 \
  -p 30004 \
  haojifei@42.81.255.249
```

然后 Attu 容器要改为连接 Mac 宿主机的 `19531`：

```bash
docker run -d \
  --name attu \
  --restart unless-stopped \
  -p 127.0.0.1:3000:3000 \
  -e MILVUS_ADDRESS=host.docker.internal:19531 \
  -e MILVUS_NAME=pi-saas-milvus \
  -e MILVUS_DATABASE=default \
  -e ATTU_SSRF_ALLOWLIST=host.docker.internal \
  -v attu-data:/data \
  zilliz/attu:v3.0.0
```

浏览器访问 `http://127.0.0.1:3000`。如果此前已创建过同名 `attu` 容器，先确认它的状态：

```bash
docker ps -a --filter name=attu
```