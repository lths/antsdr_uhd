// SPDX-License-Identifier: GPL-3.0-or-later
#include "common/mock_zero_copy.hpp"
#include "../lib/usrp/ant/ant_cores.hpp"
#include <uhdlib/usrp/cores/radio_ctrl_core_3000.hpp>
#include <uhd/utils/byteswap.hpp>
#include <boost/test/unit_test.hpp>
#include <future>
#include <mutex>
#include <stdexcept>
#include <vector>

using namespace uhd::transport;
class observed_transport : public mock_zero_copy {
public:
    observed_transport() : mock_zero_copy(vrt::if_packet_info_t::LINK_TYPE_CHDR) {}
    managed_recv_buffer::sptr get_recv_buff(double timeout) override {
        last_timeout=timeout;
        return mock_zero_copy::get_recv_buff(timeout);
    }
    double last_timeout=0;
};
static vrt::if_packet_info_t response(size_t sequence) {
    vrt::if_packet_info_t info{};
    info.link_type=vrt::if_packet_info_t::LINK_TYPE_CHDR;
    info.packet_type=vrt::if_packet_info_t::PACKET_TYPE_CONTEXT;
    info.has_sid=true; info.sid=0x00200010;
    info.packet_count=sequence;
    info.num_payload_words32=2; info.num_payload_bytes=8;
    return info;
}
BOOST_AUTO_TEST_CASE(control_timeout_returns_to_normal_after_timed_commands) {
    auto send=std::make_shared<observed_transport>();
    auto recv=std::make_shared<observed_transport>();
    auto core=radio_ctrl_core_3000::make(false,send,recv,0x00100020);
    core->set_time(uhd::time_spec_t(1.0));
    auto first=response(0);
    recv->push_back_recv_packet<uint32_t,uhd::ENDIANNESS_LITTLE>(first,{0,42});
    BOOST_CHECK_EQUAL(core->peek64(0),42);
    BOOST_CHECK_EQUAL(recv->last_timeout,10.0);
    core->set_time(uhd::time_spec_t(0.0));
    auto second=response(1);
    recv->push_back_recv_packet<uint32_t,uhd::ENDIANNESS_LITTLE>(second,{0,43});
    BOOST_CHECK_EQUAL(core->peek64(0),43);
    BOOST_CHECK_EQUAL(recv->last_timeout,2.0);
}
BOOST_AUTO_TEST_CASE(control_queue_rejects_truncated_and_oversized_responses) {
    auto send=std::make_shared<observed_transport>();
    auto core=radio_ctrl_core_3000::make(false,send,nullptr,0x00100020);
    uint32_t tiny=0;
    core->push_response(&tiny,4);
    core->push_response(nullptr,16);
    core->push_response(&tiny,36);
    uint32_t packet[8]={};
    auto info=response(0);
    vrt::if_hdr_pack_le(packet,info);
    packet[info.num_header_words32]=uhd::htowx(uint32_t(0));
    packet[info.num_header_words32+1]=uhd::htowx(uint32_t(123));
    core->push_response(packet,info.num_packet_words32*4);
    BOOST_CHECK_EQUAL(core->peek64(0),123);
}
class recording_wb : public uhd::wb_iface {
public:
    void poke32(const wb_addr_type address,const uint32_t value) override {
        std::lock_guard<std::mutex> lock(mutex); writes.emplace_back(address,value);
    }
    uint32_t peek32(const wb_addr_type) override { return 0; }
    std::mutex mutex;
    std::vector<std::pair<uint32_t,uint32_t>> writes;
};
BOOST_AUTO_TEST_CASE(spi_peripheral_is_restored_after_exception) {
    auto iface=std::make_shared<recording_wb>();
    auto spi=ant_local_spi_core::make(iface);
    uhd::spi_config_t config;
    spi->transact_spi(1,config,1,8,false);
    const auto first=iface->writes.front();
    BOOST_CHECK_THROW(spi->with_peripheral(ant_local_spi_core::PLL,[&] {
        spi->transact_spi(1,config,2,8,false);
        throw std::runtime_error("injected SPI failure");
    }),std::runtime_error);
    spi->transact_spi(1,config,3,8,false);
    std::vector<uint32_t> dividers;
    for(const auto& write:iface->writes) if(write.first==first.first) dividers.push_back(write.second);
    BOOST_REQUIRE_EQUAL(dividers.size(),3);
    BOOST_CHECK_NE(dividers[0],dividers[1]);
    BOOST_CHECK_EQUAL(dividers[0],dividers[2]);
}
BOOST_AUTO_TEST_CASE(spi_peripheral_transaction_excludes_other_threads) {
    auto iface=std::make_shared<recording_wb>();
    auto spi=ant_local_spi_core::make(iface);
    uhd::spi_config_t config;
    std::future<void> competing;
    spi->with_peripheral(ant_local_spi_core::PLL,[&] {
        competing=std::async(std::launch::async,[&] {spi->transact_spi(1,config,1,8,false);});
        BOOST_CHECK(competing.wait_for(std::chrono::milliseconds(20))==std::future_status::timeout);
        spi->transact_spi(1,config,2,8,false);
    });
    BOOST_CHECK(competing.wait_for(std::chrono::seconds(1))==std::future_status::ready);
    competing.get();
}
