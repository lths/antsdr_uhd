//
// Copyright 2012-2015 Ettus Research LLC
// Copyright 2018 Ettus Research, a National Instruments Company
//
// SPDX-License-Identifier: GPL-3.0-or-later
//

#include <uhd/exception.hpp>
#include <uhd/transport/bounded_buffer.hpp>
#include <uhd/transport/vrt_if_packet.hpp>
#include <uhd/utils/byteswap.hpp>
#include <uhd/utils/log.hpp>
#include <uhd/utils/safe_call.hpp>
#include <uhdlib/usrp/common/async_packet_handler.hpp>
#include <uhdlib/usrp/cores/radio_ctrl_core_3000.hpp>
#include <boost/format.hpp>
#include <boost/thread/mutex.hpp>
#include <boost/thread/thread.hpp>
#include <functional>
#include <chrono>
#include <queue>

using namespace uhd;
using namespace uhd::usrp;
using namespace uhd::transport;

static const double ACK_TIMEOUT     = 2.0; // supposed to be worst case practical timeout
static const double MASSIVE_TIMEOUT = 10.0; // for when we wait on a timed command
static const size_t SR_READBACK = 32;

radio_ctrl_core_3000::~radio_ctrl_core_3000(void)
{
    /* NOP */
}

class radio_ctrl_core_3000_impl : public radio_ctrl_core_3000
{
public:
    radio_ctrl_core_3000_impl(const bool big_endian,
        uhd::transport::zero_copy_if::sptr ctrl_xport,
        uhd::transport::zero_copy_if::sptr resp_xport,
        const uint32_t sid,
        const std::string& name)
        : _link_type(vrt::if_packet_info_t::LINK_TYPE_CHDR)
        , _packet_type(vrt::if_packet_info_t::PACKET_TYPE_CONTEXT)
        , _bige(big_endian)
        , _ctrl_xport(ctrl_xport)
        , _resp_xport(resp_xport)
        , _sid(sid)
        , _name(name)
        , _seq_out(0)
        , _timeout(ACK_TIMEOUT)
        , _resp_queue(128 /*max response msgs*/)
        , _resp_queue_size(_resp_xport ? _resp_xport->get_num_recv_frames() : 3)
    {
        if (resp_xport) {
            while (resp_xport->get_recv_buff(0.0)) {
            } // flush
        }
        this->set_time(uhd::time_spec_t(0.0));
        this->set_tick_rate(1.0); // something possible but bogus
    }

    ~radio_ctrl_core_3000_impl(void) override
    {
        _timeout = ACK_TIMEOUT; // reset timeout to something small
        UHD_SAFE_CALL(
            this->peek32(0); // dummy peek with the purpose of ack'ing all packets
            _async_task.reset(); // now its ok to release the task
        )
    }

    /*******************************************************************
     * Peek and poke 32 bit implementation
     ******************************************************************/
    void poke32(const wb_addr_type addr, const uint32_t data) override
    {
        boost::mutex::scoped_lock lock(_mutex);
        this->send_pkt(addr / 4, data);
        this->wait_for_ack(false);
    }

    uint32_t peek32(const wb_addr_type addr) override
    {
        boost::mutex::scoped_lock lock(_mutex);
        this->send_pkt(SR_READBACK, addr / 8);
        const uint64_t res = this->wait_for_ack(true);
        const uint32_t lo  = uint32_t(res & 0xffffffff);
        const uint32_t hi  = uint32_t(res >> 32);
        return ((addr / 4) & 0x1) ? hi : lo;
    }

    uint64_t peek64(const wb_addr_type addr) override
    {
        boost::mutex::scoped_lock lock(_mutex);
        this->send_pkt(SR_READBACK, addr / 8);
        return this->wait_for_ack(true);
    }

    /*******************************************************************
     * Update methods for time
     ******************************************************************/
    void set_time(const uhd::time_spec_t& time) override
    {
        boost::mutex::scoped_lock lock(_mutex);
        _time     = time;
        _use_time = _time != uhd::time_spec_t(0.0);
        _timeout = _use_time ? MASSIVE_TIMEOUT : ACK_TIMEOUT;
    }

    uhd::time_spec_t get_time(void) override
    {
        boost::mutex::scoped_lock lock(_mutex);
        return _time;
    }

    void set_tick_rate(const double rate) override
    {
        boost::mutex::scoped_lock lock(_mutex);
        _tick_rate = rate;
    }

private:
    // This is the buffer type for messages in radio control core.
    struct resp_buff_type
    {
        uint32_t data[8];
        size_t bytes;
    };

    /*******************************************************************
     * Primary control and interaction private methods
     ******************************************************************/
    UHD_INLINE void send_pkt(const uint32_t addr, const uint32_t data = 0)
    {
        managed_send_buffer::sptr buff = _ctrl_xport->get_send_buff(0.0);
        if (not buff) {
            throw uhd::runtime_error("fifo ctrl timed out getting a send buffer");
        }
        uint32_t* pkt = buff->cast<uint32_t*>();

        // load packet info
        vrt::if_packet_info_t packet_info;
        packet_info.link_type           = _link_type;
        packet_info.packet_type         = _packet_type;
        packet_info.num_payload_words32 = 2;
        packet_info.num_payload_bytes =
            packet_info.num_payload_words32 * sizeof(uint32_t);
        packet_info.packet_count = _seq_out;
        packet_info.tsf          = _time.to_ticks(_tick_rate);
        packet_info.sob          = false;
        packet_info.eob          = false;
        packet_info.sid          = _sid;
        packet_info.has_sid      = true;
        packet_info.has_cid      = false;
        packet_info.has_tsi      = false;
        packet_info.has_tsf      = _use_time;
        packet_info.has_tlr      = false;

        // load header
        if (_bige)
            vrt::if_hdr_pack_be(pkt, packet_info);
        else
            vrt::if_hdr_pack_le(pkt, packet_info);

        // load payload
        pkt[packet_info.num_header_words32 + 0] = (_bige) ? uhd::htonx(addr)
                                                          : uhd::htowx(addr);
        pkt[packet_info.num_header_words32 + 1] = (_bige) ? uhd::htonx(data)
                                                          : uhd::htowx(data);
        // UHD_LOGGER_INFO("radio_ctrl") << boost::format("0x%08x, 0x%08x\n") % addr %
        // data; send the buffer over the interface
        buff->commit(sizeof(uint32_t) * (packet_info.num_packet_words32));
        _outstanding_seqs.push(_seq_out);

        _seq_out++; // inc seq for next call
    }

    UHD_INLINE uint64_t wait_for_ack(const bool readback)
    {
        while (readback or (_outstanding_seqs.size() >= _resp_queue_size)) {
            // get seq to ack from outstanding packets list
            UHD_ASSERT_THROW(not _outstanding_seqs.empty());
            const size_t seq_to_ack = _outstanding_seqs.front();

            // parse the packet
            vrt::if_packet_info_t packet_info;
            resp_buff_type resp_buff;
            memset(&resp_buff, 0x00, sizeof(resp_buff));
            uint32_t const* pkt = NULL;
            managed_recv_buffer::sptr buff;

            // get buffer from response endpoint - or die in timeout
            if (_resp_xport) {
                buff = _resp_xport->get_recv_buff(_timeout);
                try {
                    UHD_ASSERT_THROW(bool(buff));
                    UHD_ASSERT_THROW(buff->size() >= 16 && buff->size() <= 32);
                    UHD_ASSERT_THROW(buff->size() % sizeof(uint32_t) == 0);
                } catch (const std::exception& ex) {
                    throw uhd::io_error(
                        str(boost::format("Radio ctrl (%s) no response packet - %s")
                            % _name % ex.what()));
                }
                pkt                            = buff->cast<const uint32_t*>();
                packet_info.num_packet_words32 = buff->size() / sizeof(uint32_t);
            }

            // get buffer from response endpoint - or die in timeout
            else {
                /*
                 * Couldn't get message with haste.
                 * Now check both possible queues for messages.
                 * Messages should come in on _resp_queue,
                 * but could end up in dump_queue.
                 * If we don't get a message --> Die in timeout.
                 */
                const auto deadline = std::chrono::steady_clock::now()
                    + std::chrono::duration<double>(_timeout);
                const double short_timeout = 0.005; // == 5ms
                while (
                    not((_resp_queue.pop_with_haste(resp_buff))
                        || (check_dump_queue(resp_buff))
                        || (_resp_queue.pop_with_timed_wait(resp_buff, short_timeout)))) {
                    /*
                     * If a message couldn't be received within a given timeout
                     * --> throw AssertionError!
                     */
                    if (std::chrono::steady_clock::now() >= deadline)
                        throw uhd::io_error("Radio ctrl response deadline expired");
                }

                pkt                            = resp_buff.data;
                packet_info.num_packet_words32 = resp_buff.bytes / sizeof(uint32_t);
            }

            // parse the buffer
            try {
                packet_info.link_type = _link_type;
                if (_bige)
                    vrt::if_hdr_unpack_be(pkt, packet_info);
                else
                    vrt::if_hdr_unpack_le(pkt, packet_info);
            } catch (const std::exception& ex) {
                throw uhd::io_error(std::string("Radio ctrl malformed packet: ") + ex.what());
            }

            // check the buffer
            try {
                UHD_ASSERT_THROW(packet_info.has_sid);
                UHD_ASSERT_THROW(
                    packet_info.sid == uint32_t((_sid >> 16) | (_sid << 16)));
                UHD_ASSERT_THROW(packet_info.packet_count == (seq_to_ack & 0xfff));
                UHD_ASSERT_THROW(packet_info.num_payload_words32 == 2);
                UHD_ASSERT_THROW(packet_info.packet_type == _packet_type);
                UHD_ASSERT_THROW(packet_info.num_header_words32 + 2 <= packet_info.num_packet_words32);
            } catch (const std::exception& ex) {
                throw uhd::io_error(
                    str(boost::format("Radio ctrl (%s) packet parse error - %s") % _name
                        % ex.what()));
            }

            _outstanding_seqs.pop();
            // return the readback value
            if (readback and _outstanding_seqs.empty()) {
                const uint64_t hi =
                    (_bige) ? uhd::ntohx(pkt[packet_info.num_header_words32 + 0])
                            : uhd::wtohx(pkt[packet_info.num_header_words32 + 0]);
                const uint64_t lo =
                    (_bige) ? uhd::ntohx(pkt[packet_info.num_header_words32 + 1])
                            : uhd::wtohx(pkt[packet_info.num_header_words32 + 1]);
                return ((hi << 32) | lo);
            }
        }

        return 0;
    }

    /*
     * If ctrl_core waits for a message that didn't arrive it can search for it in the
     * dump queue. This actually happens during shutdown. handle_async_task can't access
     * radio_ctrl_cores queue anymore thus it returns the corresponding message. msg_task
     * class implements a dump_queue to store such messages. With check_dump_queue we can
     * check if a message we are waiting for got stranded there. If a message got stuck we
     * get it here and push it onto our own message_queue.
     */
    bool check_dump_queue(resp_buff_type& b)
    {
        const size_t min_buff_size = 16;
        if (!_async_task)
            return false;
        uint32_t recv_sid = (((_sid) << 16) | ((_sid) >> 16));
        uhd::msg_task::msg_payload_t msg;
        do {
            msg = _async_task->get_msg_from_dump_queue(recv_sid);
        } while (msg.size() < min_buff_size && !msg.empty());

        if (msg.size() >= min_buff_size && msg.size() <= sizeof(b.data)
            && msg.size() % sizeof(uint32_t) == 0) {
            memset(&b, 0, sizeof(b));
            memcpy(b.data, &msg.front(), msg.size());
            b.bytes = msg.size();
            return true;
        }
        return false;
    }

    void push_response(const uint32_t* buff, size_t bytes) override
    {
        if (!buff || bytes < 16 || bytes > sizeof(resp_buff_type::data)
            || bytes % sizeof(uint32_t) != 0)
            return;
        resp_buff_type resp_buff{};
        std::memcpy(resp_buff.data, buff, bytes);
        resp_buff.bytes = bytes;
        _resp_queue.push_with_haste(resp_buff);
    }

    void hold_task(uhd::msg_task::sptr task) override
    {
        _async_task = task;
    }

    const vrt::if_packet_info_t::link_type_t _link_type;
    const vrt::if_packet_info_t::packet_type_t _packet_type;
    const bool _bige;
    const uhd::transport::zero_copy_if::sptr _ctrl_xport;
    const uhd::transport::zero_copy_if::sptr _resp_xport;
    uhd::msg_task::sptr _async_task;
    const uint32_t _sid;
    const std::string _name;
    boost::mutex _mutex;
    size_t _seq_out;
    uhd::time_spec_t _time;
    bool _use_time;
    double _tick_rate;
    double _timeout;
    std::queue<size_t> _outstanding_seqs;
    bounded_buffer<resp_buff_type> _resp_queue;
    const size_t _resp_queue_size;
};

radio_ctrl_core_3000::sptr radio_ctrl_core_3000::make(const bool big_endian,
    zero_copy_if::sptr ctrl_xport,
    zero_copy_if::sptr resp_xport,
    const uint32_t sid,
    const std::string& name)
{
    return sptr(
        new radio_ctrl_core_3000_impl(big_endian, ctrl_xport, resp_xport, sid, name));
}
