#!/usr/bin/env bash
# Build the exact STM32C0-capable OpenOCD revision used by the timing campaign.

set -euo pipefail

revision=43648fedd39440b06662ec16a0643b3081b1de53
tool_root=${SMATEWAY_TOOL_ROOT:-/srv/bulk/samteway/tooling}
source_dir=${tool_root}/openocd-stm32c0
install_dir=${tool_root}/openocd-stm32c0-install

mkdir -p "${tool_root}"
if [[ ! -d "${source_dir}/.git" ]]; then
    git clone --filter=blob:none https://github.com/openocd-org/openocd.git "${source_dir}"
fi
git -C "${source_dir}" fetch --depth=1 origin "${revision}"
git -C "${source_dir}" checkout --detach "${revision}"
git -C "${source_dir}" submodule update --init --depth=1

(
    cd "${source_dir}"
    ./bootstrap
    ./configure \
        --prefix="${install_dir}" \
        --enable-stlink \
        --enable-internal-jimtcl \
        --enable-internal-libjaylink \
        --disable-werror \
        --disable-doxygen-html \
        --disable-doxygen-pdf
    make -j"${SMATEWAY_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN)}"
    make install
)

"${install_dir}/bin/openocd" --version
