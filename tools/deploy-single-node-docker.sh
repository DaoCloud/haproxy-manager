#!/usr/bin/env bash
# First installation only. Single standalone node without VIP or Keepalived.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: bash tools/deploy-single-node-docker.sh [options]

Required:
  --interface NAME               Non-loopback host interface for forwarding traffic
  --admin-password PASSWORD      Initial single-line UI password, at least 16 characters
  --tcp-forward NAME [IP:]PORT BACKEND_ADDRESS BACKEND_PORT
                                 TCP passthrough; repeat for different IP:PORT pairs
                                 With one listener IP, PORT alone is accepted
                                 With multiple --listen-ip values, IP:PORT is required

Optional:
  --listen-ip IPV4               Fixed IPv4 on --interface; repeat for multiple IPs
                                 Omit when --interface has exactly one IPv4 address
  --mng-ip IPV4                  Host IPv4 used for node_url and status hints
                                 (default: first listener IP)
  --mng-port PORT                UI/API port (default: 8080)
  --api-key KEY                  Local API key, at least 32 visible ASCII characters
                                 (default: auto-generated and saved to api-header)
  --admin-user NAME              Initial UI administrator (default: admin)
  --image REFERENCE              Image tag or digest
                                 (default: ghcr.io/daocloud/haproxy-manager:latest)
  --data-dir PATH                Absolute, empty data directory
                                 (default: /opt/haproxy-manager/data)
  -h, --help                     Show this help

Run as root on the target host.
Credentials passed as arguments are visible in shell history and process listings.
This script configures a standalone node with no VIP, no Keepalived, no VRRP,
no host sysctl changes, and no extra NET_* container capabilities.
It pulls the image and starts haproxy-manager with host networking.
It never replaces an existing installation.
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

interface= mng_ip= api_key= admin_password=
api_key_supplied=0
mng_port=8080
admin_user=admin
image=ghcr.io/daocloud/haproxy-manager:latest
data_dir=/opt/haproxy-manager/data
listen_ips=()
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
    --interface|--listen-ip|--mng-ip|--mng-port|\
    --api-key|--admin-password|--admin-user|--image|--data-dir)
      values 1 "$@"
      ;;
    *) die "Unknown option: $1; see --help." ;;
  esac
  case "$1" in
    --interface) interface=$2 ;;
    --listen-ip) listen_ips+=("$2") ;;
    --mng-ip) mng_ip=$2 ;;
    --mng-port) mng_port=$2 ;;
    --api-key) api_key=$2; api_key_supplied=1 ;;
    --admin-password) admin_password=$2 ;;
    --admin-user) admin_user=$2 ;;
    --image) image=$2 ;;
    --data-dir) data_dir=$2 ;;
  esac
  shift 2
done

required --interface "$interface"
required --admin-password "$admin_password"
(( ${#forwards[@]} )) || die "At least one --tcp-forward is required."
[[ "$interface" != lo && "$interface" =~ ^[a-zA-Z0-9_.:-]+$ ]] ||
  die "--interface must be a non-loopback interface name."
[[ "$admin_user" =~ ^[a-zA-Z0-9_.-]+$ ]] || die "--admin-user contains invalid characters."
number --mng-port "$mng_port" 1 65535
if [[ -n "$mng_ip" ]]; then
  ipv4 --mng-ip "$mng_ip"
fi

declare -A declared_listen_ips=()
if (( ${#listen_ips[@]} )); then
  for ip in "${listen_ips[@]}"; do
    ipv4 --listen-ip "$ip"
    [[ ! ${declared_listen_ips[$ip]+present} ]] || die "--listen-ip addresses must be unique."
    declared_listen_ips[$ip]=1
  done
fi

declare -A names=()
for (( i=0; i<${#forwards[@]}; i+=4 )); do
  name=${forwards[i]}
  bind=${forwards[i+1]}
  backend=${forwards[i+2]}
  [[ "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] ||
    die "Rule names must contain only letters, digits, underscores and hyphens."
  [[ ! ${names[$name]+present} ]] || die "Rule names must be unique."
  names[$name]=1
  if [[ "$bind" == *:* ]]; then
    bind_ip=${bind%:*}
    port=${bind##*:}
    ipv4 "Listener IP" "$bind_ip"
    if (( ${#listen_ips[@]} )); then
      [[ ${declared_listen_ips[$bind_ip]+present} ]] ||
        die "Listener IP $bind_ip must be declared with --listen-ip."
    fi
  else
    (( ${#listen_ips[@]} <= 1 )) ||
      die "Multiple --listen-ip values require an explicit IP:PORT for every rule."
    port=$bind
  fi
  number "Listener port" "$port" 1 65535
  number "Backend port" "${forwards[i+3]}" 1 65535
  [[ "$port" != "$mng_port" ]] ||
    die "Listener port $mng_port is reserved for the management API."
  [[ "$backend" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ &&
     "$backend" != *..* ]] || die "Backend addresses must be IPv4 addresses or DNS names."
  if [[ "$backend" =~ ^[0-9.]+$ ]]; then
    ipv4 "Backend address" "$backend"
  fi
done

for command in id hostname docker jq ip realpath install mktemp; do
  command -v "$command" >/dev/null || die "Required command is missing: $command."
done
if (( ! api_key_supplied )); then
  for command in od tr; do
    command -v "$command" >/dev/null || die "Required command is missing: $command."
  done
fi
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

umask 077
staging=$(mktemp -d)
cleanup() {
  rm -f -- "$staging/api-key" "$staging/admin-password" "$staging/config.json" \
    "$staging/manager.env" "$staging/api-header"
  rmdir -- "$staging"
}
trap cleanup EXIT
if (( api_key_supplied )); then
  printf '%s' "$api_key" > "$staging/api-key"
else
  od -An -tx1 -N24 /dev/urandom | tr -d ' \t\r\n' > "$staging/api-key"
fi
printf '%s' "$admin_password" > "$staging/admin-password"
unset api_key admin_password
jq -en --rawfile key "$staging/api-key" --rawfile password "$staging/admin-password" '
  if (($key | test("^[!-~]{32,}$") | not) or ($key | test("[\r\n\u0000]")))
  then error("API key must contain at least 32 visible ASCII characters without whitespace.")
  elif $password | test("[\r\n\u0000]") then error("UI password must contain a single line.")
  elif ($password | length) < 16 then error("UI password must contain at least 16 characters.")
  else true end' >/dev/null

addresses=$(ip -j -4 addr show)
jq -e --arg interface "$interface" '
  any(.[]; .ifname == $interface and .operstate != "DOWN")' <<< "$addresses" >/dev/null ||
  die "Interface $interface does not exist or is DOWN."
mapfile -t interface_ips < <(jq -r --arg interface "$interface" '
  [ .[] | select(.ifname == $interface and .operstate != "DOWN")
    | .addr_info[]? | select(.local != null and .local != "") | .local ]
  | unique[]' <<< "$addresses")
(( ${#interface_ips[@]} )) || die "Interface $interface has no IPv4 addresses."

declare -A host_iface_ips=()
for ip in "${interface_ips[@]}"; do
  host_iface_ips[$ip]=1
done

if (( ${#listen_ips[@]} == 0 )); then
  if (( ${#interface_ips[@]} > 1 )); then
    die "Interface $interface has multiple IPv4 addresses (${interface_ips[*]}); specify one or more with --listen-ip."
  fi
  listen_ips=("${interface_ips[0]}")
  declared_listen_ips[${listen_ips[0]}]=1
else
  for ip in "${listen_ips[@]}"; do
    [[ ${host_iface_ips[$ip]+present} ]] ||
      die "Listener IP $ip is not assigned to interface $interface (found: ${interface_ips[*]})."
  done
fi

if [[ -z "$mng_ip" ]]; then
  mng_ip=${listen_ips[0]}
else
  jq -e --arg ip "$mng_ip" '
    any(.[]; .ifname != "lo" and any(.addr_info[]?; .local == $ip))' <<< "$addresses" >/dev/null ||
    die "Management IP $mng_ip must belong to a non-loopback interface on this host."
fi

declare -A binds=()
for (( i=0; i<${#forwards[@]}; i+=4 )); do
  name=${forwards[i]}
  bind=${forwards[i+1]}
  backend=${forwards[i+2]}
  if [[ "$bind" == *:* ]]; then
    bind_ip=${bind%:*}
    port=${bind##*:}
    [[ ${declared_listen_ips[$bind_ip]+present} ]] ||
      die "Listener IP $bind_ip must match the selected interface IP (${listen_ips[*]})."
  else
    bind_ip=${listen_ips[0]}
    port=$bind
  fi
  bind=$bind_ip:$port
  [[ ! ${binds[$bind]+present} ]] || die "IP:PORT listener pairs must be unique."
  binds[$bind]=1
  forwards[i+1]=$bind
  log "TCP route $name: $bind -> $backend:${forwards[i+3]}"
done

node_name=$(hostname)
if (( api_key_supplied )); then
  key_source="supplied"
else
  key_source="auto-generated"
fi
log "preflight: host=$node_name mode=standalone interface=$interface listen_ips=${listen_ips[*]} mng_ip=$mng_ip api_key=$key_source"

security=$(docker info --format '{{json .SecurityOptions}}')
[[ "$security" != *rootless* ]] || die "Rootful Docker is required."
existing=$(docker ps -a --filter 'name=^/haproxy-manager$' --format '{{.Names}}')
[[ -z "$existing" ]] || die "Container haproxy-manager already exists; refusing to replace it."
log "preflight passed; pulling $image"
docker pull "$image"

jq -n --arg self "http://$mng_ip:$mng_port" \
  --rawfile key "$staging/api-key" \
  --arg interface "$interface" --args '
  $ARGS.positional as $args |
  [range(0; $args | length; 4) as $i |
    {name:$args[$i], listen:$args[$i+1],
     address:$args[$i+2], port:($args[$i+3] | tonumber)}] as $rules |
  {
    local: {
      node_url: $self,
      api_key: $key,
      keepalived: {enabled: false, interface: $interface},
      sync: {peers: []}
    },
    cluster: {vips: ""},
    haproxy: {
      servers: [$rules[] | {id:("server-" + .name), name:.name,
                            address:.address, port:.port}],
      backends: [$rules[] | {id:("pool-" + .name), name:.name, mode:"tcp",
                             servers:[("server-" + .name)]}],
      frontends: [$rules[] | {id:("listener-" + .name), name:.name, mode:"tcp",
                              binds:.listen, default_backend:("pool-" + .name)}]
    }
  }' "${forwards[@]}" > "$staging/config.json"
jq -nr --arg user "$admin_user" --arg port "$mng_port" \
  --rawfile password "$staging/admin-password" '
  "HAM_ADMIN_USER=" + $user, "HAM_ADMIN_PASSWORD=" + $password,
  "HAM_LISTEN=0.0.0.0", "HAM_PORT=" + $port, "HAM_APPLY_ON_START=1"' > "$staging/manager.env"
jq -nr --rawfile key "$staging/api-key" '"X-API-Key: " + $key' > "$staging/api-header"

log "writing initial standalone configuration to $data_dir"
mkdir -p -m 0700 -- "$data_dir"
mkdir -m 0700 -- "$data_dir/manager"
mkdir -m 0700 -- "$data_dir/acme" "$data_dir/haproxy" "$data_dir/keepalived"
install -m 0600 "$staging/config.json" "$data_dir/manager/config.json"
install -m 0600 "$staging/manager.env" "$data_dir/manager.env"
install -m 0600 "$staging/api-header" "$data_dir/api-header"
log "starting haproxy-manager (standalone, no Keepalived/VIP)"
docker run -d --name haproxy-manager --hostname "$node_name" \
  --network host --restart unless-stopped \
  --env-file "$data_dir/manager.env" \
  -v "$data_dir/manager:/var/lib/haproxy-manager" \
  -v "$data_dir/acme:/var/lib/acme.sh" \
  -v "$data_dir/haproxy:/etc/haproxy" \
  -v "$data_dir/keepalived:/etc/keepalived" "$image"
log "container started; this does not confirm startup Apply"
log "check http://$mng_ip:$mng_port/api/status?brief=1 with the header file $data_dir/api-header and inspect docker logs"
