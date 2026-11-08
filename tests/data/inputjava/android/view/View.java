package android.view;
import android.content.Context;
import android.content.res.Resources;
public class View {
	public int id, x, y, w, h;
	public ViewParent parent;
	public Context ctx;
	public CharSequence desc;
	public int getId() { return id; }
	public Resources getResources() { return new Resources(); }
	public Context getContext() { return ctx; }
	public ViewParent getParent() { return parent; }
	public int getWidth() { return w; }
	public int getHeight() { return h; }
	public CharSequence getContentDescription() { return desc; }
	public void getLocationInWindow(int[] o) { int px = 0, py = 0; for (View v = this; v != null; v = v.parent instanceof View ? (View) v.parent : null) { px += v.x; py += v.y; } o[0] = px; o[1] = py; }
	public boolean performClick() { return true; }
	public boolean performLongClick() { return performLongClick(0f, 0f); }
	public boolean performLongClick(float a, float b) { return true; }
}
