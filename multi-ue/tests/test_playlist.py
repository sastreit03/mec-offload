from mec_ue.playlist import Playlist


def test_playlist_is_seeded_and_exhausts_each_cycle(tmp_path):
    for name in ("a.mp4", "b.mov", "c.mkv"):
        (tmp_path / name).touch()
    (tmp_path / "ignored.txt").touch()

    first = Playlist(tmp_path, 17)
    second = Playlist(tmp_path, 17)
    a = [first.next() for _ in range(6)]
    b = [second.next() for _ in range(6)]

    assert [x.path.name for x in a] == [x.path.name for x in b]
    assert len({x.path for x in a[:3]}) == 3
    assert len({x.path for x in a[3:]}) == 3
    assert a[2].path != a[3].path
    assert [x.cycle for x in a] == [0, 0, 0, 1, 1, 1]
