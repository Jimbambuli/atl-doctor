package android.content;
public class IntentFilter { java.util.List<String> a = new java.util.ArrayList<>();
	public IntentFilter(String... acts) { for (String s : acts) a.add(s); }
	public int countActions() { return a.size(); } public String getAction(int i) { return a.get(i); } }
