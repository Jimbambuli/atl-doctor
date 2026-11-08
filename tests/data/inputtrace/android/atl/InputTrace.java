package android.atl;

import android.view.View;
import android.widget.TextView;

// stand-in for ATL's hooks: empty static methods
public final class InputTrace {
	public static void userTouch(View target, int action) {}
	public static void textChanged(TextView v, int len) {}
	public static void key(int keyCode, int action) {}
	public static void scroll(View v, int dx, int dy) {}
}
