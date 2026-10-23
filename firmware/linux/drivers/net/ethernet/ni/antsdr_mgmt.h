/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Narrow E200 management API. No offsets, DMA access, or userspace MMIO.
 * Root opens these sysfs files before dropping privileges. kernfs protects
 * their lifetime during unbind; all multi-register transactions are serialized.
 */
#include <linux/mutex.h>
#include <linux/sysfs.h>
#define ANTSDR_MAGIC 0x414d4731
#define ANTSDR_ABI 0x00010000
#define ANTSDR_SIGNATURE 0x100c
#define ANTSDR_ABI_REG 0x1010
#define ANTSDR_OWNER 0x1014
#define ANTSDR_TIMEOUT 0x1018
#define ANTSDR_EXPIRED 0x101c
#define ANTSDR_IP 0x2000

static bool antsdr_compatible(struct nixge_priv *p)
{
	return nixge_ctrl_read_reg(p, ANTSDR_SIGNATURE) == ANTSDR_MAGIC &&
	       nixge_ctrl_read_reg(p, ANTSDR_ABI_REG) == ANTSDR_ABI;
}

static struct nixge_priv *antsdr_priv(struct device *dev)
{
	return netdev_priv(dev_get_drvdata(dev));
}

static ssize_t antsdr_status_show(struct device *dev,
		struct device_attribute *attr, char *buf)
{
	struct nixge_priv *p = antsdr_priv(dev);
	ssize_t n;
	mutex_lock(&p->antsdr_lock);
	n = scnprintf(buf, PAGE_SIZE, "%08x %08x %08x %u %08x %u %u\n",
		nixge_ctrl_read_reg(p, ANTSDR_SIGNATURE),
		nixge_ctrl_read_reg(p, ANTSDR_ABI_REG),
		nixge_ctrl_read_reg(p, ANTSDR_IP),
		nixge_ctrl_read_reg(p, NIXGE_REG_MAC_MASK),
		nixge_ctrl_read_reg(p, ANTSDR_OWNER),
		nixge_ctrl_read_reg(p, ANTSDR_TIMEOUT),
		nixge_ctrl_read_reg(p, ANTSDR_EXPIRED));
	mutex_unlock(&p->antsdr_lock);
	return n;
}
static DEVICE_ATTR_RO(antsdr_status);

static ssize_t antsdr_dna_show(struct device *dev,
		struct device_attribute *attr, char *buf)
{
	struct nixge_priv *p = antsdr_priv(dev);
	return scnprintf(buf, PAGE_SIZE, "%08x%08x\n",
		readl(p->antsdr_dna + 4), readl(p->antsdr_dna));
}
static DEVICE_ATTR_RO(antsdr_dna);

static ssize_t antsdr_network_store(struct device *dev,
		struct device_attribute *attr, const char *buf, size_t count)
{
	struct nixge_priv *p = antsdr_priv(dev);
	u32 ip;
	if (kstrtou32(buf, 16, &ip)) return -EINVAL;
	mutex_lock(&p->antsdr_lock);
	if (!antsdr_compatible(p)) { mutex_unlock(&p->antsdr_lock); return -EPROTO; }
	if (ip != nixge_ctrl_read_reg(p, ANTSDR_IP))
		nixge_ctrl_write_reg(p, ANTSDR_TIMEOUT, 0);
	nixge_ctrl_write_reg(p, ANTSDR_IP, ip);
	mutex_unlock(&p->antsdr_lock);
	return count;
}
static DEVICE_ATTR(antsdr_network, 0200, NULL, antsdr_network_store);

static ssize_t antsdr_enable_store(struct device *dev,
		struct device_attribute *attr, const char *buf, size_t count)
{
	struct nixge_priv *p = antsdr_priv(dev);
	u32 enabled;
	if (kstrtou32(buf, 10, &enabled) || enabled > 1) return -EINVAL;
	mutex_lock(&p->antsdr_lock);
	if (!antsdr_compatible(p)) { mutex_unlock(&p->antsdr_lock); return -EPROTO; }
	if (!enabled) nixge_ctrl_write_reg(p, ANTSDR_TIMEOUT, 0);
	nixge_ctrl_write_reg(p, NIXGE_REG_MAC_MASK, enabled);
	mutex_unlock(&p->antsdr_lock);
	return count;
}
static DEVICE_ATTR(antsdr_enable, 0200, NULL, antsdr_enable_store);

static ssize_t antsdr_lease_store(struct device *dev,
		struct device_attribute *attr, const char *buf, size_t count)
{
	struct nixge_priv *p = antsdr_priv(dev);
	u32 ip, ms;
	char extra;
	if (sscanf(buf, "%x %u %c", &ip, &ms, &extra) != 2 || ms > 3000 ||
	    (ms && (!ip || (ip >> 24) == 127 || (ip >> 28) >= 14))) return -EINVAL;
	mutex_lock(&p->antsdr_lock);
	if (!antsdr_compatible(p)) { mutex_unlock(&p->antsdr_lock); return -EPROTO; }
	if (ms && (!nixge_ctrl_read_reg(p, ANTSDR_IP) ||
	    nixge_ctrl_read_reg(p, NIXGE_REG_MAC_MASK) != 1)) {
		mutex_unlock(&p->antsdr_lock); return -ENETDOWN;
	}
	if (ms && ip == nixge_ctrl_read_reg(p, ANTSDR_OWNER) &&
	    !nixge_ctrl_read_reg(p, ANTSDR_TIMEOUT)) {
		mutex_unlock(&p->antsdr_lock); return -ESTALE;
	}
	if (ip != nixge_ctrl_read_reg(p, ANTSDR_OWNER))
		nixge_ctrl_write_reg(p, ANTSDR_OWNER, ip);
	nixge_ctrl_write_reg(p, ANTSDR_TIMEOUT, ms);
	mutex_unlock(&p->antsdr_lock);
	return count;
}
static DEVICE_ATTR(antsdr_lease, 0200, NULL, antsdr_lease_store);

static struct attribute *antsdr_attrs[] = {
	&dev_attr_antsdr_status.attr, &dev_attr_antsdr_dna.attr,
	&dev_attr_antsdr_network.attr, &dev_attr_antsdr_enable.attr,
	&dev_attr_antsdr_lease.attr, NULL
};
static const struct attribute_group antsdr_group = { .attrs = antsdr_attrs };

static int antsdr_management_init(struct platform_device *pdev)
{
	struct nixge_priv *p = antsdr_priv(&pdev->dev);
	struct resource *dna = platform_get_resource_byname(pdev, IORESOURCE_MEM, "dna");
	if (!dna) return 0; /* Unmodified boards retain their existing NIC. */
	if (!antsdr_compatible(p)) return -EPROTO;
	p->antsdr_dna = devm_ioremap_resource(&pdev->dev, dna);
	if (IS_ERR(p->antsdr_dna)) return PTR_ERR(p->antsdr_dna);
	mutex_init(&p->antsdr_lock);
	nixge_ctrl_write_reg(p, ANTSDR_TIMEOUT, 0);
	{
		int err = sysfs_create_group(&pdev->dev.kobj, &antsdr_group);
		p->antsdr_group_added = !err;
		return err;
	}
}
