package android.view;
import java.util.ArrayList;
public class ViewGroup extends View implements ViewParent {
	public ArrayList<View> kids = new ArrayList<>();
	public void add(View v) { kids.add(v); v.parent = this; v.ctx = ctx; }
	public int indexOfChild(View v) { return kids.indexOf(v); }
}
