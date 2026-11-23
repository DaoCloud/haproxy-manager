# 单节点生产部署：本机接口 IP 四层转发（无 VIP）

在单台 Linux 主机上运行一个包含 manager 和 HAProxy 的容器，使用宿主机网络，
直接监听宿主机指定网卡上已有的 IPv4 地址对接不同的 TCP 服务。
不配置 VIP，不启用 Keepalived 和 VRRP，不需要改写宿主机 `sysctl`，也不需要额外的 `NET_*` 容器权限。

HAProxy 只做 TCP passthrough，不终止业务 TLS，也不根据 URL path 分流。
节点以 `standalone` 模式运行，启动后自动校验并应用配置，无需调用初始化或 Apply API。
配置保存在 `/opt/haproxy-manager/data/manager/config.json`。

## 1. 网络与镜像

下面都是示例值，部署前替换为自己的内部地址：

| 对象 | 示例 |
|---|---|
| 代理节点接口与固定 IPv4 | `eth0` / `10.20.0.11` |
| 管理端口 | `8080`（可通过 `--mng-port` 修改） |
| app backend | `10.30.0.20:8443`，HTTPS |
| monitor backend | `10.30.0.21:9443`，HTTPS |

宿主机需要 rootful Docker、Bash（4+）、curl、jq 和 iproute2。宿主机命令以 root 执行。
本方案不需要在代理主机准备业务证书、私钥或 OpenSSL，也不需要网络支持 VIP 漂移、ARP 更新或 VRRP（IP 协议 112）。

- 业务接口（`--interface`）上的 IPv4 必须是**固定地址**。若使用 DHCP 导致地址变化，
  容器重启后 HAProxy 会因无法绑定原地址而失败。
- 客户端需要能访问代理节点的监听 `IP:端口`，网络需要允许相应 TCP 端口并保证回程可达；
  代理节点需要能访问全部 backend。
- 管理端口默认 TCP 8080，可通过 `--mng-port` 修改。
  该端口只允许授权管理主机访问，不要暴露到公网或普通业务网段。
- 以下 HTTP 管理连接不加密密码或 API key，只适用于隔离、可信网络；
  跨不可信网络时先建立经过认证的加密通道。
- 宿主机不能有其他服务占用相同的监听 `IP:端口` 或 `0.0.0.0:端口`（包括管理端口）。

在主机执行 `ip -br -4 addr`，确认承载流量的接口名称及其固定 IPv4 地址。
HAProxy 按规则里的 `IP:端口` 监听；默认由 HAProxy 新建到 backend 的 TCP 连接，
backend 看到的源地址通常是代理节点的出站地址，而不是客户端地址。

建议提前拉取镜像并查询 digest，生产环境可将 `--image` 固定为 `ghcr.io/daocloud/haproxy-manager@sha256:...`：

```bash
docker pull ghcr.io/daocloud/haproxy-manager:latest
docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/daocloud/haproxy-manager:latest
```

## 2. 准备参数并启动

下面仅适用于**首次部署**。已有安装必须保留原来的数据目录、完整配置及凭据，
不要重复初始化覆盖。

使用[独立单节点部署脚本](../tools/deploy-single-node-docker.sh)。
下面的调用从仓库根目录执行；单独下载脚本时，将命令中的脚本路径替换为实际路径。

调用前手工填写 `ADMIN_PASSWORD`（manager Web UI 管理员初始密码，至少 16 个字符、单行）。
单节点无需集群同步，`--api-key` 为可选项：省略时脚本会自动生成 48 位随机 key 写入配置和
`data/api-header`（权限 `0600`），避免出现在 shell history 和进程参数中。

这种命令行传参方式会使 `ADMIN_PASSWORD` 出现在 shell history 和运行中的进程参数中。
仅在受控管理环境使用，不要将真实凭据提交到 Git 或分享到公共日志。

```bash
# 16 字符随机示例，仅供展示；实际部署必须替换，至少 16 个字符、单行。
ADMIN_PASSWORD='K0Xpv!cc!!t#amhI'
```

### 常见用法：网卡只有一个 IPv4 地址

当 `--interface` 指定的网卡上只有一个 IPv4 地址时，无需填写 `--listen-ip`，
脚本会自动获取该接口的 IPv4 地址，`--tcp-forward` 可直接简写端口：

```bash
bash tools/deploy-single-node-docker.sh \
  --interface eth0 \
  --mng-port 8080 \
  --admin-password "$ADMIN_PASSWORD" \
  --image ghcr.io/daocloud/haproxy-manager:latest \
  --tcp-forward app 443 10.30.0.20 8443 \
  --tcp-forward monitor 8443 10.30.0.21 9443
```

部署后的映射（假设 `eth0` 的 IPv4 为 `10.20.0.11`）：

| 客户端访问 | backend 连接目标 |
|---|---|
| `https://10.20.0.11:443/` | `10.30.0.20:8443` |
| `https://10.20.0.11:8443/` | `10.30.0.21:9443` |

管理 UI 通过 `http://10.20.0.11:8080` 访问。

### 多 IP 网卡或管理/业务分网卡

当 `--interface` 上配置了多个 IPv4 地址（例如主 IP + 辅助 IP）时，脚本不会猜测监听地址，
需要通过 `--listen-ip` 显式指定一个或多个该接口上的 IPv4。
多个 `--listen-ip` 时，每条 `--tcp-forward` 必须明确填写 `IP:端口`；
若管理网卡与业务网卡分离，可额外指定 `--mng-ip`：

```bash
bash tools/deploy-single-node-docker.sh \
  --interface eth0 \
  --listen-ip 10.20.0.11 \
  --listen-ip 10.20.0.12 \
  --mng-ip 172.20.0.11 \
  --mng-port 8080 \
  --admin-password "$ADMIN_PASSWORD" \
  --image ghcr.io/daocloud/haproxy-manager:latest \
  --tcp-forward app 10.20.0.11:443 10.30.0.20 8443 \
  --tcp-forward monitor 10.20.0.12:443 10.30.0.21 9443
```

### HTTPS 和应用行为

HTTPS 的 TLS 握手直接发生在客户端与 backend 之间，**证书和私钥只由 backend 持有**。
代理不生成入口证书，不读取业务 TLS 私钥，也不校验或替换 backend 证书。
如果客户端严格校验 TLS 证书，backend 证书的 SAN 需要覆盖客户端实际访问的代理节点 IP 或域名，
并且证书或签发 CA 必须被客户端信任。

TCP passthrough 不改写 HTTP Host、重定向、Cookie 或路径前缀，也不添加 `X-Forwarded-*`。
本脚本仅生成 TCP 规则，不提供 UDP 转发。

### 参数与初始化行为

| 参数 | 要求与默认值 |
|---|---|
| `--interface NAME` | 必填，宿主机承载流量的非 loopback 网卡名（需存在且非 `DOWN`） |
| `--listen-ip IPV4` | 可选，可重复；须为 `--interface` 上已存在的固定 IPv4。网卡仅有一个 IPv4 时可省略 |
| `--mng-ip IPV4` | 可选，默认取第一个监听 IP；须属于本机非 loopback 网卡，用于 `local.node_url` 和提示输出，不限制 UI 监听地址 |
| `--mng-port PORT` | 默认 `8080`，范围 1–65535，UI/API 端口，保留不可用于转发规则 |
| `--admin-password` | 必填，UI 初始密码，至少 16 位、单行 |
| `--tcp-forward NAME [IP:]PORT BACKEND_ADDRESS BACKEND_PORT` | 至少一条，可重复；单监听 IP 可简写 `PORT`，多监听 IP 须填 `IP:PORT`；backend 接受 IPv4 或可解析的 DNS 名 |
| `--api-key KEY` | 可选，至少 32 个无空白可见 ASCII 字符；省略时自动生成并保存到 `data/api-header` |
| `--admin-user NAME` | 默认 `admin` |
| `--image REFERENCE` | 默认 `ghcr.io/daocloud/haproxy-manager:latest`，生产建议固定 digest |
| `--data-dir PATH` | 默认 `/opt/haproxy-manager/data`，必须是绝对路径且不存在或为空 |
| `--help` | 查看完整用法 |

与双节点部署相比，本脚本：

- 将 `cluster.vips` 置空、`local.sync.peers` 置空、`local.keepalived.enabled` 置为 `false`，
  容器不会启动 Keepalived，也不使用 VRRP。
- **不修改**宿主机 `/etc/sysctl.d/`（无需开启 `net.ipv4.ip_nonlocal_bind`）。
- 启动容器时使用 `--network host`，**不附加** `NET_ADMIN`、`NET_BROADCAST`、`NET_RAW` 权限。
- 沿用与双节点相同的四目录挂载布局（`data/manager`、`data/acme`、`data/haproxy`、`data/keepalived`），
  方便统一备份及后续按需演进。`data/manager.env` 和 `data/api-header` 权限均为 `0600`。
- `HAM_APPLY_ON_START=1`、`HAM_LISTEN=0.0.0.0`、`HAM_PORT=<mng-port>` 行为与双节点一致。
- 发现已有同名容器 `haproxy-manager` 或非空数据目录时直接拒绝，不会覆盖已有安装。

## 3. 确认启动结果

在代理节点本机查询（若修改了 `--data-dir` 或 `--mng-port`，相应替换路径和端口）：

```bash
curl --noproxy '*' -fsS --header @/opt/haproxy-manager/data/api-header \
  'http://127.0.0.1:8080/api/status?brief=1' |
  jq '{hostname,startup,role,keepalived,vips,vip_held,published_rev,published_fp,applied_rev,applied_fp,apply_error}'

docker logs --tail 100 haproxy-manager
```

正式接入业务前必须确认：

- `startup.state` 为 `ready`、`startup.local_ok` 为 `true`，`apply_error` 为空。
- `role` 为 `standalone`，`keepalived` 为 `disabled`，`vips` 和 `vip_held` 均为空数组 `[]`。
- `published_rev/published_fp` 不为空，且 `applied_rev/applied_fp` 与之一致。

从业务客户端按实际监听 `IP:端口` 验证转发：

```bash
curl --noproxy '*' -fsS --cacert /path/to/app-ca.pem https://10.20.0.11:443/
curl --noproxy '*' -fsS --cacert /path/to/monitor-ca.pem https://10.20.0.11:8443/
```

确认返回对应 backend 内容而非 manager UI，且业务功能正常。

## 4. 后续修改与恢复

**平时直接通过 Web UI 或 API 修改服务与转发规则，保存后执行 Apply 即可。**
`standalone` 节点没有只读锁；使用 `--network host` 时，在 UI 中新增端口监听无需重建容器。

需要直接编辑配置文件时：

1. 备份整个数据目录（如 `/opt/haproxy-manager/data`）。
2. 停止容器，编辑 `data/manager/config.json`，不要手工破坏 `_meta` 或重写最小配置。
3. 通过 `jq empty data/manager/config.json` 检查 JSON 语法后重新启动容器。
4. 按第 3 节重新检查 `/api/status?brief=1` 与真实业务访问。
