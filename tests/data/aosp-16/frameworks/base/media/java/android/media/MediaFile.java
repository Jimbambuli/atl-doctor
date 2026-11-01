// Synthetic test data for atl-doctor; not AOSP code.
package android.media;

public class MediaFile {
    public static String getMimeTypeForFile(String name) {
        return name.endsWith(".png") ? "image/png" : null;
    }
}
