#ifndef PROTO
#error "PROTO not defined"
#endif

// #define PROTO mqtt
#define CONCAT_INTERNAL(a, b) a##b
#define CONCAT(a, b) CONCAT_INTERNAL(a, b)
#define CONCAT3(a, b, c) CONCAT(CONCAT(a, b), c)
#define STR_HELPER(x) #x
#define STR(x) STR_HELPER(x)
#define FILE_NAME CONCAT(PROTO, _packets.h)
#define PACKETS_H STR(../../llm/PROTO/FILE_NAME)

#define PARSE CONCAT3(parse_, PROTO, _msg)
#define REASM CONCAT3(reassemble_, PROTO, _msgs)
#define PKT_T CONCAT(PROTO, _packet_t)
#define GEN_PKTS CONCAT3(generate_, PROTO, _packets)
#define PRINT_PKTS CONCAT3(print_, PROTO, _packets)

#include PACKETS_H
#include <dirent.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

typedef uint8_t u8;
typedef uint32_t u32;

extern size_t PARSE(const u8 *buf, u32 buf_len, PKT_T *out_packets,
                    u32 max_count);
extern int REASM(const PKT_T *packets, u32 num_packets, u8 *output_buf,
                 u32 *out_len);

int total = 0;
int passed = 0;

bool diff_binary(const u8 *buf1, size_t len1, const u8 *buf2, size_t len2, char* fail_message) {
  bool differences_found = false;
  size_t min_len = len1 < len2 ? len1 : len2;
  for (size_t i = 0; i < min_len; i++) {
    if (buf1[i] != buf2[i]) {
      sprintf(fail_message + strlen(fail_message), "Difference at byte %zu: buf1=0x%02X, buf2=0x%02X\n", i, buf1[i], buf2[i]);
      differences_found = true;
    }
  }
  if (len1 != len2) {
    sprintf(fail_message + strlen(fail_message), "Buffer lengths differ: len1=%zu, len2=%zu\n", len1, len2);
    differences_found = true;
  }
  return differences_found;
}

void process_file(const char *filepath) {
  // printf("--- Processing: %s ---\n", filepath);

  FILE *f = fopen(filepath, "rb");
  if (!f) {
    perror("Failed to open file");
    return;
  }

  fseek(f, 0, SEEK_END);
  long filesize = ftell(f);
  fseek(f, 0, SEEK_SET);

  if (filesize <= 0) {
    fclose(f);
    return;
  }

  uint8_t *buffer = (uint8_t *)malloc(filesize);
  if (fread(buffer, 1, filesize, f) != (size_t)filesize) {
    fprintf(stderr, "Failed to read file: %s\n", filepath);
    free(buffer);
    fclose(f);
    return;
  }
  fclose(f);

  PKT_T *packets = GEN_PKTS(100);
  int packet_cnt = PARSE(buffer, filesize, packets, 100);
  // PRINT_PKTS(packets, packet_cnt);

  uint32_t reassembled_len = filesize * 2;
  uint8_t *reassembled_buf = (uint8_t *)malloc(reassembled_len);

  bool ok = false;
  char fail_message[4096] = {0};

  if (REASM(packets, packet_cnt, reassembled_buf, &reassembled_len) == 0) {
    ok = !diff_binary(buffer, filesize, reassembled_buf, reassembled_len, fail_message);
  } else {
    fprintf(stderr, "Reassembly failed for %s\n", filepath);
  }

  if (ok) {
    printf("[PASS] %s\n", filepath);
    passed++;
  } else {
    printf("[FAIL] %s\n", filepath);
    printf("%s", fail_message);
    printf("Parsed packets: \n");
    PRINT_PKTS(packets, packet_cnt);
  }
  total++;

  free(buffer);
  free(reassembled_buf);
  free(packets);
  // printf("--- Finished: %s ---\n\n", filepath);
}

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "Usage: %s <directory_path>\n", argv[0]);
    return 1;
  }

  const char *dir_name = argv[1];
  DIR *d = opendir(dir_name);
  if (!d) {
    perror("Could not open directory");
    return 1;
  }

  struct dirent *dir;
  char path[1024];

  while ((dir = readdir(d)) != NULL) {
    if (strcmp(dir->d_name, ".") == 0 || strcmp(dir->d_name, "..") == 0) {
      continue;
    }

    snprintf(path, sizeof(path), "%s/%s", dir_name, dir->d_name);

    struct stat st;
    if (stat(path, &st) == 0 && S_ISREG(st.st_mode)) {
      process_file(path);
    }
  }

  closedir(d);

  printf("Summary: [%s] Passed %d out of %d tests.\n", passed == total ? "PASS" : "FAIL", passed, total);
  return 0;
}