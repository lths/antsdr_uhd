// SPDX-License-Identifier: GPL-3.0-or-later
#include "../lib/usrp/common/ad9361_driver/ad9361_device.h"
#include <boost/test/unit_test.hpp>
#include <uhd/exception.hpp>
#include <map>

using namespace uhd::usrp;
class receiver_registers : public ad9361_io {
public:
    std::map<uint32_t, uint8_t> values;
    uint8_t peek8(uint32_t reg) override { return values[reg]; }
    void poke8(uint32_t reg, uint8_t value) override { values[reg] = value; }
};

BOOST_AUTO_TEST_CASE(receiver_live_status_reads_correct_chain_and_shared_tracking)
{
    auto io = std::make_shared<receiver_registers>();
    ad9361_device_t device(nullptr, io);
    io->values = {{0x2b0, 42}, {0x2b5, 59}, {0x0fa, 9}, {0x2b3, 0x25},
                  {0x2b8, 0x45}, {0x2b9, 0x10}, {0x18b, 0xad}, {0x169, 0xcf}};
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_1, "gain_index"), 42);
    io->values[0x2b0] = 61; // AGC changed hardware without a host gain write.
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_1, "gain_index"), 61);
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_2, "gain_index"), 59);
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_1, "agc_mode"), 1);
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_2, "agc_mode"), 2);
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_1, "agc_state"), 5);
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_2, "agc_state"), 2);
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_1, "overload"), 0x45);
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_2, "overload"), 0x10);
    BOOST_CHECK(device.get_rx_status(device.CHAIN_2, "bb_dc_tracking"));
    io->values[0x18b] = 0x8d;
    BOOST_CHECK(!device.get_rx_status(device.CHAIN_1, "bb_dc_tracking"));
    BOOST_CHECK(device.get_rx_status(device.CHAIN_1, "iq_tracking"));
    io->values[0x169] = 0xc0;
    BOOST_CHECK(!device.get_rx_status(device.CHAIN_2, "iq_tracking"));
    BOOST_CHECK_THROW(device.get_rx_status(device.CHAIN_1, "unknown"), uhd::value_error);
}

BOOST_AUTO_TEST_CASE(receiver_agc_transitions_preserve_both_manual_gains)
{
    auto io = std::make_shared<receiver_registers>();
    ad9361_device_t device(nullptr, io);
    device.set_gain(device.RX, device.CHAIN_1, 12);
    device.set_gain(device.RX, device.CHAIN_2, 23);
    device.set_agc_mode(device.CHAIN_1, device.GAIN_MODE_FAST_AGC);
    device.set_agc(device.CHAIN_1, true);
    device.set_agc_mode(device.CHAIN_2, device.GAIN_MODE_SLOW_AGC);
    device.set_agc(device.CHAIN_2, true);
    device.set_agc(device.CHAIN_1, false);
    BOOST_CHECK_EQUAL(device.get_rx_status(device.CHAIN_2, "agc_mode"), 2);
    device.set_agc(device.CHAIN_2, false);
    BOOST_CHECK_EQUAL(io->values[0x109], 12);
    BOOST_CHECK_EQUAL(io->values[0x10c], 23);
}

BOOST_AUTO_TEST_CASE(receiver_rejects_incorrect_fir_object_without_register_writes)
{
    auto io = std::make_shared<receiver_registers>();
    ad9361_device_t device(nullptr, io);
    auto invalid = std::make_shared<uhd::filter_info_base>(
        uhd::filter_info_base::ANALOG_LOW_PASS, false, 0);
    BOOST_CHECK_THROW(device.set_filter(device.RX, device.CHAIN_1, "FIR_1", invalid),
        uhd::value_error);
    BOOST_CHECK(io->values.empty());
}
