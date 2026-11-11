# 双节点生产部署：多 VIP 四层转发

两台独立的 Linux 主机各运行一个包含 manager、HAProxy 和 Keepalived 的容器，
使用宿主机网络，通过多个业务 VIP 对接不同的 TCP 服务。
用户接入 VPN 后，直接访问内部 `VIP:端口`，不依赖域名，不需要暴露到公网。

HAProxy 只做 TCP passthrough，不终止业务 TLS，也不根据 URL path 分流。
每个服务有独立的 `VIP:端口`，backend 无需适配 `/app/` 这样的代理路径前缀。
所有 VIP 属于同一个 VRRP instance，由当前 active 节点一起持有，故障时整体漂移到另一节点。
这不是每个 VIP 独立选主，也不是双节点同时分担不同 VIP。

首次部署时，A 提供共享 VIP 和规则，B 只提供本机身份和 peer。
两边启动后自动应用配置，无需调用 create、join 或 Apply API 初始化。
B 首次同步后保存完整配置，之后可独立重启并接管全部 VIP。
配置保存在各节点的 `/opt/haproxy-manager/data/manager/config.json`，不要让两节点共用数据目录。

## 1. 网络与镜像

下面都是示例值，部署前替换为自己的内部地址：

| 对象 | 示例 |
|---|---|
| 节点 A / B 的固定管理地址 | `10.20.0.11` / `10.20.0.12` |
| 业务 VIP | `10.20.0.100/24`、`10.20.0.101/24` |
| 承载 VIP 的业务接口 | 两边都是 `eth0`，实际名称可以不同 |
| VRID | `77`，同一网络中不得与其他 VRRP 集群冲突 |
| app backend | `10.30.0.20:8443`，HTTPS |
| monitor backend | `10.30.0.21:9443`，HTTPS |

两台主机需要 rootful Docker、Bash、curl、jq 和 iproute2。宿主机命令以 root 执行。
本方案不需要在代理主机准备业务证书、私钥或 OpenSSL。

- 所有 VIP 都使用指定的业务接口。两节点必须位于可让这些 VIP 漂移的业务网络，
  网络允许 ARP 更新及两节点之间的 **IP 协议 112（VRRP）**，不是 TCP/UDP 的 112 端口。
  云平台对辅助 IP 的限制需先处理，VIP 不能与其他设备的固定地址冲突。
- VPN 客户端需要到全部 VIP 的路由，网络需要允许相应 TCP 端口，并保证回程可达。
  两个代理节点都必须能访问全部 backend。
- 管理端口默认 TCP 8080，可通过部署脚本的 `--mng-port` 修改。
  该端口只允许集群节点和授权管理主机访问，不要开放给普通 VPN 用户或公网。
- 以下 HTTP 管理连接不加密密码、API key 或同步内容，只适用于隔离、可信网络。
  跨不可信网络时，先建立经过认证的加密通道。VRRP 的 PASS 认证不能替代加密。
- 宿主机不能有其他服务占用相同监听地址和端口，也不能有其他 Keepalived 管理这些 VIP。

在每台主机执行 `ip -br -4 addr`，确认承载 VIP 的接口及固定 IPv4 地址。
部署脚本的 `--vrrp-interface-a/b` 和 `--vrrp-ip-a/b` 指定 Keepalived 使用的业务网接口和地址。
`--mng-ip-a/b` 是管理连接地址，用于生成本机和 peer 的管理 URL，
可以在另一张网卡上，**不能填任何 VIP**，也不会限制 UI/API 的监听地址。
`--mng-port` 指定两节点共用的 UI/API 端口，默认 `8080`。

HAProxy 按规则里的 `VIP:端口` 监听，不按网卡名称监听。
backend 访问和业务回包由宿主机路由决定。默认由 HAProxy 新建到 backend 的 TCP 连接，
backend 看到的源地址通常是代理节点的出站地址，而不是 VPN 客户端地址。

两台使用支持预置配置启动和共享 API key 的同一个镜像版本，可以提前拉取并查询 digest：

```bash
docker pull ghcr.io/daocloud/haproxy-manager:latest
docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/daocloud/haproxy-manager:latest
```

部署脚本会自行拉取 `--image` 指定的镜像。`latest` 可能更新，生产部署建议将下方 `--image`
固定为同一个 `ghcr.io/daocloud/haproxy-manager@sha256:...`，两台 digest 必须一致。

## 2. 准备参数并启动

下面仅适用于**首次部署**。已有安装必须保留原来的数据目录、完整配置及凭据，
不要用最小示例覆盖，也不要在每次重启时重新生成配置或 API key。

使用与镜像版本对应的[独立部署脚本](../tools/deploy-two-nodes-docker.sh)。
下面的调用从仓库根目录执行；单独下载脚本时，将命令中的脚本路径替换为实际路径。

在调用前手工填写 `API_KEY` 和 `ADMIN_PASSWORD`，两台使用相同的值。
`API_KEY` 是两节点共用的同步认证 key，至少 32 个无空白的可见 ASCII 字符，不要求十六进制。
`ADMIN_PASSWORD` 是 **manager Web UI 管理员**的初始密码，至少 16 位、单行，
不是 root 密码，也不是节点 API key。下方凭据仅为格式示例，实际部署必须替换，不要直接使用。

这种手工变量与命令行传参方式会使凭据出现在 shell history、已填写的脚本及运行中的进程参数中。
仅在受控管理环境使用，不要将真实值提交到 Git、复制到公共日志或分享。

```bash
# 32 字符随机示例，仅供展示；实际部署必须替换，至少 32 个无空白可见 ASCII 字符，两台一致。
API_KEY='a0ec4e1d0d80b23ce15a3069f70abcdb'
# 16 字符随机示例，仅供展示；实际部署必须替换，至少 16 个字符、单行，两台一致。
ADMIN_PASSWORD='K0Xpv!cc!!t#amhI'
```

在两台宿主机分别执行 `hostname`，将实际输出填入下面的 `--hostname-a/b`，包括已有的域名后缀。
修改好一组参数后，**两台以 root 执行完全相同的命令，各一次**，顺序不限：

```bash
bash tools/deploy-two-nodes-docker.sh \
  --hostname-a proxy-a --hostname-b proxy-b \
  --mng-ip-a 10.20.0.11 --mng-ip-b 10.20.0.12 \
  --mng-port 8080 \
  --vrrp-interface-a eth0 --vrrp-interface-b eth0 \
  --vrrp-ip-a 10.20.0.11 --vrrp-ip-b 10.20.0.12 \
  --vip 10.20.0.100/24 \
  --vip 10.20.0.101/24 \
  --vrid 77 \
  --api-key "$API_KEY" \
  --admin-password "$ADMIN_PASSWORD" \
  --image ghcr.io/daocloud/haproxy-manager:latest \
  --tcp-forward app 10.20.0.100:443 10.30.0.20 8443 \
  --tcp-forward monitor 10.20.0.101:443 10.30.0.21 9443
```

部署后的映射为：

| VPN 客户端访问 | backend 连接目标 |
|---|---|
| `https://10.20.0.100/` | `10.30.0.20:8443` |
| `https://10.20.0.101/` | `10.30.0.21:9443` |

管理 UI 分别通过 `http://10.20.0.11:8080` 和 `http://10.20.0.12:8080` 访问。
例如将部署脚本的参数改为 `--mng-port 18090`，两节点的 UI/API 和同步连接都会使用
TCP 18090；后文管理请求中的 `8080` 也需替换为 `18090`，防火墙应限制对应端口。
这不会改变业务 `VIP:端口` 或 Keepalived 的 VRRP 通信。

重复 `--vip` 声明多个 VIP，重复 `--tcp-forward` 定义转发规则。
每条规则填写**规则名、入口 VIP:端口、backend 地址、backend 端口**。
规则名只是 manager 中的名称，不是域名。
不同 VIP 可以使用相同端口；同一个 VIP 也可以使用不同端口，例如在首次部署命令中追加
`--tcp-forward extra 10.20.0.101:8443 10.30.0.22 443`。

只声明一个 `--vip` 时，仍兼容原来的端口简写，例如
`--tcp-forward app 443 10.30.0.20 8443`。声明多个 VIP 时，每条规则必须明确填写 `VIP:端口`，
脚本不会猜测规则属于哪个 VIP。

### HTTPS 和应用行为

HTTPS 的 TLS 握手直接发生在客户端与 backend 之间，**证书和私钥只由 backend 持有**。
代理不生成入口证书，不读取业务 TLS 私钥，也不校验或替换 backend 证书。
这不代表客户端会自动信任证书：如果客户端严格校验，
backend 证书的 IP SAN 必须覆盖对应访问 VIP，并且证书或签发 CA 必须被客户端信任。
同一个 backend 经多个 VIP 提供 HTTPS 时，应让其证书覆盖这些访问 VIP。

例如访问 `https://10.20.0.100/login?next=home`，
app backend 收到的仍是 `/login?next=home`，没有 `/app/` 前缀。
TCP passthrough 不改写 HTTP Host、响应中的重定向或 Cookie，也不添加 `X-Forwarded-*`。
应用若在响应中硬编码内部 IP 或端口，四层代理不会自动把它改成 VIP。
本脚本仅生成 TCP 规则，不提供 UDP 转发。

### 参数与初始化行为

| 参数 | 要求与默认值 |
|---|---|
| `--hostname-a/b` | 必填，两个不同的宿主机 hostname，必须与本机实际输出精确匹配 |
| `--mng-ip-a/b` | 必填，两个不同的固定管理 IPv4 地址，用于本机和 peer 的管理 URL，不限制监听地址 |
| `--mng-port` | 默认 `8080`，范围 1–65535，两节点共用的 UI/API 和同步端口 |
| `--vrrp-interface-a/b` | 必填，各节点承载全部 VIP 的非 loopback 接口 |
| `--vrrp-ip-a/b` | 必填，业务接口上的固定 IPv4 地址，可与管理地址相同 |
| `--vip IPV4/PREFIX` | 至少一项，可重复；同一 IPv4 地址不能重复声明 |
| `--vrid` | 必填，1–255；全部 VIP 共用这一个 VRID |
| `--api-key` | 必填，两节点共用的认证 key，至少 32 个无空白的可见 ASCII 字符 |
| `--admin-password` | 必填，UI 初始密码，至少 16 位、单行 |
| `--tcp-forward NAME VIP:PORT BACKEND_ADDRESS BACKEND_PORT` | 至少一条，可重复；入口 VIP 必须已声明，backend 接受 IPv4 或可解析的 DNS 名 |
| `--admin-user` | 默认 `admin` |
| `--image` | 默认 `ghcr.io/daocloud/haproxy-manager:latest`，生产建议固定 digest |
| `--data-dir` | 默认 `/opt/haproxy-manager/data`，必须是绝对路径且不存在或为空 |
| `--priority-a/b` | 默认分别为 `150`、`100` |
| `--help` | 查看完整用法 |

转发规则名只能包含字母、数字、下划线和连字符，且以字母或数字开头。
规则名及 `VIP:端口` 组合不得重复，`--mng-port` 指定的端口保留给管理入口。
修改管理端口后，原来的 TCP 8080 可以用于业务转发。
脚本检查 VIP、规则、凭据、本机接口与固定地址，以及 rootful Docker，并记录入口与 backend 映射。
发现已有同名容器或非空数据目录时拒绝初始化，不覆盖已有安装。
失败会输出错误并保留已经写入的文件，先检查现场，不要盲目重复初始化。
日志中的“容器已启动”不代表 startup Apply 或双节点部署已成功，仍须完成第 3 节。

脚本根据 hostname 自动选择 A/B，不需要 `NODE` 参数，也不会修改宿主机 hostname。
A 写入全部共享 VIP 和 TCP 规则，B 只写入本机身份和 peer，随后通过启动机制同步。
节点通过各自的管理 URL 和 hostname 区分，共用 API key 不会将对端当成本机。
已有安装仍可保留各节点独立的 API key，不要求改成共享 key。

manager 容器的环境变量 `HAM_APPLY_ON_START=1` 使进程每次启动时校验、协调并应用配置，
是本镜像的默认值。部署脚本将管理端口写入 manager 容器的环境变量 `HAM_PORT`，
例如 `--mng-port 18090` 会生成 `HAM_PORT=18090`，让 UI/API 在 TCP 18090 上接收连接。
manager 容器的环境变量 `HAM_LISTEN=0.0.0.0` 使 UI/API 监听全部 IPv4 接口，
包括健康检查所用的 `127.0.0.1`，不是只监听 `--mng-ip-a/b` 指定的地址，需按第 1 节限制访问。
`HAM_ADMIN_USER/PASSWORD` 只在没有管理员时初始化登录，不会重置已有账号。

宿主机内核参数 `net.ipv4.ip_nonlocal_bind=1` 允许 standby 上的 HAProxy 提前绑定尚未持有的 VIP。
这是宿主机级配置，会影响其他进程，执行前应确认符合本机安全策略。
Docker 的 `--network host` 让 Keepalived 直接在宿主机业务接口上管理 VIP，不使用 `-p`。

四条挂载沿用镜像现有目录布局，宿主机只需备份一个 `data/`：

| 宿主机子目录 | 容器目录 | 保存内容 |
|---|---|---|
| `data/manager` | `/var/lib/haproxy-manager` | 配置、凭据及历史 |
| `data/acme` | `/var/lib/acme.sh` | 保留镜像原有 ACME 目录，本方案不使用 |
| `data/haproxy` | `/etc/haproxy` | HAProxy 配置 |
| `data/keepalived` | `/etc/keepalived` | Keepalived 配置与健康检查脚本 |

`data/manager.env` 保存容器启动环境，`data/api-header` 保存本机 API 请求头，均为 `0600`。
使用自定义 `--data-dir` 时，后文的宿主机路径也要相应替换。
**挂载整个 manager 目录，不要单独 bind `config.json`**，因为 manager 通过临时文件与原子替换保存配置。
不要用一条挂载覆盖整个 `/etc` 或 `/var/lib`。
配置、`manager.env`、`api-header` 和填写了真实凭据的部署脚本都应限制读取，不要提交到 Git 或公共日志。

## 3. 确认启动结果

启动顺序不限。B 先启动时会等待 A 发布，不会用空共享配置覆盖 A。
A 先启动时可独立应用，B 可用后会获取并应用 publication。

在**每个节点本机**查询：

```bash
curl --noproxy '*' -fsS --header @/opt/haproxy-manager/data/api-header \
  'http://127.0.0.1:8080/api/status?brief=1' |
  jq '{hostname,startup,role,vips,vip_held,published_rev,published_fp,applied_rev,applied_fp,apply_error}'

docker logs --tail 100 haproxy-manager
```

正式接入业务前必须确认：

- 两边 `startup.state=ready`、`startup.local_ok=true`，`apply_error` 为空。
- 两边 `published_rev/published_fp` 一致且不为空，各自 `applied_rev/applied_fp` 等于 publication。
- 两边 `vips` 包含全部已声明的 VIP。恰好一个节点为 `active`，其 `vip_held` 包含全部 VIP；
  另一个节点为 `passive`，`vip_held` 为空。

`startup` 描述本次进程启动，不是磁盘上上次成功的标记。
`waiting_peer` 表示等待 peer 的 publication，约每 5 秒重试。
`failed` 或 `conflict` 时查看 `startup.error` 和日志，不要强行覆盖草稿或 publication。
等待日志使用 `startup: state=...` 标识阶段，定期输出进度，不输出 API key 或配置全文。

容器 healthy 只表示 manager 能响应，不代表本次配置已应用。
已存在的有效 HAProxy 配置可能先恢复旧业务，不能据此认定新文件已生效。

从**接入 VPN 的业务客户端**按实际 IP 访问两个服务：

```bash
curl --noproxy '*' -fsS --cacert /path/to/app-ca.pem https://10.20.0.100/
curl --noproxy '*' -fsS --cacert /path/to/monitor-ca.pem https://10.20.0.101/
```

CA 文件用于客户端信任对应 backend 的证书，不安装到代理节点。
backend 证书已被客户端系统信任时可省略 `--cacert`；自签证书可用经过安全确认的 backend 公开证书。
不要用 `-k` 代替生产证书验证。应分别获得 app 和 monitor 的响应，而不是 manager UI。
还需确认登录、跳转、静态资源和业务功能正常，`startup.state=ready` 不能代替业务检查。

在维护窗口停止当前 active 容器，确认另一节点接管**全部 VIP**，并重新访问每个服务。
再恢复原节点，检查应用版本一致。故障切换不保证已有 TCP 会话不中断，客户端需要支持重连。

## 4. 后续修改与恢复

**平时使用 UI/API 修改当前 active 上的共享 VIP 和 TCP 规则，保存后执行 Apply。**
保存但未 Apply 的内容仍是草稿，后台同步不会发布它。主动重启容器会尝试应用这些草稿。
业务 TLS 证书在各自 backend 维护，不通过本部署脚本更新。

需要直接编辑文件时：

1. 确认当前 active、对端健康及版本一致，备份该节点完整数据目录。
2. 停止该节点容器，编辑现有 `data/manager/config.json`，不要重建最小配置或手工修改 `_meta`。
3. 保留本机 `local` 身份和其他字段，通过 `jq empty config.json` 检查 JSON，再启动容器。
4. 按第 3 节确认本次启动、双方应用版本及全部 VIP 的实际业务访问。

接口、优先级、固定管理地址、API key 和 VRRP unicast 地址属于各节点自己的 `local`，
不会通过共享同步覆盖。共享规则只维护一份，不要在两个节点分别修改后期待系统自动合并。
manager 配置文件字段 `local.sync.auto_sync` 默认是 `true`，允许首次自动加入和后续后台同步。
已有安装若设置为 `false`，启动只应用本机已有配置，不主动拉取 peer 或等待自动加入；
这不会禁止接收对端推送，沿用原有同步语义。
首次加入期间若保存了新的共享草稿，普通推送不会覆盖它，需要明确 Apply 或选择 Overwrite。

**文件没有热加载，也不是另一份声明式配置源。** manager 会将 UI/API 修改、缺省值、版本、
publication 和运行状态保存回同一个文件。不要在容器运行时与 UI 并发编辑文件，
也不要用初始化脚本每次覆盖它。手工编辑后的运行效果以本机下一次进程启动为准。
