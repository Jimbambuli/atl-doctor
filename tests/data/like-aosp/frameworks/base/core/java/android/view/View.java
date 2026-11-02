// Synthetic test data for atl-doctor; not AOSP code.
package android.view;

public class View {
    public boolean isInEditMode() {
        return false;
    }

    public WindowInsets onApplyWindowInsets(WindowInsets insets) {
        return insets;
    }

    public boolean isShown() {
        return mParent != null && mParent.isShown();
    }

    public int getScaleMode() {
        return 2;
    }

    protected void onFinishInflate() {
        // nothing
    }

    public View mutate() {
        return this;
    }

    public void layout(int a) {
    }

    public void layout(int a, int b) {
        mLeft = a;
    }
}
