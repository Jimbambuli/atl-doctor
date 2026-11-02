// SPDX-License-Identifier: GPL-3.0-or-later
// synthetic test data
package android.view;

public class View {
	public boolean isInEditMode() {
		return false;
	}

	public WindowInsets onApplyWindowInsets(WindowInsets insets) {
		return insets;
	}

	public boolean isShown() {
		return false;
	}

	public int getScaleMode() {
		return 1;
	}

	public void onFinishInflate() {
	}

	public View mutate() {
		return this;
	}

	public void layout(int a) {
	}

	public void layout(int a, int b) {
	}
}
