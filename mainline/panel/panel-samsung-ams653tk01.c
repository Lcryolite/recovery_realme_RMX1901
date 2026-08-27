// SPDX-License-Identifier: GPL-2.0-only
/*
 * DRM panel driver for the Samsung AMS653TK01 used by the Realme X.
 *
 * The command sequence and timing are from the RMX1901/18041 downstream
 * display description.  The transport and panel lifecycle use the mainline
 * DRM MIPI DSI interfaces.
 */

#include <linux/backlight.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/regulator/consumer.h>

#include <video/mipi_display.h>

#include <drm/drm_mipi_dsi.h>
#include <drm/drm_modes.h>
#include <drm/drm_panel.h>

struct ams653tk01 {
	struct drm_panel panel;
	struct mipi_dsi_device *dsi;
	struct gpio_desc *reset_gpio;
	struct regulator_bulk_data *supplies;
};

static const struct regulator_bulk_data ams653tk01_supplies[] = {
	{ .supply = "vddio" },
	{ .supply = "vdda-3p3" },
	{ .supply = "lab" },
	{ .supply = "ibb" },
};

static inline struct ams653tk01 *to_ams653tk01(struct drm_panel *panel)
{
	return container_of(panel, struct ams653tk01, panel);
}

static void ams653tk01_reset(struct ams653tk01 *ctx)
{
	/* Matches the 18041 downstream high-low-high reset sequence. */
	gpiod_set_value_cansleep(ctx->reset_gpio, 1);
	usleep_range(10000, 11000);
	gpiod_set_value_cansleep(ctx->reset_gpio, 0);
	usleep_range(5000, 6000);
	gpiod_set_value_cansleep(ctx->reset_gpio, 1);
	usleep_range(10000, 11000);
}

static int ams653tk01_on(struct ams653tk01 *ctx)
{
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };

	/* Sleep out and the panel-specific setup from the 18041 command table. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0xa5, 0xa5);
	mipi_dsi_dcs_exit_sleep_mode_multi(&dsi_ctx);
	mipi_dsi_msleep(&dsi_ctx, 5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0x5a, 0x5a);

	/* FD setting. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x01);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xcd, 0x01);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* TE output. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0xa5, 0xa5);
	mipi_dsi_dcs_set_tear_on_multi(&dsi_ctx, MIPI_DSI_DCS_TEAR_MODE_VBLANK);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0x5a, 0x5a);

	/* MIC setting. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xeb,
				     0x17, 0x41, 0x92, 0x0e,
				     0x10, 0x82, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* Full-frame column and page address. */
	mipi_dsi_dcs_set_column_address_multi(&dsi_ctx, 0x0000, 0x0437);
	mipi_dsi_dcs_set_page_address_multi(&dsi_ctx, 0x0000, 0x0923);

	/* ESD setting. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xfc, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x01);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xe3, 0x88);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x07);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xed, 0x67);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xfc, 0xa5, 0xa5);

	/* Backlight dimming and ACL defaults. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x08);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb7, 0x12);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x53, 0x20);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x55, 0x00);

	/* Seed CRC and TCS settings. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0xde);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb9, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x81, 0x90);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x02);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb1,
				     0xe0, 0x00, 0x08, 0x1c, 0xf8,
				     0x00, 0x04, 0x0f, 0xff, 0x24, 0xfd,
				     0xdc, 0xfd, 0x00, 0xf6, 0xf1, 0xf0,
				     0x00, 0xff, 0xff, 0xff);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb1, 0x00, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x23);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb3, 0x91);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x83, 0x80);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb3, 0x00, 0xc0);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);

	/* Start with the panel's native 10-bit brightness range. */
	mipi_dsi_dcs_set_display_brightness_multi(&dsi_ctx, 1023);
	mipi_dsi_msleep(&dsi_ctx, 20);

	/* The downstream sequence turns on normal mode before the display. */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x29);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x13);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0x5a, 0x5a);

	return dsi_ctx.accum_err;
}

static int ams653tk01_disable(struct drm_panel *panel)
{
	struct ams653tk01 *ctx = to_ams653tk01(panel);
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };

	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0xa5, 0xa5);
	mipi_dsi_dcs_set_display_off_multi(&dsi_ctx);
	mipi_dsi_msleep(&dsi_ctx, 10);
	mipi_dsi_dcs_enter_sleep_mode_multi(&dsi_ctx);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x05);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf4, 0x01);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_msleep(&dsi_ctx, 120);

	return dsi_ctx.accum_err;
}

static int ams653tk01_prepare(struct drm_panel *panel)
{
	struct ams653tk01 *ctx = to_ams653tk01(panel);
	int ret;

	ret = regulator_set_voltage(ctx->supplies[0].consumer, 1800000,
					1800000);
	if (ret < 0)
		return ret;

	ret = regulator_set_voltage(ctx->supplies[1].consumer, 3008000,
					3008000);
	if (ret < 0)
		return ret;

	ret = regulator_set_voltage(ctx->supplies[2].consumer, 4600000,
					6100000);
	if (ret < 0)
		return ret;

	ret = regulator_set_voltage(ctx->supplies[3].consumer, 4000000,
					6300000);
	if (ret < 0)
		return ret;

	ret = regulator_bulk_enable(ARRAY_SIZE(ams653tk01_supplies),
					    ctx->supplies);
	if (ret < 0)
		return ret;

	ams653tk01_reset(ctx);
	ret = ams653tk01_on(ctx);
	if (ret < 0) {
		gpiod_set_value_cansleep(ctx->reset_gpio, 0);
		regulator_bulk_disable(ARRAY_SIZE(ams653tk01_supplies),
				       ctx->supplies);
	}

	return ret;
}

static int ams653tk01_unprepare(struct drm_panel *panel)
{
	struct ams653tk01 *ctx = to_ams653tk01(panel);

	gpiod_set_value_cansleep(ctx->reset_gpio, 0);
	regulator_bulk_disable(ARRAY_SIZE(ams653tk01_supplies), ctx->supplies);

	return 0;
}

static const struct drm_display_mode ams653tk01_mode = {
	.clock = (1080 + 48 + 24 + 48) * (2340 + 20 + 4 + 16) * 60 / 1000,
	.hdisplay = 1080,
	.hsync_start = 1080 + 48,
	.hsync_end = 1080 + 48 + 24,
	.htotal = 1080 + 48 + 24 + 48,
	.vdisplay = 2340,
	.vsync_start = 2340 + 20,
	.vsync_end = 2340 + 20 + 4,
	.vtotal = 2340 + 20 + 4 + 16,
	.width_mm = 69,
	.height_mm = 148,
	.type = DRM_MODE_TYPE_DRIVER | DRM_MODE_TYPE_PREFERRED,
};

static int ams653tk01_get_modes(struct drm_panel *panel,
				struct drm_connector *connector)
{
	struct drm_display_mode *mode;

	mode = drm_mode_duplicate(connector->dev, &ams653tk01_mode);
	if (!mode)
		return -ENOMEM;

	drm_mode_set_name(mode);
	connector->display_info.width_mm = mode->width_mm;
	connector->display_info.height_mm = mode->height_mm;
	drm_mode_probed_add(connector, mode);

	return 1;
}

static const struct drm_panel_funcs ams653tk01_panel_funcs = {
	.prepare = ams653tk01_prepare,
	.unprepare = ams653tk01_unprepare,
	.disable = ams653tk01_disable,
	.get_modes = ams653tk01_get_modes,
};

static int ams653tk01_bl_update_status(struct backlight_device *bl)
{
	struct mipi_dsi_device *dsi = bl_get_data(bl);
	u16 brightness = backlight_get_brightness(bl);
	int ret;

	dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;
	ret = mipi_dsi_dcs_set_display_brightness_large(dsi, brightness);
	dsi->mode_flags |= MIPI_DSI_MODE_LPM;

	return ret < 0 ? ret : 0;
}

static int ams653tk01_bl_get_brightness(struct backlight_device *bl)
{
	struct mipi_dsi_device *dsi = bl_get_data(bl);
	u16 brightness;
	int ret;

	dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;
	ret = mipi_dsi_dcs_get_display_brightness_large(dsi, &brightness);
	dsi->mode_flags |= MIPI_DSI_MODE_LPM;

	return ret < 0 ? ret : brightness;
}

static const struct backlight_ops ams653tk01_bl_ops = {
	.update_status = ams653tk01_bl_update_status,
	.get_brightness = ams653tk01_bl_get_brightness,
};

static struct backlight_device *
ams653tk01_create_backlight(struct mipi_dsi_device *dsi)
{
	const struct backlight_properties props = {
		.type = BACKLIGHT_RAW,
		.brightness = 1023,
		.max_brightness = 1023,
	};

	return devm_backlight_device_register(&dsi->dev, "panel0-backlight",
					      &dsi->dev, dsi,
					      &ams653tk01_bl_ops, &props);
}

static int ams653tk01_probe(struct mipi_dsi_device *dsi)
{
	struct device *dev = &dsi->dev;
	struct ams653tk01 *ctx;
	int ret;

	ctx = devm_drm_panel_alloc(dev, struct ams653tk01, panel,
				   &ams653tk01_panel_funcs,
				   DRM_MODE_CONNECTOR_DSI);
	if (IS_ERR(ctx))
		return PTR_ERR(ctx);

	ret = devm_regulator_bulk_get_const(dev,
					    ARRAY_SIZE(ams653tk01_supplies),
					    ams653tk01_supplies, &ctx->supplies);
	if (ret < 0)
		return dev_err_probe(dev, ret, "Failed to get panel regulators\n");

	ctx->reset_gpio = devm_gpiod_get(dev, "reset", GPIOD_OUT_LOW);
	if (IS_ERR(ctx->reset_gpio))
		return dev_err_probe(dev, PTR_ERR(ctx->reset_gpio),
				     "Failed to get reset GPIO\n");

	ctx->dsi = dsi;
	mipi_dsi_set_drvdata(dsi, ctx);

	dsi->lanes = 4;
	dsi->format = MIPI_DSI_FMT_RGB888;
	dsi->mode_flags = MIPI_DSI_MODE_LPM | MIPI_DSI_CLOCK_NON_CONTINUOUS;

	ctx->panel.prepare_prev_first = true;
	ctx->panel.backlight = ams653tk01_create_backlight(dsi);
	if (IS_ERR(ctx->panel.backlight))
		return dev_err_probe(dev, PTR_ERR(ctx->panel.backlight),
				     "Failed to create backlight\n");

	drm_panel_add(&ctx->panel);
	ret = devm_mipi_dsi_attach(dev, dsi);
	if (ret < 0) {
		drm_panel_remove(&ctx->panel);
		return dev_err_probe(dev, ret, "Failed to attach to DSI host\n");
	}

	return 0;
}

static void ams653tk01_remove(struct mipi_dsi_device *dsi)
{
	struct ams653tk01 *ctx = mipi_dsi_get_drvdata(dsi);

	drm_panel_remove(&ctx->panel);
}

static const struct of_device_id ams653tk01_of_match[] = {
	{ .compatible = "samsung,ams653tk01" },
	{ /* sentinel */ }
};
MODULE_DEVICE_TABLE(of, ams653tk01_of_match);

static struct mipi_dsi_driver ams653tk01_driver = {
	.probe = ams653tk01_probe,
	.remove = ams653tk01_remove,
	.driver = {
		.name = "panel-samsung-ams653tk01",
		.of_match_table = ams653tk01_of_match,
	},
};
module_mipi_dsi_driver(ams653tk01_driver);

MODULE_DESCRIPTION("DRM driver for Samsung AMS653TK01 command mode DSI panel");
MODULE_LICENSE("GPL");
