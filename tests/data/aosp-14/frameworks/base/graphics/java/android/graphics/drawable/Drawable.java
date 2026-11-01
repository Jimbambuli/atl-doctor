// Synthetic test data for atl-doctor; not AOSP code.
package android.graphics.drawable;

import android.graphics.BlendMode;
import android.os.ServiceManager;

public abstract class Drawable {
    /**
     * Maps an enum value.
     *
     * @hide
     */
    @UnsupportedAppUsage(maxTargetSdk = 30)
    public static BlendMode parseBlendMode(int value, BlendMode defaultMode) {
        switch (value) {
            case 3: return BlendMode.SRC_OVER;
            default: return defaultMode;
        }
    }

    private static native long nCreate(int a);

    public long create() {
        return nCreate(1);
    }

    public Object service() {
        return ServiceManager.getService("x");
    }
}
