// SPDX-License-Identifier: GPL-3.0-or-later
// synthetic test data
package android.content.res;

public class AssetManager {
	public AssetManager() {
	}

	public void destroy() {
		System.out.println("AssetManager.destroy(): STUB");
	}

	public static class Inner {
		public boolean isOk() {
			return true;
		}
	}
}
