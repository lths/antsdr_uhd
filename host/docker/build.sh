#!/bin/bash
set -euo pipefail
cmake -S host -B host/build-antsdr -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DENABLE_PYTHON_API=ON -DENABLE_TESTS=ON -DENABLE_ANT=ON \
    -DENABLE_B200=OFF -DENABLE_X300=OFF -DENABLE_MPMD=OFF \
    -DENABLE_N300=OFF -DENABLE_N320=OFF -DENABLE_E320=OFF -DENABLE_E300=OFF \
    -DENABLE_USRP1=OFF -DENABLE_USRP2=OFF -DENABLE_B100=OFF \
    -DENABLE_OCTOCLOCK=OFF -DENABLE_MANUAL=OFF -DENABLE_DOXYGEN=OFF
cmake --build host/build-antsdr -j "${JOBS:-8}" --target \
    pyuhd_library uhd_usrp_probe benchmark_rate ant_control_test ad9361_receiver_test vrt_test sph_recv_test
ctest --test-dir host/build-antsdr -R '^(ant_control_test|ad9361_receiver_test|vrt_test|sph_recv_test)$' --output-on-failure
