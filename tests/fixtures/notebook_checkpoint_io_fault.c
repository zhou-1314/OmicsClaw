#define _GNU_SOURCE
#define _LARGEFILE64_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* Only the private fixture's native output is affected; imports and good saves pass. */
static int target(int fd, const char *operation) {
    char name[64], path[4096];
    const char *mode = getenv("OMICSCLAW_TEST_NATIVE_FAULT");
    if (!mode || strcmp(mode, operation)) return 0;
    snprintf(name, sizeof(name), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(name, path, sizeof(path) - 1);
    if (n < 0) return 0;
    path[n] = 0;
    if (!strstr(path, "/faulted-native/adata.h5ad")) return 0;
    struct stat state;
    if (fstat(fd, &state) || state.st_size < 4096) return 0;
    const char *marker = getenv("OMICSCLAW_TEST_NATIVE_MARKER");
    if (marker) {
        int sink = open(marker, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (sink >= 0) {
            ssize_t (*original)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
            original(sink, operation, strlen(operation));
            original(sink, "\n", 1); close(sink);
        }
    }
    errno = EIO;
    return 1;
}

ssize_t write(int fd, const void *buffer, size_t size) {
    ssize_t (*original)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
    if (target(fd, "write")) return -1;
    static int partial = 0;
    if (target(fd, "zero_write")) return 0;
    if (target(fd, "short_write")) { if (partial++ == 0 && size > 1) return original(fd, buffer, size / 2); return -1; }
    return original(fd, buffer, size);
}
ssize_t pwrite(int fd, const void *buffer, size_t size, off_t offset) {
    ssize_t (*original)(int, const void *, size_t, off_t) = dlsym(RTLD_NEXT, "pwrite");
    if (target(fd, "write")) return -1;
    static int partial = 0;
    if (target(fd, "zero_write")) return 0;
    if (target(fd, "short_write")) { if (partial++ == 0 && size > 1) return original(fd, buffer, size / 2, offset); return -1; }
    return original(fd, buffer, size, offset);
}
ssize_t pwrite64(int fd, const void *buffer, size_t size, off64_t offset) {
    ssize_t (*original)(int, const void *, size_t, off64_t) = dlsym(RTLD_NEXT, "pwrite64");
    if (target(fd, "write")) return -1;
    static int partial = 0;
    if (target(fd, "zero_write")) return 0;
    if (target(fd, "short_write")) { if (partial++ == 0 && size > 1) return original(fd, buffer, size / 2, offset); return -1; }
    return original(fd, buffer, size, offset);
}
int ftruncate(int fd, off_t length) {
    int (*original)(int, off_t) = dlsym(RTLD_NEXT, "ftruncate");
    if (target(fd, "truncate")) return -1;
    return original(fd, length);
}
int ftruncate64(int fd, off64_t length) {
    int (*original)(int, off64_t) = dlsym(RTLD_NEXT, "ftruncate64");
    if (target(fd, "truncate")) return -1;
    return original(fd, length);
}
