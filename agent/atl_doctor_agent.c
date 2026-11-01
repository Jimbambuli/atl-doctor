// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * atl_doctor_agent.c - JVMTI agent of atl-doctor. One source for both sides (glibc/ATL: gcc, Android: NDK clang).
 * It only observes, it changes nothing.
 *
 * Loading:  -agentpath:libatl_doctor_agent.so=OUT[,opt...]
 *   or attached once the runtime runs (ATL --attach-agent=libatl_doctor_agent.so=OUT[,opt...], like Android's
 *   "am start --attach-agent"): ART loads its JVMTI plugin then and the runtime stays non-debuggable and fast;
 *   exceptions are recorded from the first app code on.
 *   ATL needs ART's JVMTI plugin: either -Xplugin:libopenjdkjvmti.so at start (runtime becomes debuggable and slow)
 *   or late=PLUGIN (see below).
 *   opt: noex          no exceptions
 *        nolc          no lifecycle
 *        arm=FILE      set the lifecycle breakpoints only once FILE exists (breakpoints right at start make some
 *                      apps lose start-up races; exceptions are recorded from the start anyway)
 *        late=PLUGIN   load the JVMTI plugin itself latesec seconds after start (like Android's attach-agent): the
 *                      runtime stays non-debuggable and fast; exceptions work, breakpoints are best effort
 *        latesec=N     delay for late= (default 20)
 *        marks=FILE    marks file of the capture (index\tboot_us\twall_us\tname), watched by later probes
 *        frames        frame times from the GDK frame clock of every toplevel window (GTK looked up with dlsym;
 *                      without GTK, e.g. on Android, one I line "frames unavailable")
 *        heap          with marks=: at every mark force a GC and count instances and bytes per class (C lines,
 *                      50 largest classes) and the JNI global references (M lines); slow
 *        sample=US     ART's sampling profiler (VMDebug.startMethodTracing, sampling every US microseconds, streamed) into
 *                      the file of sampletrace=FILE, started with the JVMTI environment: a time line of all threads
 *                      for little cost, unlike -Xmethod-trace (every call)
 *        sampletrace=FILE  see sample=
 *        hang=MS       main-thread hangs: a watchdog samples the main thread's Java stack every 20 ms; a blockade
 *                      is an unbroken run of samples deeper than the idle depth (smallest depth seen) longer than MS
 *
 * Output (one line per event, tab separated, fixed column count, t = CLOCK_BOOTTIME in us):
 *   E  t tid type message catcher|UNCAUGHT frame0|frame1|...   every thrown exception (24 frames, "B:" = boot class)
 *   L  t tid group method receiver declaring hash caller
 *        A/W/F: outermost call of an Activity / Window.Callback / androidx Fragment callback (breakpoints in every
 *        override; super()/wrapper chains with the same name count once). V: breakpoint only in android.view.View
 *        (onAttachedToWindow & co. must call super), receiver = concrete view class
 *   I  t tid text     info (breakpoints set, errors)
 *   P  t frame_counter frame_time_us presentation_us   one painted frame: frame_time_us = duration from the start
 *        of the frame clock cycle (before-paint) to after-paint; presentation_us = presentation time of the previous
 *        frame (0 if unknown, CLOCK_MONOTONIC)
 *   G  t pause_us     one garbage collection (GarbageCollectionStart to Finish)
 *   H  t tid duration_ms frame0|frame1|...   main thread blocked; stack sampled when the blockade passed hang=MS
 *   M  t mark_index java_used_bytes java_total_bytes jni_globals   at every new line of marks=FILE
 *        (Runtime.totalMemory()-freeMemory(); jni_globals only with heap, else -1)
 *   C  t mark_index class instances bytes   with heap: the 50 classes with the most bytes at that mark
 */
#define _GNU_SOURCE
#include <jvmti.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static jvmtiEnv *J;
static int32_t out_fd = -1;
static uint8_t opt_ex = 1, opt_lc = 1;
static volatile uint8_t lc_active;
static char opt_arm[512], opt_late[512], opt_marks[512];
static uint32_t opt_latesec = 20;
static uint8_t opt_frames, opt_heap;
static uint32_t opt_hang_ms;
static uint32_t opt_sample_us;
static char opt_sampletrace[512];
static __thread uint8_t in_callback;   /* recursion guard per thread */

static int64_t now_us(void) {
	struct timespec ts; clock_gettime(CLOCK_BOOTTIME, &ts);
	return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}
static int32_t tid(void) { return (int32_t)syscall(SYS_gettid); }
static int64_t mono_us(void) {
	struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}
static void sleep_ms(uint32_t ms) { struct timespec ts = { ms / 1000, (int64_t)(ms % 1000) * 1000000 }; nanosleep(&ts, NULL); }

static void emit(const char *s, size_t n) { if (out_fd >= 0) { ssize_t r = write(out_fd, s, n); (void)r; } }

/* control characters (tab, newline ...) would break the fixed columns */
static size_t clean(char *d, size_t dn, const char *s) {
	size_t i = 0;
	for (; s && *s && i + 1 < dn; s++) d[i++] = ((uint8_t)*s < 0x20 || *s == 0x7f) ? ' ' : *s;
	d[i] = 0;
	return i;
}
static void info(const char *fmt, const char *a) {
	char text[900], b[1024];
	snprintf(text, sizeof text, fmt, a);
	int32_t n = snprintf(b, sizeof b, "I\t%lld\t%d\t", (long long)now_us(), tid());
	n += (int32_t)clean(b + n, sizeof b - (size_t)n - 1, text);
	b[n++] = '\n';
	emit(b, (size_t)n);
}

static void start_agent_thread(JNIEnv *jni, const char *name, jvmtiStartFunction fn);
static void JNICALL hang_watchdog(jvmtiEnv *je, JNIEnv *jni, void *arg);
static void JNICALL marks_watcher(jvmtiEnv *je, JNIEnv *jni, void *arg);

/* ---------- method cache: jmethodID -> "pkg.Class.name" ---------- */
#define MC_N 262144
struct mc { jmethodID m; char *txt; const char *name; uint8_t boot; };
static struct mc mcache[MC_N];
static pthread_mutex_t mc_lock = PTHREAD_MUTEX_INITIALIZER;

static void sig_to_name(const char *sig, char *o, size_t on) {   /* "Lpkg/A$B;" -> "pkg.A$B" */
	size_t i = 0; const char *p = sig;
	if (*p == 'L') p++;
	for (; *p && *p != ';' && i + 1 < on; p++) o[i++] = (*p == '/') ? '.' : *p;
	o[i] = 0;
}
static struct mc *method(jmethodID m) {
	uint64_t h = ((uint64_t)(uintptr_t)m * 0x9E3779B97F4A7C15ull) >> 46;  /* 18 bits */
	pthread_mutex_lock(&mc_lock);
	for (uint32_t k = 0; k < MC_N; k++) {
		struct mc *e = &mcache[(h + k) & (MC_N - 1)];
		if (e->m == m) { pthread_mutex_unlock(&mc_lock); return e; }
		if (e->m == NULL) {
			char *name = NULL, *sig = NULL, *csig = NULL; jclass kl = NULL; jobject ld = NULL; char kn[512];
			(*J)->GetMethodName(J, m, &name, &sig, NULL);
			(*J)->GetMethodDeclaringClass(J, m, &kl);
			if (kl) { (*J)->GetClassSignature(J, kl, &csig, NULL); (*J)->GetClassLoader(J, kl, &ld); }
			sig_to_name(csig ? csig : "?", kn, sizeof kn);
			size_t n = strlen(kn) + strlen(name ? name : "?") + 2;
			e->txt = malloc(n); snprintf(e->txt, n, "%s.%s", kn, name ? name : "?");
			e->name = e->txt + strlen(kn) + 1;
			e->boot = (kl && ld == NULL);
			e->m = m;
			if (name) (*J)->Deallocate(J, (unsigned char *)name);
			if (sig) (*J)->Deallocate(J, (unsigned char *)sig);
			if (csig) (*J)->Deallocate(J, (unsigned char *)csig);
			pthread_mutex_unlock(&mc_lock);
			return e;
		}
	}
	pthread_mutex_unlock(&mc_lock);
	return NULL;
}
static void class_name(jclass k, char *o, size_t on) {
	char *s = NULL;
	if (k && (*J)->GetClassSignature(J, k, &s, NULL) == JVMTI_ERROR_NONE && s) { sig_to_name(s, o, on); (*J)->Deallocate(J, (unsigned char *)s); }
	else snprintf(o, on, "?");
}

/* ---------- exceptions ---------- */
static jmethodID mid_getMessage;
static void JNICALL cb_exception(jvmtiEnv *je, JNIEnv *jni, jthread th, jmethodID thrower, jlocation loc,
                                 jobject exc, jmethodID catch_m, jlocation catch_loc) {
	(void)je; (void)thrower; (void)loc; (void)catch_loc;
	if (in_callback) return;
	in_callback = 1;
	char buf[16384]; size_t n;
	char type[512]; jclass ek = (*jni)->GetObjectClass(jni, exc); class_name(ek, type, sizeof type);
	char msg[400] = "";
	if (mid_getMessage) {
		jstring js = (jstring)(*jni)->CallObjectMethod(jni, exc, mid_getMessage);
		if ((*jni)->ExceptionCheck(jni)) { (*jni)->ExceptionClear(jni); snprintf(msg, sizeof msg, "<getMessage threw>"); }
		else if (js) {
			const char *u = (*jni)->GetStringUTFChars(jni, js, NULL);
			if (u) { clean(msg, sizeof msg, u); (*jni)->ReleaseStringUTFChars(jni, js, u); }
			(*jni)->DeleteLocalRef(jni, js);
		} else snprintf(msg, sizeof msg, "<null>");
	}
	(*jni)->DeleteLocalRef(jni, ek);
	struct mc *cm = catch_m ? method(catch_m) : NULL;
	/* snprintf returns the wanted length: cut at the buffer, keep one byte for the newline */
	n = (size_t)snprintf(buf, sizeof buf, "E\t%lld\t%d\t%s\t%s\t%s\t", (long long)now_us(), tid(), type, msg,
	                     cm ? cm->txt : "UNCAUGHT");
	if (n > sizeof buf - 2) n = sizeof buf - 2;
	jvmtiFrameInfo fr[24]; jint cnt = 0;
	if ((*J)->GetStackTrace(J, th, 0, 24, fr, &cnt) == JVMTI_ERROR_NONE) {
		for (jint i = 0; i < cnt && n < sizeof buf - 600; i++) {
			struct mc *e = method(fr[i].method);
			n += (size_t)snprintf(buf + n, sizeof buf - n, "%s%s%s", i ? "|" : "", (e && e->boot) ? "B:" : "", e ? e->txt : "?");
			if (n > sizeof buf - 2) n = sizeof buf - 2;
		}
	}
	buf[n++] = '\n'; emit(buf, n);
	in_callback = 0;
}

/* ---------- lifecycle via breakpoints ---------- */
enum { G_ACT = 1, G_WIN = 2, G_FRAG = 4, G_VIEW = 8 };
static const char *n_act[] = { "onCreate", "onStart", "onRestart", "onResume", "onPostCreate", "onPostResume", "onPause",
	"onStop", "onDestroy", "onConfigurationChanged", "onSaveInstanceState", "onRestoreInstanceState", "onNewIntent",
	"onUserLeaveHint", "onTopResumedActivityChanged", "onMultiWindowModeChanged", "onBackPressed", NULL };
static const char *n_win[] = { "onWindowFocusChanged", "onAttachedToWindow", "onDetachedFromWindow", "onContentChanged",
	"onWindowAttributesChanged", "onCreatePanelMenu", "onPreparePanel", "onMenuOpened", "onPanelClosed",
	"onActionModeStarted", "onActionModeFinished", "onPointerCaptureChanged", NULL };
static const char *n_frag[] = { "onAttach", "onCreate", "onCreateView", "onViewCreated", "onViewStateRestored", "onStart",
	"onResume", "onPause", "onStop", "onSaveInstanceState", "onDestroyView", "onDestroy", "onDetach",
	"onConfigurationChanged", "onHiddenChanged", "onPrimaryNavigationFragmentChanged", NULL };
static const char *n_view[] = { "onAttachedToWindow", "onDetachedFromWindow", "onWindowFocusChanged",
	"onConfigurationChanged", "onFinishInflate", NULL };
static uint8_t in_list(const char **l, const char *n) { for (; *l; l++) if (!strcmp(*l, n)) return 1; return 0; }

/* breakpoint methods: jmethodID -> group mask */
#define BP_N 65536
static jmethodID bp_m[BP_N]; static uint8_t bp_g[BP_N]; static uint32_t bp_count;
static pthread_mutex_t bp_lock = PTHREAD_MUTEX_INITIALIZER;
static int32_t bp_find(jmethodID m) {
	uint64_t h = ((uint64_t)(uintptr_t)m * 0x9E3779B97F4A7C15ull) >> 48;
	for (uint32_t k = 0; k < BP_N; k++) { uint32_t i = (uint32_t)(h + k) & (BP_N - 1); if (bp_m[i] == m || bp_m[i] == NULL) return (int32_t)i; }
	return -1;
}
static jclass k_activity, k_wincb, k_fragment, k_view;   /* global refs once loaded */

static void JNICALL cb_breakpoint(jvmtiEnv *je, JNIEnv *jni, jthread th, jmethodID m, jlocation loc) {
	(void)je; (void)loc;
	if (in_callback) return;
	in_callback = 1;
	pthread_mutex_lock(&bp_lock);
	int32_t i = bp_find(m); uint8_t g = (i >= 0 && bp_m[i] == m) ? bp_g[i] : 0;
	pthread_mutex_unlock(&bp_lock);
	jvmtiFrameInfo fr[2]; jint cnt = 0;
	(*J)->GetStackTrace(J, th, 0, 2, fr, &cnt);
	struct mc *e0 = method(m), *e1 = cnt > 1 ? method(fr[1].method) : NULL;
	if (!e0) { in_callback = 0; return; }
	/* count a super()/wrapper chain once; view breakpoints sit only in android.view.View itself (every override
	   calls super), there every hit counts */
	if (g != G_VIEW && e1 && !strcmp(e0->name, e1->name)) {
		/* skip only if the caller is itself an A/W/F breakpoint (super()/wrapper); DecorView for example calls
		   cb.onWindowFocusChanged from its own onWindowFocusChanged - that is the callback itself */
		pthread_mutex_lock(&bp_lock);
		int32_t k = bp_find(fr[1].method);
		uint8_t chain = k >= 0 && bp_m[k] == fr[1].method && (bp_g[k] & (G_ACT | G_WIN | G_FRAG));
		pthread_mutex_unlock(&bp_lock);
		if (chain) { in_callback = 0; return; }
	}
	jobject self = NULL; char rk[512] = "?"; jint hash = 0;
	if ((*J)->GetLocalInstance(J, th, 0, &self) == JVMTI_ERROR_NONE && self) {
		jclass k = (*jni)->GetObjectClass(jni, self); class_name(k, rk, sizeof rk);
		(*J)->GetObjectHashCode(J, self, &hash);
		/* exact group (a method can be in Activity AND Window$Callback) */
		uint8_t g2 = 0;
		if ((g & G_ACT) && k_activity && (*jni)->IsInstanceOf(jni, self, k_activity) && in_list(n_act, e0->name)) g2 |= G_ACT;
		if ((g & G_WIN) && k_wincb && (*jni)->IsInstanceOf(jni, self, k_wincb) && in_list(n_win, e0->name)) g2 |= G_WIN;
		if ((g & G_FRAG) && k_fragment && (*jni)->IsInstanceOf(jni, self, k_fragment)) g2 |= G_FRAG;
		if ((g & G_VIEW) && k_view && (*jni)->IsInstanceOf(jni, self, k_view)) g2 |= G_VIEW;
		g = g2;
		(*jni)->DeleteLocalRef(jni, k); (*jni)->DeleteLocalRef(jni, self);
	}
	char gs[8]; uint32_t q = 0;
	if (g & G_ACT) gs[q++] = 'A';
	if (g & G_WIN) gs[q++] = 'W';
	if (g & G_FRAG) gs[q++] = 'F';
	if (g & G_VIEW) gs[q++] = 'V';
	gs[q] = 0;
	if (q) {
		char buf[2048];
		int32_t n = snprintf(buf, sizeof buf, "L\t%lld\t%d\t%s\t%s\t%s\t%s\t%08x\t%s\n", (long long)now_us(), tid(), gs, e0->name,
		                     rk, e0->txt, (uint32_t)hash, e1 ? e1->txt : "-");
		if (n > (int32_t)sizeof buf - 1) { n = sizeof buf - 1; buf[n - 1] = '\n'; }
		emit(buf, (size_t)n);
	}
	in_callback = 0;
}

static uint8_t is_subclass(JNIEnv *jni, jclass k, jclass base) { return base && (*jni)->IsAssignableFrom(jni, k, base); }

static void check_class(JNIEnv *jni, jclass k) {
	char *s = NULL;
	if ((*J)->GetClassSignature(J, k, &s, NULL) != JVMTI_ERROR_NONE || !s) return;
	if (s[0] != 'L') { (*J)->Deallocate(J, (unsigned char *)s); return; }
	/* remember the base classes as soon as they show up */
	if (!k_activity && !strcmp(s, "Landroid/app/Activity;")) k_activity = (*jni)->NewGlobalRef(jni, k);
	if (!k_wincb && !strcmp(s, "Landroid/view/Window$Callback;")) k_wincb = (*jni)->NewGlobalRef(jni, k);
	if (!k_fragment && !strcmp(s, "Landroidx/fragment/app/Fragment;")) k_fragment = (*jni)->NewGlobalRef(jni, k);
	if (!k_view && !strcmp(s, "Landroid/view/View;")) k_view = (*jni)->NewGlobalRef(jni, k);
	(*J)->Deallocate(J, (unsigned char *)s);
	uint8_t g = 0;
	if (is_subclass(jni, k, k_activity)) g |= G_ACT;
	if (is_subclass(jni, k, k_wincb)) g |= G_WIN;
	if (is_subclass(jni, k, k_fragment)) g |= G_FRAG;
	/* View only in the base class: a breakpoint in every subclass costs a deoptimization each and makes ATL so
	   slow that apps lose start-up races */
	if (k_view && (*jni)->IsSameObject(jni, k, k_view)) g |= G_VIEW;
	if (!g) return;
	jint mn = 0; jmethodID *ms = NULL;
	if ((*J)->GetClassMethods(J, k, &mn, &ms) != JVMTI_ERROR_NONE) return;
	for (jint i = 0; i < mn; i++) {
		jint mod = 0; (*J)->GetMethodModifiers(J, ms[i], &mod);
		if (mod & (0x0400 /*abstract*/ | 0x0100 /*native*/ | 0x0008 /*static*/)) continue;
		struct mc *e = method(ms[i]);
		if (!e) continue;
		uint8_t mg = 0;
		if ((g & G_ACT) && in_list(n_act, e->name)) mg |= G_ACT;
		if ((g & G_WIN) && in_list(n_win, e->name)) mg |= G_WIN;
		if ((g & G_FRAG) && in_list(n_frag, e->name)) mg |= G_FRAG;
		if ((g & G_VIEW) && in_list(n_view, e->name)) mg |= G_VIEW;
		if (!mg) continue;
		pthread_mutex_lock(&bp_lock);
		int32_t j = bp_find(ms[i]); uint8_t fresh = (j >= 0 && bp_m[j] == NULL);
		if (j >= 0) { bp_m[j] = ms[i]; bp_g[j] |= mg; }
		if (fresh) bp_count++;
		pthread_mutex_unlock(&bp_lock);
		if (fresh && (*J)->SetBreakpoint(J, ms[i], 0) != JVMTI_ERROR_NONE) info("SetBreakpoint failed %s", e->txt);
	}
	if (ms) (*J)->Deallocate(J, (unsigned char *)ms);
}
static void JNICALL cb_class_prepare(jvmtiEnv *je, JNIEnv *jni, jthread th, jclass k) {
	(void)je; (void)th;
	if (!lc_active) return;
	uint8_t old = in_callback; in_callback = 1; check_class(jni, k); in_callback = old;
}
/* check all classes loaded so far: base classes first (the order of loaded classes is arbitrary) */
static void arm(JNIEnv *jni) {
	jint n = 0; jclass *ks = NULL;
	(*J)->GetLoadedClasses(J, &n, &ks);
	uint8_t old = in_callback; in_callback = 1;
	for (uint32_t pass = 0; pass < 2; pass++)
		for (jint i = 0; i < n; i++) {
			if (pass == 0) {
				char *s = NULL; (*J)->GetClassSignature(J, ks[i], &s, NULL);
				if (s && (!strcmp(s, "Landroid/app/Activity;") || !strcmp(s, "Landroid/view/Window$Callback;")
				          || !strcmp(s, "Landroid/view/View;") || !strcmp(s, "Landroidx/fragment/app/Fragment;")))
					check_class(jni, ks[i]);
				if (s) (*J)->Deallocate(J, (unsigned char *)s);
			} else {
				jint st = 0; (*J)->GetClassStatus(J, ks[i], &st);
				if (st & JVMTI_CLASS_STATUS_PREPARED) check_class(jni, ks[i]);
			}
		}
	in_callback = old;
	if (ks) { for (jint i = 0; i < n; i++) (*jni)->DeleteLocalRef(jni, ks[i]); (*J)->Deallocate(J, (unsigned char *)ks); }
	char z[64]; snprintf(z, sizeof z, "%u", bp_count); info("armed, breakpoints %s", z);
}
/* arm=FILE: set the lifecycle breakpoints only once FILE exists (own agent thread) */
static void JNICALL wait_for_arm(jvmtiEnv *je, JNIEnv *jni, void *arg) {
	(void)je; (void)arg;
	while (access(opt_arm, F_OK) != 0) { struct timespec ts = { 0, 500000000L }; nanosleep(&ts, NULL); }
	lc_active = 1; arm(jni);
}
/* sample=US: ART's sampling profiler, streamed into sampletrace=FILE (ART dups the descriptor) */
static void start_sampling(JNIEnv *jni) {
	int32_t fd = open(opt_sampletrace, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
	if (fd < 0) { info("sample: cannot open %s", opt_sampletrace); return; }
	jclass k = (*jni)->FindClass(jni, "dalvik/system/VMDebug");
	jmethodID m = k ? (*jni)->GetStaticMethodID(jni, k, "startMethodTracingFd", "(Ljava/lang/String;IIIZIZ)V") : NULL;
	if (m) (*jni)->CallStaticVoidMethod(jni, k, m, (*jni)->NewStringUTF(jni, opt_sampletrace), (jint)fd,
	                                    (jint)(64 << 10), (jint)0, JNI_TRUE, (jint)opt_sample_us, JNI_TRUE);  /* small buffer: streamed often */
	if (!m || (*jni)->ExceptionCheck(jni)) { (*jni)->ExceptionClear(jni); info("sample: VMDebug refused %s", opt_sampletrace); }
	else { char z[32]; snprintf(z, sizeof z, "%u us", opt_sample_us); info("sample: every %s", z); }
	close(fd);
}
static void JNICALL cb_vm_init(jvmtiEnv *je, JNIEnv *jni, jthread th) {
	(void)je; (void)th;
	jclass t = (*jni)->FindClass(jni, "java/lang/Throwable");
	if (t) mid_getMessage = (*jni)->GetMethodID(jni, t, "getMessage", "()Ljava/lang/String;");
	(*jni)->ExceptionClear(jni);
	info("vm_init%s", "");
	if (opt_lc && !opt_arm[0]) { lc_active = 1; arm(jni); }
	else if (opt_lc) start_agent_thread(jni, "atl-doctor-arm", wait_for_arm);
	if (opt_ex) (*J)->SetEventNotificationMode(J, JVMTI_ENABLE, JVMTI_EVENT_EXCEPTION, NULL);
	if (opt_hang_ms) start_agent_thread(jni, "atl-doctor-hang", hang_watchdog);
	if (opt_sample_us && opt_sampletrace[0]) start_sampling(jni);
	if (opt_marks[0]) start_agent_thread(jni, "atl-doctor-marks", marks_watcher);
	if ((*J)->SetEventNotificationMode(J, JVMTI_ENABLE, JVMTI_EVENT_GARBAGE_COLLECTION_START, NULL) == JVMTI_ERROR_NONE)
		(*J)->SetEventNotificationMode(J, JVMTI_ENABLE, JVMTI_EVENT_GARBAGE_COLLECTION_FINISH, NULL);
	else info("gc pauses unavailable%s", "");
}

/* ---------- frame times (GDK frame clock, GTK looked up at run time) ---------- */
/* GLib/GTK types are not included: pointers stay void *, gboolean is a 32-bit int, gulong is 64 bits wide on the
   64-bit Linux targets where GTK exists (on Android dlsym finds nothing and nothing is called) */
typedef int32_t (*g_source_fn)(void *);
static uint32_t (*p_g_idle_add)(g_source_fn, void *);
static uint64_t (*p_g_signal_connect_data)(void *, const char *, void (*)(void *, void *), void *, void *, uint32_t);
static void *(*p_gtk_window_list_toplevels)(void);          /* GList * of GtkWindow, container owned by us */
static void (*p_g_list_free)(void *);
static void *(*p_gtk_widget_get_frame_clock)(void *);
static int64_t (*p_gdk_frame_clock_get_frame_counter)(void *);
static void *(*p_gdk_frame_clock_get_timings)(void *, int64_t);
static int32_t (*p_gdk_frame_timings_get_complete)(void *);
static int64_t (*p_gdk_frame_timings_get_presentation_time)(void *);
struct g_list { void *data; struct g_list *next, *prev; };   /* GList layout */
#define CLOCKS_N 64
static void *clocks[CLOCKS_N];
static int64_t cycle_start[CLOCKS_N];   /* CLOCK_MONOTONIC us of the clock's last before-paint (frame start) */
static uint32_t clocks_n;

static int32_t clock_index(void *clock) {
	for (uint32_t i = 0; i < clocks_n; i++) if (clocks[i] == clock) return (int32_t)i;
	return -1;
}
static void cycle_begins(void *clock, void *data) {
	(void)data;
	int32_t i = clock_index(clock);
	if (i >= 0) cycle_start[i] = mono_us();
}

static void after_paint(void *clock, void *data) {
	(void)data;
	int64_t n = p_gdk_frame_clock_get_frame_counter(clock);
	int32_t i = clock_index(clock);
	int64_t dur = i >= 0 && cycle_start[i] ? mono_us() - cycle_start[i] : 0, pres = 0;
	void *prev = p_gdk_frame_clock_get_timings(clock, n - 1);
	if (prev && p_gdk_frame_timings_get_complete(prev)) pres = p_gdk_frame_timings_get_presentation_time(prev);
	char b[128];
	int32_t len = snprintf(b, sizeof b, "P\t%lld\t%lld\t%lld\t%lld\n", (long long)now_us(), (long long)n,
	                       (long long)(dur > 0 ? dur : 0), (long long)pres);
	emit(b, (size_t)len);
}
/* main thread (g_idle_add): connect after-paint of every new frame clock */
static int32_t hook_clocks(void *data) {
	(void)data;
	void *list = p_gtk_window_list_toplevels();
	for (struct g_list *l = list; l; l = l->next) {
		void *clock = p_gtk_widget_get_frame_clock(l->data);
		if (clock && clock_index(clock) < 0 && clocks_n < CLOCKS_N) {
			clocks[clocks_n++] = clock;
			p_g_signal_connect_data(clock, "before-paint", cycle_begins, NULL, NULL, 0);
			p_g_signal_connect_data(clock, "after-paint", after_paint, NULL, NULL, 0);
		}
	}
	p_g_list_free(list);
	return 0;   /* G_SOURCE_REMOVE */
}
#define SYM(v, name) (*(void **)&(v) = dlsym(RTLD_DEFAULT, name))
static void *frames_thread(void *arg) {
	(void)arg;
	if (!SYM(p_g_idle_add, "g_idle_add") || !SYM(p_g_signal_connect_data, "g_signal_connect_data")
	    || !SYM(p_gtk_window_list_toplevels, "gtk_window_list_toplevels") || !SYM(p_g_list_free, "g_list_free")
	    || !SYM(p_gtk_widget_get_frame_clock, "gtk_widget_get_frame_clock")
	    || !SYM(p_gdk_frame_clock_get_frame_counter, "gdk_frame_clock_get_frame_counter")
	    || !SYM(p_gdk_frame_clock_get_timings, "gdk_frame_clock_get_timings")
	    || !SYM(p_gdk_frame_timings_get_complete, "gdk_frame_timings_get_complete")
	    || !SYM(p_gdk_frame_timings_get_presentation_time, "gdk_frame_timings_get_presentation_time")) {
		info("frames unavailable: no GTK in the process%s", "");
		return NULL;
	}
	info("frames: GDK frame clock%s", "");
	for (;;) { p_g_idle_add(hook_clocks, NULL); sleep_ms(1000); }
	return NULL;
}

/* ---------- garbage collection pauses ---------- */
static volatile int64_t gc_start;
static void JNICALL cb_gc_start(jvmtiEnv *je) { (void)je; gc_start = now_us(); }
static void JNICALL cb_gc_finish(jvmtiEnv *je) {
	(void)je;
	int64_t t = now_us(), s0 = gc_start;
	if (!s0) return;
	char b[96]; int32_t n = snprintf(b, sizeof b, "G\t%lld\t%lld\n", (long long)t, (long long)(t - s0));
	emit(b, (size_t)n);
}

/* ---------- main-thread hangs: watchdog samples the Java stack of the thread "main" ---------- */
#define HANG_SAMPLE_MS 20
static jthread find_main(JNIEnv *jni) {
	jint n = 0; jthread *ts = NULL; jthread found = NULL;
	if ((*J)->GetAllThreads(J, &n, &ts) != JVMTI_ERROR_NONE) return NULL;
	for (jint i = 0; i < n; i++) {
		jvmtiThreadInfo ti; memset(&ti, 0, sizeof ti);
		if (!found && (*J)->GetThreadInfo(J, ts[i], &ti) == JVMTI_ERROR_NONE && ti.name && !strcmp(ti.name, "main"))
			found = (*jni)->NewGlobalRef(jni, ts[i]);
		if (ti.name) (*J)->Deallocate(J, (unsigned char *)ti.name);
		(*jni)->DeleteLocalRef(jni, ts[i]);
	}
	(*J)->Deallocate(J, (unsigned char *)ts);
	return found;
}
static void JNICALL hang_watchdog(jvmtiEnv *je, JNIEnv *jni, void *arg) {
	(void)je; (void)arg;
	jthread main_t = find_main(jni);
	if (!main_t) { info("hang: no thread named main%s", ""); return; }
	char z[32]; snprintf(z, sizeof z, "%u ms", opt_hang_ms); info("hang: watchdog %s", z);
	int32_t idle = -1; int64_t since = 0; uint8_t over = 0;
	static char stack[16384]; size_t sn = 0;
	for (;;) {
		sleep_ms(HANG_SAMPLE_MS);
		jvmtiFrameInfo fr[24]; jint cnt = 0, depth = 0;
		if ((*J)->GetFrameCount(J, main_t, &depth) != JVMTI_ERROR_NONE) return;   /* thread gone */
		int64_t t = now_us();
		if (idle < 0 || depth <= idle) {   /* back at (or below) the idle depth: blockade over */
			if (over) {
				char b[16512];
				int32_t n = snprintf(b, sizeof b, "H\t%lld\t%d\t%lld\t%s\n", (long long)t, (int32_t)getpid(),
				                     (long long)((t - since) / 1000), stack);
				if (n > (int32_t)sizeof b - 1) { n = sizeof b - 1; b[n - 1] = '\n'; }
				emit(b, (size_t)n);
			}
			if (idle < 0 || depth < idle) idle = depth;   /* idle depth = smallest depth seen */
			since = 0; over = 0;
			continue;
		}
		if (!since) since = t;
		if (!over && t - since >= (int64_t)opt_hang_ms * 1000
		    && (*J)->GetStackTrace(J, main_t, 0, 24, fr, &cnt) == JVMTI_ERROR_NONE) {
			sn = 0; stack[0] = 0;
			for (jint i = 0; i < cnt && sn < sizeof stack - 600; i++) {
				struct mc *e = method(fr[i].method);
				sn += (size_t)snprintf(stack + sn, sizeof stack - sn, "%s%s%s", i ? "|" : "", (e && e->boot) ? "B:" : "",
				                       e ? e->txt : "?");
			}
			over = 1;
		}
	}
}
/* ---------- memory per mark: watches marks=FILE ---------- */
#define TOP_CLASSES 50
static int64_t jni_global_count;
static jint JNICALL count_jni_global(jvmtiHeapReferenceKind kind, const jvmtiHeapReferenceInfo *info, jlong class_tag,
                                     jlong referrer_class_tag, jlong size, jlong *tag_ptr, jlong *referrer_tag_ptr,
                                     jint length, void *user_data) {
	(void)info; (void)class_tag; (void)referrer_class_tag; (void)size; (void)tag_ptr; (void)referrer_tag_ptr;
	(void)length; (void)user_data;
	if (kind == JVMTI_HEAP_REFERENCE_JNI_GLOBAL) jni_global_count++;
	return 0;   /* do not follow: only the roots are of interest */
}
struct class_stat { int64_t instances, bytes; };
static struct class_stat *cstats;
static jint JNICALL count_object(jlong class_tag, jlong size, jlong *tag_ptr, jint length, void *user_data) {
	(void)tag_ptr; (void)length;
	int64_t n = *(int64_t *)user_data;
	if (class_tag > 0 && class_tag <= n) { cstats[class_tag - 1].instances++; cstats[class_tag - 1].bytes += size; }
	return 0;
}
static void heap_classes(JNIEnv *jni, uint32_t mark) {
	jint n = 0; jclass *ks = NULL;
	if ((*J)->GetLoadedClasses(J, &n, &ks) != JVMTI_ERROR_NONE) return;
	cstats = calloc((size_t)n, sizeof *cstats);
	for (jint i = 0; cstats && i < n; i++) (*J)->SetTag(J, ks[i], (jlong)i + 1);
	int64_t total = n;
	jvmtiHeapCallbacks hc; memset(&hc, 0, sizeof hc); hc.heap_iteration_callback = count_object;
	if (cstats && (*J)->IterateThroughHeap(J, 0, NULL, &hc, &total) == JVMTI_ERROR_NONE) {
		for (uint32_t k = 0; k < TOP_CLASSES; k++) {   /* the largest classes by bytes, one at a time */
			int64_t best = -1;
			for (jint i = 0; i < n; i++)
				if (cstats[i].instances && (best < 0 || cstats[i].bytes > cstats[best].bytes)) best = i;
			if (best < 0) break;
			char name[512], b[700]; class_name(ks[best], name, sizeof name);
			int32_t len = snprintf(b, sizeof b, "C\t%lld\t%u\t%s\t%lld\t%lld\n", (long long)now_us(), mark, name,
			                       (long long)cstats[best].instances, (long long)cstats[best].bytes);
			if (len > (int32_t)sizeof b - 1) { len = sizeof b - 1; b[len - 1] = '\n'; }
			emit(b, (size_t)len);
			cstats[best].instances = 0;
		}
	} else info("heap: class counting failed%s", "");
	free(cstats); cstats = NULL;
	for (jint i = 0; i < n; i++) (*jni)->DeleteLocalRef(jni, ks[i]);
	(*J)->Deallocate(J, (unsigned char *)ks);
}
static void memory_line(JNIEnv *jni, uint32_t mark) {
	if (opt_heap) {
		(*J)->ForceGarbageCollection(J);
		heap_classes(jni, mark);
	}
	int64_t used = -1, total = -1, globals = -1;
	jclass rk = (*jni)->FindClass(jni, "java/lang/Runtime");
	jmethodID get = rk ? (*jni)->GetStaticMethodID(jni, rk, "getRuntime", "()Ljava/lang/Runtime;") : NULL;
	jobject rt = get ? (*jni)->CallStaticObjectMethod(jni, rk, get) : NULL;
	if (rt) {
		jlong t = (*jni)->CallLongMethod(jni, rt, (*jni)->GetMethodID(jni, rk, "totalMemory", "()J"));
		jlong f = (*jni)->CallLongMethod(jni, rt, (*jni)->GetMethodID(jni, rk, "freeMemory", "()J"));
		total = t; used = t - f;
		(*jni)->DeleteLocalRef(jni, rt);
	}
	(*jni)->ExceptionClear(jni);
	if (rk) (*jni)->DeleteLocalRef(jni, rk);
	if (opt_heap) {
		jvmtiHeapCallbacks hc; memset(&hc, 0, sizeof hc); hc.heap_reference_callback = count_jni_global;
		jni_global_count = 0;
		if ((*J)->FollowReferences(J, 0, NULL, NULL, &hc, NULL) == JVMTI_ERROR_NONE) globals = jni_global_count;
	}
	char b[160];
	int32_t len = snprintf(b, sizeof b, "M\t%lld\t%u\t%lld\t%lld\t%lld\n", (long long)now_us(), mark,
	                       (long long)used, (long long)total, (long long)globals);
	emit(b, (size_t)len);
}
/* every complete new line of the marks file: index\tboot_us\twall_us\tname */
static void JNICALL marks_watcher(jvmtiEnv *je, JNIEnv *jni, void *arg) {
	(void)je; (void)arg;
	int64_t done = 0;   /* bytes of the marks file already handled */
	for (;;) {
		FILE *f = fopen(opt_marks, "r");
		if (f) {
			char line[1024];
			if (fseek(f, done, SEEK_SET) == 0)
				while (fgets(line, sizeof line, f) && strchr(line, '\n')) {
					done += (int64_t)strlen(line);
					memory_line(jni, (uint32_t)strtoul(line, NULL, 10));
				}
			fclose(f);
		}
		sleep_ms(250);
	}
}

static void start_agent_thread(JNIEnv *jni, const char *name, jvmtiStartFunction fn) {
	jclass tk = (*jni)->FindClass(jni, "java/lang/Thread");
	jobject thr = tk ? (*jni)->NewObject(jni, tk, (*jni)->GetMethodID(jni, tk, "<init>", "(Ljava/lang/String;)V"),
	                                      (*jni)->NewStringUTF(jni, name)) : NULL;
	if (thr) (*J)->RunAgentThread(J, thr, fn, NULL, JVMTI_THREAD_NORM_PRIORITY);
	(*jni)->ExceptionClear(jni);
}

static jint setup_env(JavaVM *vm);
/* late=PLUGIN: load the plugin ourselves after start-up. The runtime stays NOT debuggable (only kArtTiVersion,
   exceptions work, breakpoints best effort), the app runs at normal speed. */
static void *late_thread(void *arg) {
	JavaVM *vm = arg; JNIEnv *jni = NULL;
	struct timespec ts = { (time_t)opt_latesec, 0 }; nanosleep(&ts, NULL);
	JavaVMAttachArgs aa = { JNI_VERSION_1_6, (char *)"atl-doctor-late", NULL };
	if ((*vm)->AttachCurrentThread(vm, (void **)&jni, &aa) != JNI_OK) { info("late: attach failed%s", ""); return NULL; }
	void *h = dlopen(opt_late, RTLD_NOW | RTLD_GLOBAL);
	bool (*init)(void) = NULL;
	if (h) *(void **)&init = dlsym(h, "ArtPlugin_Initialize"); /* like SYM: no object-to-function cast (-Wpedantic) */
	if (!init) { info("late: cannot load plugin: %s", dlerror()); (*vm)->DetachCurrentThread(vm); return NULL; }
	init();
	if (setup_env(vm) == JNI_OK) { info("late: plugin loaded %s", opt_late); cb_vm_init(J, jni, NULL); }
	(*vm)->DetachCurrentThread(vm);
	return NULL;
}

static jint start(JavaVM *vm, char *options) {
	char opts[2048]; snprintf(opts, sizeof opts, "%s", options ? options : "");
	char *path = opts, *rest = strchr(opts, ',');
	if (rest) *rest++ = 0;
	char unknown[512] = "";
	for (char *o = rest; o && *o;) {
		char *nx = strchr(o, ',');
		if (nx) *nx++ = 0;
		if (!strcmp(o, "noex")) opt_ex = 0;
		else if (!strcmp(o, "nolc")) opt_lc = 0;
		else if (!strncmp(o, "arm=", 4)) snprintf(opt_arm, sizeof opt_arm, "%s", o + 4);
		else if (!strncmp(o, "late=", 5)) snprintf(opt_late, sizeof opt_late, "%s", o + 5);
		else if (!strncmp(o, "latesec=", 8)) opt_latesec = (uint32_t)strtoul(o + 8, NULL, 10);
		else if (!strncmp(o, "marks=", 6)) snprintf(opt_marks, sizeof opt_marks, "%s", o + 6);
		else if (!strcmp(o, "frames")) opt_frames = 1;
		else if (!strcmp(o, "heap")) opt_heap = 1;
		else if (!strncmp(o, "hang=", 5)) opt_hang_ms = (uint32_t)strtoul(o + 5, NULL, 10);
		else if (!strncmp(o, "sample=", 7)) opt_sample_us = (uint32_t)strtoul(o + 7, NULL, 10);
		else if (!strncmp(o, "sampletrace=", 12)) snprintf(opt_sampletrace, sizeof opt_sampletrace, "%s", o + 12);
		else snprintf(unknown, sizeof unknown, "%s", o);
		o = nx;
	}
	if (!path[0]) { fprintf(stderr, "atl-doctor agent: no output file (-agentpath:LIB=OUT[,opt...])\n"); return JNI_ERR; }
	out_fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
	if (out_fd < 0) { perror(path); return JNI_ERR; }
	if (unknown[0]) info("unknown option %s", unknown);
	if (opt_marks[0]) info("marks %s", opt_marks);
	if (opt_frames) { pthread_t t; pthread_create(&t, NULL, frames_thread, NULL); pthread_detach(t); }
	if (opt_late[0]) {
		pthread_t t; pthread_create(&t, NULL, late_thread, vm); pthread_detach(t);
		info("loaded, plugin later %s", opt_late);
		return JNI_OK;
	}
	return setup_env(vm);
}
static jint setup_env(JavaVM *vm) {
	jint r = (*vm)->GetEnv(vm, (void **)&J, JVMTI_VERSION_1_2);
	if (r != JNI_OK) r = (*vm)->GetEnv(vm, (void **)&J, 0x70010200);   /* kArtTiVersion */
	if (r != JNI_OK) { info("no JVMTI environment%s", ""); return JNI_ERR; }
	jvmtiCapabilities c; memset(&c, 0, sizeof c);
	c.can_generate_exception_events = 1; c.can_generate_breakpoint_events = 1;
	c.can_access_local_variables = 1;
	if ((*J)->AddCapabilities(J, &c) != JVMTI_ERROR_NONE) {
		info("AddCapabilities failed, retrying without local variables%s", "");
		c.can_access_local_variables = 0; (*J)->AddCapabilities(J, &c);
	}
	jvmtiCapabilities g; memset(&g, 0, sizeof g);
	g.can_generate_garbage_collection_events = 1;
	(*J)->AddCapabilities(J, &g);   /* optional: without it the GC events cannot be enabled (I line then) */
	if (opt_heap) {
		jvmtiCapabilities h; memset(&h, 0, sizeof h);
		h.can_tag_objects = 1;
		if ((*J)->AddCapabilities(J, &h) != JVMTI_ERROR_NONE) info("heap: cannot tag objects, no class counts%s", "");
	}
	jvmtiEventCallbacks cb; memset(&cb, 0, sizeof cb);
	cb.VMInit = cb_vm_init; cb.Exception = cb_exception; cb.Breakpoint = cb_breakpoint; cb.ClassPrepare = cb_class_prepare;
	cb.GarbageCollectionStart = cb_gc_start; cb.GarbageCollectionFinish = cb_gc_finish;
	(*J)->SetEventCallbacks(J, &cb, sizeof cb);
	(*J)->SetEventNotificationMode(J, JVMTI_ENABLE, JVMTI_EVENT_VM_INIT, NULL);
	(*J)->SetEventNotificationMode(J, JVMTI_ENABLE, JVMTI_EVENT_CLASS_PREPARE, NULL);
	(*J)->SetEventNotificationMode(J, JVMTI_ENABLE, JVMTI_EVENT_BREAKPOINT, NULL);
	info("loaded%s", "");
	return JNI_OK;
}
JNIEXPORT jint JNICALL Agent_OnLoad(JavaVM *vm, char *options, void *reserved) { (void)reserved; return start(vm, options); }
JNIEXPORT jint JNICALL Agent_OnAttach(JavaVM *vm, char *options, void *reserved) {
	(void)reserved; jint r = start(vm, options);
	/* late attach: no VMInit any more -> do it ourselves */
	if (r == JNI_OK && !opt_late[0]) { JNIEnv *jni; (*vm)->GetEnv(vm, (void **)&jni, JNI_VERSION_1_6); cb_vm_init(J, jni, NULL); }
	return r;
}
