// Synthetic test data for atl-doctor; not AOSP code.
package android.graphics;

public class BitmapFactory {
    public static Bitmap decodeFile(String path) {
        return nativeDecode(path);
    }

    private static native Bitmap nativeDecode(String path);
}
