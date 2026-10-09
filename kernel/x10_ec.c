// SPDX-License-Identifier: GPL-2.0-only
/*
 * x10_ec - Secure Boot compatible EC and voltage-offset access for the
 *          MEDION Erazer Major X10 (board N68630, BIOS M1IB008).
 *
 * Under Secure Boot the kernel enables lockdown, which blocks /dev/port and
 * MSR writes through /dev/cpu/N/msr even for root. This module, once signed
 * with an enrolled MOK, replaces both with a deliberately narrow interface:
 *
 *   /dev/x10-ec    pread/pwrite of exactly one byte at offset 0x6c (command)
 *                  or 0x68 (data), i.e. the same semantics as /dev/port for
 *                  the two vendor ports only. Command bytes and the selector
 *                  or action that follows them are checked against the
 *                  documented allowlist (docs/protocol.md). One opener only.
 *
 *   sysfs          /sys/class/misc/x10-ec/undervolt_{core,gpu,cache,uncore,analogio}
 *                  Read/write voltage offsets (mV) through the OC mailbox
 *                  MSR 0x150. Disabled unless allow_undervolt=1. Writes are
 *                  bounded to [undervolt_min_mv, 0] and verified by readback;
 *                  a BIOS-locked mailbox reports -EPERM instead of pretending.
 */

#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/ioport.h>
#include <linux/mutex.h>
#include <linux/atomic.h>
#include <linux/dmi.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/device.h>
#include <linux/sysfs.h>
#include <asm/msr.h>
#include <linux/version.h>
#include <asm/processor.h>

#define X10_CMD_PORT 0x6c
#define X10_DATA_PORT 0x68
#define X10_MSR_OC_MAILBOX 0x150

static bool allow_undervolt;
module_param(allow_undervolt, bool, 0444);
MODULE_PARM_DESC(allow_undervolt, "Expose voltage-offset controls (default: off)");

static int undervolt_min_mv = -150;
module_param(undervolt_min_mv, int, 0444);
MODULE_PARM_DESC(undervolt_min_mv, "Most negative accepted offset in mV (-250..0, default -150)");

static bool skip_dmi_check;
module_param(skip_dmi_check, bool, 0444);
MODULE_PARM_DESC(skip_dmi_check, "Development only: do not require the exact DMI identity");

static const struct dmi_system_id x10_dmi_table[] = {
	{
		.ident = "MEDION Erazer Major X10",
		.matches = {
			DMI_EXACT_MATCH(DMI_SYS_VENDOR, "MEDION"),
			DMI_EXACT_MATCH(DMI_PRODUCT_NAME, "Major X10"),
			DMI_EXACT_MATCH(DMI_BOARD_NAME, "N68630"),
			DMI_EXACT_MATCH(DMI_BIOS_VERSION, "M1IB008"),
		},
	},
	{ }
};
MODULE_DEVICE_TABLE(dmi, x10_dmi_table);

/* ---------------------------------------------------------------------- */
/* EC byte transport with a command/selector allowlist                     */
/* ---------------------------------------------------------------------- */

struct x10_allow {
	u8 command;
	u8 count;
	u8 values[8];
};

static const struct x10_allow x10_allowlist[] = {
	/* fan tachometers, read-only selectors */
	{ 0xd5, 4, { 0x16, 0x17, 0x18, 0x19 } },
	/* CPU and GPU temperature */
	{ 0xdd, 2, { 0x20, 0x23 } },
	/* profiles 1..3, power state, full speed on/off/read, profile read */
	{ 0xde, 8, { 0x01, 0x02, 0x03, 0x05, 0x0e, 0x0f, 0x10, 0x11 } },
};

struct x10_file {
	const struct x10_allow *pending; /* command written, awaiting selector */
};

static DEFINE_MUTEX(x10_io_lock);
static atomic_t x10_open_count = ATOMIC_INIT(0);
static bool x10_cmd_region, x10_data_region;

static const struct x10_allow *x10_find_command(u8 command)
{
	size_t i;

	for (i = 0; i < ARRAY_SIZE(x10_allowlist); i++)
		if (x10_allowlist[i].command == command)
			return &x10_allowlist[i];
	return NULL;
}

static bool x10_value_allowed(const struct x10_allow *entry, u8 value)
{
	u8 i;

	for (i = 0; i < entry->count; i++)
		if (entry->values[i] == value)
			return true;
	return false;
}

static int x10_open(struct inode *inode, struct file *file)
{
	struct x10_file *state;

	if (atomic_cmpxchg(&x10_open_count, 0, 1) != 0)
		return -EBUSY;
	state = kzalloc(sizeof(*state), GFP_KERNEL);
	if (!state) {
		atomic_set(&x10_open_count, 0);
		return -ENOMEM;
	}
	file->private_data = state;
	return 0;
}

static int x10_release(struct inode *inode, struct file *file)
{
	kfree(file->private_data);
	file->private_data = NULL;
	atomic_set(&x10_open_count, 0);
	return 0;
}

static ssize_t x10_read(struct file *file, char __user *buf, size_t count, loff_t *ppos)
{
	u8 value;

	if (count != 1 || *ppos != X10_DATA_PORT)
		return -EINVAL;
	mutex_lock(&x10_io_lock);
	value = inb(X10_DATA_PORT);
	mutex_unlock(&x10_io_lock);
	if (put_user(value, buf))
		return -EFAULT;
	return 1;
}

static ssize_t x10_write(struct file *file, const char __user *buf, size_t count, loff_t *ppos)
{
	struct x10_file *state = file->private_data;
	const struct x10_allow *entry;
	u8 value;

	if (count != 1)
		return -EINVAL;
	if (get_user(value, buf))
		return -EFAULT;

	if (*ppos == X10_CMD_PORT) {
		entry = x10_find_command(value);
		if (!entry) {
			state->pending = NULL;
			return -EPERM;
		}
		mutex_lock(&x10_io_lock);
		outb(value, X10_CMD_PORT);
		mutex_unlock(&x10_io_lock);
		state->pending = entry;
		return 1;
	}

	if (*ppos == X10_DATA_PORT) {
		entry = state->pending;
		state->pending = NULL;
		if (!entry || !x10_value_allowed(entry, value))
			return -EPERM;
		mutex_lock(&x10_io_lock);
		outb(value, X10_DATA_PORT);
		mutex_unlock(&x10_io_lock);
		return 1;
	}

	return -EINVAL;
}

static const struct file_operations x10_fops = {
	.owner = THIS_MODULE,
	.open = x10_open,
	.release = x10_release,
	.read = x10_read,
	.write = x10_write,
	.llseek = default_llseek,
};

/* ---------------------------------------------------------------------- */
/* Voltage offsets through the OC mailbox (MSR 0x150)                      */
/* ---------------------------------------------------------------------- */

enum x10_plane { PLANE_CORE = 0, PLANE_GPU = 1, PLANE_CACHE = 2, PLANE_UNCORE = 3, PLANE_ANALOGIO = 4 };

static DEFINE_MUTEX(x10_msr_lock);

static int x10_offset_to_units(int mv)
{
	/* 1/1.024 mV units, rounded to nearest */
	int scaled = mv * 1024;

	return scaled >= 0 ? (scaled + 500) / 1000 : (scaled - 500) / 1000;
}

static int x10_units_to_mv(int units)
{
	int scaled = units * 1000;

	return scaled >= 0 ? (scaled + 512) / 1024 : (scaled - 512) / 1024;
}

static int x10_mailbox(u32 hi, u32 lo, u32 *out_lo)
{
	u32 rlo, rhi;
	int err;

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
	u64 val;

	err = wrmsrq_safe_on_cpu(0, X10_MSR_OC_MAILBOX, ((u64)hi << 32) | lo);
	if (err)
		return err;
	err = rdmsrq_safe_on_cpu(0, X10_MSR_OC_MAILBOX, &val);
	if (err)
		return err;
	rlo = (u32)val;
	rhi = (u32)(val >> 32);
#else
	err = wrmsr_safe_on_cpu(0, X10_MSR_OC_MAILBOX, lo, hi);
	if (err)
		return err;
	err = rdmsr_safe_on_cpu(0, X10_MSR_OC_MAILBOX, &rlo, &rhi);
	if (err)
		return err;
#endif
	/* bits 39:32 of the response hold the mailbox status; 0 is success */
	if (rhi & 0xff)
		return -EIO;
	*out_lo = rlo;
	return 0;
}

static int x10_read_offset(enum x10_plane plane, int *mv)
{
	u32 lo;
	int units, err;

	mutex_lock(&x10_msr_lock);
	err = x10_mailbox(0x80000010u | ((u32)plane << 8), 0, &lo);
	mutex_unlock(&x10_msr_lock);
	if (err)
		return err;
	units = (lo >> 21) & 0x7ff;
	if (units & 0x400)
		units -= 0x800; /* sign-extend 11 bits */
	*mv = x10_units_to_mv(units);
	return 0;
}

static int x10_write_offset(enum x10_plane plane, int mv)
{
	int units = x10_offset_to_units(mv);
	u32 lo = ((u32)units & 0x7ff) << 21;
	u32 unused;
	int readback, err;

	mutex_lock(&x10_msr_lock);
	err = x10_mailbox(0x80000011u | ((u32)plane << 8), lo, &unused);
	mutex_unlock(&x10_msr_lock);
	if (err)
		return err;
	err = x10_read_offset(plane, &readback);
	if (err)
		return err;
	/* firmware with undervolt protection accepts the write but ignores it */
	if (abs(readback - mv) > 1)
		return -EPERM;
	return 0;
}

static enum x10_plane x10_attr_plane(const char *name)
{
	if (!strcmp(name, "undervolt_gpu"))
		return PLANE_GPU;
	if (!strcmp(name, "undervolt_cache"))
		return PLANE_CACHE;
	if (!strcmp(name, "undervolt_uncore"))
		return PLANE_UNCORE;
	if (!strcmp(name, "undervolt_analogio"))
		return PLANE_ANALOGIO;
	return PLANE_CORE;
}

static ssize_t undervolt_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	int mv, err;

	if (!allow_undervolt)
		return -EOPNOTSUPP;
	err = x10_read_offset(x10_attr_plane(attr->attr.name), &mv);
	if (err)
		return err;
	return sysfs_emit(buf, "%d\n", mv);
}

static ssize_t undervolt_store(struct device *dev, struct device_attribute *attr,
			       const char *buf, size_t count)
{
	int mv, err;

	if (!allow_undervolt)
		return -EOPNOTSUPP;
	err = kstrtoint(buf, 10, &mv);
	if (err)
		return err;
	if (mv > 0 || mv < undervolt_min_mv)
		return -ERANGE;
	err = x10_write_offset(x10_attr_plane(attr->attr.name), mv);
	return err ? err : count;
}

static ssize_t undervolt_limits_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%d 0\n", undervolt_min_mv);
}

static ssize_t undervolt_enabled_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%d\n", allow_undervolt ? 1 : 0);
}

static struct device_attribute dev_attr_undervolt_core = __ATTR(undervolt_core, 0644, undervolt_show, undervolt_store);
static struct device_attribute dev_attr_undervolt_gpu = __ATTR(undervolt_gpu, 0644, undervolt_show, undervolt_store);
static struct device_attribute dev_attr_undervolt_cache = __ATTR(undervolt_cache, 0644, undervolt_show, undervolt_store);
static struct device_attribute dev_attr_undervolt_uncore = __ATTR(undervolt_uncore, 0644, undervolt_show, undervolt_store);
static struct device_attribute dev_attr_undervolt_analogio = __ATTR(undervolt_analogio, 0644, undervolt_show, undervolt_store);
static DEVICE_ATTR_RO(undervolt_limits);
static DEVICE_ATTR_RO(undervolt_enabled);

static struct attribute *x10_attrs[] = {
	&dev_attr_undervolt_core.attr,
	&dev_attr_undervolt_gpu.attr,
	&dev_attr_undervolt_cache.attr,
	&dev_attr_undervolt_uncore.attr,
	&dev_attr_undervolt_analogio.attr,
	&dev_attr_undervolt_limits.attr,
	&dev_attr_undervolt_enabled.attr,
	NULL,
};
ATTRIBUTE_GROUPS(x10);

static struct miscdevice x10_misc = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "x10-ec",
	.fops = &x10_fops,
	.groups = x10_groups,
	.mode = 0600,
};

static int __init x10_init(void)
{
	int err;

	if (!skip_dmi_check && !dmi_check_system(x10_dmi_table)) {
		pr_info("x10_ec: unsupported machine, not loading\n");
		return -ENODEV;
	}
	if (undervolt_min_mv < -250 || undervolt_min_mv > 0) {
		pr_err("x10_ec: undervolt_min_mv must be within -250..0\n");
		return -EINVAL;
	}
	if (allow_undervolt && boot_cpu_data.x86_vendor != X86_VENDOR_INTEL) {
		pr_warn("x10_ec: not an Intel CPU, voltage offsets disabled\n");
		allow_undervolt = false;
	}

	/*
	 * The ports are normally only covered by ACPI PNP0C02 motherboard
	 * reservations, so a failed claim is reported but not fatal.
	 */
	x10_cmd_region = request_region(X10_CMD_PORT, 1, "x10_ec") != NULL;
	x10_data_region = request_region(X10_DATA_PORT, 1, "x10_ec") != NULL;
	if (!x10_cmd_region || !x10_data_region)
		pr_info("x10_ec: ports 0x68/0x6c already reserved (usually PNP0C02); continuing\n");

	err = misc_register(&x10_misc);
	if (err) {
		if (x10_cmd_region)
			release_region(X10_CMD_PORT, 1);
		if (x10_data_region)
			release_region(X10_DATA_PORT, 1);
		return err;
	}
	pr_info("x10_ec: ready (undervolt %s)\n", allow_undervolt ? "enabled" : "disabled");
	return 0;
}

static void __exit x10_exit(void)
{
	misc_deregister(&x10_misc);
	if (x10_cmd_region)
		release_region(X10_CMD_PORT, 1);
	if (x10_data_region)
		release_region(X10_DATA_PORT, 1);
}

module_init(x10_init);
module_exit(x10_exit);

MODULE_AUTHOR("Dane64");
MODULE_DESCRIPTION("MEDION Erazer Major X10 EC allowlist and voltage-offset interface");
MODULE_LICENSE("GPL");
