package android.net;
public class ConnectivityManager {
	public Network getActiveNetwork() { return new Network(); }
	public NetworkCapabilities getNetworkCapabilities(Network n) { return new NetworkCapabilities(); }
	public NetworkInfo getActiveNetworkInfo() { return new NetworkInfo(); }
}
