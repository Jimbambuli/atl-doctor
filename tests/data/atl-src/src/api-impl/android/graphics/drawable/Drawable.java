// SPDX-License-Identifier: GPL-3.0-or-later
// synthetic test data
package android.graphics.drawable;

public class Drawable {
	public static BlendMode parseBlendMode(int value, BlendMode def) {
		return null;
	}

	public void setAlpha(int a) {
		this.alpha = a;
	}

	public String tricky() {
		String s = "}";  /* { */
		return s + "{";
	}

	public void doNothing() {
		// TODO nothing here
	}
}
