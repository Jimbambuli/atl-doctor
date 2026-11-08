import android.app.Activity;
import android.atl.InputTrace;
import android.content.ContextWrapper;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.TextView;
public class TraceMain {
	static class MainActivity extends Activity { }
	public static void main(String[] a) throws Exception {
		Activity act = new MainActivity();
		ContextWrapper themed = new ContextWrapper(act);
		ViewGroup content = new ViewGroup(); content.ctx = themed; content.id = 0x1020002;
		TextView save = new TextView(); save.id = 0x7f01; save.text = "Save"; save.w = 100; save.h = 50;
		TextView plain = new TextView(); plain.id = 0x99; plain.w = 4; plain.h = 4;
		EditText edit = new EditText(); edit.id = 0x7f02; edit.text = "secret"; edit.w = 30; edit.h = 9;
		content.add(save); content.add(plain); content.add(edit);
		Thread.sleep(800);   // the agent has armed its breakpoints
		plain.performClick();   // the app's own click: no T line once the hooks exist
		InputTrace.userTouch(save, 0);
		save.performClick();    // what ATL does next: the click itself adds nothing
		InputTrace.textChanged(edit, 6);
		InputTrace.key(4, 0);
		InputTrace.key(66, 0);
		InputTrace.scroll(save, 0, 300);
		InputTrace.userTouch(plain, 1);
		Thread.sleep(300);
	}
}
