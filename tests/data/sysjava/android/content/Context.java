package android.content;
public abstract class Context {
	public java.util.List<BroadcastReceiver> receivers = new java.util.ArrayList<>();
	public Intent registerReceiver(BroadcastReceiver r, IntentFilter f) { if (r != null) receivers.add(r); return null; }
	public Intent registerReceiver(BroadcastReceiver r, IntentFilter f, int flags) { return registerReceiver(r, f); }
	public abstract Object getSystemService(String name);
	public abstract String getPackageName();
	public abstract android.content.pm.PackageManager getPackageManager();
	public abstract android.content.res.Resources getResources();
}
