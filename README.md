<!-- docs-site:skip -->
<img src="static/logo.svg" alt="HAProxy Cluster Manager" width="268" height="64">
<!-- /docs-site:skip -->

HAProxy Cluster Manager 是一个轻量的自托管管理工具，通过 Web UI 和 API 管理
HAProxy，并使用 Keepalived 实现共享虚拟 IP（VIP）和节点故障切换。

## 核心功能

- **服务管理**：配置 HTTP/HTTPS 服务与 TCP 转发规则，先保存草稿，再通过 Apply 应用。
- **高可用集群**：节点间同步配置和证书，active 节点持有 VIP，故障时由其他节点接管。
- **证书管理**：申请和自动续期 Let's Encrypt 证书，也支持 TLS passthrough，由 backend 持有证书并终止 TLS。
- **无需数据库**：配置保存在本地 JSON 文件中，不依赖 PostgreSQL、Redis 或消息中间件。

更多功能说明可参考[上游项目文档](https://ham.iothing.net/docs/)。

## 双节点生产部署

在两台独立的 Linux 主机上各运行一个包含 manager、HAProxy 和 Keepalived 的容器，
使用宿主机网络，共用实际业务网络中的多个 VIP。
用户接入 VPN 后通过不同 `VIP:端口` 访问各自的 backend，HTTPS 使用 TCP passthrough，
不需要域名或 URL path 分流。

按[双节点生产部署：多 VIP 四层转发](docs/deploy-two-nodes-docker.md)运行部署脚本，
生成 `config.json` 并启动容器。共享 VIP 和规则只需在一台节点预置，
另一台配置本机身份和 peer，首次同步后也会保存完整配置，无需手动调用初始化 API。
直接使用本仓库发布的 `ghcr.io/daocloud/haproxy-manager:latest` 镜像，
两节点应使用相同的仓库 digest。

部署前确认两节点具备 rootful Docker，网络允许 VIP 漂移、ARP 更新和
**IP 协议 112（VRRP，不是 TCP/UDP 端口 112）** 双向通信。
管理端口（默认 8080）仅允许节点和授权管理主机访问，不要暴露到公网。
指南中的 HTTP 管理 API 仅适用于隔离、可信的网络，跨不可信网络时必须使用经过认证的加密通道。

容器健康检查只报告 manager 是否响应，不能自动重启卡死的 manager，
生产环境需要额外的监测和恢复机制。VIP 故障切换不保证已有 TCP 会话不中断。

## 本地双节点实验环境

在一台 Linux 宿主机上准备 rootful Docker、Docker Compose v2、Python 3 和 `iproute2`，
当前用户需要有访问 Docker 的权限。在项目根目录执行：

```bash
python3 tools/integration/lab.py up
```

脚本默认会构建当前工作区的代码，启动两个代理节点和一个 HTTP/HTTPS backend，
并配置 VIP 和两条 TCP 转发规则。HTTPS 使用 TLS passthrough，自签名证书的私钥只由 backend 持有。
终端会打印 UI 地址、演示登录信息、VIP 和 backend 地址。重复执行 `up` 会保留已有配置。

默认 UI 地址为 `http://127.0.0.1:18081` 和 `http://127.0.0.1:18082`，
登录账号为 `admin`，密码为 `lab-demo-password`。演示账号仅用于实验，不适合生产环境。

远程访问优先使用 SSH 端口转发。也可以设置实验启动脚本的环境变量
`HAM_LAB_UI_BIND=0.0.0.0`，让 Docker 将 UI 端口映射到宿主机的全部 IPv4 接口：

```bash
HAM_LAB_UI_BIND=0.0.0.0 python3 tools/integration/lab.py up
```

然后打开 `http://<宿主机 IP>:18081` 或 `http://<宿主机 IP>:18082`。
**只应在可信网络中这样使用**，因为演示账号是公开的，且 UI 使用 HTTP。
再次执行 `up` 时需保留同一个环境变量。

**`HAM_LAB_IMAGE`：指定在线应用镜像，跳过本地应用构建。**

如需使用已发布镜像，在启动命令前设置该环境变量：

```bash
HAM_LAB_IMAGE=ghcr.io/daocloud/haproxy-manager:V1.0.0 \
HAM_LAB_UI_BIND=0.0.0.0 python3 tools/integration/lab.py up
```

将示例 tag 替换为待测的已发布版本，也可以指定完整的 `镜像名@sha256:...`。
脚本每次会拉取指定镜像，两个代理节点使用同一镜像，不再构建工作区的应用代码；
实验 backend 仍从本地构建。拉取失败时直接报错，不会回退到本地构建。
未设置 `HAM_LAB_IMAGE` 或其值为空时，默认构建当前工作区的应用代码。
`test` 命令也支持该环境变量。再次执行 `up` 时需保留同一个镜像设置，否则恢复本地构建。

VIP 位于独立的 Docker bridge 网络中，不在宿主机的局域网上。
检查转发时，在 Docker 宿主机的项目根目录执行：

```bash
python3 tools/integration/lab.py check          # 检查真实 HTTP、TLS 转发和已应用版本
python3 tools/integration/lab.py status
```

体验故障切换时，先在 UI 中确认当前持有 VIP 的节点。以下以 `proxy-a` 为例：

```bash
python3 tools/integration/lab.py stop proxy-a
# 观察另一节点接管 VIP，运行 check 确认转发恢复，再启动原节点
python3 tools/integration/lab.py start proxy-a
```

体验结束后执行以下命令，**删除实验环境的容器、网络和数据卷，包括 UI 中保存的配置**：

```bash
python3 tools/integration/lab.py down
```

完整操作、curl 示例和端口、网段调整方法见[快速开始：双节点容器实验环境](docs/get-started-lab.md)。
