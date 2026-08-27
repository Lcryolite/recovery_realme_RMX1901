#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
KERNEL_SOURCE="${1:?usage: build_kernel.sh KERNEL_SOURCE [KERNEL_OUTPUT] [CROSS_COMPILE] [JOBS]}"
KERNEL_SOURCE="$(CDPATH= cd -- "${KERNEL_SOURCE}" && pwd)"
KERNEL_OUTPUT="${2:-${KERNEL_SOURCE}/out-rmx1901}"
if [[ "${KERNEL_OUTPUT}" != /* ]]; then
    KERNEL_OUTPUT="${KERNEL_SOURCE}/${KERNEL_OUTPUT}"
fi
CROSS_COMPILE="${3:-${CROSS_COMPILE:-aarch64-linux-gnu-}}"
JOBS="${4:-${KERNEL_JOBS:-$(nproc)}}"
LINUX_COMMIT="${LINUX_COMMIT:-8d3ae59288f1e7d58d76558a6ee96d533bc5019f}"
DTS_NAME="sdm710-realme-rmx1901.dts"
DTB_NAME="sdm710-realme-rmx1901.dtb"
DTS_DIR="${KERNEL_SOURCE}/arch/arm64/boot/dts/qcom"
DTB_ENTRY='dtb-$(CONFIG_ARCH_QCOM) += sdm710-realme-rmx1901.dtb'

if ! git -C "${KERNEL_SOURCE}" rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "kernel source is not a git checkout: ${KERNEL_SOURCE}" >&2
    exit 1
fi

ACTUAL_COMMIT="$(git -C "${KERNEL_SOURCE}" rev-parse HEAD)"
if [[ "${ACTUAL_COMMIT}" != "${LINUX_COMMIT}" ]]; then
    echo "expected Linux commit ${LINUX_COMMIT}, got ${ACTUAL_COMMIT}" >&2
    exit 1
fi

install -D -m 0644 "${SCRIPT_DIR}/dts/sdm710.dtsi" \
    "${DTS_DIR}/sdm710.dtsi"
install -D -m 0644 "${SCRIPT_DIR}/dts/${DTS_NAME}" \
    "${DTS_DIR}/${DTS_NAME}"

if ! grep -Fqx "${DTB_ENTRY}" "${DTS_DIR}/Makefile"; then
    printf '\n%s\n' "${DTB_ENTRY}" >> "${DTS_DIR}/Makefile"
fi

make -C "${KERNEL_SOURCE}" O="${KERNEL_OUTPUT}" ARCH=arm64 \
    CROSS_COMPILE="${CROSS_COMPILE}" defconfig
"${KERNEL_SOURCE}/scripts/kconfig/merge_config.sh" -m -y \
    -O "${KERNEL_OUTPUT}" "${KERNEL_OUTPUT}/.config" \
    "${SCRIPT_DIR}/kernel.fragment"
make -C "${KERNEL_SOURCE}" O="${KERNEL_OUTPUT}" ARCH=arm64 \
    CROSS_COMPILE="${CROSS_COMPILE}" olddefconfig
make -C "${KERNEL_SOURCE}" O="${KERNEL_OUTPUT}" ARCH=arm64 \
    CROSS_COMPILE="${CROSS_COMPILE}" -j"${JOBS}" \
    Image.gz "qcom/${DTB_NAME}"

mkdir -p "${PROJECT_ROOT}/prebuilt"
cat "${KERNEL_OUTPUT}/arch/arm64/boot/Image.gz" \
    "${KERNEL_OUTPUT}/arch/arm64/boot/dts/qcom/${DTB_NAME}" \
    > "${PROJECT_ROOT}/prebuilt/Image.gz-dtb"

python3 "${PROJECT_ROOT}/tools/recovery_image_guard.py" kernel \
    "${PROJECT_ROOT}/prebuilt/Image.gz-dtb"
