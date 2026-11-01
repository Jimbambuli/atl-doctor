// Synthetic test data for atl-doctor; not AOSP code.
package android.media;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.util.Size;

import java.io.File;

public class ThumbnailUtils {
    public static int other() {
        return 0;
    }

    public static Bitmap createImageThumbnail(String path, int kind) {
        return createImageThumbnail(new File(path), new Size(kind, kind));
    }

    public static Bitmap createImageThumbnail(File file, Size size) {
        final Resizer resizer = new Resizer(size);
        final MediaFile.Info info = new MediaFile.Info();
        final String mime = MediaFile.getMimeTypeForFile(file.getName());
        Bitmap bitmap = BitmapFactory.decodeFile(file.getPath());
        return resizer.apply(bitmap, mime);
    }

    private static class Resizer {
        private final Size mSize;

        Resizer(Size size) {
            mSize = size;
        }

        Bitmap apply(Bitmap b, String mime) {
            return b;
        }
    }
}
