package android.net;
public class NetworkCapabilities { public boolean hasTransport(int t) { return t == 1; } public boolean hasCapability(int c) { return c == 12 || c == 16; } }
