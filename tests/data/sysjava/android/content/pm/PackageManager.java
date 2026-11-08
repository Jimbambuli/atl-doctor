package android.content.pm;
public class PackageManager {
	public static class NameNotFoundException extends Exception { }
	public boolean hasSystemFeature(String f) { return f.equals("android.hardware.touchscreen") || f.equals("android.hardware.wifi"); }
	public PackageInfo getPackageInfo(String p, int flags) throws NameNotFoundException {
		if (p.equals("com.example.app")) return new PackageInfo();
		throw new NameNotFoundException();
	}
}
