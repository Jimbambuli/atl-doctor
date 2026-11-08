package android.app;
import android.content.*;
public class Application extends Context {
	public Object getSystemService(String n) {
		if (n.equals("connectivity")) return new android.net.ConnectivityManager();
		if (n.equals("account")) return new android.accounts.AccountManager();
		return null;
	}
	public String getPackageName() { return "com.example.app"; }
	public android.content.pm.PackageManager getPackageManager() { return new android.content.pm.PackageManager(); }
	public android.content.res.Resources getResources() { return new android.content.res.Resources(); }
}
