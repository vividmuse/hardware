// SPDX-License-Identifier: GPL-2.0-only
/*
 * j208-gpu-dt: the T8011 GPU and its mailbox on J208, added to the live
 * device tree at load and removed at unload.
 *
 * The image's device tree has no GPU node, and its GFX and DWI power domains
 * have no phandle, so no node can name them. At load, in this order:
 *
 *   1. GFX becomes a subdomain of DWI. Stage G: GFX may only be powered with
 *      DWI on. genpd then powers DWI before GFX.
 *   1b. DWI is held on by a consumer device of its own, so genpd never
 *      powers it off after GFX. DWI is the AP-to-PMU link: stage F/G found
 *      the GPU dies without it, and turning it off right after GFX hung the
 *      SoC after GPU work (runs 20261109-*). T8010 never turns it off (its
 *      dwi_bl backlight node keeps it); iBoot leaves it on until Linux's
 *      unused-domain cut. By default it stays on after unload until reboot
 *      (release_dwi=1 powers it off at unload instead).
 *   2. A changeset adds /soc/mbox@201d24000 and /soc/gpu@201000000. The
 *      platform devices appear with it, before any driver: this module loads
 *      first (j208-rtkit and j208-powervr refuse a device outside a domain).
 *   3. Both devices join the GFX domain (of_genpd_add_device).
 *
 * No register is accessed. Nothing is powered: the drivers do that.
 *
 * The changeset attaches each node before adding its properties, so the
 * kernel never sees the mailbox's "phandle" property at attach time; the
 * phandle is set on the node once it is in the tree.
 *
 * Unload reverses it: the drivers are unbound, the devices leave the domain,
 * the changeset is reverted (removing the devices), the subdomain link goes.
 * An unbound device has runtime PM disabled, and genpd counts it as busy:
 * unbinding the mailbox (the driver core resumes it for that) leaves the
 * domain on. So with the mailbox out of the domain, the GPU device is
 * resumed and suspended once, and genpd powers GFX off; DWI stays held.
 *
 * Values: J208's ADT gfx-kf (reg 0x201d20000, interrupts 0xba 0xb9 0xbc 0xbb)
 * and sgx (reg 0x201000000); T8010's ADT lists its gfx-kf interrupts in the
 * same order (0xbe 0xbd 0xc0 0xbf), which t8010.dtsi names send-not-empty,
 * send-empty, recv-not-empty, recv-empty. Bridges 7 and 9 as stages B2-J.
 * No OPP table: the GPU stays in its fastest state, 4 (stage J held it).
 */
#include <linux/device.h>
#include <linux/err.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/pm_domain.h>
#include <linux/pm_runtime.h>
#include <linux/string.h>

#define PFX "j208-gpu-dt: "

static bool release_dwi;
module_param(release_dwi, bool, 0444);
MODULE_PARM_DESC(release_dwi, "Power DWI off at unload (default: it stays on until reboot)");

#define SOC_NODE "/soc"
#define PMGR_BASE 0x20e000000ULL
#define GFX_NODE "/soc/power-management@20e000000/power-controller@80338"
#define GFX_OFFSET 0x80338
#define DWI_NODE "/soc/power-management@20e000000/power-controller@80110"
#define DWI_OFFSET 0x80110

#define MBOX_NAME "mbox@201d24000"
#define GPU_NAME "gpu@201000000"

/* <AIC_IRQ n IRQ_TYPE_LEVEL_HIGH> */
#define AIC_IRQ 0
#define LEVEL_HIGH 4

/* #address-cells = #size-cells = 2 under /soc */
static const u32 mbox_reg[] = { 0x2, 0x01d24000, 0x0, 0x1000 };
static const u32 mbox_irqs[] = {
	AIC_IRQ, 185, LEVEL_HIGH,
	AIC_IRQ, 186, LEVEL_HIGH,
	AIC_IRQ, 187, LEVEL_HIGH,
	AIC_IRQ, 188, LEVEL_HIGH,
};
static const char *const mbox_irq_names[] = {
	"send-empty", "send-not-empty", "recv-empty", "recv-not-empty",
};

static const u32 gpu_reg[] = {
	0x2, 0x01000000, 0x0, 0x100000,	/* rgx */
	0x2, 0x01d00000, 0x0, 0x5000,	/* fender */
	0x2, 0x01d20000, 0x0, 0x4000,	/* iop */
	0x2, 0x01f00000, 0x0, 0x9000,	/* bridge0: iOS bridge 7 */
	0x2, 0x01f80000, 0x0, 0x9000,	/* bridge1: iOS bridge 9 */
};
static const char *const gpu_reg_names[] = {
	"rgx", "fender", "iop", "bridge0", "bridge1",
};

static struct of_changeset cs;
static bool cs_applied;
static struct of_phandle_args gfx, dwi;
static bool subdomain;
static struct platform_device *mbox_pdev, *gpu_pdev;
static bool mbox_in_domain, gpu_in_domain;
static struct platform_device *dwi_pdev;	/* holds DWI on */
static bool dwi_held;

/* A PMGR power-state node, exactly as the image's DT has it */
static int check_domain(struct device_node *np, const char *label, u32 offset)
{
	struct resource parent;
	const char *value;
	u32 reg[2], cells;

	if (!of_device_is_compatible(np, "apple,pmgr-pwrstate") ||
	    of_property_read_string(np, "label", &value) || strcmp(value, label) ||
	    of_property_read_u32_array(np, "reg", reg, 2) ||
	    reg[0] != offset || reg[1] != 4 ||
	    of_property_read_u32(np, "#power-domain-cells", &cells) || cells ||
	    !np->parent || of_address_to_resource(np->parent, 0, &parent) ||
	    parent.start != PMGR_BASE)
		return -ENODEV;
	return 0;
}

/* One above the largest phandle in the tree */
static u32 free_phandle(void)
{
	struct device_node *np;
	u32 max = 0;

	/* of_find_all_nodes() drops the previous node's reference */
	for (np = of_find_all_nodes(NULL); np; np = of_find_all_nodes(np))
		max = max(max, (u32)np->phandle);
	return max + 1;
}

static int build(struct device_node *soc, u32 mbox_phandle)
{
	struct device_node *mbox, *gpu;
	int ret;

	mbox = of_changeset_create_node(&cs, soc, MBOX_NAME);
	if (!mbox)
		return -ENOMEM;
	ret = of_changeset_add_prop_string(&cs, mbox, "compatible",
					   "apple,t8011-a7iop-mailbox");
	ret = ret ?: of_changeset_add_prop_u32_array(&cs, mbox, "reg", mbox_reg,
						     ARRAY_SIZE(mbox_reg));
	ret = ret ?: of_changeset_add_prop_u32_array(&cs, mbox, "interrupts", mbox_irqs,
						     ARRAY_SIZE(mbox_irqs));
	ret = ret ?: of_changeset_add_prop_string_array(&cs, mbox, "interrupt-names",
							mbox_irq_names,
							ARRAY_SIZE(mbox_irq_names));
	ret = ret ?: of_changeset_add_prop_u32(&cs, mbox, "#mbox-cells", 0);
	ret = ret ?: of_changeset_add_prop_u32(&cs, mbox, "phandle", mbox_phandle);
	of_node_put(mbox);
	if (ret)
		return ret;

	gpu = of_changeset_create_node(&cs, soc, GPU_NAME);
	if (!gpu)
		return -ENOMEM;
	ret = of_changeset_add_prop_string(&cs, gpu, "compatible", "apple,t8011-gpu");
	ret = ret ?: of_changeset_add_prop_u32_array(&cs, gpu, "reg", gpu_reg,
						     ARRAY_SIZE(gpu_reg));
	ret = ret ?: of_changeset_add_prop_string_array(&cs, gpu, "reg-names", gpu_reg_names,
							ARRAY_SIZE(gpu_reg_names));
	ret = ret ?: of_changeset_add_prop_u32(&cs, gpu, "mboxes", mbox_phandle);
	ret = ret ?: of_changeset_add_prop_string(&cs, gpu, "firmware-name",
						  "apple/gfx-t8011.bin");
	of_node_put(gpu);
	return ret;
}

/* The device the changeset created for @path, with no driver yet */
static struct platform_device *created(const char *path)
{
	struct platform_device *pdev;
	struct device_node *np;

	np = of_find_node_by_path(path);
	if (!np)
		return ERR_PTR(-ENODEV);
	pdev = of_find_device_by_node(np);
	of_node_put(np);
	if (!pdev)
		return ERR_PTR(-ENODEV);
	if (device_is_bound(&pdev->dev) || pdev->dev.pm_domain) {
		put_device(&pdev->dev);
		return ERR_PTR(-EBUSY);
	}
	return pdev;
}

/* A device of our own in the DWI domain, resumed: DWI stays on */
static int dwi_hold(void)
{
	struct platform_device_info info = {
		.name = "j208-gpu-dt-dwi",
		.id = PLATFORM_DEVID_AUTO,
	};
	int ret;

	dwi_pdev = platform_device_register_full(&info);
	if (IS_ERR(dwi_pdev)) {
		ret = PTR_ERR(dwi_pdev);
		dwi_pdev = NULL;
		return ret;
	}
	ret = of_genpd_add_device(&dwi, &dwi_pdev->dev);
	if (ret)
		goto unregister;
	pm_runtime_enable(&dwi_pdev->dev);
	ret = pm_runtime_resume_and_get(&dwi_pdev->dev);
	if (ret < 0 || !pm_runtime_active(&dwi_pdev->dev)) {
		ret = ret < 0 ? ret : -EIO;
		pm_runtime_disable(&dwi_pdev->dev);
		pm_genpd_remove_device(&dwi_pdev->dev);
		goto unregister;
	}
	dwi_held = true;
	return 0;

unregister:
	platform_device_unregister(dwi_pdev);
	dwi_pdev = NULL;
	return ret;
}

/*
 * Without release_dwi the holder is left registered, active and in the
 * domain: it refers to no code of this module, so DWI stays on until reboot.
 */
static void dwi_release(void)
{
	if (!dwi_held)
		return;
	dwi_held = false;
	if (!release_dwi) {
		pr_info(PFX "DWI stays on until reboot (%s)\n", dev_name(&dwi_pdev->dev));
		dwi_pdev = NULL;
		return;
	}
	pm_runtime_put_sync_suspend(&dwi_pdev->dev);
	pm_runtime_disable(&dwi_pdev->dev);
	if (pm_genpd_remove_device(&dwi_pdev->dev))
		pr_err(PFX "DWI holder stays in the domain\n");
	else
		platform_device_unregister(dwi_pdev);
	dwi_pdev = NULL;
	pr_info(PFX "DWI released\n");
}

/* The GPU device, alone in the domain and unbound: one resume and suspend */
static void power_off_through(struct device *dev)
{
	int ret;

	pm_runtime_enable(dev);
	ret = pm_runtime_get_sync(dev);
	pm_runtime_put_sync(dev);
	pr_info(PFX "power-off pass resume=%d suspended=%d\n", ret,
		pm_runtime_status_suspended(dev));
	pm_runtime_disable(dev);
}

static void teardown(void)
{
	/* The GPU first: it holds RTKit, which holds the mailbox */
	if (gpu_pdev)
		device_release_driver(&gpu_pdev->dev);
	if (mbox_pdev)
		device_release_driver(&mbox_pdev->dev);
	if (mbox_in_domain && pm_genpd_remove_device(&mbox_pdev->dev))
		pr_err(PFX "mailbox stays in the GFX domain\n");
	if (gpu_in_domain) {
		if (subdomain)
			power_off_through(&gpu_pdev->dev);
		if (pm_genpd_remove_device(&gpu_pdev->dev))
			pr_err(PFX "gpu stays in the GFX domain\n");
	}
	gpu_in_domain = mbox_in_domain = false;
	if (gpu_pdev)
		put_device(&gpu_pdev->dev);
	if (mbox_pdev)
		put_device(&mbox_pdev->dev);
	gpu_pdev = mbox_pdev = NULL;
	if (cs_applied && of_changeset_revert(&cs))
		pr_err(PFX "changeset not reverted\n");
	cs_applied = false;
	of_changeset_destroy(&cs);
	if (subdomain && of_genpd_remove_subdomain(&dwi, &gfx))
		pr_err(PFX "GFX stays a subdomain of DWI\n");
	subdomain = false;
	dwi_release();
	of_node_put(gfx.np);
	of_node_put(dwi.np);
	gfx.np = dwi.np = NULL;
}

static int __init j208_gpu_dt_init(void)
{
	struct device_node *soc, *np;
	u32 phandle;
	int ret;

	if (!of_machine_is_compatible("apple,j208"))
		return -ENODEV;

	of_changeset_init(&cs);
	gfx.np = of_find_node_by_path(GFX_NODE);
	dwi.np = of_find_node_by_path(DWI_NODE);
	if (!gfx.np || !dwi.np || check_domain(gfx.np, "gfx", GFX_OFFSET) ||
	    check_domain(dwi.np, "dwi", DWI_OFFSET)) {
		pr_err(PFX "GFX/DWI power-state nodes differ\n");
		ret = -ENODEV;
		goto fail;
	}
	np = of_find_node_by_path(SOC_NODE "/" GPU_NAME);
	if (!np)
		np = of_find_node_by_path(SOC_NODE "/" MBOX_NAME);
	if (np) {
		of_node_put(np);
		pr_err(PFX "a GPU node exists already\n");
		ret = -EEXIST;
		goto fail;
	}

	/* genpd refuses this while GFX is on and DWI off */
	ret = of_genpd_add_subdomain(&dwi, &gfx);
	if (ret) {
		pr_err(PFX "GFX not made a subdomain of DWI: %d\n", ret);
		goto fail;
	}
	subdomain = true;

	ret = dwi_hold();
	if (ret) {
		pr_err(PFX "DWI not held on: %d\n", ret);
		goto fail;
	}

	soc = of_find_node_by_path(SOC_NODE);
	if (!soc) {
		ret = -ENODEV;
		goto fail;
	}
	phandle = free_phandle();
	ret = build(soc, phandle);
	of_node_put(soc);
	if (ret)
		goto fail;
	ret = of_changeset_apply(&cs);
	if (ret) {
		pr_err(PFX "changeset not applied: %d\n", ret);
		goto fail;
	}
	cs_applied = true;

	np = of_find_node_by_path(SOC_NODE "/" MBOX_NAME);
	if (!np) {
		ret = -ENODEV;
		goto fail;
	}
	np->phandle = phandle;
	of_node_put(np);
	np = of_find_node_by_phandle(phandle);
	ret = np && !strcmp(kbasename(np->full_name), MBOX_NAME) ? 0 : -ENODEV;
	of_node_put(np);
	if (ret) {
		pr_err(PFX "mailbox phandle %#x not found\n", phandle);
		goto fail;
	}

	mbox_pdev = created(SOC_NODE "/" MBOX_NAME);
	if (IS_ERR(mbox_pdev)) {
		ret = PTR_ERR(mbox_pdev);
		mbox_pdev = NULL;
		pr_err(PFX "mailbox device: %d\n", ret);
		goto fail;
	}
	gpu_pdev = created(SOC_NODE "/" GPU_NAME);
	if (IS_ERR(gpu_pdev)) {
		ret = PTR_ERR(gpu_pdev);
		gpu_pdev = NULL;
		pr_err(PFX "gpu device: %d\n", ret);
		goto fail;
	}

	ret = of_genpd_add_device(&gfx, &mbox_pdev->dev);
	if (ret)
		goto fail;
	mbox_in_domain = true;
	ret = of_genpd_add_device(&gfx, &gpu_pdev->dev);
	if (ret)
		goto fail;
	gpu_in_domain = true;

	pr_info(PFX "added mailbox (phandle %#x, irqs 185-188) and gpu in GFX under DWI (held on)\n",
		phandle);
	return 0;

fail:
	teardown();
	return ret;
}

static void __exit j208_gpu_dt_exit(void)
{
	teardown();
	pr_info(PFX "removed\n");
}

module_init(j208_gpu_dt_init);
module_exit(j208_gpu_dt_exit);

MODULE_DESCRIPTION("J208: T8011 GPU and mailbox device tree nodes");
MODULE_LICENSE("GPL");
