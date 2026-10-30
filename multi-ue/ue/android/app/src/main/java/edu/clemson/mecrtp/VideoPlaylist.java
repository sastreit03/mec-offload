package edu.clemson.mecrtp;

import android.net.Uri;
import java.security.SecureRandom;
import java.util.*;

/** Seeded prerecorded-video shuffle with no repeats until each cycle is exhausted. */
public final class VideoPlaylist {
  public static final class Selection {
    public final Uri uri;
    public final String name;
    public final int cycle, position;

    Selection(Uri uri, String name, int cycle, int position) {
      this.uri = uri;
      this.name = name;
      this.cycle = cycle;
      this.position = position;
    }
  }

  private final List<Selection> files;
  private final Random random;
  public final long seed;
  private List<Selection> order = Collections.emptyList();
  private int index, cycle = -1;
  private Uri last;

  public VideoPlaylist(List<Selection> files) {
    if (files.isEmpty()) throw new IllegalArgumentException("No videos in selected directory");
    this.files = new ArrayList<>(files);
    this.files.sort(Comparator.comparing(x -> x.uri.toString()));
    seed = new SecureRandom().nextLong() & Long.MAX_VALUE;
    random = new Random(seed);
  }

  public int size() {
    return files.size();
  }

  /** Resolve a manifest media filename without changing the normal shuffle order. */
  public Selection find(String name) {
    for (Selection file : files)
      if (file.name.equals(name)) return new Selection(file.uri, file.name, -1, -1);
    throw new IllegalArgumentException("Campaign video is not staged: " + name);
  }

  public Selection next() {
    if (index >= order.size()) {
      order = new ArrayList<>(files);
      Collections.shuffle(order, random);
      if (order.size() > 1 && order.get(0).uri.equals(last))
        Collections.swap(order, 0, 1);
      index = 0;
      cycle++;
    }
    Selection source = order.get(index);
    Selection value = new Selection(source.uri, source.name, cycle, index);
    index++;
    last = value.uri;
    return value;
  }
}
