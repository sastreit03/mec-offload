package edu.clemson.mecrtp;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.WindowManager;
import android.widget.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** Foreground-only offline profiling controls, independent of the MEC connection. */
public final class EncoderProfileActivity extends Activity {
  private Uri directory;
  private EditText bitrates, fps, gop, width, height;
  private TextView status;
  private CheckBox orientationSample;
  private Button start, choose, submission;
  private final AtomicBoolean cancelled = new AtomicBoolean();
  private volatile boolean running;

  private EditText field(LinearLayout root, String label, String value) {
    TextView title = new TextView(this); title.setText(label); root.addView(title);
    EditText text = new EditText(this); text.setSingleLine(true); text.setText(value);
    root.addView(text); return text;
  }

  @Override public void onCreate(Bundle saved) {
    super.onCreate(saved);
    getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    LinearLayout root = new LinearLayout(this);
    root.setOrientation(LinearLayout.VERTICAL); root.setPadding(24, 24, 24, 24);
    TextView title = new TextView(this);
    title.setText("Offline encoder profile\nKeep this screen open. Select the eight Linux profiling videos.");
    root.addView(title);
    choose = new Button(this); choose.setText("Choose profiling video directory");
    choose.setOnClickListener(v -> startActivityForResult(
        new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION), 1));
    root.addView(choose);
    bitrates = field(root, "Bitrates (bits/s, comma separated)", "500000,1000000,1500000,2000000,4000000");
    fps = field(root, "Frames per second", "30");
    gop = field(root, "GOP length (frames)", "30");
    width = field(root, "Width (pixels)", "1280");
    height = field(root, "Height (pixels)", "720");
    start = new Button(this); start.setText("Start offline profile");
    start.setOnClickListener(v -> startProfile(false)); root.addView(start);
    orientationSample = new CheckBox(this);
    orientationSample.setText("Orientation diagnostic: save 30 frames; run first video at lowest bitrate only");
    root.addView(orientationSample);
    submission = new Button(this); submission.setText("Start paced local UDP profile");
    submission.setOnClickListener(v -> startProfile(true)); root.addView(submission);
    Button stop = new Button(this); stop.setText("Cancel profile");
    stop.setOnClickListener(v -> { cancelled.set(true); status.setText("Cancelling…"); }); root.addView(stop);
    status = new TextView(this); status.setText("Select a directory to begin."); root.addView(status);
    ScrollView scroll = new ScrollView(this); scroll.addView(root); setContentView(scroll);
  }

  private void startProfile(boolean localSubmission) {
    if (running) return;
    if (directory == null) { status.setText("Choose a profiling directory first."); return; }
    try {
      String[] parts = bitrates.getText().toString().trim().split("[,\\s]+");
      int[] rates = new int[parts.length];
      for (int i = 0; i < parts.length; i++) rates[i] = Integer.parseInt(parts[i]);
      int f = Integer.parseInt(fps.getText().toString().trim());
      int g = Integer.parseInt(gop.getText().toString().trim());
      int w = Integer.parseInt(width.getText().toString().trim());
      int h = Integer.parseInt(height.getText().toString().trim());
      cancelled.set(false); running = true; start.setEnabled(false); choose.setEnabled(false); submission.setEnabled(false);
      final boolean diagnostic = localSubmission && orientationSample.isChecked();
      orientationSample.setEnabled(false);
      Uri selected = directory;
      new Thread(() -> {
        String result;
        try {
          java.util.function.Consumer<String> progress =
              message -> runOnUiThread(() -> status.setText(message));
          java.io.File output;
          if (localSubmission) {
            output = new LocalSubmissionProfiler(getApplicationContext(), cancelled, progress, diagnostic)
                .run(selected, rates, f, g, w, h);
          } else {
            output = new EncoderProfiler(getApplicationContext(), cancelled, progress)
                .run(selected, rates, f, g, w, h);
          }
          result = "Complete. Pull results from:\n" + output + (localSubmission
              ? (diagnostic ? "\nInspect orientation-check.h264; these are diagnostic timings."
                  : "\nReview summary.csv for deadlines and receipt verification.")
              : "\nThen run the Python validator on your PC.");
        } catch (Exception error) {
          result = "Profile did not complete: " + error;
        }
        final String message = result;
        running = false;
        runOnUiThread(() -> { status.setText(message); start.setEnabled(true); choose.setEnabled(true); submission.setEnabled(true); orientationSample.setEnabled(true); });
      }, "offline-encoder-profile").start();
    } catch (NumberFormatException error) {
      status.setText("Enter integer values for all settings.");
    }
  }

  @Override protected void onActivityResult(int request, int result, Intent data) {
    super.onActivityResult(request, result, data);
    if (request == 1 && result == RESULT_OK && data != null) {
      directory = data.getData(); status.setText("Selected: " + directory);
    }
  }

  @Override protected void onStop() {
    // Leaving the screen cancels rather than silently profiling under background restrictions.
    if (running) cancelled.set(true);
    super.onStop();
  }
}
