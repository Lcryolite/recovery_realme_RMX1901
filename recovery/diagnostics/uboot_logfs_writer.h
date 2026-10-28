/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef RMX1901_UBOOT_LOGFS_WRITER_H
#define RMX1901_UBOOT_LOGFS_WRITER_H

/* FAT replacement is enabled only for a previously validated owned slot. */
static int rmx1901_logfs_test_write(struct udevice *disk, int partition)
{
	static const char *const filenames[] = { "RMXLOG01.LOG", "RMXLOG02.LOG", "RMXLOG03.LOG" };
	const char *nonce = env_get("fastboot.console-attempt");
	struct blk_desc *desc = dev_get_uclass_plat(disk);
	struct membuf snapshot;
	struct scsi_cmd sync_cmd __attribute__((aligned(ARCH_DMA_MINALIGN))) = { 0 };
	struct rmx_logfs_slot slots[3] = { 0 };
	char *buffer = NULL, *verify = NULL;
	loff_t written = 0, received = 0, size;
	uint64_t generation;
	unsigned selected;
	int header, payload, ret = -1, sync_status;

	if (!nonce || !*nonce || strlen(nonce) > 68)
		return -EINVAL;
	for (const char *p = nonce; *p; p++)
		if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
		      (*p >= '0' && *p <= '9') || *p == '-' || *p == '_'))
			return -EINVAL;
	buffer = memalign(ARCH_DMA_MINALIGN, 32769);
	verify = memalign(ARCH_DMA_MINALIGN, 32769);
	if (!buffer || !verify)
		goto out;
	for (unsigned i = 0; i < 3; i++) {
		if (fs_set_blk_dev_with_part(desc, partition))
			goto out;
		if (fs_get_type() != FS_TYPE_FAT) {
			fs_close();
			goto out;
		}
		if (!fs_exists(filenames[i]))
			continue;
		slots[i].exists = true;
		if (fs_set_blk_dev_with_part(desc, partition) ||
		    fs_size(filenames[i], &size) || size <= 0 || size > 32768)
			goto out;
		if (fs_set_blk_dev_with_part(desc, partition) ||
		    fs_read(filenames[i], (ulong)verify, 0, size, &received) || received != size)
			goto out;
		slots[i].owned = rmx_logfs_record_generation(verify, (size_t)size, i,
							 &slots[i].generation);
	}
	if (rmx_logfs_select(slots, &selected, &generation)) {
		puts("RMXLOGFS_WRITE unowned_or_generation_overflow\n");
		goto out;
	}
#if defined(CONFIG_RMX1901_LOGFS_ONESHOT_03)
	if (selected != 2 || slots[2].exists) {
		puts("RMXLOGFS_WRITE oneshot_already_used\n");
		goto out;
	}
#endif
#if defined(CONFIG_RMX1901_LOGFS_ONESHOT_01)
	if (selected != 0 || !slots[0].exists || slots[0].generation != 0) {
		puts("RMXLOGFS_WRITE oneshot_already_used\n");
		goto out;
	}
#endif
	header = snprintf(buffer, 32768,
		"RMX1901_UBOOT_LOG v1\ncandidate=%s\nconsole_overflow=%d\n\n"
		"RMX1901_LOGFS_FORMAT=2\nsequence=%llu\n",
		nonce, !!(gd->flags & GD_FLG_RECORD_OVF), (unsigned long long)generation);
	if (header <= 0 || header >= 32768)
		goto out;
	snapshot = gd->console_out;
	if (membuf_avail(&snapshot) > 32768 - header)
		goto out;
	payload = membuf_get(&snapshot, buffer + header, 32768 - header);
	if (payload <= 0)
		goto out;
	if (fs_set_blk_dev_with_part(desc, partition))
		goto out;
	if (fs_get_type() != FS_TYPE_FAT) {
		fs_close();
		goto out;
	}
	if (slots[selected].exists)
		rmx_logfs_allow_replace(filenames[selected]);
	ret = fs_write(filenames[selected], (ulong)buffer, 0, header + payload, &written);
	rmx_logfs_allow_replace(NULL);
	if (ret || written != header + payload) {
		printf("RMXLOGFS_WRITE write_failed=%d\n", ret);
		ret = -EIO;
		goto out;
	}
	sync_cmd.cmd[0] = SCSI_SYNC_CACHE;
	sync_cmd.cmdlen = 10;
	sync_cmd.target = desc->target;
	sync_cmd.lun = desc->lun;
	sync_cmd.dma_dir = DMA_NONE;
	sync_status = scsi_exec(disk->parent, &sync_cmd);
	if (fs_set_blk_dev_with_part(desc, partition)) {
		ret = -EIO;
		goto out;
	}
	if (fs_get_type() != FS_TYPE_FAT) {
		fs_close();
		ret = -EIO;
		goto out;
	}
	ret = fs_read(filenames[selected], (ulong)verify, 0, header + payload, &received);
	if (ret || received != header + payload || memcmp(buffer, verify, header + payload)) {
		printf("RMXLOGFS_WRITE readback_failed=1\n");
		ret = -EIO;
		goto out;
	}
	printf("RMXLOGFS_WRITE slot=%u sequence=%llu bytes=%d sync=%d readback=1\n",
	       selected + 1, (unsigned long long)generation, header + payload, sync_status);
	ret = sync_status;
out:
	rmx_logfs_allow_replace(NULL);
	free(verify);
	free(buffer);
	return ret;
}

#endif
