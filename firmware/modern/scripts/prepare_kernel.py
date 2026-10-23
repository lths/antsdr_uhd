#!/usr/bin/env python3
"""Carry the E200's FPGA NIC and board description into pinned Linux 6.12."""
from pathlib import Path
import sys

firmware = Path(__file__).resolve().parents[2]
source = Path(sys.argv[1])
text = (firmware / 'linux/drivers/net/ethernet/ni/nixge.c').read_text()
text = text.replace('#include <linux/of_irq.h>', '#include <linux/of_irq.h>\n#include <net/netdev_queues.h>')
text = text.replace('#include <linux/of_platform.h>', '#include <linux/of_platform.h>\n#include <linux/platform_device.h>')
text = text.replace('#define TX_BD_NUM', '#define MII_ADDR_C45 (1U << 30) /* Legacy packed register encoding. */\n\n#define TX_BD_NUM', 1)
text = text.replace('napi_reschedule(napi)', 'napi_schedule(napi)')
text = text.replace('nixge_tx_timeout(struct net_device *ndev)', 'nixge_tx_timeout(struct net_device *ndev, unsigned int txqueue)')
text = text.replace('strlcpy(', 'strscpy(')
text = text.replace('static const struct ethtool_ops nixge_ethtool_ops = {',
                    'static const struct ethtool_ops nixge_ethtool_ops = {\n'
                    '\t.supported_coalesce_params = ETHTOOL_COALESCE_RX_MAX_FRAMES | ETHTOOL_COALESCE_TX_MAX_FRAMES,')
text = text.replace('of_mdiobus_unregister(', 'mdiobus_unregister(')
text = text.replace('priv->phy_mode = of_get_phy_mode(pdev->dev.of_node);',
                    'err = of_get_phy_mode(pdev->dev.of_node, &priv->phy_mode);\n\tif (err) goto free_netdev;')
text = text.replace('class_create(THIS_MODULE, "dummy_char_class")', 'class_create("dummy_char_class")')
text = text.replace('netif_napi_add(ndev, &priv->napi, nixge_poll, NAPI_POLL_WEIGHT)',
                    'netif_napi_add_weight(ndev, &priv->napi, nixge_poll, NAPI_POLL_WEIGHT)')
text = text.replace('ether_addr_copy(ndev->dev_addr, mac_addr)', 'eth_hw_addr_set(ndev, mac_addr)')
text = text.replace('struct ethtool_coalesce *ecoalesce)',
                    'struct ethtool_coalesce *ecoalesce,\n'
                    '\t\t\t\t       struct kernel_ethtool_coalesce *kernel_coalesce,\n'
                    '\t\t\t\t       struct netlink_ext_ack *extack)')
text = text.replace('static int nixge_remove(', 'static void nixge_remove(')
text = text.replace('\n\treturn 0;\n}\n\nstatic struct platform_driver nixge_driver', '\n}\n\nstatic struct platform_driver nixge_driver')
# The source replacement does not need the legacy, unbounded char-device ABI.
# Omit it from the modern driver, rather than exposing it to local processes.
start = text.index('static int dummy_open(')
end = text.index('static int nixge_probe(', start)
text = text[:start] + text[end:]
text = text.replace('dummy_char_cleanup_module();', '')
text = text.replace('dummy_char_init_module();', '')
text = text.replace('dummy_char_init(cdev);', '')
text = text.replace('static struct class *dummy_class;', '')
text = text.replace('static struct cdev dummy_cdev;', '')
text = text.replace('struct net_device *ndev_chr;', '')
text = text.replace('ndev_chr = ndev;', '')
text = text.replace('static unsigned int major; /* major number for device */', '')
text = text.replace('struct device *cdev;', '')
(source / 'drivers/net/ethernet/ni/nixge.c').write_text(text)
(source / 'arch/arm/boot/dts/xilinx/zynq-e200.dts').write_text((firmware / 'modern/board/zynq-e200.dts').read_text())
