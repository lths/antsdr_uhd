# Fixed slot names and one-byte state avoid importing executable environment text.
setenv antsdr_active root
if fatload mmc 0 ${antsdr_state_addr} antsdr-active 1; then
    if itest.b *${antsdr_state_addr} == 0x61; then setenv antsdr_active a; fi
    if itest.b *${antsdr_state_addr} == 0x62; then setenv antsdr_active b; fi
fi
setenv antsdr_slot ${antsdr_active}
if fatload mmc 0 ${antsdr_state_addr} antsdr-trial 1; then
    setenv antsdr_trial invalid
    if itest.b *${antsdr_state_addr} == 0x61; then setenv antsdr_trial a; fi
    if itest.b *${antsdr_state_addr} == 0x62; then setenv antsdr_trial b; fi
    if test ${antsdr_trial} != invalid; then
        if test -e mmc 0 antsdr-attempted; then
            echo Unconfirmed trial: falling back to ${antsdr_active}
        else
            mw.b ${antsdr_state_addr} 1 1
            if fatwrite mmc 0 ${antsdr_state_addr} antsdr-attempted 1; then
                setenv antsdr_slot ${antsdr_trial}
            else
                echo Trial marker write failed: retaining ${antsdr_active}
            fi
        fi
    fi
fi
setenv antsdr_base_bootargs ${bootargs}
setenv antsdr_choose_prefix 'if test ${antsdr_slot} = root; then setenv antsdr_prefix; else setenv antsdr_prefix antsdr-slots/${antsdr_slot}/; fi'
setenv antsdr_load 'run antsdr_choose_prefix; echo Booting ANTSDR slot ${antsdr_slot}; fatload mmc 0 ${loadbit_addr} ${antsdr_prefix}antsdr.bit && fpga loadb 0 ${loadbit_addr} ${filesize} && fatload mmc 0 ${fit_load_address} ${antsdr_prefix}uImage && fatload mmc 0 ${devicetree_load_address} ${antsdr_prefix}devicetree.dtb && fatload mmc 0 ${ramdisk_load_address} ${antsdr_prefix}uramdisk.image.gz && setenv bootargs ${antsdr_base_bootargs} antsdr.slot=${antsdr_slot} panic=10 && bootm ${fit_load_address} ${ramdisk_load_address} ${devicetree_load_address}'
run antsdr_load
if test ${antsdr_slot} != ${antsdr_active}; then
    echo Trial load failed: booting known working slot ${antsdr_active}
    setenv antsdr_slot ${antsdr_active}
    run antsdr_load
fi
if test ${antsdr_slot} != root; then
    echo Active slot load failed: booting original SD image
    setenv antsdr_slot root
    run antsdr_load
fi
echo No bootable ANTSDR image found
