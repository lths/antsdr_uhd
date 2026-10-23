#!/bin/sh
# args from BR2_ROOTFS_POST_SCRIPT_ARGS
# $2    board name
. ${BR2_CONFIG}
set -e

INSTALL=install


grep -qE '^::sysinit:/bin/mount -t debugfs' ${TARGET_DIR}/etc/inittab || \
sed -i '/hostname/a\
::sysinit:/bin/mount -t debugfs none /sys/kernel/debug/' ${TARGET_DIR}/etc/inittab

sed -i -e '/::sysinit:\/bin\/hostname -F \/etc\/hostname/d' ${TARGET_DIR}/etc/inittab

sed -i '\|^mtd2 /mnt/jffs2 |d' "${TARGET_DIR}/etc/fstab"

BOARD_DIR="$(dirname $0)"
BOARD_NAME="$(basename ${BOARD_DIR})"
# GENIMAGE_CFG="${BOARD_DIR}/genimage-msd.cfg"
# GENIMAGE_TMP="${BUILD_DIR}/genimage.tmp"
GCC_VERSION=$(${BR2_TOOLCHAIN_EXTERNAL_PREFIX}-gcc --version | head -1 | sed 's/.*(\(.*\))/\1/')
BIN_VERSION=$(${BR2_TOOLCHAIN_EXTERNAL_PREFIX}-as --version | head -1 | sed 's/.*(\(.*\))/\1/')
GCC_TRIPLE=$(${BR2_TOOLCHAIN_EXTERNAL_PREFIX}-gcc -v -c  2>&1 | sed 's/ /\n/g' | grep -e "--target" | awk -F= '{print $2}')
if [ "$BR2_TARGET_GENERIC_ROOT_PASSWD" = "analog" ] ; then
	ROOTPASSWD=$BR2_TARGET_GENERIC_ROOT_PASSWD;
else
	ROOTPASSWD="********"
fi

# rm -rf "${GENIMAGE_TMP}"

# genimage                           \
# 	--rootpath "${TARGET_DIR}"     \
# 	--tmppath "${GENIMAGE_TMP}"    \
# 	--inputpath "${BOARD_DIR}/msd"  \
# 	--outputpath "${TARGET_DIR}/opt/" \
# 	--config "${GENIMAGE_CFG}"

rm -f ${TARGET_DIR}/opt/boot.vfat
rm -Rf ${TARGET_DIR}/etc/dropbear

mkdir -p ${TARGET_DIR}/etc/wpa_supplicant/
mkdir -p ${TARGET_DIR}/mnt/jffs2
mkdir -p ${TARGET_DIR}/etc/dropbear

${INSTALL} -D -m 0755 ${BOARD_DIR}/update_frm.sh ${TARGET_DIR}/sbin/
${INSTALL} -D -m 0755 ${BOARD_DIR}/S10mdev ${TARGET_DIR}/etc/init.d/
${INSTALL} -D -m 0755 ${BOARD_DIR}/S15watchdog ${TARGET_DIR}/etc/init.d/
${INSTALL} -D -m 0755 ${BOARD_DIR}/S20urandom ${TARGET_DIR}/etc/init.d/
${INSTALL} -D -m 0755 ${BOARD_DIR}/S21misc ${TARGET_DIR}/etc/init.d/
${INSTALL} -D -m 0755 ${BOARD_DIR}/S40network ${TARGET_DIR}/etc/init.d/
${INSTALL} -D -m 0644 ${BOARD_DIR}/fw_env.config ${TARGET_DIR}/etc/
${INSTALL} -D -m 0755 ${BOARD_DIR}/device_reboot ${TARGET_DIR}/usr/sbin/
${INSTALL} -D -m 0755 ${BOARD_DIR}/device_passwd ${TARGET_DIR}/usr/sbin/
${INSTALL} -D -m 0644 "${BOARD_DIR}/../antsdr/persistence.sh" "${TARGET_DIR}/usr/lib/antsdr/persistence.sh"
${INSTALL} -D -m 0755 "${BOARD_DIR}/../antsdr/antsdr-diagnostics" "${TARGET_DIR}/usr/sbin/antsdr-diagnostics"
${INSTALL} -D -m 0755 ${BOARD_DIR}/device_persistent_keys ${TARGET_DIR}/usr/sbin/
${INSTALL} -D -m 0755 ${BOARD_DIR}/device_format_jffs2 ${TARGET_DIR}/usr/sbin/
${INSTALL} -D -m 0644 ${BOARD_DIR}/motd ${TARGET_DIR}/etc/
${INSTALL} -D -m 0755 ${BOARD_DIR}/test_ensm_pinctrl.sh ${TARGET_DIR}/usr/sbin/
${INSTALL} -D -m 0644 ${BOARD_DIR}/device_config ${TARGET_DIR}/etc/
${INSTALL} -D -m 0644 ${BOARD_DIR}/mdev.conf ${TARGET_DIR}/etc/
${INSTALL} -D -m 0755 ${BOARD_DIR}/automounter.sh ${TARGET_DIR}/lib/mdev/automounter.sh
${INSTALL} -D -m 0755 ${BOARD_DIR}/ifupdown.sh ${TARGET_DIR}/lib/mdev/ifupdown.sh
${INSTALL} -D -m 0644 ${BOARD_DIR}/input-event-daemon.conf ${TARGET_DIR}/etc/
${INSTALL} -D -m 0755 ${BOARD_DIR}/net_set ${TARGET_DIR}/sbin
${INSTALL} -D -m 0755 ${BOARD_DIR}/e310v2 ${TARGET_DIR}/sbin
${INSTALL} -D -m 0755 ${BOARD_DIR}/ip_set ${TARGET_DIR}/bin
${INSTALL} -D -m 0755 ${BOARD_DIR}/S90device_start.sh ${TARGET_DIR}/etc/init.d/

${INSTALL} -D -m 0755 ${BOARD_DIR}/wpa_supplicant/* ${TARGET_DIR}/etc/wpa_supplicant/

ln -sf ../../wpa_supplicant/ifupdown.sh ${TARGET_DIR}/etc/network/if-up.d/wpasupplicant
ln -sf ../../wpa_supplicant/ifupdown.sh ${TARGET_DIR}/etc/network/if-down.d/wpasupplicant
ln -sf ../../wpa_supplicant/ifupdown.sh ${TARGET_DIR}/etc/network/if-pre-up.d/wpasupplicant
ln -sf ../../wpa_supplicant/ifupdown.sh ${TARGET_DIR}/etc/network/if-post-down.d/wpasupplicant

ln -sf device_reboot ${TARGET_DIR}/usr/sbin/e200_reboot

# Identify the source actually packaged, rather than historical release strings.
python3 "${BOARD_DIR}/../../../scripts/build_info.py" --board "${BOARD_NAME}" \
	--target-dir "${TARGET_DIR}" --compiler "${BR2_TOOLCHAIN_EXTERNAL_PREFIX}-gcc"

for helper in S19storage S35identity S95health S96bootconfirm; do
	${INSTALL} -D -m 0755 "${BOARD_DIR}/../antsdr/$helper" "${TARGET_DIR}/etc/init.d/$helper"
done
${INSTALL} -D -m 0755 "${BOARD_DIR}/../antsdr/antsdr-config" "${TARGET_DIR}/usr/sbin/antsdr-config"
install -d -m 0700 "${TARGET_DIR}/etc/antsdr"

${BR2_TOOLCHAIN_EXTERNAL_PREFIX}-gcc -D_DEFAULT_SOURCE -std=c11 -O2 -Wall -Wextra -Werror \
    "${BOARD_DIR}/../antsdr/health.c" -o "${TARGET_DIR}/usr/sbin/antsdr-health"
