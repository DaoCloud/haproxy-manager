# Get Started：双节点 TCP 转发实验环境

一条命令启动两个 HAProxy/Keepalived 节点和一个 HTTP/HTTPS backend，
通过 UI 管理共享转发规则，再用 `curl` 验证 VIP 转发。
这是单台宿主机上的容器实验环境，不是生产部署方案。

## 1. 准备条件

Docker 宿主机需要 Linux、rootful Docker、Docker Compose v2、Python 3 和
`iproute2`，当前用户需要有访问 Docker 的权限。首次构建镜像需要联网下载依赖。

**下面的启动、管理和 `curl` 命令都在 Docker 宿主机的项目根目录执行。**
Mac 或其他电脑可以通过浏览器访问管理 UI，但不能默认直连实验 Docker 网络中的 VIP。

## 2. 一条命令启动

```bash
HAM_LAB_UI_BIND=0.0.0.0 python3 tools/integration/lab.py up
```

脚本会构建当前工作区的代码、创建独立网络和数据卷、启动三个容器，
并初始化双节点集群、VIP 和两条 TCP 转发规则。启动后环境持续运行。
重复执行会保留已有配置，而不会重新创建初始规则。

`HAM_LAB_UI_BIND=0.0.0.0` 是实验启动脚本的环境变量：
它将 Docker 端口映射的**宿主机绑定地址**设为 `0.0.0.0`，
使管理 UI 可通过宿主机的局域网或 Tailscale 地址访问，不是修改容器内服务的监听地址。

```text
宿主机 0.0.0.0:18081 -> proxy-a 容器的 8080
宿主机 0.0.0.0:18082 -> proxy-b 容器的 8080
```

再次执行 `up` 时需保留这个环境变量，否则 UI 映射会恢复为默认的 `127.0.0.1`。
演示账号是公开的，UI 使用 HTTP，只应在可信网络中开放。

## 3. 打开管理 UI

在浏览器中使用宿主机实际可达的 IP：

| 节点 | 浏览器地址 |
|---|---|
| proxy-a | `http://<宿主机 IP>:18081/` |
| proxy-b | `http://<宿主机 IP>:18082/` |

如果浏览器在宿主机本地，IP 可以使用 `127.0.0.1`。
如果从其他电脑访问，可以使用局域网 IP，或使用已连通的宿主机 Tailscale IP。
监听全部接口并不代替网络连通性要求，路由、防火墙及 VPN 访问策略仍需允许这些连接。

登录账号为 `admin`，密码为 `lab-demo-password`。
在 Cluster 页面查看哪个节点持有 VIP，在当前 active 节点的 Services 页面管理规则。

## 4. 用 curl 验证转发

启动脚本会打印实际的 VIP 和 backend 地址，例如：

```text
VIP HTTP: http://172.30.77.100/
VIP HTTPS: https://172.30.77.100/ (self-signed backend certificate)
Backend targets: tcp://172.30.77.20:8080 (HTTP), tcp://172.30.77.20:8443 (HTTPS)
```

脚本自动选择未被占用的网段。**以下示例假设输出中的 VIP 为 `172.30.77.100`；
如果实际输出不同，请替换命令中的 IP。**

在这个示例网段中，预置规则如下：

| 规则 | VIP 入口 | backend |
|---|---|---|
| `lab-http` | `172.30.77.100:80` | `172.30.77.20:8080` |
| `lab-https` | `172.30.77.100:443` | `172.30.77.20:8443` |

两条规则在两个代理节点上都已应用，HAProxy 配置文件均使用 `mode tcp`，
只转发 TCP 数据，不按 HTTP Host 或路径分流。
HTTPS 使用 TLS passthrough，证书和私钥只由 backend 持有，代理节点不终止 TLS。
两个节点都运行 HAProxy，但同一时刻只有持有 VIP 的节点接收 VIP 流量。

在 **Docker 宿主机**上测试 HTTP：

```bash
curl --noproxy '*' -fsS http://172.30.77.100/
```

预期响应：

```json
{"service": "lab-backend", "protocol": "http"}
```

测试 HTTPS：

```bash
curl --noproxy '*' -kfsS https://172.30.77.100/
```

预期响应：

```json
{"service": "lab-backend", "protocol": "https"}
```

curl 的参数 `--noproxy '*'` 让请求直连 VIP，不经过代理。
HTTPS 命令中的 `-k` 跳过证书校验，仅用于实验 backend 的自签名证书。

**不能把上述 VIP 替换成宿主机的局域网或 Tailscale IP 来测试转发。**
当前映射到宿主机的是 UI 的 18081/18082，转发服务的 80/443 没有映射到宿主机。
如果你在 Mac 上操作，先 SSH 登录 Docker 宿主机，再执行上述 `curl` 命令。

## 5. 检查和清理

检查两个节点的应用版本、HTTP 转发、VIP HTTPS 及 backend TLS 证书：

```bash
python3 tools/integration/lab.py check
```

运行完整自动验收，包括规则发布与删除、草稿隔离、VIP 切换和节点恢复补同步：

```bash
python3 tools/integration/lab.py test
```

`test` 使用独立的临时集群，结束后自动清理，不修改正在手动体验的集群。

体验结束后，删除手动实验环境：

```bash
python3 tools/integration/lab.py down
```

`down` 删除这套实验环境的容器、网络和数据卷，包括 UI 中保存的配置。
已经构建的镜像保留在本机缓存中。
