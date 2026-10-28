/* SPDX-License-Identifier: Apache-2.0 */
/* Copy exposed pstore evidence without modifying the source filesystem. */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define MAX_RECORDS 128
#define MAX_NAME 128
#define MAX_CAPTURE (16U * 1024U * 1024U)

struct record {
    char name[MAX_NAME];
    const char *type;
    size_t total;
    size_t saved;
};

static const char *record_type(const char *name)
{
    if (!strncmp(name, "dmesg-ramoops", 13)) return "crash";
    if (!strncmp(name, "console-ramoops", 15)) return "console";
    if (!strncmp(name, "pmsg-ramoops", 12)) return "pmsg";
    return NULL;
}

static bool safe_name(const char *name)
{
    size_t i, length = strlen(name);
    if (!length || length >= MAX_NAME) return false;
    for (i = 0; i < length; i++) {
        char c = name[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              c == '-' || c == '_' || c == '.')) return false;
    }
    return true;
}

static int priority(const char *type)
{
    if (!strcmp(type, "crash")) return 0;
    if (!strcmp(type, "console")) return 1;
    return 2;
}

static int compare_records(const void *a, const void *b)
{
    const struct record *left = a, *right = b;
    int order = priority(left->type) - priority(right->type);
    return order ? order : strcmp(left->name, right->name);
}

static int write_all(int fd, const void *buffer, size_t length)
{
    const unsigned char *p = buffer;
    while (length) {
        ssize_t n = write(fd, p, length);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n;
        length -= (size_t)n;
    }
    return 0;
}

static int copy_record(int source, int destination, struct record *record, size_t limit)
{
    unsigned char chunk[4096], *tail = NULL;
    struct stat st;
    size_t next = 0;
    int input = -1, output = -1, result = -1;
    char filename[MAX_NAME + 16];

    input = openat(source, record->name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (input < 0 || fstat(input, &st)) goto done;
    if (!S_ISREG(st.st_mode)) { errno = EINVAL; goto done; }
    if (limit) {
        tail = malloc(limit);
        if (!tail) goto done;
    }
    for (;;) {
        ssize_t n = read(input, chunk, sizeof(chunk));
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) goto done;
        if (!n) break;
        if (record->total > (size_t)-1 - (size_t)n) goto done;
        for (ssize_t i = 0; i < n && limit; i++) {
            tail[next++] = chunk[i];
            if (next == limit) next = 0;
        }
        record->total += (size_t)n;
    }
    record->saved = record->total < limit ? record->total : limit;
    if (record->saved) {
        size_t start = record->total > limit ? next : 0;
        size_t first = record->saved < limit - start ? record->saved : limit - start;
        snprintf(filename, sizeof(filename), "pstore-%.127s.raw", record->name);
        output = openat(destination, filename, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (output < 0) goto done;
        if (write_all(output, tail + start, first) ||
            write_all(output, tail, record->saved - first) || fsync(output)) goto done;
    }
    result = 0;
done:
    if (input >= 0) close(input);
    if (output >= 0 && close(output)) result = -1;
    free(tail);
    return result;
}

int pstore_snapshot(const char *source_path, const char *destination_path, size_t limit)
{
    struct record records[MAX_RECORDS] = { 0 };
    struct stat source_stat, destination_stat;
    size_t count = 0, remaining = limit;
    bool partial = false, created_manifest = false;
    int source = -1, destination = -1, metadata = -1, result = -1;
    DIR *directory = NULL;
    struct dirent *entry;
    FILE *manifest = NULL;

    source = open(source_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    destination = open(destination_path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (source < 0 || destination < 0 || fstat(source, &source_stat) ||
        fstat(destination, &destination_stat))
        goto done;
    if (destination_stat.st_uid != geteuid() || (destination_stat.st_mode & 0022)) {
        errno = EPERM;
        goto done;
    }
    if (source_stat.st_dev == destination_stat.st_dev && source_stat.st_ino == destination_stat.st_ino) {
        errno = EINVAL;
        goto done;
    }
    metadata = openat(destination, "records.json",
                      O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (metadata < 0) goto done;
    created_manifest = true;
    directory = fdopendir(dup(source));
    if (!directory) goto done;
    errno = 0;
    while ((entry = readdir(directory))) {
        const char *type = record_type(entry->d_name);
        if (!type) continue;
        if (!safe_name(entry->d_name) || count == MAX_RECORDS) goto done;
        strcpy(records[count].name, entry->d_name);
        records[count++].type = type;
        errno = 0;
    }
    if (errno) goto done;
    qsort(records, count, sizeof(records[0]), compare_records);
    for (size_t i = 0; i < count; i++) {
        if (copy_record(source, destination, &records[i], remaining)) goto done;
        remaining -= records[i].saved;
        if (!records[i].total || records[i].saved != records[i].total) partial = true;
    }
    manifest = fdopen(metadata, "w");
    if (!manifest) goto done;
    metadata = -1;
    fprintf(manifest, "{\"status\":\"%s\",\"records\":[", count ? (partial ? "partial" : "captured") : "empty");
    for (size_t i = 0; i < count; i++) {
        const struct record *r = &records[i];
        fprintf(manifest, "%s{\"source_name\":\"%s\",\"type\":\"%s\",\"origin\":\"unknown\","
                "\"file\":\"%s%s%s\",\"bytes\":%zu,\"saved_bytes\":%zu,\"truncated\":%s}",
                i ? "," : "", r->name, r->type, r->saved ? "pstore-" : "",
                r->saved ? r->name : "", r->saved ? ".raw" : "", r->total, r->saved,
                r->total != r->saved ? "true" : "false");
    }
    if (fprintf(manifest, "]}\n") < 0 || ferror(manifest) || fflush(manifest) ||
        fsync(fileno(manifest))) goto done;
    if (fclose(manifest)) { manifest = NULL; goto done; }
    manifest = NULL;
    if (fsync(destination)) goto done;
    result = 0;
done:
    if (result) fprintf(stderr, "pstore snapshot failed: %s\n", strerror(errno));
    if (manifest) fclose(manifest);
    if (metadata >= 0) close(metadata);
    if (result && created_manifest && destination >= 0) {
        struct stat st;
        if (!fstatat(destination, "records.json", &st, AT_SYMLINK_NOFOLLOW) && !st.st_size)
            unlinkat(destination, "records.json", 0);
    }
    if (directory) closedir(directory);
    if (source >= 0) close(source);
    if (destination >= 0) close(destination);
    return result;
}

#ifndef RMX_PSTORE_EMBEDDED
int main(int argc, char **argv)
{
    char *end = NULL;
    unsigned long long limit;
    if (argc != 4 || !*argv[3] || *argv[3] == '-') {
        fprintf(stderr, "usage: pstore_snapshot SOURCE_DIR DESTINATION_DIR MAX_BYTES\n");
        return 2;
    }
    errno = 0;
    limit = strtoull(argv[3], &end, 10);
    if (errno || *end || limit > MAX_CAPTURE) {
        fprintf(stderr, "invalid pstore capture limit\n");
        return 2;
    }
    return pstore_snapshot(argv[1], argv[2], (size_t)limit) ? 1 : 0;
}
#endif
