// SPDX-License-Identifier: GPL-3.0-or-later
// atl-doctor-tracecount - follow an ART streaming method trace (-Xmethod-trace-stream; v3 dual clock or
// v2 single clock) while it is written and count method entries per phase.
//
// usage: atl-doctor-tracecount TRACE MARKS OUTPREFIX WATCH APP_PREFIX
//   TRACE       the growing trace file; what has been read is released with fallocate(PUNCH_HOLE)
//   MARKS       marks.tsv of the capture ("index\tboot_us\twall_us\tname"); re-read while running.
//               ART stamps the trace with CLOCK_MONOTONIC (header start time + wall delta); the marks' CLOCK_BOOTTIME
//               is converted with the current BOOTTIME-MONOTONIC offset (time suspended since boot).
//               A record belongs to the last mark at or before it (before the first: phase 0).
//               Phases 0..255; later marks count to phase 255.
//   OUTPREFIX   outputs OUTPREFIX.methods (id\tclass\tname\tsig), .counts (phase\tid\tn),
//               .callers (phase\tstub_id\tcaller_id\tkind\tn), .threads (tid\tname); ends when OUTPREFIX.stop exists
//   WATCH       watched methods, lines "class\tname\tsig" (empty sig = every overload). For each entry into a
//               watched method the nearest caller frame is counted: kind a = class in APP_PREFIX, o = other
//               non-platform class (androidx, kotlin, obfuscated), - = none on the stack.
//   APP_PREFIX  package prefix of the app, e.g. "org.example."
#define _GNU_SOURCE
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAXPH 256
#define MAXMARKS 4096
#define MAXM (1u << 20)
#define NONE 0xFFFFFu
#define STACK 4096

enum { F_WATCH = 1, F_APP = 2, F_OTHER = 4 };

typedef struct { uint64_t k; uint32_t n; } ent_t;
typedef struct { ent_t *t; uint32_t cap, used; } table_t;

static uint32_t *cnt[MAXPH];
static table_t callers[MAXPH];
static uint64_t mark_t[MAXMARKS];
static uint8_t mark_p[MAXMARKS];
static uint32_t nmarks;
static uint8_t limit_reported;
static const char *marksfile;
static uint8_t *mflag;
static const char *app_pfx;
static char **watch;
static uint32_t nwatch;
static uint32_t *stk[65536];
static uint16_t sdep[65536];

static uint32_t slot(uint64_t k, uint32_t cap) { return (uint32_t)((k * 0x9E3779B97F4A7C15ull) >> 40) & (cap - 1); }

static void table_add(table_t *tb, uint64_t k) {
	if (!tb->t) { tb->cap = 1u << 12; tb->t = calloc(tb->cap, sizeof(ent_t)); }
	if (tb->used * 2 >= tb->cap) {
		uint32_t oc = tb->cap; ent_t *o = tb->t;
		tb->cap = oc * 2; tb->t = calloc(tb->cap, sizeof(ent_t));
		for (uint32_t i = 0; i < oc; i++) if (o[i].n) {
			uint32_t h = slot(o[i].k, tb->cap);
			while (tb->t[h].n) h = (h + 1) & (tb->cap - 1);
			tb->t[h] = o[i];
		}
		free(o);
	}
	uint32_t h = slot(k, tb->cap);
	while (tb->t[h].n && tb->t[h].k != k) h = (h + 1) & (tb->cap - 1);
	if (!tb->t[h].n) { tb->t[h].k = k; tb->used++; }
	tb->t[h].n++;
}

static int cmpstr(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }

static void load_watch(const char *p) {
	FILE *f = fopen(p, "r");
	if (!f) { perror(p); exit(2); }
	char line[4096]; uint32_t cap = 4096; watch = malloc(cap * sizeof(char *));
	while (fgets(line, sizeof line, f)) {
		line[strcspn(line, "\n")] = 0;
		if (!*line) continue;
		if (nwatch == cap) { cap *= 2; watch = realloc(watch, cap * sizeof(char *)); }
		watch[nwatch++] = strdup(line);
	}
	fclose(f);
	qsort(watch, nwatch, sizeof(char *), cmpstr);
}

static uint8_t watched(const char *key) {
	return nwatch && bsearch(&key, watch, nwatch, sizeof(char *), cmpstr) != NULL;
}

static uint8_t is_platform(const char *c) {
	static const char *pf[] = {"android.", "java.", "javax.", "dalvik.", "libcore.", "sun.", "jdk.", "com.android.",
		"org.apache.harmony", "org.json.", "org.xmlpull.", "org.w3c.", "org.xml.", "org.kxml2.",
		"com.google.android.gles_jni.", NULL};
	for (uint32_t i = 0; pf[i]; i++) if (strncmp(c, pf[i], strlen(pf[i])) == 0) return 1;
	return 0;
}

// method lines "0x<id<<2>\t<class>\t<name>\t<sig>\t<source>\n", one or more per record
static void note_methods(const char *s, uint16_t len) {
	char buf[8192]; const char *e = s + len;
	while (s < e) {
		const char *nl = memchr(s, '\n', (size_t)(e - s));
		if (!nl) nl = e;
		size_t L = (size_t)(nl - s);
		if (L >= sizeof buf) L = sizeof buf - 1;
		memcpy(buf, s, L); buf[L] = 0; s = nl + 1;
		char *t1 = strchr(buf, '\t');
		if (!t1) continue;
		*t1 = 0;
		uint32_t id = (uint32_t)(strtoul(buf, 0, 16) >> 2);
		if (id >= MAXM) continue;
		char *cls = t1 + 1, *t2 = strchr(cls, '\t');
		if (!t2) continue;
		char *t3 = strchr(t2 + 1, '\t');
		if (!t3) continue;
		char *t4 = strchr(t3 + 1, '\t');
		if (t4) *t4 = 0;
		if (watched(cls)) mflag[id] |= F_WATCH;
		t3[1] = 0;  // "class\tname\t": watch entries without signature
		if (watched(cls)) mflag[id] |= F_WATCH;
		*t2 = 0;
		if (strncmp(cls, app_pfx, strlen(app_pfx)) == 0) mflag[id] |= F_APP;
		else if (!is_platform(cls)) mflag[id] |= F_OTHER;
	}
}

static int64_t boot_minus_mono_us(void) {
	struct timespec b, m;
	clock_gettime(CLOCK_BOOTTIME, &b); clock_gettime(CLOCK_MONOTONIC, &m);
	return ((int64_t)b.tv_sec - m.tv_sec) * 1000000 + (b.tv_nsec - m.tv_nsec) / 1000;
}

static void load_marks(void) {
	FILE *f = fopen(marksfile, "r");
	if (!f) return;
	char line[4096]; nmarks = 0;
	int64_t off = boot_minus_mono_us();
	while (nmarks < MAXMARKS && fgets(line, sizeof line, f)) {
		if (!strchr(line, '\n')) break;  // partial last line
		uint64_t idx, boot, wall;
		if (sscanf(line, "%" SCNu64 "\t%" SCNu64 "\t%" SCNu64, &idx, &boot, &wall) != 3) continue;
		if (idx >= MAXPH && !limit_reported) { fprintf(stderr, "phase limit %u reached\n", MAXPH); limit_reported = 1; }
		(void)wall;
		mark_t[nmarks] = (uint64_t)((int64_t)boot - off); mark_p[nmarks++] = (uint8_t)(idx >= MAXPH ? MAXPH - 1 : idx);
	}
	fclose(f);
}

static uint8_t phase_at(uint64_t t) {
	uint8_t ph = nmarks ? mark_p[0] : 0;
	for (uint32_t i = 0; i < nmarks; i++) { if (mark_t[i] <= t) ph = mark_p[i]; else break; }
	return ph;
}

static FILE *open_out(const char *out, const char *ext) {
	char path[4096]; snprintf(path, sizeof path, "%s.%s", out, ext);
	FILE *f = fopen(path, "w");
	if (!f) { perror(path); exit(1); }
	return f;
}

static void enter(uint16_t tid, uint32_t id, uint8_t ph) {
	if (!cnt[ph]) cnt[ph] = calloc(MAXM, sizeof(uint32_t));
	cnt[ph][id]++;
	if (mflag[id] & F_WATCH) {
		uint32_t fr = NONE, kind = 0, d = sdep[tid] > STACK ? STACK : sdep[tid];
		for (uint32_t q = d; q > 0; q--) {
			uint32_t m2 = stk[tid][q - 1];
			if (mflag[m2] & F_APP) { fr = m2; kind = F_APP; break; }
			if (!kind && (mflag[m2] & F_OTHER)) { fr = m2; kind = F_OTHER; }
		}
		table_add(&callers[ph], ((uint64_t)kind << 48) | ((uint64_t)fr << 24) | id);
	}
	if (sdep[tid] < STACK) stk[tid][sdep[tid]] = id;
	if (sdep[tid] < 65535) sdep[tid]++;
}

static void leave(uint16_t tid, uint32_t id) {
	// pop to the matching frame if present (exit/unroll)
	uint16_t d = sdep[tid];
	while (d > 0 && d <= STACK && stk[tid][d - 1] != id) d--;
	if (d > 0) sdep[tid] = d - 1;
	else if (sdep[tid] > STACK) sdep[tid]--;
}

static void write_results(const char *out) {
	FILE *fc = open_out(out, "counts");
	for (uint32_t p = 0; p < MAXPH; p++) if (cnt[p])
		for (uint32_t i = 0; i < MAXM; i++) if (cnt[p][i]) fprintf(fc, "%u\t0x%x\t%u\n", p, i << 2, cnt[p][i]);
	fclose(fc);
	FILE *fj = open_out(out, "callers");
	for (uint32_t p = 0; p < MAXPH; p++) for (uint32_t i = 0; i < callers[p].cap; i++) if (callers[p].t[i].n) {
		uint64_t k = callers[p].t[i].k;
		uint32_t kind = (uint32_t)(k >> 48), fr = (uint32_t)(k >> 24) & 0xFFFFFFu, id = (uint32_t)k & 0xFFFFFFu;
		if (fr == NONE) fprintf(fj, "%u\t0x%x\t-\t-\t%u\n", p, id << 2, callers[p].t[i].n);
		else fprintf(fj, "%u\t0x%x\t0x%x\t%s\t%u\n", p, id << 2, fr << 2, kind == F_APP ? "a" : "o", callers[p].t[i].n);
	}
	fclose(fj);
}

int main(int argc, char **argv) {
	if (argc != 6) { fprintf(stderr, "usage: atl-doctor-tracecount TRACE MARKS OUTPREFIX WATCH APP_PREFIX\n"); return 2; }
	marksfile = argv[2]; app_pfx = argv[5];
	const char *out = argv[3];
	mflag = calloc(MAXM, 1);
	load_watch(argv[4]);
	FILE *fm = open_out(out, "methods"), *ft = open_out(out, "threads");
	char stoppath[4096]; snprintf(stoppath, sizeof stoppath, "%s.stop", out);
	int32_t fd = -1;
	while (fd < 0) {
		fd = open(argv[1], O_RDWR);
		if (fd >= 0) break;
		if (access(stoppath, F_OK) == 0) { fclose(fm); fclose(ft); write_results(out); return 0; }  // no trace at all
		usleep(200000);
	}
	uint64_t cap = 32u << 20; uint8_t *buf = malloc(cap);
	uint64_t have = 0, used = 0, pos = 0, punched = 0, recs = 0;
	uint8_t hdr = 0, eof_seen = 0; uint16_t recsz = 14; uint64_t start_us = 0; uint32_t last_wall = 0; uint64_t wrap = 0;
	uint32_t since_reload = 0;
	load_marks();
	for (;;) {
		if (used > 0) { memmove(buf, buf + used, have - used); have -= used; used = 0; }
		ssize_t r = pread(fd, buf + have, cap - have, (off_t)pos);
		if (r > 0) { have += (uint64_t)r; pos += (uint64_t)r; }
		else {
			if (eof_seen) break;
			if (access(stoppath, F_OK) == 0) eof_seen = 1;  // read once more, then end
			else usleep(300000);
		}
		uint64_t hole = (pos > 65536 ? pos - 65536 : 0) & ~4095ull;
		if (hole > punched) { fallocate(fd, FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE, 0, (off_t)hole); punched = hole; }
		if (!hdr) {
			if (have < 32) continue;
			uint32_t magic; memcpy(&magic, buf, 4);
			if (magic != 0x574f4c53) { fprintf(stderr, "atl-doctor-tracecount: bad magic %08x\n", magic); return 1; }
			uint16_t ver, hl; memcpy(&ver, buf + 4, 2); memcpy(&hl, buf + 6, 2); memcpy(&start_us, buf + 8, 8);
			// 2 = single clock, 3 = dual clock (record size in the header); streaming mode adds 0xF0
			recsz = (ver & 0x0F) >= 3 ? (uint16_t)(buf[16] | (buf[17] << 8)) : 10;
			if (recsz < 10) { fprintf(stderr, "atl-doctor-tracecount: bad record size %u\n", recsz); return 1; }
			used = hl; hdr = 1;
			continue;
		}
		if (++since_reload >= 4) { load_marks(); since_reload = 0; }
		for (;;) {
			if (have - used < 2) break;
			uint16_t tid = (uint16_t)(buf[used] | (buf[used + 1] << 8));
			if (tid == 0) {
				if (have - used < 5) break;
				uint8_t op = buf[used + 2];
				if (op == 1) {
					uint16_t len = (uint16_t)(buf[used + 3] | (buf[used + 4] << 8));
					if (have - used < 5u + len) break;
					fwrite(buf + used + 5, 1, len, fm); note_methods((char *)buf + used + 5, len); used += 5u + len;
				} else if (op == 2) {
					if (have - used < 7) break;
					uint16_t t2 = (uint16_t)(buf[used + 3] | (buf[used + 4] << 8));
					uint16_t len = (uint16_t)(buf[used + 5] | (buf[used + 6] << 8));
					if (have - used < 7u + len) break;
					fprintf(ft, "%u\t%.*s\n", t2, (int)len, (char *)buf + used + 7); used += 7u + len;
				} else if (op == 3) goto done;
				else {
					fprintf(stderr, "atl-doctor-tracecount: unknown op %u at %" PRIu64 ", rest dropped\n", op, pos - (have - used));
					goto done;
				}
				continue;
			}
			if (have - used < recsz) break;
			uint32_t mv, wall; memcpy(&mv, buf + used + 2, 4); memcpy(&wall, buf + used + recsz - 4, 4);
			used += recsz; recs++;
			if (wall < last_wall && last_wall - wall > 0x80000000u) wrap += 1ull << 32;
			last_wall = wall;
			uint32_t id = mv >> 2;
			if (id >= MAXM) continue;
			if (!stk[tid]) stk[tid] = calloc(STACK, sizeof(uint32_t));
			if ((mv & 3) == 0) enter(tid, id, phase_at(start_us + wrap + wall));
			else leave(tid, id);
		}
	}
done:
	if (have - used > 0 && hdr) fprintf(stderr, "atl-doctor-tracecount: %" PRIu64 " bytes of a partial record dropped\n", have - used);
	fclose(fm); fclose(ft);
	write_results(out);
	fprintf(stderr, "atl-doctor-tracecount: %" PRIu64 " records, %" PRIu64 " bytes\n", recs, pos);
	return 0;
}
