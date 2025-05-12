#!/usr/bin/env bash


# Example of accessing environment variables
echo "Cluster Discovery Strategy: $EMQX_CLUSTER__DISCOVERY_STRATEGY"
echo "DNS Name: $EMQX_CLUSTER__DNS__NAME"
echo "DNS Record Type: $EMQX_CLUSTER__DNS__RECORD_TYPE"

## Shell setting
if [[ -n "$DEBUG" ]]; then
    set -ex
else
    set -e
fi

shopt -s nullglob

## Local IP address setting

LOCAL_IP=$(hostname -i | grep -oE '((25[0-5]|(2[0-4]|1[0-9]|[1-9]|)[0-9])\.){3}(25[0-5]|(2[0-4]|1[0-9]|[1-9]|)[0-9])' | head -n 1)

export EMQX_NAME="${EMQX_NAME:-emqx}"

## EMQX_NODE_NAME or EMQX_NODE__NAME to indicate the full node name to be used by EMQX
## If both are set EMQX_NODE_NAME takes higher precedence than EMQX_NODE__NAME
if [[ -z "${EMQX_NODE_NAME:-}" ]] && [[ -z "${EMQX_NODE__NAME:-}" ]]; then
    # No node name is provide from environment variables
    # try to resolve from other settings
    if [[ -z "$EMQX_HOST" ]]; then
        if [[ "$EMQX_CLUSTER__DISCOVERY_STRATEGY" == "dns" ]] && \
            [[ "$EMQX_CLUSTER__DNS__RECORD_TYPE" == "srv" ]] && \
            grep -q "$(hostname).$EMQX_CLUSTER__DNS__NAME" /etc/hosts; then
                EMQX_HOST="$(hostname).$EMQX_CLUSTER__DNS__NAME"
        elif [[ "$EMQX_CLUSTER__DISCOVERY_STRATEGY" == "k8s" ]] && \
            [[ "$EMQX_CLUSTER__K8S__ADDRESS_TYPE" == "dns" ]] && \
            [[ -n "$EMQX_CLUSTER__K8S__NAMESPACE" ]]; then
                EMQX_CLUSTER__K8S__SUFFIX=${EMQX_CLUSTER__K8S__SUFFIX:-"pod.cluster.local"}
                EMQX_HOST="${LOCAL_IP//./-}.$EMQX_CLUSTER__K8S__NAMESPACE.$EMQX_CLUSTER__K8S__SUFFIX"
        elif [[ "$EMQX_CLUSTER__DISCOVERY_STRATEGY" == "k8s" ]] && \
            [[ "$EMQX_CLUSTER__K8S__ADDRESS_TYPE" == 'hostname' ]] && \
            [[ -n "$EMQX_CLUSTER__K8S__NAMESPACE" ]]; then
                EMQX_CLUSTER__K8S__SUFFIX=${EMQX_CLUSTER__K8S__SUFFIX:-'svc.cluster.local'}
                EMQX_HOST=$(grep -h "^$LOCAL_IP" /etc/hosts | grep -o "$(hostname).*.$EMQX_CLUSTER__K8S__NAMESPACE.$EMQX_CLUSTER__K8S__SUFFIX")
        else
            EMQX_HOST="$LOCAL_IP"
        fi
        export EMQX_HOST
    fi
    export EMQX_NODE_NAME="$EMQX_NAME@$EMQX_HOST"
fi

# Set the dashboard default username and password
export EMQX_DASHBOARD__DEFAULT_USERNAME="${EMQX_DASH_ADMIN_USER}"
export EMQX_DASHBOARD__DEFAULT_PASSWORD="${EMQX_DASH_ADMIN_PASSWORD}"

# Create init_user.json with environment variables
cat <<EOF > /opt/emqx/init_user.json
[
  {
    "user_id": "${ADMIN_USER}",
    "password": "${ADMIN_PASSWORD}",
    "is_superuser": true
  },
  {
    "user_id": "${SUB_USER}",
    "password": "${SUB_PASSWORD}",
    "is_superuser": false
  },
  {
    "user_id": "${PUB_USER}",
    "password": "${PUB_PASSWORD}",
    "is_superuser": false
  },
  {
    "user_id": "${EVERYONE_USER}",
    "password": "${EVERYONE_PASSWORD}",
    "is_superuser": false
  }
]
EOF

exec "$@"