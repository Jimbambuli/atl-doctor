// Synthetic test data for atl-doctor; not AOSP code.
package android.graphics.drawable;

import android.graphics.BlendMode;
import android.graphics.flags.Flags;

public abstract class Drawable {
    private int mAlpha;

    /** @hide */
    @FlaggedApi(Flags.FLAG_NEW_TINT)
    public int tintLevel() {
        if (Flags.newTint()) {
            return 2;
        }
        return mAlpha;
    }

    /**
     * Maps an enum value.
     *
     * @hide
     */
    @UnsupportedAppUsage(maxTargetSdk = 30)
    public static BlendMode parseBlendMode(int value, BlendMode defaultMode) {
        switch (value) {
            case 3: return BlendMode.SRC_OVER;
            case 5: return BlendMode.SRC_IN;
            default: return defaultMode;
        }
    }
}
