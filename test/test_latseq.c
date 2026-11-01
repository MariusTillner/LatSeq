#define _GNU_SOURCE
#include <stdio.h>
#include <unistd.h>
#include <pthread.h>
#include <x86intrin.h>
#include <stdlib.h>
#include <string.h>

#ifdef LATSEQ
  #include "latseq.h"
#endif

double cpuf;
const char * test_log = "test";

volatile int  oai_exit = 1; //Emulate global variable used by oai to indicate that oai is running

void print_usage(void) 
{
  printf("help test_latseq\n");
  printf("h \t: Help\n");
  printf("a \t: test_full() \t: a full unit test\n");
  printf("i \t: test_init_and_close() \t: test a simple init/close case\n");
  printf("t \t: test_multi_thread() \t: test multi-producers in different thread case\n");
  printf("m \t: measure_log_measure() \t: measure time took by log_measure\n");
  printf("n \t: measure_log_n() \t: measure time took by log_measure with n varargs\n");
  printf("w \t: measure_writer() \t: measure time to write\n");
  printf("p [gap_ns=0] [cold 0|1|2] [samples=200000] \t: measure_latseq_p() \t: per-call LATSEQ_P latency distribution sweep (2..10 ids, rdtsc, cold 0|1|2)\n");
}

int test_init_and_close() 
{
  oai_exit = 0;
  printf("[TEST] %s\n",__func__);
  if(!init_latseq(test_log, 0)) {
    printf("[ERROR] : init_latseq()\n");
    exit(EXIT_FAILURE);
  }
  sleep(1);
  if(!close_latseq()) {
    printf("[ERROR] : close_latseq()\n");
    exit(EXIT_FAILURE);
  }

  return 0;
}

int test_full() 
{
  oai_exit = 0;
  printf("[TEST] %s\n",__func__);
  if(!init_latseq(test_log, 0)) {
    printf("[ERROR] : init_latseq()\n");
    exit(EXIT_FAILURE);
  }
  int num = 1;
  int i;
  for (i=0; i < num; i++){
    LATSEQ_P("full3 D", "ip%d", 0);
    usleep(1);
    LATSEQ_P("full2 D", "ip%d.mac%d", 0, 1);
  }
  printf("sizeof latseq_element : %ld\n", sizeof(struct latseq_element_t));
  oai_exit = 1;
  if(!close_latseq()) {
    printf("[ERROR] : close_latseq()\n");
    exit(EXIT_FAILURE);
  }
  return 0;
}

void thread_test1(void)
{
  pthread_t thId = pthread_self();
  printf("[TEST] [%ld] thread started\n", thId);
  int i = 0;
  while(!oai_exit) {
    if (!i) {
      LATSEQ_P("full3 D", "ip%d", 0);
      usleep(11000);
      LATSEQ_P("full2 D", "ip%d.mac%d", 0, 1);
      i = 1;
      continue;
    }
  }
  pthread_exit(NULL);
}

void thread_test2(void)
{
  pthread_t thId = pthread_self();
  printf("[TEST] [%ld] thread started\n", thId);
  int i = 0;
  while(!oai_exit) {
    if (!i) {
      LATSEQ_P("full3 D", "ip%d", 1);
      usleep(1000);
      LATSEQ_P("full2 D", "ip%d.mac%d", 1, 1);
      usleep(9000);
      LATSEQ_P("full1 D", "ip%d.mac%d.phy%d", 1, 1, 4);
      i = 1;
      continue;
    }
  }
  printf("[TEST] [%ld] thread stopped\n", thId);
  pthread_exit(NULL);
}

int test_multithread() 
{
  oai_exit = 0;
  printf("[TEST] %s\n",__func__);
  if(!init_latseq(test_log, 0)) {
    printf("[ERROR] : init_latseq()\n");
    exit(EXIT_FAILURE);
  }
  pthread_t th1;
  pthread_t th2;
  pthread_create(&th1, NULL, (void *) &thread_test1, NULL);
  pthread_create(&th2, NULL, (void *) &thread_test2, NULL);
  usleep(25000);
  oai_exit = 1;
  pthread_join(th1, NULL);
  pthread_join(th2, NULL);

  if(!close_latseq()) {
    printf("[ERROR] : close_latseq()\n");
    exit(EXIT_FAILURE);
  }
  return 0;
}

int measure_log_measure()
{
  oai_exit = 0;
  printf("[TEST] %s\n",__func__);
  if(!init_latseq(test_log, 0)) {
    printf("[ERROR] : init_latseq()\n");
    exit(EXIT_FAILURE);
  }
#ifdef TEST_LATSEQ
  struct timeval begin, end;
  gettimeofday(&begin, NULL);
#endif
  const uint32_t num_call = 1000000;
  for (int i = 0; i < num_call; i++)
  {
    LATSEQ_P("meas", "call.%d.%d.%d.%d.%d.%d.%d", i,i,i,i,i,i,i);
  }

#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
#endif
  oai_exit = 1;
  
  if(!close_latseq()) {
    printf("[ERROR] : close_latseq()\n");
    exit(EXIT_FAILURE);
  }
#ifdef TEST_LATSEQ
  printf("[LATSEQ] %d log_measure took : %lu us\n", num_call, (end.tv_usec - begin.tv_usec));
#endif

  return 0;
}

int measure_log_n() 
{
  oai_exit = 0;
  printf("[TEST] %s\n",__func__);
  if(!init_latseq(test_log, 0)) {
    printf("[ERROR] : init_latseq()\n");
    exit(EXIT_FAILURE);
  }
  const uint32_t num_call = 5000000;
  int i;
#ifdef TEST_LATSEQ
  struct timeval begin, end;
  long t1, t2, t3, t4, t5, t6, t7, t8, t9, t10, carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas1", "call.%d", i); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t1 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas2", "call.%d.%d", i,0); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t2 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas3", "call.%d.%d.%d", i,0,1); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t3 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas4", "call.%d.%d.%d.%d", i,0,1,2); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t4 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas5", "call.%d.%d.%d.%d.%d", i,0,1,2,3); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t5 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas6", "call.%d.%d.%d.%d.%d.%d", i,0,1,2,3,4); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t6 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas7", "call.%d.%d.%d.%d.%d.%d.%d", i,0,1,2,3,4,5); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t7 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas8", "call.%d.%d.%d.%d.%d.%d.%d.%d", i,0,1,2,3,4,5,6); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t8 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas9", "call.%d.%d.%d.%d.%d.%d.%d.%d.%d", i,0,1,2,3,4,5,6,7); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t9 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
  gettimeofday(&begin, NULL);
#endif
  for (i = 0; i < num_call; i++) { LATSEQ_P("meas10", "call.%d.%d.%d.%d.%d.%d.%d.%d.%d.%d", i,0,1,2,3,4,5,7,8); }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  carry_sec_us = 1000 * 1000 * (end.tv_sec - begin.tv_sec);
  t10 = (end.tv_usec - begin.tv_usec) + carry_sec_us;
#endif
  oai_exit = 1;
  sleep(1);
  
  if(!close_latseq()) {
    printf("[ERROR] : close_latseq()\n");
    exit(EXIT_FAILURE);
  }
#ifdef TEST_LATSEQ
  printf("[LATSEQ] log_measure took :\n");
  printf("\tvar_args=1 : %.1f ns/call\n", 1000*(double)t1/num_call);
  printf("\tvar_args=2 : %.1f ns/call\n", 1000*(double)t2/num_call);
  printf("\tvar_args=3 : %.1f ns/call\n", 1000*(double)t3/num_call);
  printf("\tvar_args=4 : %.1f ns/call\n", 1000*(double)t4/num_call);
  printf("\tvar_args=5 : %.1f ns/call\n", 1000*(double)t5/num_call);
  printf("\tvar_args=6 : %.1f ns/call\n", 1000*(double)t6/num_call);
  printf("\tvar_args=7 : %.1f ns/call\n", 1000*(double)t7/num_call);
  printf("\tvar_args=8 : %.1f ns/call\n", 1000*(double)t8/num_call);
  printf("\tvar_args=9 : %.1f ns/call\n", 1000*(double)t9/num_call);
  printf("\tvar_args=10 : %.1f ns/call\n", 1000*(double)t10/num_call);
#endif
  return 0;
}

void test_writer(FILE * f, char * tmps, int i)
{
  sprintf(tmps, "a%d.b%d", i, i+1);
  fprintf(f, "%d.%06d %s %s\n", 1, 234567, "D write", tmps);
}

int measure_writer()
{
  oai_exit = 0;
  printf("[TEST] %s\n",__func__);
  FILE * fout = fopen("test1.lseq", "w");
  char hdr[] = "# LatSeq format\n# By Alexandre Ferrieux and Flavien Ronteix Jacquet\n# timestamp\tU/D\tsrc--dest\tdataId\n#funcId ip.entry sdap.mapping sdap.header pdcp.txbuf pdcp.rohc pdcp.intcipher pdcp.header pdcp.routing rlc.am.txbuf rlc.am.seg rlc.am.header rlc.am.retbuf mac.mux mac.harq.[0-7] phy.crc phy.cbseg phy.msc phy.mod phy.map phy.ant\n";
  fwrite(hdr, sizeof(char), sizeof(hdr) - 1, fout);
  const int num_call = 1000000;
  char * tmps;
  tmps = calloc(2*10, sizeof(char));

#ifdef TEST_LATSEQ
  struct timeval begin, end;
  gettimeofday(&begin, NULL);
#endif
  for (int i = 0; i < num_call; i++) {
    test_writer(fout, tmps, i);
  }
#ifdef TEST_LATSEQ
  gettimeofday(&end, NULL);
  printf("[LATSEQ] measure_write took : ");
  printf("%d writes : %.1ld us\n", num_call, (end.tv_usec - begin.tv_usec));
#endif
  fclose(fout);
  free(tmps);
  oai_exit = 1;
  return 0;
}

/*--- Per-call latency benchmark with ID Sweep (2..10 IDs) --------------------*/
static inline uint64_t rdtsc_begin(void)
{
  _mm_lfence();
  uint64_t t = __rdtsc();
  _mm_lfence();
  return t;
}

static inline uint64_t rdtsc_end(void)
{
  _mm_lfence();
  uint64_t t = __rdtsc();
  _mm_lfence();
  return t;
}

static int cmp_u64(const void *a, const void *b)
{
  uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
  return (x > y) - (x < y);
}

static void print_dist(const char *name, uint64_t *v, size_t n, uint64_t overhead, double cyc_per_ns)
{
  for (size_t i = 0; i < n; i++) v[i] = v[i] > overhead ? v[i] - overhead : 0;
  qsort(v, n, sizeof(uint64_t), cmp_u64);
  const double pct[] = {50, 90, 99, 99.9, 99.99, 99.999};
  const char *lbl[] = {"p50", "p90", "p99", "p99.9", "p99.99", "p99.999"};
  printf("[LATSEQ] %-16s n=%zu  min=%lu cyc (%.1f ns)", name, n, v[0], v[0] / cyc_per_ns);
  for (int k = 0; k < 6; k++) {
    uint64_t c = v[(size_t)(pct[k] / 100.0 * (n - 1))];
    printf("  %s=%lu (%.1f ns)", lbl[k], c, c / cyc_per_ns);
  }
  printf("  max=%lu (%.1f ns)\n", v[n - 1], v[n - 1] / cyc_per_ns);
}

static inline void pre_call_ex(uint64_t gap_cyc, int cold, volatile char *thrash, size_t thrash_sz, size_t *thrash_pos)
{
  if (gap_cyc) {
    uint64_t s = __rdtsc();
    while (__rdtsc() - s < gap_cyc) _mm_pause();
  }
  if (cold >= 1) {
    const char *slot = (const char *)&tls_latseq.log_buffer[tls_latseq.i_write_head % RING_BUFFER_SIZE];
    _mm_clflush(slot);
    _mm_clflush(slot + 64);
    _mm_clflush((const void *)&tls_latseq.i_write_head);
    _mm_clflush((const void *)&tls_latseq.th_latseq_id);
  }
  if (cold >= 2 && thrash) {
    for (size_t k = 0; k < thrash_sz; k += 4096 * 16) {
      thrash[(*thrash_pos + k) % thrash_sz]++;
    }
    *thrash_pos = (*thrash_pos + 64 * 977) % thrash_sz;
  }
  _mm_mfence();
}

#define BENCH_FN_EX(N, FMT, ...)                                                      \
  static void __attribute__((noinline)) bench_ids##N(uint64_t gap_cyc, int cold,      \
                                                     volatile char *thrash, size_t thrash_sz, \
                                                     size_t *thrash_pos, size_t samples, uint64_t *res) \
  {                                                                                  \
    for (size_t i = 0; i < samples; i++) {                                           \
      uint32_t v = (uint32_t)i;                                                      \
      pre_call_ex(gap_cyc, cold, thrash, thrash_sz, thrash_pos);                     \
      uint64_t t0 = rdtsc_begin();                                                   \
      LATSEQ_P("meas" #N, FMT, __VA_ARGS__);                                         \
      uint64_t t1 = rdtsc_end();                                                     \
      res[i] = t1 - t0;                                                              \
    }                                                                                \
  }

BENCH_FN_EX(2,  "c.%d.%d", v, v+1)
BENCH_FN_EX(3,  "c.%d.%d.%d", v, v+1, v+2)
BENCH_FN_EX(4,  "c.%d.%d.%d.%d", v, v+1, v+2, v+3)
BENCH_FN_EX(5,  "c.%d.%d.%d.%d.%d", v, v+1, v+2, v+3, v+4)
BENCH_FN_EX(6,  "c.%d.%d.%d.%d.%d.%d", v, v+1, v+2, v+3, v+4, v+5)
BENCH_FN_EX(7,  "c.%d.%d.%d.%d.%d.%d.%d", v, v+1, v+2, v+3, v+4, v+5, v+6)
BENCH_FN_EX(8,  "c.%d.%d.%d.%d.%d.%d.%d.%d", v, v+1, v+2, v+3, v+4, v+5, v+6, v+7)
BENCH_FN_EX(9,  "c.%d.%d.%d.%d.%d.%d.%d.%d.%d", v, v+1, v+2, v+3, v+4, v+5, v+6, v+7, v+8)
BENCH_FN_EX(10, "c.%d.%d.%d.%d.%d.%d.%d.%d.%d.%d", v, v+1, v+2, v+3, v+4, v+5, v+6, v+7, v+8, v+9)

typedef void (*bench_fn_t)(uint64_t, int, volatile char *, size_t, size_t *, size_t, uint64_t *);

int measure_latseq_p(uint64_t gap_ns, int cold, size_t samples)
{
  oai_exit = 0;
  printf("[TEST] %s gap_ns=%lu cold=%d samples=%zu\n", __func__, gap_ns, cold, samples);
  if (!init_latseq(test_log, 0)) {
    printf("[ERROR] : init_latseq()\n");
    exit(EXIT_FAILURE);
  }
  const double cyc_per_ns = (double)g_latseq.cpu_freq / 1e9;
  printf("[LATSEQ] TSC = %.3f GHz, sizeof(element) = %zu B, ring = %zu KiB\n",
         cyc_per_ns, sizeof(latseq_element_t), sizeof(tls_latseq.log_buffer) / 1024);

  bench_fn_t fns[11] = {0, 0, bench_ids2, bench_ids3, bench_ids4, bench_ids5, bench_ids6,
                        bench_ids7, bench_ids8, bench_ids9, bench_ids10};

  uint64_t *res = malloc(samples * sizeof(uint64_t));
  uint64_t *cal = malloc(samples * sizeof(uint64_t));
  const size_t thrash_sz = 64u << 20; // > L3 on most CPUs
  volatile char *thrash = cold >= 2 ? malloc(thrash_sz) : NULL;
  if (!res || !cal || (cold >= 2 && !thrash)) { perror("malloc"); exit(EXIT_FAILURE); }
  if (thrash) memset((void *)thrash, 1, thrash_sz);

  // Register this thread and warm up.
  for (int i = 0; i < 2048; i++) LATSEQ_P("warm", "w.%d", i);
  usleep(100000);

  // 1) calibrate: cost of the timer pair alone
  for (size_t i = 0; i < samples; i++) {
    uint64_t t0 = rdtsc_begin();
    uint64_t t1 = rdtsc_end();
    cal[i] = t1 - t0;
  }
  qsort(cal, samples, sizeof(uint64_t), cmp_u64);
  const uint64_t overhead = cal[samples / 2];
  printf("[LATSEQ] timer overhead (subtracted) : %lu cyc (%.1f ns)\n", overhead, overhead / cyc_per_ns);

  // 2) measure sweep over 2..10 IDs
  const uint64_t gap_cyc = (uint64_t)(gap_ns * cyc_per_ns);
  size_t thrash_pos = 0;

  for (int n = 2; n <= 10; n++) {
    fns[n](gap_cyc, cold, thrash, thrash_sz, &thrash_pos, 2048, res); // warm up
    fns[n](gap_cyc, cold, thrash, thrash_sz, &thrash_pos, samples, res);
    char name[32];
    snprintf(name, sizeof(name), "LATSEQ_P(%2d ids)", n);
    print_dist(name, res, samples, overhead, cyc_per_ns);
  }

  oai_exit = 1;
  sleep(1);

  free(res);
  free(cal);
  if (thrash) free((void *)thrash);
  if (!close_latseq()) {
    printf("[ERROR] : close_latseq()\n");
    exit(EXIT_FAILURE);
  }
  return 0;
}

int main (int argc, char **argv) 
{
  #ifdef LATSEQ
    printf("[TEST] #ifdef LATSEQ\n");
  #endif
  if (argc < 2) {
    print_usage();
    exit(-1);
  }
  char opt = (char)argv[1][0];
  switch (opt)
  {
  case 'p':
  case 'q': // q aliases to p sweep test
    (void)measure_latseq_p(argc > 2 ? strtoull(argv[2], NULL, 10) : 0,
                           argc > 3 ? atoi(argv[3]) : 0,
                           argc > 4 ? strtoull(argv[4], NULL, 10) : 200000);
    break;

  case 'h':
    print_usage();
    break;

  case 'i':
    (void)test_init_and_close();
    break;
  
  case 'a':
    (void)test_full();
    break;

  case 't':
    (void)test_multithread();
    break;
  
  case 'm':
    (void)measure_log_measure();
    break;

  case 'n':
    (void)measure_log_n();
    break;
  
  case 'w':
    (void)measure_writer();
    break;
  
  default:
    print_usage();
    break;
  }
  
  oai_exit = 1;
  return 0;
}