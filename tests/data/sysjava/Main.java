import android.app.*;
import android.content.*;
public class Main {
	static class Rcv extends BroadcastReceiver { public void onReceive(Context c, Intent i) { } }
	public static void main(String[] a) throws Exception {
		Application app = ActivityThread.currentApplication();
		Rcv r = new Rcv();
		app.registerReceiver(r, new IntentFilter("android.net.conn.CONNECTIVITY_CHANGE", "com.example.PING"), 0);
		app.registerReceiver(new Rcv(), new IntentFilter("android.intent.action.SCREEN_ON"));
		Thread.sleep(500);
		r.onReceive(app, new Intent("com.example.PING"));
		Thread.sleep(300);
		java.nio.file.Files.writeString(java.nio.file.Path.of(a[0]), "1\n");   // the marks file: one new mark
		Thread.sleep(1500);
	}
}
