//
// Copyright 2013-2014 Ettus Research LLC
// Copyright 2018 Ettus Research, a National Instruments Company
//
// SPDX-License-Identifier: GPL-3.0-or-later
//

#include "ant_cores.hpp"
#include "ant_regs.hpp"
#include <mutex>

namespace { constexpr double SPI_BUS_CLOCK_RATE = 100e6; }

ant_local_spi_core::ant_local_spi_core(uhd::wb_iface::sptr iface, perif_t default_perif)
    : _spi_core(spi_core_3000::make(iface, TOREG(SR_CORE_SPI), RB32_CORE_SPI))
    , _current_perif(default_perif)
    , _last_perif(default_perif)
{
    change_perif(default_perif);
}

uint32_t ant_local_spi_core::transact_spi(int which_slave,
    const uhd::spi_config_t& config,
    uint32_t data,
    size_t num_bits,
    bool readback)
{
    std::lock_guard<std::recursive_mutex> lock(_mutex);
    return _spi_core->transact_spi(which_slave, config, data, num_bits, readback);
}

void ant_local_spi_core::change_perif(perif_t perif)
{
    std::lock_guard<std::recursive_mutex> lock(_mutex);
    _last_perif    = _current_perif;
    _current_perif = perif;

    switch (_current_perif) {
        case CODEC:
            _spi_core->set_divider(SPI_BUS_CLOCK_RATE / AD9361_SPI_RATE);
            break;
        case PLL:
            _spi_core->set_divider(SPI_BUS_CLOCK_RATE / ADF4001_SPI_RATE);
            break;
    }
}

void ant_local_spi_core::restore_perif()
{
    change_perif(_last_perif);
}

void ant_local_spi_core::with_peripheral(perif_t perif,
    const std::function<void()>& transaction)
{
    std::lock_guard<std::recursive_mutex> lock(_mutex);
    const auto previous = _current_perif;
    const auto last = _last_perif;
    try {
        change_perif(perif);
        transaction();
    } catch (...) {
        // Preserve the original transaction exception if restoration also fails.
        try { change_perif(previous); } catch (...) {}
        _last_perif = last;
        throw;
    }
    change_perif(previous);
    _last_perif = last;
}

ant_ref_pll_ctrl::ant_ref_pll_ctrl(ant_local_spi_core::sptr spi)
    : uhd::usrp::adf4001_ctrl(spi, ADF4001_SLAVENO), _spi(spi)
{
}

void ant_ref_pll_ctrl::set_lock_to_ext_ref(bool external)
{
    _spi->with_peripheral(ant_local_spi_core::PLL,
        [this, external]() { adf4001_ctrl::set_lock_to_ext_ref(external); });
}


ant_local_spi_core::sptr ant_local_spi_core::make(
    uhd::wb_iface::sptr iface, ant_local_spi_core::perif_t default_perif)
{
    return sptr(new ant_local_spi_core(iface, default_perif));
}
