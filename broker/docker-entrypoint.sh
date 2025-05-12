#!/bin/ash

set -e

# Fix write permissions for mosquitto directories
chown --no-dereference --recursive mosquitto /mosquitto
chown --no-dereference --recursive mosquitto /mosquitto/log
chown --no-dereference --recursive mosquitto /mosquitto/data

mkdir -p /var/run/mosquitto \
  && chown --no-dereference --recursive mosquitto /var/run/mosquitto

# Adjust configuration to support Mosquitto 2.x
if [ $(echo $VERSION | cut -d "." -f1) -gt 1 ]; then
  # Use 'listener' instead of 'port'
  sed -i "s/port 1883/listener 1883/g" /mosquitto/config/mosquitto.conf
fi

if ( [ -z "${ADMIN_USER}" ] || [ -z "${ADMIN_PASSWORD}" ] ); then
  echo "ADMIN_USER or ADMIN_PASSWORD not defined"
  exit 1
fi

if ( [ -z "${WIS2_USER}" ] || [ -z "${WIS2_PASSWORD}" ] ); then
  echo "WIS_USER or WIS_PASSWORD not defined"
  exit 1
fi

# create mosquitto passwordfile
touch /mosquitto/passwordfile
chmod 0700 /mosquitto/passwordfile
chown mosquitto /mosquitto/passwordfile
mosquitto_passwd -b /mosquitto/passwordfile $ADMIN_USER $ADMIN_PASSWORD
mosquitto_passwd -b /mosquitto/passwordfile $WIS2_USER $WIS2_PASSWORD

# create acl file
cat > /mosquitto/permissions.acl <<EOF
user $ADMIN_USER
topic #
topic \$SYS/#

user $WIS2_USER
topic read cache/#
topic deny \$SYS/#
EOF

chmod 0700 /mosquitto/permissions.acl
chown mosquitto /mosquitto/permissions.acl
exec "$@"