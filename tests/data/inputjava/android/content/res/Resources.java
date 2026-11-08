package android.content.res;
public class Resources {
	public static class NotFoundException extends RuntimeException { }
	public String getResourceName(int id) { if (id == 0x99) throw new NotFoundException(); return "org.example:id/v" + Integer.toHexString(id); }
}
