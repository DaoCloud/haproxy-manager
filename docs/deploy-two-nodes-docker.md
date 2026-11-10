# 双节点生产部署：Docker 与 API 初始化

本文面向两台独立的 Linux 主机：每台通过 `docker run` 运行一个包含 manager、
HAProxy 和 Keepalived 的容器，共用一个 VIP，通过 API 下发 TCP 转发规则。
两台节点都加载完整规则，只有持有 VIP 的节点接收 VIP 流量。

这与[单机容器实验环境](get-started-lab.md)不同：这里使用宿主机网络，
VIP 位于实际业务网络中，不使用实验环境的 Docker bridge。

## 1. 网络与部署前提

以下均为文档示例地址，部署前替换为实际值：

| 对象 | 示例 |
|---|---|
| 节点 A | `192.0.2.11`，主机名 `proxy-a` |
| 节点 B | `192.0.2.12`，主机名 `proxy-b` |
| VIP | `192.0.2.100/24` |
| 两节点承载 VIP 的接口 | `eth0`，可分别使用不同的实际接口名 |
| VRID | `77`，同一网络中不得与其他 VRRP 集群冲突 |
| backend | `198.51.100.20:8080` 提供 HTTP，`:8443` 提供 HTTPS |

两台主机需要 rootful Docker，并具备 `bash`、`curl`、`jq`、`openssl`、
`iproute2` 和 SSH 工具。以下宿主机部署命令以 root 身份执行。

部署前确认：

- 两节点的业务接口可以通过 VRRP 互通，网络允许 VIP 漂移及相应的 ARP 更新。
  云平台若限制辅助 IP 或禁止地址漂移，需先按平台要求配置，不能直接假定 Keepalived 可用。
- 双向放行两节点之间的 **IP 协议 112（VRRP）**，不是 TCP/UDP 的 112 端口。
  加入完成后使用 unicast VRRP，初始化期间不要提前将 VIP 接入正式业务。
- 管理端口 TCP 8080 只允许集群节点和授权管理主机访问。业务客户端允许访问 VIP 的
  TCP 80/443，两个代理节点均需能访问 backend。
- 宿主机上没有占用相同监听地址和端口的服务，也没有管理同一 VIP 的其他 Keepalived 实例。

**下方管理 API 使用 HTTP，要求在隔离、可信的网络中执行。**
它不加密管理员密码、API key 或同步内容。跨不可信网络时，必须先建立经过认证的加密通道，
或部署具有可信证书的 HTTPS 管理入口，并启用 peer 的 TLS 校验。
不要将 8080 直接暴露到公网，也不要把 VRRP 的 PASS 认证当成加密保护。

### 多网卡主机如何选择接口

先在每台主机执行以下命令，确认实际接口名及其 IPv4 地址：

```bash
ip -br -4 addr
```

第 4 节的 `VIP_INTERFACE_A` 和 `VIP_INTERFACE_B` 是部署脚本变量，分别填写两台主机上
承载业务 VIP 的接口名，例如 `ens192` 和 `enp1s0`。脚本将它们传给 manager 初始化 API
的请求体字段 `interface`，让各节点的 Keepalived 在指定接口上发送 VRRP 报文、添加或移除 VIP。
该接口必须已有本机固定 IPv4 地址，且能与另一节点的业务接口通信，不要直接照抄 `eth0`。

**HAProxy 监听的是业务规则中指定的 VIP 和端口，不是网卡名称。**
因此 `docker run` 无需另设 HAProxy 网卡参数。访问 backend 和业务回包走哪张网卡，
仍由宿主机路由及策略路由决定，需要按实际网络检查。节点的 API 地址可以使用独立管理网的固定 IP，
不必与 VIP 位于同一接口，但必须能被另一节点和部署控制端访问。

## 2. 拉取仓库发布的镜像

直接使用本仓库发布到 GitHub Container Registry 的镜像，不需要本地构建或传输镜像归档。
在**两台主机上分别**执行：

```bash
docker pull ghcr.io/daocloud/haproxy-manager:latest
docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/daocloud/haproxy-manager:latest
```

确认两台主机输出的仓库 digest 一致。`latest` 会随发布更新；如果两次拉取之间发生了发布，
应改用同一个 `ghcr.io/daocloud/haproxy-manager@sha256:...` 引用重新拉取，
并将下一节脚本中的 `IMAGE` 改为该引用。

## 3. 在两台主机分别运行容器

下面是**首次部署**流程，不要对已有集群重复生成 API key 或重新运行初始化接口。
确认宿主机的 `/opt/haproxy-manager/data/` 中没有旧集群数据。
已有安装应保留原来的数据和凭据，不要直接把原命名卷替换为空目录，否则会按全新安装启动。

在**每台主机**执行下面的脚本，先修改顶部变量。
节点 A 使用 `NODE_NAME=proxy-a`、`NODE_IP=192.0.2.11`；
节点 B 改为 `NODE_NAME=proxy-b`、`NODE_IP=192.0.2.12`。

`ADMIN_USER` 和 `ADMIN_PASSWORD` 是部署脚本变量，用于初始化 **manager Web UI 的管理员账号**，
不是宿主机 root 密码，也不是 API key。两节点使用相同的 UI 登录凭据，脚本无需人工交互。

```bash
set -euo pipefail

# 按当前节点修改名称和固定 API 地址。
NODE_NAME=proxy-a
NODE_IP=192.0.2.11
IMAGE=ghcr.io/daocloud/haproxy-manager:latest
DATA_DIR=/opt/haproxy-manager/data

# UI 管理员账号，不是 root 账号。必须将密码改为至少 16 位的单行强密码，两节点保持一致。
ADMIN_USER=admin
ADMIN_PASSWORD='CHANGE_ME'

if [ "$ADMIN_PASSWORD" = CHANGE_ME ] || [ "${#ADMIN_PASSWORD}" -lt 16 ]; then
  echo "请先修改顶部 ADMIN_PASSWORD，使用至少 16 位的 UI 管理员密码。" >&2
  exit 1
fi

umask 077
mkdir -p "$DATA_DIR"/{manager,acme,haproxy,keepalived}
cd /opt/haproxy-manager

if [ -e manager.env ] || [ -e api-header ]; then
  echo "已存在部署凭据，请勿重复初始化。" >&2
  exit 1
fi

printf 'HAM_ADMIN_USER=%s\nHAM_ADMIN_PASSWORD=%s\nHAM_LISTEN=0.0.0.0\n' \
  "$ADMIN_USER" "$ADMIN_PASSWORD" > manager.env
unset ADMIN_PASSWORD

printf 'net.ipv4.ip_nonlocal_bind = 1\n' \
  > /etc/sysctl.d/90-haproxy-manager.conf
sysctl -p /etc/sysctl.d/90-haproxy-manager.conf

docker run -d \
  --name haproxy-manager \
  --hostname "$NODE_NAME" \
  --network host \
  --restart unless-stopped \
  --cap-add NET_ADMIN \
  --cap-add NET_BROADCAST \
  --cap-add NET_RAW \
  --env-file /opt/haproxy-manager/manager.env \
  -v "$DATA_DIR/manager:/var/lib/haproxy-manager" \
  -v "$DATA_DIR/acme:/var/lib/acme.sh" \
  -v "$DATA_DIR/haproxy:/etc/haproxy" \
  -v "$DATA_DIR/keepalived:/etc/keepalived" \
  "$IMAGE"

for attempt in $(seq 1 60); do
  if curl --noproxy '*' -fsS --connect-timeout 2 --max-time 3 \
    "http://$NODE_IP:8080/api/whoami" > /dev/null; then
    break
  fi
  sleep 1
done
curl --noproxy '*' -fsS --connect-timeout 2 --max-time 3 \
  "http://$NODE_IP:8080/api/whoami" > /dev/null

API_KEY=$(openssl rand -hex 32)
printf '%s' "$API_KEY" | docker exec -i haproxy-manager \
  python3 /opt/haproxy-manager/app.py set-api-key -
printf 'X-API-Key: %s\n' "$API_KEY" > api-header
unset API_KEY
```

这些设置的作用是：

- Docker 的 `--network host` 让容器使用宿主机网络，不再使用 `-p` 做端口映射。
  Keepalived 在宿主机接口上管理 VIP，HAProxy 也直接绑定宿主机地址。
- manager 容器的环境变量 `HAM_LISTEN=0.0.0.0` 让 UI/API 监听宿主机全部 IPv4 接口，
  包括镜像健康检查和进程内探测使用的 `127.0.0.1`。须按第 1 节限制 8080 的访问范围，
  集群成员仍使用各自的固定 IP 互相访问。`HAM_ADMIN_USER=admin` 和
  `HAM_ADMIN_PASSWORD` 只在没有管理员时初始化登录。
- 宿主机内核参数 `net.ipv4.ip_nonlocal_bind=1` 允许 standby 节点的 HAProxy 提前绑定尚未持有的 VIP。
  这是宿主机级配置，会影响其他进程，应先确认符合本机安全策略。
- Docker 的四条 `-v` 将同一个宿主机数据目录下的子目录映射到镜像现有路径：

| 宿主机目录 | 容器目录 | 保存内容 |
|---|---|---|
| `/opt/haproxy-manager/data/manager` | `/var/lib/haproxy-manager` | manager 配置 |
| `/opt/haproxy-manager/data/acme` | `/var/lib/acme.sh` | ACME 账号、证书及续期状态 |
| `/opt/haproxy-manager/data/haproxy` | `/etc/haproxy` | HAProxy 配置与证书 |
| `/opt/haproxy-manager/data/keepalived` | `/etc/keepalived` | Keepalived 配置与健康检查脚本 |

**宿主机只需管理和备份一个 `data/` 目录，但当前镜像仍需要四条挂载。**
这些容器路径不在同一个可安全覆盖的目录下，不能仅将四条 `-v` 换成一条而保持现有镜像正常工作。
不要为了减少挂载条数而覆盖容器的整个 `/etc` 或 `/var/lib`。
两台主机分别保存自己的数据，不共用数据目录；重建容器时必须复用原目录。

`manager.env` 含有初始管理员密码，`api-header` 含有当前节点的 API key。
它们及填写了真实密码的部署脚本只应由授权管理员读取，不要提交到 Git、复制到工单或输出到公共日志。
两个节点的 API key 必须不同。容器管理权限本身也必须严格限制。

此时 UI/API 可以访问，但**还没有业务转发规则**。
Docker 的 healthy 仅表示管理接口能响应，不代表集群和业务已就绪。

## 4. 通过 API 初始化规则和集群

后续命令统一在**节点 A 的同一个 Bash 会话**执行，作为部署控制端。
将节点 B 的请求头文件通过 SSH 安全复制到节点 A：

```bash
cd /opt/haproxy-manager
scp root@192.0.2.12:/opt/haproxy-manager/api-header ./api-header-b
chmod 600 api-header api-header-b

A=http://192.0.2.11:8080
B=http://192.0.2.12:8080
VIP=192.0.2.100
# 分别填写节点 A、B 上承载 VIP 的真实接口名，两节点可以不同。
VIP_INTERFACE_A=eth0
VIP_INTERFACE_B=eth0
KEY_A=$PWD/api-header
KEY_B=$PWD/api-header-b

api() {
  local base=$1 header=$2 method=$3 path=$4
  shift 4
  curl --noproxy '*' -fsS --connect-timeout 5 --max-time 240 \
    --header "@$header" \
    --header 'Content-Type: application/json' \
    --request "$method" "$base$path" "$@"
}
```

SSH 用户和凭据文件路径按本地运维规范调整。
这里通过文件传递 `X-API-Key` 请求头，避免把 API key 直接写入 curl 的命令行参数。
`A`、`B` 必须是各节点自己的地址，**不能使用 VIP**，否则同步可能访问到错误的节点。

### 4.1 在节点 A 保存两条规则草稿

HTTP 流量也按 TCP 转发，不在代理上解析 HTTP：

```bash
jq -n --arg url "tcp://$VIP:80" \
  '{name:"http-pass", url:$url, target:"tcp://198.51.100.20:8080",
    certificate:false, health:{type:"tcp"}, apply:false}' |
  api "$A" "$KEY_A" POST /api/wizard/publish --data-binary @- |
  jq -e 'if .ok == true then del(.preview) else error(tostring) end'
```

HTTPS 使用 TLS passthrough，代理不申请或加载业务 TLS 证书：

```bash
jq -n --arg url "tcp://$VIP:443" \
  '{name:"https-pass", url:$url, target:"tcp://198.51.100.20:8443",
    certificate:false, health:{type:"tcp"}, apply:false}' |
  api "$A" "$KEY_A" POST /api/wizard/publish --data-binary @- |
  jq -e 'if .ok == true then del(.preview) else error(tostring) end'
```

`/api/wizard/publish` 请求体中的 `apply:false` 只保存草稿，尚不影响运行中的转发。
两个 `url` 都使用 VIP，而不是 `0.0.0.0`，使业务端口只绑定 VIP。
`target` 必须使用 `tcp://` 才符合这里的 TLS passthrough 设计；
不要将 HTTPS backend 改成 `https://` 后仍当作同一种配置。

### 4.2 在节点 A 创建集群并 Apply

下面将第 4 节顶部的 `VIP_INTERFACE_A` 传给创建接口，让节点 A 的 Keepalived 管理该接口上的 VIP：

```bash
jq -n --arg url "$A" --arg vips "$VIP/24" --arg iface "$VIP_INTERFACE_A" \
  '{mode:"cluster", node_url:$url, vips:$vips, vrid:77,
    interface:$iface, priority:150, apply:true}' |
  api "$A" "$KEY_A" POST /api/setup/create --data-binary @- |
  jq -e '
    if .ok == true and .applied.ok == true and .applied.local_ok == true
    then del(.api_key)
    else error((del(.api_key)) | tostring)
    end'
```

接口会沿用已经设置的本机 API key，响应中的原始 key 被上述 jq 命令移除。
这次 Apply 会生成 HAProxy/Keepalived 配置并加载前面保存的两条规则。

检查节点 A，等待 `role` 为 `active`，且 `vip_held` 包含预期 VIP 后再继续：

```bash
api "$A" "$KEY_A" GET '/api/status?brief=1' |
  jq '{hostname,role,vip_held,haproxy,keepalived,apply_error}'
```

### 4.3 将节点 B 加入集群

下面使用 `VIP_INTERFACE_B` 指定节点 B 自己的接口。请求中的 peer key 是 A 的 key，
并非 B 的 key：

```bash
jq -n --arg peer "$A" --arg url "$B" --arg iface "$VIP_INTERFACE_B" --rawfile header "$KEY_A" \
  '{peer_url:$peer, node_url:$url,
    peer_api_key:($header | sub("^X-API-Key: *"; "") | rtrimstr("\n")),
    interface:$iface, priority:100}' |
  api "$B" "$KEY_B" POST /api/setup/join --data-binary @- |
  jq -e '
    if .ok == true and .synced == true
    then del(.api_key)
    else error((del(.api_key)) | tostring)
    end'
```

加入会注册双方关系，拉取并应用 A 已发布的规则。
**只看到顶层 `ok:true` 不够，还必须确认 `synced:true` 并检查返回的 `steps`。**

加入流程会保存各节点的 unicast VRRP 设置。随后在 A 再执行一次 Apply，
使 A 也加载加入后的成员信息，并同步确认 B 的应用状态：

```bash
api "$A" "$KEY_A" POST /api/apply --data '{}' |
  jq -e '
    if .ok == true and .local_ok == true and .cluster_ok == true
    then .
    else error(tostring)
    end'
```

## 5. 验证实际应用和业务访问

分别查询两个节点：

```bash
api "$A" "$KEY_A" GET '/api/status?brief=1' |
  jq '{hostname,role,vip_held,published_rev,published_fp,applied_rev,applied_fp,apply_error}'
api "$B" "$KEY_B" GET '/api/status?brief=1' |
  jq '{hostname,role,vip_held,published_rev,published_fp,applied_rev,applied_fp,apply_error}'
```

接入正式业务前，必须确认：

- 恰好一个节点为 `active` 并持有 VIP，另一个为 `passive`。
- 两节点的 `published_rev`、`published_fp` 一致，且 fingerprint 不为空。
- 每个节点的 `applied_rev/applied_fp` 等于自己的 `published_rev/published_fp`，
  `apply_error` 为空。不能只检查 HTTP 200、容器 healthy 或配置版本相同。

从能路由到业务 VIP 的客户端测试 HTTP：

```bash
curl --noproxy '*' -fsS http://192.0.2.100/
```

假设 backend 的 TLS 证书对应真实业务域名 `service.example.com`，验证 HTTPS：

```bash
curl --noproxy '*' -fsS \
  --resolve service.example.com:443:192.0.2.100 \
  https://service.example.com/
```

将域名替换为实际证书对应的域名。curl 的 `--resolve` 将该域名定向到 VIP，
同时保留正确的 Host 和 TLS SNI，并正常校验证书。
如果 backend 使用内部 CA，可给 curl 增加 `--cacert /path/to/ca.pem` 来信任相应 CA，
不要用 `-k` 代替生产证书验证。应获得 backend 的实际业务响应，而不是 manager UI。

## 6. 后续快速下发规则

从第 5 节的状态中找到当前 `active` 节点，选择它的地址和请求头文件。
**不要始终固定写入节点 A**，因为加入、故障或维护后 active 可能变为 B。
下面假设当前 active 是 A；如果是 B，将两个变量分别改为 `$B` 和 `$KEY_B`：

```bash
ACTIVE=$A
ACTIVE_HEADER=$KEY_A
```

例如用一次 API 请求新增 `VIP:9000 -> backend:8080`，并立即应用、同步到另一个节点：

```bash
jq -n --arg url "tcp://$VIP:9000" \
  '{name:"extra-tcp", url:$url, target:"tcp://198.51.100.20:8080",
    certificate:false, health:{type:"tcp"}, apply:true}' |
  api "$ACTIVE" "$ACTIVE_HEADER" POST /api/wizard/publish --data-binary @- |
  jq -e '
    if .ok == true and .applied.ok == true and
       .applied.local_ok == true and .applied.cluster_ok == true
    then del(.preview)
    else error((del(.preview)) | tostring)
    end'
```

这里请求体的 `apply:true` 会在保存规则后执行 Apply，成功后将已发布版本推送到 peer。
响应的顶层 `ok` 只说明创建规则的请求成功，实际应用结果在 `applied` 中。
批量创建时，将各请求的 `apply` 设为 `false`，最后统一调用一次 `/api/apply`。

如果已经有通过 `/api/export/config` 导出的 manager JSON 备份，也可在当前 active 上导入：

```bash
api "$ACTIVE" "$ACTIVE_HEADER" POST /api/import/config \
  --data-binary @services-backup.json |
  jq -e 'if .ok == true then . else error(tostring) end'
api "$ACTIVE" "$ACTIVE_HEADER" POST /api/apply --data '{}' |
  jq -e '
    if .ok == true and .local_ok == true and .cluster_ok == true
    then .
    else error(tostring)
    end'
```

**导入会替换备份中包含的共享配置部分，并非追加规则，导入前应先备份。**
它不替换各节点的本地身份，也不会自动 Apply。
这里的备份是 manager JSON，不是原生 `haproxy.cfg`，且不包含完整的本机设置和证书私钥。

如果返回 `local_ok:true`、`cluster_ok:false`，表示本机已应用，但另一节点没有确认成功，
不能当作双节点部署完成，也不会自动回滚本机。检查 `error`、`sync` 和 peer 的 `apply_error`，
恢复后重新查询两节点状态。不要在失败后盲目重复新增同一条规则。

## 7. 运维注意事项

- 先在维护窗口验证停止 active 容器后 VIP 是否转移，再恢复该容器，确认自动补同步和业务访问。
  恢复后的节点不一定立即抢回 VIP，应以实际角色为准。VIP 切换不保证已有 TCP 会话保持。
- `--restart unless-stopped` 不能解决“进程仍存活但卡死”的情况，
  Docker 也不会仅因 unhealthy 自动重启容器。需要外部监控、告警和受控恢复机制。
- 保护并备份整个 `/opt/haproxy-manager/data/` 目录及必要的运维凭据，
  升级时复用原目录并使用已验证的同一镜像。
  不要将 UI 导出的共享配置备份当成完整的灾难恢复备份。
- 修改转发规则不需要重新 `docker run`。现有镜像没有“挂入规则文件后自动首次导入”的启动选项，
  本文通过容器启动后的 API 初始化实现自动下发，不应在每次容器重启时重新覆盖配置。
