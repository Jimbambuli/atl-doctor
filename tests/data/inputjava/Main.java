import android.app.Activity;
import android.content.ContextWrapper;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.TextView;
public class Main {
	static class MainActivity extends Activity { }
	public static void main(String[] a) throws Exception {
		Activity act = new MainActivity();
		ContextWrapper themed = new ContextWrapper(act);   // a themed context between the view and its activity
		ViewGroup root = new ViewGroup(); root.ctx = themed; root.x = 5; root.y = 30;
		ViewGroup content = new ViewGroup(); content.ctx = themed; content.id = 0x1020002; root.add(content);
		ViewGroup bar = new ViewGroup(); bar.ctx = themed; content.add(new ViewGroup()); content.add(bar);
		TextView save = new TextView(); save.id = 0x7f01; save.text = "Save\tit"; save.desc = "Save button"; save.x = 10; save.y = 20; save.w = 100; save.h = 50;
		TextView plain = new TextView(); plain.id = 0x99; plain.w = 4; plain.h = 4;
		EditText edit = new EditText(); edit.id = 0x7f02; edit.text = "secret"; edit.w = 30; edit.h = 9;
		bar.add(save); bar.add(plain); bar.add(edit);
		Thread.sleep(800);   // the agent has armed its breakpoints
		save.performClick();
		edit.performClick();
		plain.performLongClick();
		new TextView().performClick();   // a view that hangs in no tree: no content view above it
		Thread.sleep(300);
	}
}
