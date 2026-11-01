// SPDX-License-Identifier: GPL-3.0-or-later
// synthetic test data
#include <jni.h>

JNIEXPORT jint JNICALL Java_android_graphics_Foo_nativeBaz(JNIEnv *env, jclass cls, jlong h)
{
	return 0;
}

JNIEXPORT jint JNICALL Java_android_graphics_Foo_nativeReal(JNIEnv *env, jclass cls, jlong h)
{
	return compute(h) + 1;
}
