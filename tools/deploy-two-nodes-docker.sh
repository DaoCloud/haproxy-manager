#!/usr/bin/env bash
# First installation only. Both hosts use the same arguments.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: bash tools/deploy-two-nodes-docker.sh [options]

Required:
  --hostname-a NAME              Exact output of hostname on host A
  --hostname-b NAME              Exact output of hostname on host B
  --ip-a IPV4                    Host A's fixed management address
  --ip-b IPV4                    Host B's fixed management address
  --interface-a NAME             Host A's interface carrying the VIPs
  --interface-b NAME             Host B's interface carrying the VIPs
  --vrrp-ip-a IPV4               Host A's fixed address on that interface
  --vrrp-ip-b IPV4               Host B's fixed address on that interface
  --vip IPV4/PREFIX              Shared business VIP; repeat for multiple VIPs
  --vrid NUMBER                  VRRP ID, 1-255, unique on the business network
  --api-key KEY                 Shared cluster key, at least 32 visible ASCII characters
  --admin-password PASSWORD     Initial single-line UI password, at least 16 characters
  --tcp-forward NAME VIP:PORT BACKEND_ADDRESS BACKEND_PORT
                                 TCP passthrough; repeat for different VIP:PORT pairs
                                 VIP must be declared with --vip
                                 With only one VIP, PORT alone is also accepted

Optional:
  --admin-user NAME              Initial UI administrator (default: admin)
  --image REFERENCE              Image tag or digest
                                 (default: ghcr.io/daocloud/haproxy-manager:latest)
  --data-dir PATH                Absolute, empty data directory
                                 (default: /opt/haproxy-manager/data)
  --priority-a NUMBER            Host A's VRRP priority (default: 150)
  --priority-b NUMBER            Host B's VRRP priority (default: 100)
  -h, --help                     Show this help

Run as root, once on each host, using the same API key and UI password.
Credentials passed as arguments are visible in shell history and process listings.
All VIPs move together on the selected interface under one VRRP instance.
The script pulls the image, enables persistent host-level ip_nonlocal_bind,
and starts haproxy-manager with host networking. It never replaces an installation.
HTTPS passes through unchanged. Certificates and TLS remain on the backends.
EOF
}

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }
trap 'printf "[deploy] ERROR: operation failed at line %s; inspect existing files before retrying.\n" "$LINENO" >&2' ERR

values() {
  local count=$1 option=$2 value
  shift 2
  (( $# >= count )) || die "$option requires $count value(s); see --help."
  for value in "${@:1:count}"; do
    [[ -n "$value" && "$value" != --* ]] || die "$option requires $count value(s)."
  done
}

required() { [[ -n "$2" ]] || die "$1 is required; see --help."; }

number() {
  [[ "$2" =~ ^(0|[1-9][0-9]{0,4})$ ]] && (( $2 >= $3 && $2 <= $4 )) ||
    die "$1 must be an integer between $3 and $4."
}

ipv4() {
  local address=$2 octet
  local -a octets
  [[ "$address" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || die "$1 needs an IPv4 address."
  IFS=. read -r -a octets <<< "$address"
  for octet in "${octets[@]}"; do
    [[ "$octet" =~ ^(0|[1-9][0-9]{0,2})$ ]] && (( octet <= 255 )) ||
      die "$1 needs an IPv4 address without leading zeros."
  done
}

hostname_a= hostname_b= ip_a= ip_b= interface_a= interface_b=
vrrp_ip_a= vrrp_ip_b= vrid= api_key= admin_password=
admin_user=admin
image=ghcr.io/daocloud/haproxy-manager:latest
data_dir=/opt/haproxy-manager/data
priority_a=150
priority_b=100
vips=()
forwards=()

while (( $# )); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --tcp-forward)
      values 4 "$@"
      forwards+=("$2" "$3" "$4" "$5")
      shift 5
      continue
      ;;
    --hostname-a|--hostname-b|--ip-a|--ip-b|--interface-a|--interface-b|\
    --vrrp-ip-a|--vrrp-ip-b|--vip|--vrid|--api-key|--admin-password|\
    --admin-user|--image|--data-dir|--priority-a|--priority-b)
      values 1 "$@"
      ;;
    *) die "Unknown option: $1; see --help." ;;
  esac
  case "$1" in
    --hostname-a) hostname_a=$2 ;;
    --hostname-b) hostname_b=$2 ;;
    --ip-a) ip_a=$2 ;;
    --ip-b) ip_b=$2 ;;
    --interface-a) interface_a=$2 ;;
    --interface-b) interface_b=$2 ;;
    --vrrp-ip-a) vrrp_ip_a=$2 ;;
    --vrrp-ip-b) vrrp_ip_b=$2 ;;
    --vip) vips+=("$2") ;;
    --vrid) vrid=$2 ;;
    --api-key) api_key=$2 ;;
    --admin-password) admin_password=$2 ;;
    --admin-user) admin_user=$2 ;;
    --image) image=$2 ;;
    --data-dir) data_dir=$2 ;;
    --priority-a) priority_a=$2 ;;
    --priority-b) priority_b=$2 ;;
  esac
  shift 2
done

required --hostname-a "$hostname_a"
required --hostname-b "$hostname_b"
required --ip-a "$ip_a"
required --ip-b "$ip_b"
required --interface-a "$interface_a"
required --interface-b "$interface_b"
required --vrrp-ip-a "$vrrp_ip_a"
required --vrrp-ip-b "$vrrp_ip_b"
(( ${#vips[@]} )) || die "At least one --vip is required; see --help."
required --vrid "$vrid"
required --api-key "$api_key"
required --admin-password "$admin_password"
(( ${#forwards[@]} )) || die "At least one --tcp-forward is required."
[[ "$hostname_a" != "$hostname_b" ]] || die "Hostnames must be different."
[[ "$ip_a" != "$ip_b" && "$vrrp_ip_a" != "$vrrp_ip_b" ]] ||
  die "The two hosts need different management and VRRP addresses."
[[ "$admin_user" =~ ^[a-zA-Z0-9_.-]+$ ]] || die "--admin-user contains invalid characters."
for interface in "$interface_a" "$interface_b"; do
  [[ "$interface" != lo && "$interface" =~ ^[a-zA-Z0-9_.:-]+$ ]] ||
    die "VIP interfaces must be non-loopback interface names."
done
for address in "$ip_a" "$ip_b" "$vrrp_ip_a" "$vrrp_ip_b"; do
  ipv4 "Node addresses" "$address"
done
declare -A vip_addresses=()
for vip in "${vips[@]}"; do
  [[ "$vip" == */* ]] || die "--vip requires an IPv4 address and prefix."
  vip_ip=${vip%/*}
  ipv4 --vip "$vip_ip"
  number "VIP prefix" "${vip##*/}" 1 32
  [[ ! ${vip_addresses[$vip_ip]+present} ]] || die "VIP addresses must be unique."
  vip_addresses[$vip_ip]=1
  for address in "$ip_a" "$ip_b" "$vrrp_ip_a" "$vrrp_ip_b"; do
    [[ "$vip_ip" != "$address" ]] || die "A VIP must not be a node's fixed address."
  done
done
number --vrid "$vrid" 1 255
number --priority-a "$priority_a" 1 255
number --priority-b "$priority_b" 1 255
declare -A names=() binds=()
for (( i=0; i<${#forwards[@]}; i+=4 )); do
  name=${forwards[i]}
  bind=${forwards[i+1]}
  backend=${forwards[i+2]}
  [[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] ||
    die "Rule names must contain only letters, digits, underscores and hyphens."
  if [[ "$bind" == *:* ]]; then
    vip_ip=${bind%:*}
    port=${bind##*:}
    ipv4 "Listener VIP" "$vip_ip"
    [[ ${vip_addresses[$vip_ip]+present} ]] || die "Listener VIP must be declared with --vip."
  else
    (( ${#vips[@]} == 1 )) || die "Multiple VIPs require an explicit VIP:PORT for every rule."
    vip_ip=${vips[0]%/*}
    port=$bind
  fi
  number "Listener port" "$port" 1 65535
  number "Backend port" "${forwards[i+3]}" 1 65535
  [[ "$port" != 8080 ]] || die "Listener port 8080 is reserved for the management API."
  bind=$vip_ip:$port
  [[ ! ${names[$name]+present} && ! ${binds[$bind]+present} ]] ||
    die "Rule names and VIP:PORT listener pairs must be unique."
  names[$name]=1
  binds[$bind]=1
  forwards[i+1]=$bind
  [[ "$backend" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ &&
     "$backend" != *..* ]] || die "Backend addresses must be IPv4 addresses or DNS names."
  if [[ "$backend" =~ ^[0-9.]+$ ]]; then
    ipv4 "Backend address" "$backend"
  fi
  log "TCP route $name: $bind -> $backend:${forwards[i+3]}"
done
printf -v vip_list '%s\n' "${vips[@]}"
vip_list=${vip_list%$'\n'}

for command in id hostname docker jq ip realpath install sysctl mktemp; do
  command -v "$command" >/dev/null || die "Required command is missing: $command."
done
[[ $(id -u) == 0 ]] || die "Run this script as root."
[[ "$data_dir" == /* && "$data_dir" != *:* && "$data_dir" != *$'\n'* &&
   "$data_dir" != *$'\r'* ]] || die "--data-dir must be an absolute path without colons or newlines."
data_dir=$(realpath -m -- "$data_dir")
[[ "$data_dir" != / ]] || die "--data-dir cannot be the filesystem root."
if [[ -e "$data_dir" ]]; then
  [[ -d "$data_dir" ]] || die "--data-dir is not a directory."
  shopt -s nullglob dotglob
  entries=("$data_dir"/*)
  (( ${#entries[@]} == 0 )) || die "Data directory is not empty; refusing to replace an installation."
fi

node_name=$(hostname)
case "$node_name" in
  "$hostname_a")
    node=A; node_ip=$ip_a; peer_ip=$ip_b
    interface=$interface_a; priority=$priority_a
    vrrp_self=$vrrp_ip_a; vrrp_peer=$vrrp_ip_b
    ;;
  "$hostname_b")
    node=B; node_ip=$ip_b; peer_ip=$ip_a
    interface=$interface_b; priority=$priority_b
    vrrp_self=$vrrp_ip_b; vrrp_peer=$vrrp_ip_a
    ;;
  *) die "Local hostname '$node_name' matches neither configured host." ;;
esac
log "preflight: host=$node_name node=$node interface=$interface VIPs=${vips[*]}; using a shared cluster API key"
umask 077
staging=$(mktemp -d)
cleanup() {
  rm -f -- "$staging/api-key" "$staging/admin-password" "$staging/config.json" \
    "$staging/manager.env" "$staging/api-header" "$staging/sysctl.conf"
  rmdir -- "$staging"
}
trap cleanup EXIT
printf '%s' "$api_key" > "$staging/api-key"
printf '%s' "$admin_password" > "$staging/admin-password"
unset api_key admin_password
jq -en --rawfile key "$staging/api-key" --rawfile password "$staging/admin-password" '
  if (($key | test("^[!-~]{32,}$") | not) or ($key | test("[\r\n\u0000]")))
  then error("API key must contain at least 32 visible ASCII characters without whitespace.")
  elif $password | test("[\r\n\u0000]") then error("UI password must contain a single line.")
  elif ($password | length) < 16 then error("UI password must contain at least 16 characters.")
  else true end' >/dev/null
addresses=$(ip -j -4 addr show)
jq -e --arg ip "$node_ip" --arg interface "$interface" --arg source "$vrrp_self" '
  any(.[]; .ifname != "lo" and any(.addr_info[]?; .local == $ip)) and
  any(.[]; .ifname == $interface and .operstate != "DOWN" and
      any(.addr_info[]?; .local == $source))' <<< "$addresses" >/dev/null ||
  die "Management IP must belong to this host; VRRP source must belong to the selected, up VIP interface."
security=$(docker info --format '{{json .SecurityOptions}}')
[[ "$security" != *rootless* ]] || die "Rootful Docker is required."
existing=$(docker ps -a --filter 'name=^/haproxy-manager$' --format '{{.Names}}')
[[ -z "$existing" ]] || die "Container haproxy-manager already exists; refusing to replace it."
log "preflight passed; pulling $image"
docker pull "$image"

jq -n --arg node "$node" --arg self "http://$node_ip:8080" --arg peer "http://$peer_ip:8080" \
  --rawfile key "$staging/api-key" \
  --arg interface "$interface" --argjson priority "$priority" \
  --arg source "$vrrp_self" --arg destination "$vrrp_peer" \
  --arg vip "$vip_list" --argjson vrid "$vrid" --args '
  {
    local: {
      node_url: $self, api_key: $key,
      keepalived: {interface: $interface, priority: $priority,
                   unicast_src: $source, unicast_peer: $destination},
      sync: {peers: [{id:"peer", name:"peer", url:$peer,
                     api_key:$key, verify_tls:false}]}
    }
  } + (if $node == "A" then
    $ARGS.positional as $args |
    [range(0; $args | length; 4) as $i |
      {name:$args[$i], listen:$args[$i+1],
       address:$args[$i+2], port:($args[$i+3] | tonumber)}] as $rules |
    {
      cluster: {vips:$vip, vrid:$vrid},
      haproxy: {
        servers: [$rules[] | {id:("server-" + .name), name:.name,
                              address:.address, port:.port}],
        backends: [$rules[] | {id:("pool-" + .name), name:.name, mode:"tcp",
                               servers:[("server-" + .name)]}],
        frontends: [$rules[] | {id:("listener-" + .name), name:.name, mode:"tcp",
                                binds:.listen, default_backend:("pool-" + .name)}]
      }
    }
  else {} end)' "${forwards[@]}" > "$staging/config.json"
jq -nr --arg user "$admin_user" --rawfile password "$staging/admin-password" '
  "HAM_ADMIN_USER=" + $user, "HAM_ADMIN_PASSWORD=" + $password,
  "HAM_LISTEN=0.0.0.0", "HAM_APPLY_ON_START=1"' > "$staging/manager.env"
jq -nr --rawfile key "$staging/api-key" '"X-API-Key: " + $key' > "$staging/api-header"
printf 'net.ipv4.ip_nonlocal_bind = 1\n' > "$staging/sysctl.conf"

log "writing initial node $node configuration to $data_dir"
mkdir -p -m 0700 -- "$data_dir"
mkdir -m 0700 -- "$data_dir/manager"
mkdir -m 0700 -- "$data_dir/acme" "$data_dir/haproxy" "$data_dir/keepalived"
install -m 0600 "$staging/config.json" "$data_dir/manager/config.json"
install -m 0600 "$staging/manager.env" "$data_dir/manager.env"
install -m 0600 "$staging/api-header" "$data_dir/api-header"
log "enabling persistent host-level net.ipv4.ip_nonlocal_bind"
install -m 0644 "$staging/sysctl.conf" /etc/sysctl.d/90-haproxy-manager.conf
sysctl -p /etc/sysctl.d/90-haproxy-manager.conf
log "starting haproxy-manager"
docker run -d --name haproxy-manager --hostname "$node_name" \
  --network host --restart unless-stopped \
  --cap-add NET_ADMIN --cap-add NET_BROADCAST --cap-add NET_RAW \
  --env-file "$data_dir/manager.env" \
  -v "$data_dir/manager:/var/lib/haproxy-manager" \
  -v "$data_dir/acme:/var/lib/acme.sh" \
  -v "$data_dir/haproxy:/etc/haproxy" \
  -v "$data_dir/keepalived:/etc/keepalived" "$image"
log "container started; this does not confirm startup Apply or peer convergence"
log "check /api/status?brief=1 with the header file $data_dir/api-header and inspect docker logs"
