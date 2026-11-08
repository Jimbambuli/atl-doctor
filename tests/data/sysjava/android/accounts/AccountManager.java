package android.accounts;
public class AccountManager {
	public Account[] getAccounts() { return new Account[] { new Account(), new Account() }; }
	public Account[] getAccountsByType(String t) { return new Account[] { new Account() }; }
}
