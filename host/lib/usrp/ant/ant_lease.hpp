// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <uhd/exception.hpp>
#include <uhd/transport/udp_simple.hpp>
#include <uhd/transport/zero_copy.hpp>
#include <uhd/utils/byteswap.hpp>
#include <uhd/utils/log.hpp>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <random>
#include <string>
#include <utility>
#include <thread>

// The lease owns the radio for the lifetime of ant_impl, including all controller
// teardown. No automatic takeover/reacquire: lost ownership invalidates the
// session. The FPGA independently expires it even if this process is SIGKILLed.
class ant_device_lease {
public:
    explicit ant_device_lease(uhd::transport::udp_simple::sptr transport,
        std::chrono::milliseconds interval = std::chrono::milliseconds(500))
        : _transport(std::move(transport)), _nonce(std::random_device{}())
    {
        transact(1);
        _valid = true;
        try {
            _thread = std::thread([this, interval] {
                std::unique_lock<std::mutex> lock(_mutex);
                while (!_stop.wait_for(lock, interval, [this] { return _stopping; })) {
                    try { transact(2); }
                    catch (const std::exception& e) {
                        _valid = false;
                        UHD_LOGGER_ERROR("ANT") << "Device ownership lost: " << e.what()
                            << ". Close this device and reconnect.";
                        try { transact(3); } catch (...) {}
                        return;
                    }
                }
            });
        } catch (...) {
            try { transact(3); } catch (...) {}
            throw;
        }
    }
    ~ant_device_lease()
    {
        { std::lock_guard<std::mutex> lock(_mutex); _stopping = true; }
        _stop.notify_all();
        if (_thread.joinable()) _thread.join();
        if (_valid) { try { transact(3); } catch (...) {} }
    }
    void check() const
    {
        if (!_valid) throw uhd::runtime_error("ANTSDR device ownership lease has expired");
    }
private:
    void transact(uint32_t operation)
    {
        uint32_t request[8] = {0x414c5331, 1, operation, ++_nonce,
            uint32_t(_token >> 32), uint32_t(_token), 0, 0};
        for (auto& word : request) word = uhd::htonx(word);
        for (unsigned attempt = 0; attempt < 3; attempt++) {
            _transport->send(boost::asio::buffer(request));
            const auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(200);
            do {
                // An oversized datagram must not be mistaken for a valid reply.
                uint32_t response[9] = {};
                const auto n = _transport->recv(boost::asio::buffer(response), .05);
                if (n != 32) continue;
                for (auto& word : response) word = uhd::ntohx(word);
                if (response[0] != 0x414c5331 || response[1] != 1 ||
                    response[2] != operation || response[3] != _nonce) continue;
                if (response[6] == 1) throw uhd::runtime_error("ANTSDR is owned by another session");
                if (response[6]) throw uhd::runtime_error("ANTSDR lease rejected (status "
                    + std::to_string(response[6]) + ")");
                if (operation != 3) {
                    const uint64_t value = (uint64_t(response[4]) << 32) | response[5];
                    if (!value || response[7] < 1000 || response[7] > 3000 ||
                        (operation == 2 && value != _token))
                        throw uhd::runtime_error("Invalid ANTSDR lease acknowledgement");
                    _token = value;
                }
                return;
            } while (std::chrono::steady_clock::now() < until);
        }
        throw uhd::runtime_error("ANTSDR lease service did not respond on UDP 49101. "
            "Use matching runtime 2 firmware. Older firmware requires legacy_no_lease=1.");
    }
    uhd::transport::udp_simple::sptr _transport;
    uint32_t _nonce;
    uint64_t _token = 0;
    std::atomic<bool> _valid{false};
    std::mutex _mutex;
    std::condition_variable _stop;
    bool _stopping = false;
    std::thread _thread;
};

// Property-tree subscribers can retain radio controllers after ant_impl's
// members are destroyed. Keep ownership attached to their actual RPC transport
// so their final acknowledgement still happens before the device is released.
class ant_leased_transport : public uhd::transport::zero_copy_if {
public:
    ant_leased_transport(std::shared_ptr<ant_device_lease> lease, sptr transport)
        : _lease(std::move(lease)), _transport(std::move(transport)) {}
    uhd::transport::managed_recv_buffer::sptr get_recv_buff(double timeout) override
    { return _transport->get_recv_buff(timeout); }
    uhd::transport::managed_send_buffer::sptr get_send_buff(double timeout) override
    {
        // Local SPI and radio time/control endpoints share one UDP buffer
        // pool. Its send-buffer index is not thread safe, even though each
        // radio controller separately locks its own command sequence.
        std::lock_guard<std::mutex> lock(_send_mutex);
        if (_lease) _lease->check();
        return _transport->get_send_buff(timeout);
    }
    size_t get_num_recv_frames() const override { return _transport->get_num_recv_frames(); }
    size_t get_recv_frame_size() const override { return _transport->get_recv_frame_size(); }
    size_t get_num_send_frames() const override { return _transport->get_num_send_frames(); }
    size_t get_send_frame_size() const override { return _transport->get_send_frame_size(); }
private:
    std::shared_ptr<ant_device_lease> _lease;
    sptr _transport;
    std::mutex _send_mutex;
};
