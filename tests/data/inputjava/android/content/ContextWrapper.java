package android.content;
public class ContextWrapper extends Context {
	Context base;
	public ContextWrapper(Context b) { base = b; }
	public Context getBaseContext() { return base; }
}
