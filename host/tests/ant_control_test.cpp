// SPDX-License-Identifier: GPL-3.0-or-later
#include "common/mock_zero_copy.hpp"
#include "../lib/usrp/ant/ant_cores.hpp"
#include "../lib/usrp/ant/ant_lease.hpp"
#include <uhdlib/usrp/cores/radio_ctrl_core_3000.hpp>
#include <uhd/utils/byteswap.hpp>
#include <boost/test/unit_test.hpp>
#include <future>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <vector>

class lease_transport : public uhd::transport::udp_simple {
public:
    size_t send(const boost::asio::const_buffer& buffer) override {
        std::lock_guard<std::mutex> lock(mutex);
        const auto* words=boost::asio::buffer_cast<const uint32_t*>(buffer);
        for (size_t i=0;i<8;i++) response[i]=uhd::ntohx(words[i]);
        const auto op=response[2];
        operations.push_back(op);
        if (op==1) response[4]=1, response[5]=2;
        if (op==2 && (response[4]!=1 || response[5]!=2)) throw std::runtime_error("wrong token");
        response[6]=(op==1 && busy)?1:(op==2 && reject_renew)?2:0;
        response[7]=3000;
        for (auto& word:response) word=uhd::htonx(word);
        pending=true;
        return buffer.size();
    }
    size_t recv(const boost::asio::mutable_buffer& buffer,double) override {
        std::lock_guard<std::mutex> lock(mutex);
        if (!pending) return 0;
        std::memcpy(boost::asio::buffer_cast<void*>(buffer),response,sizeof(response));
        pending=false;
        return sizeof(response);
    }
    std::string get_recv_addr() override { return "127.0.0.1"; }
    std::string get_send_addr() override { return "127.0.0.1"; }
    std::mutex mutex;
    std::vector<uint32_t> operations;
    uint32_t response[8]{};
    bool pending=false,busy=false,reject_renew=false;
};
BOOST_AUTO_TEST_CASE(device_lease_renews_and_releases_after_host_teardown) {
    auto transport=std::make_shared<lease_transport>();
    {
        ant_device_lease lease(transport,std::chrono::milliseconds(5));
        for (unsigned i=0;i<100;i++) {
            { std::lock_guard<std::mutex> lock(transport->mutex);
              if(transport->operations.size()>1) break; }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        lease.check();
    }
    BOOST_REQUIRE_GE(transport->operations.size(),3);
    BOOST_CHECK_EQUAL(transport->operations.front(),1);
    BOOST_CHECK_EQUAL(transport->operations[1],2);
    BOOST_CHECK_EQUAL(transport->operations.back(),3);
}
BOOST_AUTO_TEST_CASE(device_lease_rejects_a_competing_host) {
    auto transport=std::make_shared<lease_transport>(); transport->busy=true;
    BOOST_CHECK_THROW(ant_device_lease lease(transport),uhd::runtime_error);
    BOOST_REQUIRE_EQUAL(transport->operations.size(),1);
}
BOOST_AUTO_TEST_CASE(device_lease_does_not_reacquire_after_ownership_loss) {
    auto transport=std::make_shared<lease_transport>(); transport->reject_renew=true;
    {
        ant_device_lease lease(transport,std::chrono::milliseconds(5));
        bool lost=false;
        for (unsigned i=0;i<100;i++) {
            try { lease.check(); } catch(const uhd::runtime_error&) { lost=true; break; }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        BOOST_CHECK(lost);
    }
    BOOST_REQUIRE_EQUAL(transport->operations.size(),3);
    BOOST_CHECK_EQUAL(transport->operations[0],1);
    BOOST_CHECK_EQUAL(transport->operations[1],2);
    BOOST_CHECK_EQUAL(transport->operations[2],3);
}
BOOST_AUTO_TEST_CASE(device_lease_survives_until_the_last_control_transport) {
    auto udp=std::make_shared<lease_transport>();
    auto lease=std::make_shared<ant_device_lease>(udp,std::chrono::seconds(10));
    std::weak_ptr<ant_device_lease> observed=lease;
    auto inner=std::make_shared<mock_zero_copy>(uhd::transport::vrt::if_packet_info_t::LINK_TYPE_CHDR);
    auto transport=std::make_shared<ant_leased_transport>(lease,inner);
    lease.reset();
    BOOST_CHECK(!observed.expired());
    BOOST_CHECK_EQUAL(udp->operations.size(),1);
    transport.reset();
    BOOST_CHECK(observed.expired());
    BOOST_REQUIRE_EQUAL(udp->operations.size(),2);
    BOOST_CHECK_EQUAL(udp->operations.back(),3);
}

using namespace uhd::transport;
class concurrent_send_probe : public mock_zero_copy {
public:
    concurrent_send_probe() : mock_zero_copy(vrt::if_packet_info_t::LINK_TYPE_CHDR) {}
    managed_send_buffer::sptr get_send_buff(double) override {
        if (++active > 1) overlap = true;
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        --active;
        return {};
    }
    std::atomic<unsigned> active{0};
    std::atomic<bool> overlap{false};
};
BOOST_AUTO_TEST_CASE(shared_control_transport_serializes_concurrent_buffer_allocation) {
    auto inner = std::make_shared<concurrent_send_probe>();
    // Null lease also exercises the explicit legacy-firmware path.
    ant_leased_transport transport(nullptr, inner);
    std::atomic<bool> start{false};
    auto worker = [&] {
        while (!start) std::this_thread::yield();
        for (unsigned i = 0; i < 100; ++i) transport.get_send_buff(0.0);
    };
    auto first = std::async(std::launch::async, worker);
    auto second = std::async(std::launch::async, worker);
    start = true;
    first.get();
    second.get();
    BOOST_CHECK(!inner->overlap);
}
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
