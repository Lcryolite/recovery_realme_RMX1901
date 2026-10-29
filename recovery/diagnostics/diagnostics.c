/* SPDX-License-Identifier: Apache-2.0 */
/* Recovery-only, bounded pstore and kernel-console retention. */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <linux/fs.h>
#include <time.h>
#include <unistd.h>

#define PSTORE_LIMIT (3U * 1024U * 1024U)
#define KERNEL_SEGMENT_LIMIT (512U * 1024U)
#define UBOOT_LIMIT (128U * 1024U)
#define LOGFS_RECORD_LIMIT (32U * 1024U)
#define STATUS_PATH "/tmp/rmx1901-diagnostics.status"
#define DEBUG_FLAG_PATH "/tmp/rmx1901-diagnostics.debug"

int pstore_snapshot(const char *source_path, const char *destination_path, size_t limit);

static volatile sig_atomic_t stop_requested;

static void request_stop(int signal_number)
{
    (void)signal_number;
    stop_requested = 1;
}

static int install_stop_handler(void)
{
    struct sigaction action = { 0 };
    action.sa_handler = request_stop;
    if (sigemptyset(&action.sa_mask)) return -1;
    return sigaction(SIGTERM, &action, NULL);
}

static int write_all(int fd, const void *data, size_t length)
{
    const char *p = data;
    while (length) {
        ssize_t n = write(fd, p, length);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n;
        length -= (size_t)n;
    }
    return 0;
}

static int status_write(const char *path, const char *state, const char *reason)
{
    struct stat st;
    char value[256], temporary[4096];
    int temporary_length = snprintf(temporary, sizeof(temporary), "%s.tmp.XXXXXX", path);
    if (temporary_length < 0 || (size_t)temporary_length >= sizeof(temporary)) return -1;
    if (!lstat(path, &st)) {
        if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
            errno = EPERM;
            return -1;
        }
    } else if (errno != ENOENT) return -1;
    int fd = mkstemp(temporary);
    if (fd < 0) return -1;
    int length = snprintf(value, sizeof(value), "state=%s\nreason=%s\n", state, reason);
    int error = length < 0 || (size_t)length >= sizeof(value) ||
                write_all(fd, value, (size_t)length) || fsync(fd);
    if (close(fd)) error = 1;
    if (!error && rename(temporary, path)) error = 1;
    if (error) unlink(temporary);
    return error ? -1 : 0;
}

static int private_directory(int parent, const char *name, bool create)
{
    struct stat st;
    if (create && mkdirat(parent, name, 0700) && errno != EEXIST) return -1;
    int fd = openat(parent, name, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return -1;
    if (fstat(fd, &st) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
        close(fd);
        errno = EPERM;
        return -1;
    }
    return fd;
}

static bool valid_boot_id(const char *id)
{
    size_t n = strlen(id);
    if (!n || n > 64) return false;
    for (size_t i = 0; i < n; i++) {
        char c = id[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) return false;
    }
    return true;
}

static bool safe_record_name(const char *name)
{
    size_t length = strlen(name);
    if (length < 12 || strncmp(name, "pstore-", 7) || strcmp(name + length - 4, ".raw"))
        return false;
    for (size_t i = 0; i < length; i++) {
        char c = name[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              c == '-' || c == '_' || c == '.')) return false;
    }
    return true;
}

static bool logfs_archive_name(const char *name, const char *suffix)
{
    return !strncmp(name, "uboot-logfs-0", 13) && name[13] >= '1' && name[13] <= '3' &&
           !strcmp(name + 14, suffix);
}

static int inspect_files(int directory_fd, bool pstore, bool remove_files, bool incomplete)
{
    int scan_fd = openat(directory_fd, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    DIR *directory = scan_fd < 0 ? NULL : fdopendir(scan_fd);
    struct dirent *entry;
    bool manifest = false, pstore_directory = false;
    size_t pstore_bytes = 0;
    int result = -1;
    if (!directory) {
        if (scan_fd >= 0) close(scan_fd);
        return -1;
    }
    errno = 0;
    while ((entry = readdir(directory))) {
        struct stat st;
        const char *name = entry->d_name;
        if (!strcmp(name, ".") || !strcmp(name, "..")) continue;
        if (pstore) {
            if (!strcmp(name, "records.json")) manifest = true;
            else if (!safe_record_name(name)) { errno = EINVAL; goto done; }
        } else if (strcmp(name, "boot-id") && strcmp(name, "boot-id.interrupted") &&
                   strcmp(name, "incomplete") && strcmp(name, "pstore.recovered") &&
                   strcmp(name, "kernel.current") &&
                   strcmp(name, "kernel.previous") && strcmp(name, "kernel.truncated") &&
                   strcmp(name, "kernel.seq") && strcmp(name, "kernel.seq.tmp") &&
                   strcmp(name, "uboot-usb.log") && strcmp(name, "uboot-usb.json") &&
                   strcmp(name, "uboot.truncated") &&
                   strcmp(name, "uboot-logfs.partial") &&
                   strcmp(name, "uboot-logfs.unavailable") &&
                   !logfs_archive_name(name, ".log") && !logfs_archive_name(name, ".json") &&
                   strcmp(name, "pstore")) {
            errno = EINVAL;
            goto done;
        }
        if (fstatat(directory_fd, name, &st, AT_SYMLINK_NOFOLLOW) || st.st_uid != geteuid()) goto done;
        if (!pstore && !strcmp(name, "pstore")) {
            if (!S_ISDIR(st.st_mode)) { errno = EINVAL; goto done; }
            pstore_directory = true;
            int child = private_directory(directory_fd, name, false);
            if (child < 0) goto done;
            int error = inspect_files(child, true, remove_files, incomplete);
            close(child);
            if (error || (remove_files && unlinkat(directory_fd, name, AT_REMOVEDIR))) goto done;
        } else {
            if (!S_ISREG(st.st_mode) || (st.st_mode & 0077)) { errno = EINVAL; goto done; }
            if (st.st_size < 0) { errno = EINVAL; goto done; }
            if (pstore) {
                if (!strcmp(name, "records.json") && (size_t)st.st_size > 65535U) {
                    errno = EOVERFLOW;
                    goto done;
                }
                if ((size_t)st.st_size > PSTORE_LIMIT + 65536U - pstore_bytes) {
                    errno = EOVERFLOW;
                    goto done;
                }
                pstore_bytes += (size_t)st.st_size;
            } else if ((!strcmp(name, "kernel.current") || !strcmp(name, "kernel.previous")) &&
                       (size_t)st.st_size > KERNEL_SEGMENT_LIMIT) {
                errno = EOVERFLOW;
                goto done;
            } else if (((!strcmp(name, "boot-id") || !strcmp(name, "boot-id.interrupted")) && st.st_size > 65) ||
                       ((!strcmp(name, "incomplete") || !strcmp(name, "pstore.recovered")) && st.st_size > 256) ||
                       ((!strcmp(name, "kernel.truncated") || !strcmp(name, "uboot.truncated") ||
                         !strcmp(name, "uboot-logfs.partial") ||
                         !strcmp(name, "uboot-logfs.unavailable")) && st.st_size > 256) ||
                       ((!strcmp(name, "kernel.seq") || !strcmp(name, "kernel.seq.tmp")) && st.st_size > 32) ||
                       (!strcmp(name, "uboot-usb.log") && (size_t)st.st_size > UBOOT_LIMIT) ||
                       (!strcmp(name, "uboot-usb.json") && st.st_size > 4096) ||
                       (logfs_archive_name(name, ".log") && (size_t)st.st_size > LOGFS_RECORD_LIMIT) ||
                       (logfs_archive_name(name, ".json") && st.st_size > 4096)) {
                errno = EOVERFLOW;
                goto done;
            }
            if (remove_files && unlinkat(directory_fd, name, 0)) goto done;
        }
        if (remove_files) rewinddir(directory);
        errno = 0;
    }
    if (errno || (!incomplete && ((pstore && !manifest) || (!pstore && !pstore_directory)))) goto done;
    result = 0;
done:
    closedir(directory);
    return result;
}

static int incomplete_session(int session)
{
    struct stat st;
    if (fstatat(session, "incomplete", &st, AT_SYMLINK_NOFOLLOW))
        return errno == ENOENT ? 0 : -1;
    if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077) || st.st_size > 256) {
        errno = EINVAL;
        return -1;
    }
    return 1;
}

static int session_info(int base, const char *name, int *fd, char *boot_id, size_t capacity)
{
    *fd = private_directory(base, name, false);
    if (*fd < 0) return errno == ENOENT ? 0 : -1;
    int marker = openat(*fd, "boot-id", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (marker < 0) return -1;
    ssize_t length = read(marker, boot_id, capacity - 1);
    close(marker);
    if (length <= 0 || (size_t)length >= capacity - 1) { errno = EINVAL; return -1; }
    boot_id[length] = '\0';
    if (boot_id[length - 1] == '\n') boot_id[length - 1] = '\0';
    int incomplete = incomplete_session(*fd);
    if (incomplete < 0 || !valid_boot_id(boot_id) || inspect_files(*fd, false, false, incomplete)) return -1;
    return 1;
}

static int rotate_sessions(int base, const char *boot_id, int *reused)
{
    const char *names[] = { "current", "previous", "oldest" };
    int fds[] = { -1, -1, -1 }, present[3] = { 0, 0, 0 }, result = -1;
    char ids[3][66];
    for (size_t i = 0; i < 3; i++) {
        present[i] = session_info(base, names[i], &fds[i], ids[i], sizeof(ids[i]));
        if (present[i] < 0) goto done;
    }
    if (reused && present[0] && !strcmp(ids[0], boot_id)) {
        *reused = fds[0];
        fds[0] = -1;
        result = 1;
        goto done;
    }
    if (present[0] && present[1] && present[2]) {
        int incomplete = incomplete_session(fds[2]);
        if (incomplete < 0 || inspect_files(fds[2], false, true, incomplete) ||
            unlinkat(base, "oldest", AT_REMOVEDIR)) goto done;
        present[2] = 0;
    }
    if (present[0] && present[1] && renameat(base, "previous", base, "oldest")) goto done;
    if (present[0] && renameat(base, "current", base, "previous")) goto done;
    if (fsync(base)) goto done;
    result = 0;
done:
    for (size_t i = 0; i < 3; i++) if (fds[i] >= 0) close(fds[i]);
    return result;
}

static bool valid_pstore_manifest(int directory, const char *data, bool *partial, bool *empty,
                                  bool *empty_record, bool *all_records_empty);

/* Return 2 for an interrupted snapshot with no preservable bytes. */
static int pending_has_evidence(int directory_fd, bool pstore)
{
    static const char empty_manifest[] = "{\"status\":\"empty\",\"records\":[]}\n";
    int scan_fd = openat(directory_fd, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    DIR *directory = scan_fd < 0 ? NULL : fdopendir(scan_fd);
    struct dirent *entry;
    int result = 0;
    bool notice = false;
    if (!directory) {
        if (scan_fd >= 0) close(scan_fd);
        return -1;
    }
    for (;;) {
        errno = 0;
        entry = readdir(directory);
        if (!entry) { if (errno) result = -1; break; }
        struct stat st;
        const char *name = entry->d_name;
        if (!strcmp(name, ".") || !strcmp(name, "..")) continue;
        if (!pstore && !strcmp(name, "pstore")) {
            int child = private_directory(directory_fd, name, false);
            if (child < 0) { result = -1; break; }
            int child_result = pending_has_evidence(child, true);
            close(child);
            if (child_result < 0 || child_result == 1) { result = child_result; break; }
            if (child_result == 2) notice = true;
        } else {
            if (fstatat(directory_fd, name, &st, AT_SYMLINK_NOFOLLOW)) { result = -1; break; }
            if (pstore && !st.st_size && safe_record_name(name)) { notice = true; continue; }
            if (pstore && !strcmp(name, "records.json")) {
                if (!st.st_size) { notice = true; continue; }
                if (st.st_size == sizeof(empty_manifest) - 1) {
                    char content[sizeof(empty_manifest) - 1];
                    int fd = openat(directory_fd, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
                    if (fd < 0) { result = -1; break; }
                    ssize_t n = read(fd, content, sizeof(content));
                    int closed = close(fd);
                    if (n < 0 || closed) { result = -1; break; }
                    if (n == sizeof(content) && !memcmp(content, empty_manifest, sizeof(content))) continue;
                }
                if (st.st_size > 0 && st.st_size < 65536) {
                    char data[65536];
                    size_t used = 0;
                    int fd = openat(directory_fd, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
                    if (fd < 0) { result = -1; break; }
                    while (used < (size_t)st.st_size) {
                        ssize_t n = read(fd, data + used, (size_t)st.st_size - used);
                        if (n < 0 && errno == EINTR) continue;
                        if (n <= 0) { result = -1; break; }
                        used += (size_t)n;
                    }
                    if (close(fd)) result = -1;
                    if (result) break;
                    data[used] = '\0';
                    bool partial, empty, empty_record, all_empty;
                    if (valid_pstore_manifest(directory_fd, data, &partial, &empty,
                                              &empty_record, &all_empty) && all_empty) {
                        notice = true;
                        continue;
                    }
                }
            } else if (!pstore && (!strcmp(name, "boot-id") || !strcmp(name, "boot-id.interrupted") ||
                                   !strcmp(name, "incomplete") || !strcmp(name, "pstore.recovered"))) {
                continue;
            }
            result = 1;
            break;
        }
    }
    if (!result && notice) result = 2;
    closedir(directory);
    return result;
}

static int write_session_marker(int session, const char *name, const char *value)
{
    struct stat st;
    int fd = openat(session, name, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0 && errno == EEXIST) {
        fd = openat(session, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (fd < 0) return -1;
        if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() ||
            (st.st_mode & 0077) || st.st_size < 0 || st.st_size > 256) {
            close(fd);
            errno = EINVAL;
            return -1;
        }
        int error = fsync(fd);
        if (close(fd)) error = 1;
        return error || fsync(session) ? -1 : 0;
    }
    if (fd < 0) return -1;
    int error = write_all(fd, value, strlen(value)) || fsync(fd);
    if (close(fd)) error = 1;
    return error || fsync(session) ? -1 : 0;
}

static int recover_pending(int base, const char *boot_id)
{
    char synthetic[66], existing[66];
    struct stat st;
    int pending = private_directory(base, "pending", false), marker = -1;
    int result = -1;
    if (pending < 0) return errno == ENOENT ? 0 : -1;
    if (inspect_files(pending, false, false, true)) goto done;
    int evidence = pending_has_evidence(pending, false);
    if (evidence < 0) goto done;
    if (evidence != 1) {
        bool prior_notice = !fstatat(pending, "pstore.recovered", &st, AT_SYMLINK_NOFOLLOW);
        if (!prior_notice && errno != ENOENT) goto done;
        if ((evidence == 2 || prior_notice) &&
            write_session_marker(base, "pstore.pending-warning",
                                 "An interrupted pstore snapshot may not have retained its evidence.\n"))
            goto done;
        if (inspect_files(pending, false, true, true) || unlinkat(base, "pending", AT_REMOVEDIR) || fsync(base))
            goto done;
        result = (evidence == 2 || prior_notice) ? 1 : 0;
        goto done;
    }
    if (write_session_marker(pending, "incomplete", "Interrupted pstore snapshot; raw files may be partial.\n"))
        goto done;
    int length = snprintf(synthetic, sizeof(synthetic), "interrupted-%.40s", boot_id);
    if (length < 0 || (size_t)length >= sizeof(synthetic) || !valid_boot_id(synthetic)) goto done;
    marker = openat(pending, "boot-id", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (marker >= 0) {
        ssize_t n = read(marker, existing, sizeof(existing) - 1);
        if (n < 0 || (size_t)n >= sizeof(existing) - 1) goto done;
        existing[n] = '\0';
        if (n && existing[n - 1] == '\n') existing[n - 1] = '\0';
        close(marker);
        marker = -1;
        if (!strncmp(existing, "interrupted-", sizeof("interrupted-") - 1) && valid_boot_id(existing)) {
            strcpy(synthetic, existing);
        } else if (strcmp(existing, synthetic)) {
            if (!fstatat(pending, "boot-id.interrupted", &st, AT_SYMLINK_NOFOLLOW) || errno != ENOENT ||
                renameat(pending, "boot-id", pending, "boot-id.interrupted") || fsync(pending)) goto done;
        }
    } else if (errno != ENOENT) goto done;
    char value[68];
    length = snprintf(value, sizeof(value), "%s\n", synthetic);
    if (length < 0 || (size_t)length >= sizeof(value) || write_session_marker(pending, "boot-id", value)) goto done;
    /* A pending snapshot is distinct evidence even when its synthetic ID matches current. */
    int rotation = rotate_sessions(base, synthetic, NULL);
    if (rotation != 0) { errno = EEXIST; goto done; }
    if (renameat(base, "pending", base, "current") || fsync(base)) goto done;
    result = 1;
done:
    if (marker >= 0) close(marker);
    close(pending);
    return result;
}

static int create_session(const char *cache, const char *source, const char *boot_id, int *session)
{
    int cache_fd = -1, base = -1, current = -1, pstore = -1, marker = -1, existing = -1;
    char pstore_path[4096];
    int result = -1;
    if (!valid_boot_id(boot_id)) { errno = EINVAL; return -1; }
    cache_fd = open(cache, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (cache_fd < 0) goto done;
    base = private_directory(cache_fd, "rmx1901-diagnostics", true);
    if (base < 0) goto done;
    int recovered = recover_pending(base, boot_id);
    if (recovered < 0) goto done;
    struct stat notice_stat;
    bool pending_warning = !fstatat(base, "pstore.pending-warning", &notice_stat, AT_SYMLINK_NOFOLLOW);
    if (pending_warning) {
        if (!S_ISREG(notice_stat.st_mode) || notice_stat.st_uid != geteuid() ||
            (notice_stat.st_mode & 0077) || notice_stat.st_size > 256) { errno = EINVAL; goto done; }
        recovered = 1;
    } else if (errno != ENOENT) goto done;
    char existing_id[66];
    int present = session_info(base, "current", &existing, existing_id, sizeof(existing_id));
    if (present < 0) goto done;
    if (present) {
        int incomplete = incomplete_session(existing);
        if (incomplete < 0) goto done;
        if (incomplete) recovered = 1;
    }
    if (present && !strcmp(existing_id, boot_id)) {
        if (recovered && write_session_marker(existing, "pstore.recovered",
                                               "An interrupted pstore snapshot was detected; evidence may be incomplete.\n"))
            goto done;
        if (pending_warning && (unlinkat(base, "pstore.pending-warning", 0) || fsync(base))) goto done;
        *session = existing;
        existing = -1;
        result = 0;
        goto done;
    }
    if (mkdirat(base, "pending", 0700)) goto done;
    current = private_directory(base, "pending", false);
    if (current < 0 || mkdirat(current, "pstore", 0700)) goto done;
    pstore = private_directory(current, "pstore", false);
    if (pstore < 0) goto done;
    int length = snprintf(pstore_path, sizeof(pstore_path), "%s/rmx1901-diagnostics/pending/pstore", cache);
    if (length < 0 || (size_t)length >= sizeof(pstore_path)) { errno = ENAMETOOLONG; goto done; }
    if (pstore_snapshot(source, pstore_path, PSTORE_LIMIT)) goto done;
    marker = openat(current, "boot-id", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (marker < 0 || write_all(marker, boot_id, strlen(boot_id)) ||
        write_all(marker, "\n", 1) || fsync(marker)) goto done;
    if (recovered && write_session_marker(current, "pstore.recovered",
                                           "An interrupted pstore snapshot was detected; evidence may be incomplete.\n"))
        goto done;
    if (fsync(current)) goto done;
    int rotation = rotate_sessions(base, boot_id, session);
    if (rotation != 0) { errno = EEXIST; goto done; }
    if (renameat(base, "pending", base, "current") || fsync(base)) goto done;
    if (pending_warning && (unlinkat(base, "pstore.pending-warning", 0) || fsync(base))) goto done;
    *session = current;
    current = -1;
    result = 0;
done:
    if (marker >= 0) close(marker);
    if (existing >= 0) close(existing);
    if (pstore >= 0) close(pstore);
    if (current >= 0) close(current);
    if (base >= 0) close(base);
    if (cache_fd >= 0) close(cache_fd);
    return result;
}

static int open_kernel_file(int session)
{
    struct stat st;
    int fd = openat(session, "kernel.current", O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) return -1;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() ||
        (st.st_mode & 0077) || st.st_size < 0 || st.st_size > KERNEL_SEGMENT_LIMIT) {
        close(fd);
        errno = EINVAL;
        return -1;
    }
    return fd;
}

static int mark_kernel_truncated(int session)
{
    struct stat st;
    int marker = openat(session, "kernel.truncated",
                        O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (marker < 0 && errno == EEXIST) {
        marker = openat(session, "kernel.truncated", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (marker >= 0 && (fstat(marker, &st) || !S_ISREG(st.st_mode) ||
                            st.st_uid != geteuid() || (st.st_mode & 0077))) {
            close(marker);
            errno = EPERM;
            return -1;
        }
    } else if (marker >= 0) {
        const char text[] = "Earlier kernel log data was rotated or lost.\n";
        if (write_all(marker, text, sizeof(text) - 1) || fsync(marker)) {
            close(marker);
            return -1;
        }
    }
    if (marker < 0 || close(marker) || fsync(session)) return -1;
    return 0;
}

static int discard_previous_kernel(int session)
{
    struct stat st;
    if (fstatat(session, "kernel.previous", &st, AT_SYMLINK_NOFOLLOW))
        return errno == ENOENT ? 0 : -1;
    if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
        errno = EPERM;
        return -1;
    }
    if (mark_kernel_truncated(session)) return -1;
    return unlinkat(session, "kernel.previous", 0);
}

static bool debug_enabled(const char *path)
{
    struct stat st;
    char flag[2];
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return false;
    bool enabled = !fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_uid == geteuid() &&
                   !(st.st_mode & 0077) && st.st_size == 2 &&
                   read(fd, flag, sizeof(flag)) == sizeof(flag) && flag[0] == '1' && flag[1] == '\n';
    close(fd);
    return enabled;
}

static int kernel_record_header(const char *line, unsigned long *priority, uint64_t *sequence)
{
    char *end;
    if (line[0] < '0' || line[0] > '9') return -1;
    errno = 0;
    *priority = strtoul(line, &end, 10);
    if (errno || *end != ',' || *priority > 191 || end[1] < '0' || end[1] > '9') return -1;
    const char *start = end + 1;
    errno = 0;
    unsigned long long number = strtoull(start, &end, 10);
    if (errno || *end != ',') return -1;
    *sequence = (uint64_t)number;
    return 0;
}

static int saved_kernel_sequence(int session, bool *have_sequence, uint64_t *sequence)
{
    const char *names[] = { "kernel.previous", "kernel.current" };
    *have_sequence = false;
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        int fd = openat(session, names[i], O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (fd < 0 && errno == ENOENT) continue;
        if (fd < 0) return -1;
        struct stat st;
        if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() ||
            (st.st_mode & 0077) || st.st_size < 0 || st.st_size > KERNEL_SEGMENT_LIMIT) {
            close(fd);
            errno = EINVAL;
            return -1;
        }
        FILE *file = fdopen(fd, "r");
        if (!file) { close(fd); return -1; }
        char *line = NULL;
        size_t capacity = 0;
        ssize_t length;
        int failed = 0;
        while ((length = getline(&line, &capacity, file)) >= 0) {
            if (!length || line[length - 1] != '\n') {
                if (mark_kernel_truncated(session)) failed = 1;
                break;
            }
            if (line[0] == ' ' || line[0] == '\n') continue;
            unsigned long priority;
            uint64_t number;
            if (kernel_record_header(line, &priority, &number)) {
                if (mark_kernel_truncated(session)) failed = 1;
                continue;
            }
            if (!*have_sequence || number > *sequence) *sequence = number;
            *have_sequence = true;
        }
        if (ferror(file)) failed = 1;
        free(line);
        if (fclose(file)) failed = 1;
        if (failed) return -1;
    }
    return 0;
}

static int read_kernel_checkpoint(int session, bool *have_sequence, uint64_t *sequence)
{
    char data[33], canonical[33], *end;
    struct stat st;
    *have_sequence = false;
    int fd = openat(session, "kernel.seq", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return errno == ENOENT ? 0 : -1;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() ||
        (st.st_mode & 0077) || st.st_size <= 0 || st.st_size > 32) {
        close(fd);
        return mark_kernel_truncated(session);
    }
    ssize_t length = read(fd, data, (size_t)st.st_size);
    int closed = close(fd);
    if (length != st.st_size || closed) return -1;
    data[length] = '\0';
    errno = 0;
    unsigned long long value = strtoull(data, &end, 10);
    int n = snprintf(canonical, sizeof(canonical), "%llu\n", value);
    if (errno || n < 0 || (size_t)n >= sizeof(canonical) || strcmp(data, canonical))
        return mark_kernel_truncated(session);
    *sequence = (uint64_t)value;
    *have_sequence = true;
    return 0;
}

static int save_kernel_checkpoint(int session, uint64_t sequence)
{
    const char *names[] = { "kernel.seq", "kernel.seq.tmp" };
    struct stat st;
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        if (fstatat(session, names[i], &st, AT_SYMLINK_NOFOLLOW)) {
            if (errno == ENOENT) continue;
            return -1;
        }
        if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077) ||
            st.st_size < 0 || st.st_size > 32) { errno = EINVAL; return -1; }
        if (i == 1 && unlinkat(session, names[i], 0)) return -1;
    }
    int fd = openat(session, "kernel.seq.tmp", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) return -1;
    char value[33];
    int length = snprintf(value, sizeof(value), "%llu\n", (unsigned long long)sequence);
    int error = length < 0 || (size_t)length >= sizeof(value) ||
                write_all(fd, value, (size_t)length) || fsync(fd);
    if (close(fd)) error = 1;
    if (!error && renameat(session, "kernel.seq.tmp", session, "kernel.seq")) error = 1;
    if (!error && fsync(session)) error = 1;
    if (error) unlinkat(session, "kernel.seq.tmp", 0);
    return error ? -1 : 0;
}

static int append_kernel_line(int session, int *output, size_t *size, const char *line, size_t length,
                              bool *have_record, bool *keep_record, bool include_debug,
                              bool have_resume, uint64_t resume_sequence,
                              bool *have_seen, uint64_t *seen_sequence)
{
    if (line[0] == ' ' || line[0] == '\n') {
        if (!*have_record) return mark_kernel_truncated(session);
    } else {
        unsigned long priority;
        uint64_t sequence;
        if (kernel_record_header(line, &priority, &sequence)) { errno = EINVAL; return -1; }
        *have_record = true;
        *keep_record = !(priority >> 3) && ((priority & 7) != 7 || include_debug);
        if (!*have_seen || sequence > *seen_sequence) {
            if (have_resume && *have_seen) {
                if (sequence - *seen_sequence > 1 && mark_kernel_truncated(session)) return -1;
            }
            *seen_sequence = sequence;
            *have_seen = true;
        }
        if (have_resume) {
            if (sequence <= resume_sequence) *keep_record = false;
        }
    }
    if (!*keep_record) return 0;
    if (length > KERNEL_SEGMENT_LIMIT) { errno = EOVERFLOW; return -1; }
    if (*size + length > KERNEL_SEGMENT_LIMIT) {
        if (fsync(*output) || close(*output)) return -1;
        *output = -1;
        if (discard_previous_kernel(session)) return -1;
        if (renameat(session, "kernel.current", session, "kernel.previous")) return -1;
        *output = open_kernel_file(session);
        if (*output < 0) return -1;
        *size = 0;
    }
    if (write_all(*output, line, length)) {
        int saved_errno = errno;
        mark_kernel_truncated(session);
        errno = saved_errno;
        return -1;
    }
    *size += length;
    return 0;
}

static int capture_status(int session, const char *status);
static bool is_mounted(const char *path, const char *type);
static bool valid_attempt_nonce(const char *nonce);
static bool valid_usb_identity(const char *nonce, const char *sha);
static int snapshot_logfs(int session, const char *source);
static int snapshot_device_logfs(int session);

static int capture_kernel(int session, const char *input_path, const char *status,
                          bool once, bool monitor_mounts, const char *logfs_source,
                          const char *debug_path)
{
    char chunk[8192], line[8192];
    size_t used = 0, size = 0;
    struct stat st;
    int input = open(input_path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
    int output = -1, result = -1;
    struct timespec last_sync, now;
    bool dirty = true;
    bool have_record = false, keep_record = false;
    bool have_resume = false;
    bool have_seen = false, have_candidate = false, have_saved = false;
    uint64_t resume_sequence = 0, seen_sequence = 0, candidate = 0, saved_sequence = 0;
    bool include_debug = debug_path && debug_enabled(debug_path);
    unsigned int retry_seconds = 0;
    if (input < 0) goto done;
    if (fstat(input, &st)) goto done;
    bool input_is_char = S_ISCHR(st.st_mode);
    if (once && !S_ISREG(st.st_mode)) {
        fprintf(stderr, "one-shot capture requires a regular kernel input file; use --stream for live input\n");
        errno = EINVAL;
        goto done;
    }
    if (saved_kernel_sequence(session, &have_saved, &saved_sequence) ||
        read_kernel_checkpoint(session, &have_resume, &resume_sequence)) goto done;
    if (have_saved && (!have_resume || saved_sequence > resume_sequence) && mark_kernel_truncated(session))
        goto done;
    seen_sequence = resume_sequence;
    have_seen = have_resume;
    output = open_kernel_file(session);
    if (output < 0 || fstat(output, &st)) goto done;
    size = (size_t)st.st_size;
    if (size) {
        char last;
        int check = openat(session, "kernel.current", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (check < 0) goto done;
        ssize_t n = pread(check, &last, 1, (off_t)size - 1);
        int closed = close(check);
        if (n != 1 || closed) goto done;
        if (last != '\n') {
            if (mark_kernel_truncated(session)) goto done;
            if (size < KERNEL_SEGMENT_LIMIT) {
                if (write_all(output, "\n", 1) || fsync(output)) goto done;
                size++;
            }
        }
    }
    if (clock_gettime(CLOCK_MONOTONIC, &last_sync) ||
        status_write(status, "pending", "waiting_for_kernel_sync")) goto done;
    for (;;) {
        if (stop_requested) {
            if (used && mark_kernel_truncated(session)) goto done;
            break;
        }
        if (clock_gettime(CLOCK_MONOTONIC, &now)) goto done;
        long elapsed = (now.tv_sec - last_sync.tv_sec) * 1000L +
                       (now.tv_nsec - last_sync.tv_nsec) / 1000000L;
        if (elapsed >= 1000) {
            include_debug = debug_path && debug_enabled(debug_path);
            if (monitor_mounts && (!is_mounted("/cache", NULL) ||
                                   !is_mounted("/sys/fs/pstore", "pstore"))) {
                errno = ENODEV;
                goto done;
            }
            if ((monitor_mounts || logfs_source) && ++retry_seconds >= 5) {
                retry_seconds = 0;
                if (!fstatat(session, "uboot-logfs.unavailable", &st, AT_SYMLINK_NOFOLLOW)) {
                    if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077) ||
                        (logfs_source ? snapshot_logfs(session, logfs_source) : snapshot_device_logfs(session)))
                        goto done;
                } else if (errno != ENOENT) goto done;
            }
            if (dirty && fsync(output)) {
                mark_kernel_truncated(session);
                goto done;
            }
            if (have_candidate && (!have_resume || candidate > resume_sequence)) {
                if (save_kernel_checkpoint(session, candidate)) { mark_kernel_truncated(session); goto done; }
                resume_sequence = candidate;
                have_resume = true;
            } else if (dirty && fsync(session)) { mark_kernel_truncated(session); goto done; }
            if (capture_status(session, status)) goto done;
            dirty = false;
            last_sync = now;
            elapsed = 0;
        }
        struct pollfd item = { .fd = input, .events = POLLIN };
        int ready = poll(&item, 1, (int)(1000 - elapsed));
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) goto done;
        if (!ready) continue;
        ssize_t n = read(input, chunk, sizeof(chunk));
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (n < 0 && errno == EPIPE && !once) {
            if (mark_kernel_truncated(session) || capture_status(session, status)) goto done;
            used = 0;
            have_record = false;
            keep_record = false;
            continue;
        }
        if (n < 0) goto done;
        if (!n) {
            if (!once || used) { errno = EIO; goto done; }
            if (have_seen) { candidate = seen_sequence; have_candidate = true; }
            break;
        }
        for (ssize_t i = 0; i < n; i++) {
            if (used == sizeof(line) - 1) { errno = EOVERFLOW; goto done; }
            line[used++] = chunk[i];
            if (chunk[i] == '\n') {
                line[used] = '\0';
                if (append_kernel_line(session, &output, &size, line, used,
                                       &have_record, &keep_record, include_debug,
                                       have_resume, resume_sequence, &have_seen, &seen_sequence)) goto done;
                dirty = true;
                used = 0;
            }
        }
        if (input_is_char && !used && have_seen) { candidate = seen_sequence; have_candidate = true; }
    }
    if (fsync(output) ||
        (have_candidate && (!have_resume || candidate > resume_sequence) ?
         save_kernel_checkpoint(session, candidate) : fsync(session))) {
        mark_kernel_truncated(session);
        goto done;
    }
    result = 0;
done:
    if (input >= 0) close(input);
    if (output >= 0) close(output);
    return result;
}

static bool valid_usb_metadata(const char *data, uint64_t *stored, bool *truncated)
{
    char nonce[69], sha[65], flag[6], canonical[512];
    unsigned long long source, saved;
    int end = -1;
    int fields = sscanf(data,
        "{\"source\":\"uboot_usb_console\",\"candidate_nonce\":\"%68[A-Za-z0-9_-]\","
        "\"reported_sha256\":\"%64[0-9a-f]\",\"boot_instance_verified\":false,"
        "\"source_bytes\":%llu,\"stored_bytes\":%llu,\"truncated\":%5[a-z]}%n",
        nonce, sha, &source, &saved, flag, &end);
    if (fields != 5 || end < 0 || data[end] != '\n' || data[end + 1] != '\0' ||
        !valid_usb_identity(nonce, sha) || saved > UBOOT_LIMIT || source < saved ||
        (strcmp(flag, "true") && strcmp(flag, "false"))) return false;
    *truncated = !strcmp(flag, "true");
    if (*truncated != (source > saved)) return false;
    int length = snprintf(canonical, sizeof(canonical),
        "{\"source\":\"uboot_usb_console\",\"candidate_nonce\":\"%s\",\"reported_sha256\":\"%s\","
        "\"boot_instance_verified\":false,\"source_bytes\":%llu,\"stored_bytes\":%llu,\"truncated\":%s}\n",
        nonce, sha, source, saved, flag);
    if (length < 0 || (size_t)length >= sizeof(canonical) || strcmp(data, canonical)) return false;
    *stored = (uint64_t)saved;
    return true;
}

static bool valid_logfs_metadata(const char *data, uint64_t *stored, bool *overflow)
{
    char nonce[69], flag[6], canonical[512];
    unsigned long long saved;
    int end = -1;
    int fields = sscanf(data,
        "{\"source\":\"uboot_logfs\",\"candidate_nonce\":\"%68[A-Za-z0-9_-]\","
        "\"boot_instance_verified\":false,\"console_overflow\":%5[a-z],\"bytes\":%llu}%n",
        nonce, flag, &saved, &end);
    if (fields != 3 || end < 0 || data[end] != '\n' || data[end + 1] != '\0' ||
        !valid_attempt_nonce(nonce) || !saved || saved > LOGFS_RECORD_LIMIT ||
        (strcmp(flag, "true") && strcmp(flag, "false"))) return false;
    int length = snprintf(canonical, sizeof(canonical),
        "{\"source\":\"uboot_logfs\",\"candidate_nonce\":\"%s\","
        "\"boot_instance_verified\":false,\"console_overflow\":%s,\"bytes\":%llu}\n",
        nonce, flag, saved);
    if (length < 0 || (size_t)length >= sizeof(canonical) || strcmp(data, canonical)) return false;
    *stored = (uint64_t)saved;
    *overflow = !strcmp(flag, "true");
    return true;
}

static bool valid_pstore_manifest(int directory, const char *data, bool *partial, bool *empty,
                                  bool *empty_record, bool *all_records_empty)
{
    char state[9], files[128][144];
    size_t count = 0, saved_total = 0, files_count = 0;
    bool incomplete = false, zero_length = false, all_zero = true;
    int header = -1;
    if (sscanf(data, "{\"status\":\"%8[a-z]\",\"records\":[%n", state, &header) != 1 || header < 0 ||
        (strcmp(state, "empty") && strcmp(state, "captured") && strcmp(state, "partial"))) return false;
    const char *cursor = data + header;
    while (*cursor && *cursor != ']') {
        char source[128], type[8], file[144], flag[6], canonical[512], expected[144];
        unsigned long long bytes, saved;
        int prefix = -1, suffix = -1;
        if (count && *cursor++ != ',') return false;
        const char *record = cursor;
        if (count == 128 ||
            sscanf(cursor, "{\"source_name\":\"%127[a-z0-9._-]\",\"type\":\"%7[a-z]\","
                           "\"origin\":\"unknown\",\"file\":\"%n", source, type, &prefix) != 2 || prefix < 0)
            return false;
        cursor += prefix;
        size_t name_length = 0;
        while (cursor[name_length] && cursor[name_length] != '"') {
            char c = cursor[name_length];
            if (name_length >= sizeof(file) - 1 ||
                !((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.'))
                return false;
            file[name_length++] = c;
        }
        file[name_length] = '\0';
        cursor += name_length;
        if (sscanf(cursor, "\",\"bytes\":%llu,\"saved_bytes\":%llu,\"truncated\":%5[a-z]}%n",
                   &bytes, &saved, flag, &suffix) != 3 || suffix < 0) return false;
        cursor += suffix;
        if ((strcmp(type, "crash") || strncmp(source, "dmesg-ramoops", 13)) &&
            (strcmp(type, "console") || strncmp(source, "console-ramoops", 15)) &&
            (strcmp(type, "pmsg") || strncmp(source, "pmsg-ramoops", 12))) return false;
        if (saved > bytes || saved > PSTORE_LIMIT - saved_total ||
            (strcmp(flag, "true") && strcmp(flag, "false")) ||
            (!strcmp(flag, "true")) != (saved != bytes)) return false;
        int n = snprintf(canonical, sizeof(canonical),
                         "{\"source_name\":\"%s\",\"type\":\"%s\",\"origin\":\"unknown\","
                         "\"file\":\"%s\",\"bytes\":%llu,\"saved_bytes\":%llu,\"truncated\":%s}",
                         source, type, file, bytes, saved, flag);
        if (n < 0 || (size_t)n >= sizeof(canonical) || (size_t)n != (size_t)(cursor - record) ||
            memcmp(record, canonical, (size_t)n)) return false;
        if (saved) {
            n = snprintf(expected, sizeof(expected), "pstore-%s.raw", source);
            if (n < 0 || (size_t)n >= sizeof(expected) || strcmp(file, expected)) return false;
            for (size_t i = 0; i < files_count; i++) if (!strcmp(files[i], file)) return false;
            strcpy(files[files_count++], file);
            struct stat st;
            if (fstatat(directory, file, &st, AT_SYMLINK_NOFOLLOW) || !S_ISREG(st.st_mode) ||
                st.st_uid != geteuid() || (st.st_mode & 0077) || st.st_size < 0 ||
                (uint64_t)st.st_size != saved) return false;
        } else if (*file) return false;
        saved_total += (size_t)saved;
        zero_length |= bytes == 0;
        all_zero &= bytes == 0;
        incomplete |= saved != bytes || bytes == 0;
        count++;
    }
    if (strcmp(cursor, "]}\n") || (!strcmp(state, "empty") != (count == 0)) ||
        (!strcmp(state, "partial") != incomplete)) return false;
    int scan_fd = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    DIR *scan = scan_fd < 0 ? NULL : fdopendir(scan_fd);
    if (!scan) { if (scan_fd >= 0) close(scan_fd); return false; }
    size_t observed = 0;
    struct dirent *entry;
    errno = 0;
    while ((entry = readdir(scan))) {
        if (!safe_record_name(entry->d_name)) continue;
        bool found = false;
        for (size_t i = 0; i < files_count; i++) found |= !strcmp(files[i], entry->d_name);
        if (!found) { closedir(scan); return false; }
        observed++;
        errno = 0;
    }
    bool valid = !errno && observed == files_count;
    closedir(scan);
    *partial = incomplete;
    *empty = count == 0;
    *empty_record = zero_length;
    *all_records_empty = count && all_zero;
    return valid;
}

static int capture_status(int session, const char *status)
{
    char data[65536];
    struct stat st;
    bool kernel_truncated = false;
    bool uboot_truncated = false;
    bool uboot_log = false, uboot_metadata = false;
    bool uboot_invalid = false;
    bool logfs_incomplete = false;
    bool pstore_interrupted = false;
    off_t usb_size = 0;
    if (!fstatat(session, "kernel.truncated", &st, AT_SYMLINK_NOFOLLOW)) {
        if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
            errno = EINVAL;
            return -1;
        }
        kernel_truncated = true;
    } else if (errno != ENOENT) return -1;
    if (!fstatat(session, "uboot-usb.log", &st, AT_SYMLINK_NOFOLLOW)) {
        if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
            errno = EINVAL;
            return -1;
        }
        uboot_log = true;
        usb_size = st.st_size;
    } else if (errno != ENOENT) return -1;
    if (!fstatat(session, "uboot-usb.json", &st, AT_SYMLINK_NOFOLLOW)) {
        if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
            errno = EINVAL;
            return -1;
        }
        uboot_metadata = true;
    } else if (errno != ENOENT) return -1;
    if (uboot_metadata) {
        char usb_data[4097];
        size_t used = 0;
        int usb = openat(session, "uboot-usb.json", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (usb < 0) return -1;
        for (;;) {
            ssize_t n = read(usb, usb_data + used, sizeof(usb_data) - 1 - used);
            if (n < 0 && errno == EINTR) continue;
            if (n < 0 || (size_t)n == sizeof(usb_data) - 1 - used) {
                close(usb);
                errno = EINVAL;
                return -1;
            }
            if (!n) break;
            used += (size_t)n;
        }
        close(usb);
        usb_data[used] = '\0';
        bool truncated = false;
        uint64_t stored = 0;
        if (!valid_usb_metadata(usb_data, &stored, &truncated) ||
            (uboot_log && stored != (uint64_t)usb_size)) {
            uboot_invalid = true;
        } else if (truncated) uboot_truncated = true;
    }
    if (!fstatat(session, "uboot.truncated", &st, AT_SYMLINK_NOFOLLOW)) {
        if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
            errno = EINVAL;
            return -1;
        }
        uboot_truncated = true;
    } else if (errno != ENOENT) return -1;
    const char *logfs_markers[] = { "uboot-logfs.partial", "uboot-logfs.unavailable" };
    for (size_t i = 0; i < sizeof(logfs_markers) / sizeof(logfs_markers[0]); i++) {
        if (!fstatat(session, logfs_markers[i], &st, AT_SYMLINK_NOFOLLOW)) {
            if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
                errno = EINVAL;
                return -1;
            }
            logfs_incomplete = true;
        } else if (errno != ENOENT) return -1;
    }
    if (!fstatat(session, "pstore.recovered", &st, AT_SYMLINK_NOFOLLOW)) {
        if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
            errno = EINVAL;
            return -1;
        }
        pstore_interrupted = true;
    } else if (errno != ENOENT) return -1;
    for (int slot = 1; slot <= 3; slot++) {
        char log_name[32], meta_name[32], logfs_data[4097];
        snprintf(log_name, sizeof(log_name), "uboot-logfs-%02d.log", slot);
        snprintf(meta_name, sizeof(meta_name), "uboot-logfs-%02d.json", slot);
        bool log_exists = !fstatat(session, log_name, &st, AT_SYMLINK_NOFOLLOW);
        off_t log_size = 0;
        if (log_exists) {
            if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077) ||
                st.st_size <= 0 || (uint64_t)st.st_size > LOGFS_RECORD_LIMIT) return -1;
            log_size = st.st_size;
        } else if (errno != ENOENT) return -1;
        bool meta_exists = !fstatat(session, meta_name, &st, AT_SYMLINK_NOFOLLOW);
        if (meta_exists) {
            if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077) ||
                st.st_size <= 0 || st.st_size > 4096) return -1;
            int meta = openat(session, meta_name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
            if (meta < 0) return -1;
            size_t used = 0;
            for (;;) {
                ssize_t n = read(meta, logfs_data + used, sizeof(logfs_data) - 1 - used);
                if (n < 0 && errno == EINTR) continue;
                if (n < 0 || (size_t)n == sizeof(logfs_data) - 1 - used) {
                    close(meta);
                    errno = EINVAL;
                    return -1;
                }
                if (!n) break;
                used += (size_t)n;
            }
            if (close(meta)) return -1;
            logfs_data[used] = '\0';
            uint64_t stored = 0;
            bool overflow = false;
            if (!valid_logfs_metadata(logfs_data, &stored, &overflow) || overflow ||
                (log_exists && stored != (uint64_t)log_size)) logfs_incomplete = true;
        } else if (errno != ENOENT) return -1;
        if (log_exists != meta_exists) logfs_incomplete = true;
    }
    int pstore = private_directory(session, "pstore", false);
    if (pstore < 0) return -1;
    int fd = openat(pstore, "records.json", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) { close(pstore); return -1; }
    size_t length = 0;
    for (;;) {
        ssize_t n = read(fd, data + length, sizeof(data) - 1 - length);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 || (size_t)n == sizeof(data) - 1 - length) {
            close(fd);
            close(pstore);
            errno = EINVAL;
            return -1;
        }
        if (!n) break;
        length += (size_t)n;
    }
    close(fd);
    data[length] = '\0';
    bool pstore_partial = false, pstore_empty = false, pstore_empty_record = false, pstore_all_empty = false;
    bool pstore_invalid = !valid_pstore_manifest(pstore, data, &pstore_partial, &pstore_empty,
                                                 &pstore_empty_record, &pstore_all_empty);
    close(pstore);
    if (kernel_truncated || uboot_truncated || uboot_invalid || uboot_log != uboot_metadata ||
        logfs_incomplete || pstore_interrupted || pstore_invalid || pstore_partial)
        return status_write(status, "partial", kernel_truncated ? "kernel_truncated" :
                            ((uboot_invalid || uboot_log != uboot_metadata) ? "uboot_incomplete" :
                             (uboot_truncated ? "uboot_truncated" :
                              (logfs_incomplete ? "uboot_logfs_incomplete" :
                               (pstore_interrupted ? "pstore_interrupted" :
                                (pstore_invalid ? "pstore_incomplete" :
                                 (pstore_empty_record ? "pstore_empty_record" : "pstore_truncated")))))));
    return status_write(status, "ok", pstore_empty ? "pstore_empty" : "captured");
}

static bool is_mounted(const char *path, const char *type)
{
    char device[512], mountpoint[512], filesystem[128], remainder[1024];
    FILE *mounts = fopen("/proc/mounts", "r");
    if (!mounts) return false;
    bool found = false;
    while (fscanf(mounts, "%511s %511s %127s %1023[^\n]\n",
                  device, mountpoint, filesystem, remainder) == 4) {
        if (!strcmp(mountpoint, path) && (!type || !strcmp(filesystem, type))) {
            found = true;
            break;
        }
    }
    fclose(mounts);
    return found;
}

static bool valid_attempt_nonce(const char *nonce)
{
    size_t length = strlen(nonce);
    if (!length || length > 68) return false;
    for (size_t i = 0; i < length; i++) {
        char c = nonce[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
    }
    return true;
}

static bool valid_usb_identity(const char *nonce, const char *sha)
{
    if (strncmp(nonce, "usb-", 4) || strlen(nonce) < 5 ||
        !valid_attempt_nonce(nonce) || strlen(sha) != 64) return false;
    for (size_t i = 0; i < 64; i++)
        if (!((sha[i] >= '0' && sha[i] <= '9') || (sha[i] >= 'a' && sha[i] <= 'f'))) return false;
    return true;
}

static int import_uboot(const char *cache, const char *source, const char *nonce,
                        const char *sha, const char *expected_boot, const char *status)
{
    int cache_fd = -1, base = -1, session = -1, input = -1, output = -1, metadata = -1, marker = -1;
    bool log_created = false, meta_created = false, marker_created = false, committed = false;
    char boot_id[66], chunk[8192], json[512];
    struct stat st;
    int result = -1;
    if (!valid_usb_identity(nonce, sha) || !valid_boot_id(expected_boot)) { errno = EINVAL; goto done; }
    cache_fd = open(cache, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (cache_fd < 0) goto done;
    base = private_directory(cache_fd, "rmx1901-diagnostics", false);
    if (base < 0 || session_info(base, "current", &session, boot_id, sizeof(boot_id)) != 1) goto done;
    if (strcmp(boot_id, expected_boot)) { errno = EINVAL; goto done; }
    input = open(source, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (input < 0 || fstat(input, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() ||
        (st.st_mode & 0077) || st.st_size < 0) goto done;
    off_t total = st.st_size;
    size_t saved = total > UBOOT_LIMIT ? UBOOT_LIMIT : (size_t)total;
    bool truncated = total > UBOOT_LIMIT;
    if (lseek(input, total - (off_t)saved, SEEK_SET) < 0) goto done;
    output = openat(session, "uboot-usb.log", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (output < 0) goto done;
    log_created = true;
    size_t remaining = saved;
    while (remaining) {
        size_t want = remaining < sizeof(chunk) ? remaining : sizeof(chunk);
        ssize_t n = read(input, chunk, want);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0 || write_all(output, chunk, (size_t)n)) goto done;
        remaining -= (size_t)n;
    }
    if (fsync(output)) goto done;
    metadata = openat(session, "uboot-usb.json", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (metadata < 0) goto done;
    meta_created = true;
    int length = snprintf(json, sizeof(json),
        "{\"source\":\"uboot_usb_console\",\"candidate_nonce\":\"%s\",\"reported_sha256\":\"%s\","
        "\"boot_instance_verified\":false,\"source_bytes\":%lld,\"stored_bytes\":%zu,\"truncated\":%s}\n",
        nonce, sha, (long long)total, saved, truncated ? "true" : "false");
    if (length < 0 || (size_t)length >= sizeof(json) || write_all(metadata, json, (size_t)length) || fsync(metadata))
        goto done;
    if (truncated) {
        marker = openat(session, "uboot.truncated", O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (marker < 0) goto done;
        marker_created = true;
        const char text[] = "The beginning of the U-Boot USB transcript was truncated.\n";
        if (write_all(marker, text, sizeof(text) - 1) || fsync(marker)) goto done;
    }
    if (fsync(session)) goto done;
    committed = true;
    result = capture_status(session, status);
done:
    if (marker >= 0) close(marker);
    if (metadata >= 0) close(metadata);
    if (output >= 0) close(output);
    if (input >= 0) close(input);
    if (!committed && session >= 0) {
        if (marker_created) unlinkat(session, "uboot.truncated", 0);
        if (meta_created) unlinkat(session, "uboot-usb.json", 0);
        if (log_created) unlinkat(session, "uboot-usb.log", 0);
    }
    if (session >= 0) close(session);
    if (base >= 0) close(base);
    if (cache_fd >= 0) close(cache_fd);
    return result;
}

static int mark_logfs_problem(int session, const char *name)
{
    const char message[] = "At least one U-Boot logfs record could not be preserved.\n";
    struct stat st;
    int fd = openat(session, name,
                    O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0 && errno == EEXIST) {
        fd = openat(session, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (fd < 0) return -1;
        int error = fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() ||
                    (st.st_mode & 0077) || st.st_size > 256;
        if (close(fd)) error = 1;
        if (error) { errno = EINVAL; return -1; }
        return 0;
    }
    if (fd < 0) return -1;
    int error = write_all(fd, message, sizeof(message) - 1) || fsync(fd);
    if (close(fd)) error = 1;
    if (error || fsync(session)) return -1;
    return 0;
}

static int mark_logfs_partial(int session)
{
    return mark_logfs_problem(session, "uboot-logfs.partial");
}

static int mark_logfs_unavailable(int session)
{
    return mark_logfs_problem(session, "uboot-logfs.unavailable");
}

static int clear_logfs_unavailable(int session)
{
    struct stat st;
    if (fstatat(session, "uboot-logfs.unavailable", &st, AT_SYMLINK_NOFOLLOW))
        return errno == ENOENT ? 0 : -1;
    if (!S_ISREG(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0077)) {
        errno = EINVAL;
        return -1;
    }
    return unlinkat(session, "uboot-logfs.unavailable", 0) || fsync(session) ? -1 : 0;
}

static bool valid_logfs_record(char *data, size_t length, char *nonce, bool *overflow)
{
    static const char prefix[] = "RMX1901_UBOOT_LOG v1\ncandidate=";
    static const char complete[] = "console_overflow=0\n\n";
    static const char truncated[] = "console_overflow=1\n\n";
    if (length < sizeof(prefix) || memchr(data, '\0', length) ||
        memcmp(data, prefix, sizeof(prefix) - 1)) return false;
    data[length] = '\0';
    char *first = data + sizeof(prefix) - 1;
    char *end = strchr(first, '\n');
    if (!end || end == first || (size_t)(end - first) > 68) return false;
    memcpy(nonce, first, (size_t)(end - first));
    nonce[end - first] = '\0';
    if (!valid_attempt_nonce(nonce)) return false;
    const char *body = end + 1;
    if (!strncmp(body, complete, sizeof(complete) - 1)) *overflow = false;
    else if (!strncmp(body, truncated, sizeof(truncated) - 1)) *overflow = true;
    else return false;
    return body + sizeof(complete) - 1 < data + length;
}

static bool archive_matches(int session, const char *name, const char *data, size_t length)
{
    char chunk[4096];
    struct stat st;
    int fd = openat(session, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return false;
    bool matches = !fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_uid == geteuid() &&
                   !(st.st_mode & 0077) && st.st_size >= 0 && (uint64_t)st.st_size == length;
    size_t offset = 0;
    while (matches && offset < length) {
        size_t want = length - offset < sizeof(chunk) ? length - offset : sizeof(chunk);
        ssize_t n = read(fd, chunk, want);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0 || memcmp(chunk, data + offset, (size_t)n)) matches = false;
        else offset += (size_t)n;
    }
    if (close(fd)) matches = false;
    return matches;
}

static int snapshot_logfs(int session, const char *source)
{
    char data[LOGFS_RECORD_LIMIT + 1], nonce[69], json[512];
    int directory = open(source, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (directory < 0) return mark_logfs_unavailable(session);
    for (int slot = 1; slot <= 3; slot++) {
        char input_name[16], log_name[32], meta_name[32];
        struct stat st;
        bool overflow = false, log_created = false, meta_created = false;
        int input = -1, output = -1, metadata = -1, error = 0;
        snprintf(input_name, sizeof(input_name), "RMXLOG%02d.LOG", slot);
        snprintf(log_name, sizeof(log_name), "uboot-logfs-%02d.log", slot);
        snprintf(meta_name, sizeof(meta_name), "uboot-logfs-%02d.json", slot);
        input = openat(directory, input_name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
        if (input < 0 && errno == ENOENT) continue;
        if (input < 0 || fstat(input, &st) || !S_ISREG(st.st_mode) ||
            st.st_size <= 0 || (uint64_t)st.st_size > LOGFS_RECORD_LIMIT) {
            error = 1;
            goto next;
        }
        size_t length = (size_t)st.st_size, used = 0;
        while (used < length) {
            ssize_t n = read(input, data + used, length - used);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) { error = 1; goto next; }
            used += (size_t)n;
        }
        if (!valid_logfs_record(data, length, nonce, &overflow)) { error = 1; goto next; }

        int log_exists = !fstatat(session, log_name, &st, AT_SYMLINK_NOFOLLOW);
        if (!log_exists && errno != ENOENT) { error = 1; goto next; }
        int meta_exists = !fstatat(session, meta_name, &st, AT_SYMLINK_NOFOLLOW);
        if (!meta_exists && errno != ENOENT) { error = 1; goto next; }
        if (log_exists || meta_exists) {
            if (!log_exists || !meta_exists || !archive_matches(session, log_name, data, length)) error = 1;
            goto next;
        }
        output = openat(session, log_name, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (output < 0) { error = 1; goto next; }
        log_created = true;
        if (write_all(output, data, length) || fsync(output)) { error = 1; goto next; }
        metadata = openat(session, meta_name, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (metadata < 0) { error = 1; goto next; }
        meta_created = true;
        int n = snprintf(json, sizeof(json),
                         "{\"source\":\"uboot_logfs\",\"candidate_nonce\":\"%s\","
                         "\"boot_instance_verified\":false,\"console_overflow\":%s,\"bytes\":%zu}\n",
                         nonce, overflow ? "true" : "false", length);
        if (n < 0 || (size_t)n >= sizeof(json) || write_all(metadata, json, (size_t)n) ||
            fsync(metadata) || fsync(session)) error = 1;
next:
        if (metadata >= 0 && close(metadata)) error = 1;
        if (output >= 0 && close(output)) error = 1;
        if (input >= 0 && close(input)) error = 1;
        if (error) {
            if (meta_created) unlinkat(session, meta_name, 0);
            if (log_created) unlinkat(session, log_name, 0);
            if (mark_logfs_partial(session)) { close(directory); return -1; }
        }
    }
    if (close(directory)) return -1;
    return clear_logfs_unavailable(session);
}

static int snapshot_device_logfs(int session)
{
    static const char device[] = "/dev/block/bootdevice/by-name/logfs";
    char temporary[] = "/tmp/rmx1901-logfs-XXXXXX";
    uint64_t bytes = 0;
    int sector = 0, result = 0;
    struct stat st;
    int fd = open(device, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return mark_logfs_unavailable(session);
    if (fstat(fd, &st) || !S_ISBLK(st.st_mode) ||
        ioctl(fd, BLKGETSIZE64, &bytes) || ioctl(fd, BLKSSZGET, &sector) ||
        bytes != 8388608U || sector != 4096) {
        close(fd);
        return mark_logfs_unavailable(session);
    }
    if (!mkdtemp(temporary)) {
        close(fd);
        return mark_logfs_unavailable(session);
    }
    if (mount(device, temporary, "vfat", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL)) {
        result = mark_logfs_unavailable(session);
    } else {
        result = snapshot_logfs(session, temporary);
        if (umount(temporary) && mark_logfs_partial(session)) result = -1;
    }
    if (rmdir(temporary) && mark_logfs_partial(session)) result = -1;
    if (close(fd) && mark_logfs_partial(session)) result = -1;
    return result;
}

static int ensure_cache_recovery(const char *path)
{
    if (mkdir(path, 0700) && errno != EEXIST) return -1;
    int fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return -1;
    return close(fd);
}

static int service(void)
{
    char boot_id[66];
    int fd = open("/proc/sys/kernel/random/boot_id", O_RDONLY | O_CLOEXEC);
    ssize_t length = fd < 0 ? -1 : read(fd, boot_id, sizeof(boot_id) - 1);
    if (fd >= 0) close(fd);
    if (length <= 0 || (size_t)length >= sizeof(boot_id) - 1) {
        status_write(STATUS_PATH, "error", "boot_id_unavailable");
        return 1;
    }
    boot_id[length] = '\0';
    if (boot_id[length - 1] == '\n') boot_id[length - 1] = '\0';
    if (!valid_boot_id(boot_id) || status_write(STATUS_PATH, "pending", "waiting_for_mounts")) return 1;
    unsigned int seconds = 0;
    while (!is_mounted("/cache", NULL) || !is_mounted("/sys/fs/pstore", "pstore")) {
        if (stop_requested) return 0;
        if (seconds >= 15 && status_write(STATUS_PATH, "error", "mount_unavailable")) return 1;
        sleep(1);
        if (seconds < 15) seconds++;
    }
    if (stop_requested) return 0;
    int session = -1;
    if (ensure_cache_recovery("/cache/recovery") ||
        create_session("/cache/recovery", "/sys/fs/pstore", boot_id, &session) ||
        snapshot_device_logfs(session) ||
        capture_kernel(session, "/dev/kmsg", STATUS_PATH, false, true, NULL, DEBUG_FLAG_PATH)) {
        status_write(STATUS_PATH, "error", "capture_failed");
        if (session >= 0) close(session);
        return 1;
    }
    close(session);
    return 0;
}

int main(int argc, char **argv)
{
    int session = -1;
    if (argc >= 2 && (!strcmp(argv[1], "--service") || !strcmp(argv[1], "--stream")) &&
        install_stop_handler()) return 1;
    if (argc == 2 && !strcmp(argv[1], "--service")) return service();
    if (argc == 8 && !strcmp(argv[1], "--import-uboot"))
        return import_uboot(argv[2], argv[3], argv[4], argv[5], argv[6], argv[7]) ? 1 : 0;
    if ((argc != 7 && argc != 8 && argc != 9) ||
        (strcmp(argv[1], "--once") && strcmp(argv[1], "--stream") && strcmp(argv[1], "--device-once")) ||
        (argc != 7 && !strcmp(argv[1], "--device-once"))) {
        fprintf(stderr, "usage: diagnostics {--once|--stream|--device-once} PSTORE CACHE BOOT_ID KMSG STATUS [LOGFS [DEBUG_FLAG]]\n");
        return 2;
    }
    if (create_session(argv[3], argv[2], argv[4], &session) ||
        (argc >= 8 && snapshot_logfs(session, argv[7])) ||
        (!strcmp(argv[1], "--device-once") && snapshot_device_logfs(session)) ||
        capture_kernel(session, argv[5], argv[6], strcmp(argv[1], "--stream"), false,
                       argc >= 8 ? argv[7] : NULL, argc == 9 ? argv[8] : NULL)) {
        status_write(argv[6], "error", "capture_failed");
        if (session >= 0) close(session);
        return 1;
    }
    int result = capture_status(session, argv[6]);
    if (result) status_write(argv[6], "error", "capture_failed");
    close(session);
    return result ? 1 : 0;
}
